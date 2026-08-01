#!/usr/bin/env python3
"""扫描一个显式源快照并登记增量对象、版本和待处理任务。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning import (  # noqa: E402
    ConfigurationError,
    InventoryError,
    discover_increment,
    load_config,
)


def build_parser() -> argparse.ArgumentParser:
    """构造增量发现命令的参数解析器。"""

    parser = argparse.ArgumentParser(description="登记显式快照中的源对象版本和待处理任务。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="版本化清洗配置")
    parser.add_argument("--snapshot-id", required=True, help="已登记且输入契约合格的快照标识")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行增量发现，并输出不含原始字段的分类计数。"""

    args = build_parser().parse_args(argv)
    try:
        result = discover_increment(
            args.derived_db,
            args.snapshot_id,
            load_config(args.config),
        )
    except ConfigurationError:
        reason_code = "invalid_config"
    except InventoryError as exc:
        reason_code = exc.reason_code
    else:
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "snapshot_id": result.snapshot_id,
                    "post_changes": dict(result.post_changes),
                    "image_changes": dict(result.image_changes),
                    "tasks_created": result.tasks_created,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    print(json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
