#!/usr/bin/env python3
"""从只读SQLite快照生成不含作者标识的平台覆盖审计manifest。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tourism_ugc_study.annotation.role_platform_coverage import (
    audit_role_platform_coverage,
)


def build_parser() -> argparse.ArgumentParser:
    """构建显式数据库和输出路径的命令行接口。"""

    parser = argparse.ArgumentParser(
        description="按平台审计V0作者身份材料与纵向历史覆盖"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    """只读分析数据库，并拒绝覆盖已有审计结果。"""

    args = build_parser().parse_args()
    if args.output.exists():
        raise SystemExit(f"输出已存在，拒绝覆盖：{args.output}")
    payload = audit_role_platform_coverage(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
