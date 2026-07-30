#!/usr/bin/env python3
"""执行确定性文本任务，或基于显式快照构建重复候选。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning import (  # noqa: E402
    ConfigurationError,
    StateTransitionError,
    TextRepositoryError,
    build_text_candidates,
    claim_tasks,
    load_config,
    load_text_config,
    process_text_tasks,
)


def build_parser() -> argparse.ArgumentParser:
    """构造职责互斥的处理与候选构建子命令。"""

    parser = argparse.ArgumentParser(description="执行本地可复现的确定性文本清洗。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="主清洗配置")
    parser.add_argument("--text-config", required=True, help="版本化文本规则配置")
    subcommands = parser.add_subparsers(dest="command", required=True)

    process = subcommands.add_parser("process", help="领取并处理冻结批次的文本任务")
    process.add_argument("--batch-id", required=True, help="冻结批次标识")
    process.add_argument("--actor", help="进程标识，仅保存 SHA-256")
    process.add_argument(
        "--drain",
        action="store_true",
        help="反复领取直至该批没有就绪文本任务；默认只处理一个 claim_size",
    )

    candidates = subcommands.add_parser(
        "build-candidates",
        help="从显式运行和快照构建不可变重复候选",
    )
    candidates.add_argument("--run-id", required=True, help="运行标识")
    candidates.add_argument("--snapshot-id", required=True, help="冻结快照标识")
    candidates.add_argument(
        "--allow-partial",
        action="store_true",
        help="允许为尚未全部规范化的语料建立中间构建",
    )
    return parser


def _process(args: argparse.Namespace, config: object, text_config: object) -> tuple[dict[str, object], int]:
    """按配置领取上限处理一批；可选择持续领取到当前阶段清空。"""

    succeeded = failed = claimed = 0
    status_counts: dict[str, int] = {}
    while True:
        claims = claim_tasks(
            args.derived_db,
            args.batch_id,
            config,
            stage_name="text_deterministic",
            actor=args.actor,
        )
        if not claims:
            break
        claimed += len(claims)
        result = process_text_tasks(
            args.derived_db,
            claims,
            config=config,
            text_config=text_config,
            actor=args.actor,
        )
        succeeded += len(result.succeeded)
        failed += len(result.failed_task_ids)
        for item in result.succeeded:
            status_counts[item.structure_status] = status_counts.get(item.structure_status, 0) + 1
        if not args.drain:
            break
    payload: dict[str, object] = {
        "batch_id": args.batch_id,
        "claimed": claimed,
        "succeeded": succeeded,
        "failed": failed,
        "structure_status_counts": dict(sorted(status_counts.items())),
    }
    return payload, int(failed > 0)


def main(argv: list[str] | None = None) -> int:
    """加载双层配置并输出不含正文、作者或本地路径的 JSON 回执。"""

    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        text_config = load_text_config(
            args.text_config,
            expected_version_lock=str(config.algorithm_versions["text_normalization"]),
        )
        if args.command == "process":
            payload, exit_code = _process(args, config, text_config)
        else:
            result = build_text_candidates(
                args.derived_db,
                run_id=args.run_id,
                snapshot_id=args.snapshot_id,
                config=config,
                text_config=text_config,
                allow_partial=args.allow_partial,
            )
            payload = result.__dict__
            exit_code = 0
    except ConfigurationError:
        reason_code = "invalid_config"
    except (StateTransitionError, TextRepositoryError) as exc:
        reason_code = exc.reason_code
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exit_code
    print(json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
