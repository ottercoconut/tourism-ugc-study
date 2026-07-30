#!/usr/bin/env python3
"""从一次运行的未分配任务中创建不可变清洗批次。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning import (  # noqa: E402
    ConfigurationError,
    SchedulerError,
    create_batch,
    load_config,
)


def build_parser() -> argparse.ArgumentParser:
    """构造冻结批次命令的参数解析器。"""

    parser = argparse.ArgumentParser(description="稳定选择待处理帖子并冻结任务清单。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="版本化清洗配置")
    parser.add_argument("--run-id", required=True, help="已完成增量发现的运行标识")
    parser.add_argument("--max-posts", type=int, help="本批帖子上限，不得超过配置值")
    return parser


def main(argv: list[str] | None = None) -> int:
    """创建并冻结批次，仅输出批次身份、规模与清单哈希。"""

    args = build_parser().parse_args(argv)
    try:
        result = create_batch(
            args.derived_db,
            args.run_id,
            load_config(args.config),
            max_posts=args.max_posts,
        )
    except ConfigurationError:
        reason_code = "invalid_config"
    except SchedulerError as exc:
        reason_code = exc.reason_code
    else:
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return 0
    print(json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
