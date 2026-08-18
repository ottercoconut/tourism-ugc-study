#!/usr/bin/env python3
"""从显式仲裁 ID 清单训练旅游相关性基线；默认不执行训练。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.models.text.repository import (
    TrainingOptions,
    train_relevance_from_adjudications,
)


def _read_ids(path: Path) -> tuple[str, ...]:
    """读取每行一个仲裁 ID 的显式清单，拒绝隐式“全部金标”。"""

    values = tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not values:
        raise ValueError("gold adjudication ID manifest is empty")
    return values


def _read_post_ids(path: Path) -> tuple[int, ...]:
    """读取 smoke 专用候选帖子 ID 清单；核心 API 会再次核验容量和唯一性。"""

    try:
        return tuple(int(value) for value in _read_ids(path))
    except ValueError as exc:
        raise ValueError("candidate post ID manifest contains a non-integer") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument("--candidate-build-id", required=True)
    parser.add_argument("--leakage-build-id", required=True)
    parser.add_argument("--gold-adjudication-ids", type=Path, required=True)
    parser.add_argument("--artifact-directory", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/cleaning-v3.0.yaml")
    )
    modes = parser.add_subparsers(dest="run_mode", required=True)
    formal = modes.add_parser("formal", help="按正式 20 条/平台测试约束训练")
    formal.add_argument(
        "--execute-formal-training",
        action="store_true",
        help="显式确认执行正式训练；缺少此开关时拒绝运行",
    )
    smoke = modes.add_parser("smoke", help="仅对不超过上限的小样本做连通测试")
    smoke.add_argument("--test-min-per-platform", type=int, default=2)
    smoke.add_argument(
        "--candidate-post-ids",
        type=Path,
        required=True,
        help="最多 100 条、每行一个 source_post_id 的隔离候选清单",
    )
    return parser


def main() -> int:
    """执行显式 formal/smoke 模式；两者不可互相降级或冒充。"""

    parser = _parser()
    args = parser.parse_args()
    if args.run_mode == "formal" and not args.execute_formal_training:
        parser.error("formal training requires --execute-formal-training")
    if args.run_mode == "smoke" and args.test_min_per_platform <= 0:
        parser.error("smoke test minimum must be positive")
    options = (
        TrainingOptions(run_mode="formal", formal_execution_confirmed=True)
        if args.run_mode == "formal"
        else TrainingOptions(
            run_mode="smoke",
            temporal_test_min_per_platform_override=args.test_min_per_platform,
            smoke_candidate_post_ids=_read_post_ids(args.candidate_post_ids),
        )
    )
    result = train_relevance_from_adjudications(
        args.derived_db,
        candidate_build_id=args.candidate_build_id,
        leakage_build_id=args.leakage_build_id,
        gold_adjudication_ids=_read_ids(args.gold_adjudication_ids),
        artifact_directory=args.artifact_directory,
        config=load_config(args.config),
        options=options,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
