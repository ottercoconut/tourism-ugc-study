#!/usr/bin/env python3
"""从工程快照创建研究切片，并生成V0/V1—V6双任务试编码轮次。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tourism_ugc_study.annotation.role_pilot import (
    create_research_slice,
    prepare_pilot_package,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    """构建显式输入、不可覆盖的试点命令行接口。"""

    parser = argparse.ArgumentParser(
        description="创建role-pilot研究切片并生成V0与V1—V6共同校准双任务包"
    )
    parser.add_argument("--source-database", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        default=REPOSITORY_ROOT / "data/processed/role-pilot.sqlite",
        help="已存在或待创建的只读研究切片",
    )
    parser.add_argument(
        "--database-metadata",
        type=Path,
        default=REPOSITORY_ROOT / "data/processed/role-pilot.snapshot.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "data/annotations/private/round_20260824_role_text_calibration_v01"
        ),
    )
    parser.add_argument("--seed", type=int, default=20260824)
    parser.add_argument("--role-authors", type=int, default=25)
    parser.add_argument("--text-posts", type=int, default=25)
    parser.add_argument("--max-role-posts", type=int, default=5)
    parser.add_argument(
        "--coder-ids",
        default="CODER_A,CODER_B",
        help="两个项目内匿名ID，以逗号分隔",
    )
    parser.add_argument(
        "--content-workbook-template",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "data/annotations/templates/all-label-manual-coding.xlsx"
        ),
    )
    parser.add_argument(
        "--slice-only",
        action="store_true",
        help="只创建研究切片和元数据，不生成私有标注轮次",
    )
    return parser


def main() -> None:
    """校验源谱系、创建必要切片并生成不可覆盖的试点轮次。"""

    args = build_parser().parse_args()
    if not args.database.exists():
        if args.source_database is None:
            raise SystemExit("研究切片不存在时必须提供--source-database")
        metadata = create_research_slice(
            args.source_database,
            args.database,
            args.database_metadata,
            source_manifest=args.source_manifest,
        )
        print(json.dumps({"research_slice": metadata}, ensure_ascii=False, indent=2))
    if args.slice_only:
        return
    coder_ids = tuple(item.strip() for item in args.coder_ids.split(",") if item.strip())
    result = prepare_pilot_package(
        args.database,
        args.output_dir,
        seed=args.seed,
        role_author_count=args.role_authors,
        text_post_count=args.text_posts,
        max_role_posts=args.max_role_posts,
        coder_ids=coder_ids,
        content_workbook_template=args.content_workbook_template,
        database_metadata=args.database_metadata,
    )
    print(
        json.dumps(
            {
                "output_dir": result.output_dir.as_posix(),
                "manifest": result.manifest_path.as_posix(),
                "role_author_count": result.role_author_count,
                "text_post_count": result.text_post_count,
                "role_strata": result.role_strata,
                "role_platform_counts": result.platform_role_counts,
                "text_platform_counts": result.platform_text_counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
