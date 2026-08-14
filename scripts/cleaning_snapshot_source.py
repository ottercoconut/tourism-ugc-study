#!/usr/bin/env python3
"""为一次清洗运行创建并登记只读源快照。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning import (  # noqa: E402
    ConfigurationError,
    SnapshotError,
    load_config,
    snapshot_source,
)


def build_parser() -> argparse.ArgumentParser:
    """构造只读快照命令的参数解析器。"""

    parser = argparse.ArgumentParser(
        description="验证只读输入契约并创建一致性 SQLite 快照。",
    )
    parser.add_argument("--source-db", required=True, help="正式采集 SQLite（只读）")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="版本化清洗配置")
    parser.add_argument("--run-id", required=True, help="新的运行标识，不得复用")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行快照命令，并仅输出不含源内容和本地路径的 JSON。"""

    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        result = snapshot_source(
            source_db=args.source_db,
            derived_db=args.derived_db,
            config=config,
            run_id=args.run_id,
        )
    except ConfigurationError:
        print(
            json.dumps({"status": "failed", "reason_code": "invalid_config"}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    except SnapshotError as exc:
        print(
            json.dumps({"status": "failed", "reason_code": exc.reason_code}, sort_keys=True),
            file=sys.stderr,
        )
        return 1

    payload = {
        "run_id": result.run_id,
        "run_status": result.run_status,
        "snapshot_id": result.snapshot_id,
        "input_contract_status": result.input_contract_status,
        "input_contract_method": result.input_contract_method,
        "input_contract_reason_code": result.input_contract_reason_code,
        "post_count": result.post_count,
        "source_sha256": result.source_sha256,
        "snapshot_sha256": result.snapshot_sha256,
        "object_manifest_sha256": result.object_manifest_sha256,
        "manifest_name": result.manifest_path.name,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.input_contract_status == "accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
