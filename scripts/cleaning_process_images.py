#!/usr/bin/env python3
"""导入本地图片清单并执行角色、指纹与重复候选框架。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning import (  # noqa: E402
    ConfigurationError,
    ImageManifestError,
    ImageRepositoryError,
    StateTransitionError,
    build_image_candidates,
    import_image_manifest,
    load_config,
    process_image_fingerprints,
    sync_image_stage_tasks,
)


def build_parser() -> argparse.ArgumentParser:
    """构造图片清洗命令行的参数契约。

    函数无参数，返回包含 `import-manifest`、`roles`、`fingerprints` 和
    `candidates` 四个互斥子命令的 :class:`argparse.ArgumentParser`。数据库、
    配置、manifest 和图片根路径只作为进程输入，不进入帮助之外的运行回执。

    构造解析器不打开文件、不连接数据库也不改变任务状态，可重复调用。缺少必填
    参数、未知子命令或类型不合法由 argparse 输出用法并以退出码 2 终止；这类
    参数错误发生在领域操作之前，不生成 manifest、attempt 或候选构建。
    """

    parser = argparse.ArgumentParser(description="处理上游已下载到本地的图片清单。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="主清洗配置")
    subcommands = parser.add_subparsers(dest="command", required=True)

    imported = subcommands.add_parser("import-manifest", help="校验并导入本地图片清单")
    imported.add_argument("--run-id", required=True, help="清洗运行标识")
    imported.add_argument("--snapshot-id", required=True, help="冻结源快照标识")
    imported.add_argument("--manifest", required=True, help="上游图片 CSV 清单")
    imported.add_argument("--image-root", required=True, help="清单相对路径的本地根目录")

    roles = subcommands.add_parser("roles", help="把 manifest 角色证据同步到批次任务")
    roles.add_argument("--batch-id", required=True, help="冻结批次标识")
    roles.add_argument("--manifest-id", help="已导入清单；省略时显式阻塞图片阶段")
    roles.add_argument("--actor", help="进程标识，仅保存 SHA-256")

    fingerprints = subcommands.add_parser("fingerprints", help="只读计算本地内容图片指纹")
    fingerprints.add_argument("--batch-id", required=True, help="冻结批次标识")
    fingerprints.add_argument("--manifest-id", help="已导入清单；省略时显式阻塞图片阶段")
    fingerprints.add_argument("--image-root", help="清单相对路径的本地根目录")
    fingerprints.add_argument("--actor", help="进程标识，仅保存 SHA-256")

    candidates = subcommands.add_parser("candidates", help="构建图片精确簇和近似候选")
    candidates.add_argument("--batch-id", required=True, help="冻结批次标识")
    candidates.add_argument("--manifest-id", help="已导入清单；省略时显式阻塞图片阶段")
    candidates.add_argument("--actor", help="进程标识，仅保存 SHA-256")
    return parser


def _sync_payload(result: object) -> dict[str, object]:
    """将编排结果转为 JSON，并用计数推导正常/阻塞状态。"""

    payload = asdict(result)
    payload["status"] = "blocked" if payload["blocked_count"] else "completed"
    return payload


def main(argv: list[str] | None = None) -> int:
    """执行一次图片清洗 CLI 操作并返回进程退出码。

    `argv` 为不含程序名的可选参数列表，省略时读取 `sys.argv`。成功返回 0，并
    向 stdout 输出只含标识、状态、计数和哈希的单行 JSON；配置、清单、仓储或
    状态机领域失败返回 1，并向 stderr 输出固定 `reason_code`，不回显路径、URL、
    图片内容、作者值或底层异常。argparse 参数错误按其契约直接退出码 2。

    `import-manifest` 幂等追加清单/角色证据；三个处理子命令依据现有不可变证据
    同步任务，缺少 manifest 时显式阻塞，修复后须先恢复再重试。候选指纹不完整
    会转为任务阻塞，其余契约冲突不会修改状态。相同冻结身份可安全复用，函数
    从不下载图片、不联网、不回写正式源库，也不输出最终图片排除标签。
    """

    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "import-manifest":
            payload = asdict(
                import_image_manifest(
                    args.derived_db,
                    run_id=args.run_id,
                    source_snapshot_id=args.snapshot_id,
                    manifest_path=args.manifest,
                    image_root=args.image_root,
                    config=config,
                )
            )
        elif args.command == "roles":
            payload = _sync_payload(
                sync_image_stage_tasks(
                    args.derived_db,
                    batch_id=args.batch_id,
                    stage="roles",
                    manifest_id=args.manifest_id,
                    config=config,
                    actor=args.actor,
                )
            )
        elif args.command == "fingerprints":
            if args.manifest_id is None:
                payload = _sync_payload(
                    sync_image_stage_tasks(
                        args.derived_db,
                        batch_id=args.batch_id,
                        stage="fingerprints",
                        manifest_id=None,
                        config=config,
                        actor=args.actor,
                    )
                )
            else:
                if not args.image_root:
                    raise ImageRepositoryError("image_root_required")
                batch_result = process_image_fingerprints(
                    args.derived_db,
                    manifest_id=args.manifest_id,
                    image_root=args.image_root,
                    config=config,
                )
                stage_result = sync_image_stage_tasks(
                    args.derived_db,
                    batch_id=args.batch_id,
                    stage="fingerprints",
                    manifest_id=args.manifest_id,
                    config=config,
                    actor=args.actor,
                )
                payload = {
                    "processing": asdict(batch_result),
                    "tasks": _sync_payload(stage_result),
                    "status": "blocked" if stage_result.blocked_count else "completed",
                }
        else:
            if args.manifest_id is None:
                payload = _sync_payload(
                    sync_image_stage_tasks(
                        args.derived_db,
                        batch_id=args.batch_id,
                        stage="candidates",
                        manifest_id=None,
                        config=config,
                        actor=args.actor,
                    )
                )
            else:
                try:
                    build = build_image_candidates(
                        args.derived_db,
                        manifest_id=args.manifest_id,
                        config=config,
                    )
                except ImageRepositoryError as exc:
                    if exc.reason_code != "image_fingerprints_incomplete":
                        raise
                    stage_result = sync_image_stage_tasks(
                        args.derived_db,
                        batch_id=args.batch_id,
                        stage="candidates",
                        manifest_id=args.manifest_id,
                        config=config,
                        blocking_reason_code="image_fingerprints_incomplete",
                        actor=args.actor,
                    )
                    payload = _sync_payload(stage_result)
                else:
                    stage_result = sync_image_stage_tasks(
                        args.derived_db,
                        batch_id=args.batch_id,
                        stage="candidates",
                        manifest_id=args.manifest_id,
                        config=config,
                        build_id=build.build_id,
                        actor=args.actor,
                    )
                    payload = {
                        "build": asdict(build),
                        "tasks": _sync_payload(stage_result),
                        "status": "blocked" if stage_result.blocked_count else "completed",
                    }
    except ConfigurationError:
        reason_code = "invalid_config"
    except (ImageManifestError, ImageRepositoryError, StateTransitionError) as exc:
        reason_code = exc.reason_code
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    print(
        json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
