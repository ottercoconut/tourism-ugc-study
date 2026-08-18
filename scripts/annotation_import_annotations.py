#!/usr/bin/env python3
"""把帖子或近重复的审核/最终复核结果追加导入派生库。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.annotation.repository import (
    import_duplicate_final_reviews,
    import_duplicate_annotations,
    import_post_final_reviews,
    import_post_annotations,
)
from tourism_ugc_study.cleaning.config import load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--imported-by-hash", required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/cleaning-v3.2.yaml")
    )
    parser.add_argument(
        "record_kind",
        choices=(
            "post-reviews",
            "post-final-reviews",
            "duplicate-reviews",
            "duplicate-final-reviews",
        ),
    )
    return parser


def main() -> int:
    """选择追加式导入接口；脚本本身不解释、合并或覆盖标签。"""

    args = _parser().parse_args()
    config = load_config(args.config)
    functions = {
        "post-reviews": import_post_annotations,
        "post-final-reviews": import_post_final_reviews,
        "duplicate-reviews": import_duplicate_annotations,
        "duplicate-final-reviews": import_duplicate_final_reviews,
    }
    kwargs = {
        "csv_path": args.input,
        "guide_version": config.text_label_guide_version,
        "imported_by_hash": args.imported_by_hash,
    }
    result = functions[args.record_kind](
        args.derived_db,
        **kwargs,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
