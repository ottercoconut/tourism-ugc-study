#!/usr/bin/env python3
"""显式恢复可重试失败、超时任务及已修复的阻塞任务。"""

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
    load_config,
    resume_batch,
)


def build_parser() -> argparse.ArgumentParser:
    """构造显式恢复命令的参数解析器。"""

    parser = argparse.ArgumentParser(description="显式重排可恢复任务，不静默重试。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="版本化清洗配置")
    parser.add_argument("--batch-id", required=True, help="需要恢复的批次标识")
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="不重排 blocked 任务；超时 running 任务仍会恢复",
    )
    parser.add_argument("--actor", help="操作者或进程标识，仅保存 SHA-256")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行显式恢复并输出恢复、耗尽和忽略计数。"""

    args = build_parser().parse_args(argv)
    try:
        result = resume_batch(
            args.derived_db,
            args.batch_id,
            load_config(args.config),
            include_blocked=not args.failed_only,
            actor=args.actor,
        )
    except ConfigurationError:
        reason_code = "invalid_config"
    except StateTransitionError as exc:
        reason_code = exc.reason_code
    else:
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return 0
    print(json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
