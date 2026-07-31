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
    """构造互斥子命令；所有路径只作为输入，绝不写入终端回执。"""

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
    """执行选定子命令并仅输出标识、状态、计数和哈希。"""

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
