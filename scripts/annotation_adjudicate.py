#!/usr/bin/env python3
"""从人工确认的近重复关系构建训练泄漏分组。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.annotation.leakage_groups import create_leakage_build


def _read_ids(path: Path | None) -> tuple[str, ...]:
    """读取每行一个 ID 的显式清单；空清单表示不使用确认近重复边。"""

    if path is None:
        return ()
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derived-db", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    leakage = commands.add_parser("build-leakage", help="构建训练泄漏分量")
    leakage.add_argument("--candidate-build-id", required=True)
    leakage.add_argument("--duplicate-final-review-ids", type=Path)
    return parser


def main() -> int:
    """输出去标识化报告，不自动替代人工最终确认。"""

    args = _parser().parse_args()
    result = create_leakage_build(
        args.derived_db,
        candidate_build_id=args.candidate_build_id,
        duplicate_adjudication_ids=_read_ids(args.duplicate_final_review_ids),
    )
    payload = result.__dict__
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
