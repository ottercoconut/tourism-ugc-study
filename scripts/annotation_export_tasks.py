#!/usr/bin/env python3
"""创建文本抽样运行并导出盲审轮次或近重复候选 CSV。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.annotation.repository import (
    create_initial_sampling_run,
    create_periodic_sampling_run,
    export_near_duplicate_candidates,
    export_post_annotation_tasks,
    export_supplement_annotation_tasks,
)
from tourism_ugc_study.cleaning.config import load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/cleaning-v3.1.yaml")
    )
    commands = parser.add_subparsers(dest="command", required=True)

    initial = commands.add_parser("create-initial", help="创建首轮 500+200 抽样")
    initial.add_argument("--candidate-build-id", required=True)

    periodic = commands.add_parser("create-periodic", help="创建累计新增复核抽样")
    periodic.add_argument("--candidate-build-id", required=True)
    periodic.add_argument("--baseline-sample-run-id", required=True)
    periodic.add_argument("--round-number", type=int, required=True)

    post = commands.add_parser("export-post", help="导出初审或间隔盲复核轮次")
    post.add_argument("--sample-run-id", required=True)
    post.add_argument("--review-round", type=int, choices=(1, 2), required=True)
    post.add_argument("--output", type=Path, required=True)

    supplement = commands.add_parser(
        "export-supplement", help="导出稳定性补充轮次的初审或复核任务"
    )
    supplement.add_argument("--supplement-run-id", required=True)
    supplement.add_argument("--review-round", type=int, choices=(1, 2), required=True)
    supplement.add_argument("--output", type=Path, required=True)

    duplicate = commands.add_parser("export-duplicates", help="导出近重复候选对")
    duplicate.add_argument("--candidate-build-id", required=True)
    duplicate.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    """解析路径与子命令；所有抽样规则和持久化逻辑留在核心模块。"""

    args = _parser().parse_args()
    if args.command == "create-initial":
        result = create_initial_sampling_run(
            args.derived_db,
            candidate_build_id=args.candidate_build_id,
            config=load_config(args.config),
        )
        payload = result.__dict__
    elif args.command == "create-periodic":
        result = create_periodic_sampling_run(
            args.derived_db,
            candidate_build_id=args.candidate_build_id,
            baseline_sample_run_id=args.baseline_sample_run_id,
            round_number=args.round_number,
            config=load_config(args.config),
        )
        payload = result.__dict__
    elif args.command == "export-post":
        count = export_post_annotation_tasks(
            args.derived_db,
            sample_run_id=args.sample_run_id,
            review_round=args.review_round,
            output_path=args.output,
        )
        payload = {"exported_count": count, "review_round": args.review_round}
    elif args.command == "export-supplement":
        count = export_supplement_annotation_tasks(
            args.derived_db,
            supplement_run_id=args.supplement_run_id,
            review_round=args.review_round,
            output_path=args.output,
        )
        payload = {
            "exported_count": count,
            "review_round": args.review_round,
            "supplement_run_id": args.supplement_run_id,
        }
    else:
        count = export_near_duplicate_candidates(
            args.derived_db,
            candidate_build_id=args.candidate_build_id,
            output_path=args.output,
        )
        payload = {"exported_pair_count": count}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
