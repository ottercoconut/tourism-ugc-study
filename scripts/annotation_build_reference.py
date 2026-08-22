#!/usr/bin/env python3
"""分阶段生成最终700条不重复建模参考集及其不可变谱系。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.annotation.reference_contract import ReferenceDatasetError
from tourism_ugc_study.annotation.reference_artifacts import (
    seal_duplicate_decision_artifact,
    seal_label_resolution_artifact,
    seal_supplemental_label_artifact,
)
from tourism_ugc_study.annotation.reference_workflow import (
    ReferenceWorkflowContext,
    build_duplicate_review_stage,
    build_label_conflict_review_stage,
    build_replacement_duplicate_review_stage,
    build_replacement_label_conflict_review_stage,
    build_replacement_queue_stage,
    build_supplemental_label_stage,
    finalize_reference_stage,
)
from tourism_ugc_study.cleaning.config import (
    ConfigurationError,
    load_cleaning_config_bundle,
)


def _common(parser: argparse.ArgumentParser) -> None:
    """向子命令添加旧证据迁移所需的共享参数。

    Args:
        parser: 待扩展的子命令解析器。
    """

    parser.add_argument("--config", type=Path, default=Path("configs/cleaning.yaml"))
    parser.add_argument("--legacy-csv", type=Path, required=True)
    parser.add_argument("--legacy-manifest", type=Path, required=True)
    parser.add_argument("--derived-db", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    """构造分阶段参考集生成命令行。

    Returns:
        只负责参数解析的顶层解析器。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser(
        "build-duplicate-review", help="完整比较700条并生成近重复人工复核 CSV"
    )
    _common(review)
    review.add_argument("--output-csv", type=Path, required=True)
    review.add_argument("--output-manifest", type=Path, required=True)

    queue = commands.add_parser(
        "build-replacement-queue", help="消费最终重复结论并冻结全局候补队列"
    )
    _common(queue)
    queue.add_argument("--duplicate-decisions-csv", type=Path, required=True)
    queue.add_argument("--duplicate-decisions-manifest", type=Path, required=True)
    queue.add_argument("--label-resolutions-csv", type=Path, required=True)
    queue.add_argument("--label-resolutions-manifest", type=Path, required=True)
    queue.add_argument("--output-csv", type=Path, required=True)
    queue.add_argument("--output-manifest", type=Path, required=True)

    replacement_review = commands.add_parser(
        "build-replacement-duplicate-review",
        help="对候补前缀与初始全部谱系/同轮记录做重复复核",
    )
    _common(replacement_review)
    replacement_review.add_argument("--duplicate-decisions-csv", type=Path, required=True)
    replacement_review.add_argument(
        "--duplicate-decisions-manifest", type=Path, required=True
    )
    replacement_review.add_argument("--label-resolutions-csv", type=Path, required=True)
    replacement_review.add_argument(
        "--label-resolutions-manifest", type=Path, required=True
    )
    replacement_review.add_argument("--max-queue-rank", type=int, required=True)
    replacement_review.add_argument("--output-csv", type=Path, required=True)
    replacement_review.add_argument("--output-manifest", type=Path, required=True)

    replacement_conflict = commands.add_parser(
        "build-replacement-label-conflict-review",
        help="根据候补重复决定和累积标签生成分量标签冲突任务",
    )
    _common(replacement_conflict)
    replacement_conflict.add_argument(
        "--duplicate-decisions-csv", type=Path, required=True
    )
    replacement_conflict.add_argument(
        "--duplicate-decisions-manifest", type=Path, required=True
    )
    replacement_conflict.add_argument(
        "--label-resolutions-csv", type=Path, required=True
    )
    replacement_conflict.add_argument(
        "--label-resolutions-manifest", type=Path, required=True
    )
    replacement_conflict.add_argument(
        "--replacement-duplicate-decisions-csv", type=Path, required=True
    )
    replacement_conflict.add_argument(
        "--replacement-duplicate-decisions-manifest", type=Path, required=True
    )
    replacement_conflict.add_argument("--max-queue-rank", type=int, required=True)
    replacement_conflict.add_argument("--prior-supplemental-labels-csv", type=Path)
    replacement_conflict.add_argument(
        "--prior-supplemental-labels-manifest", type=Path
    )
    replacement_conflict.add_argument("--output-csv", type=Path, required=True)
    replacement_conflict.add_argument("--output-manifest", type=Path, required=True)

    labels = commands.add_parser(
        "build-supplemental-labels",
        help="在候补重复复核完成后生成补充旅游相关性任务",
    )
    _common(labels)
    labels.add_argument("--duplicate-decisions-csv", type=Path, required=True)
    labels.add_argument("--duplicate-decisions-manifest", type=Path, required=True)
    labels.add_argument("--label-resolutions-csv", type=Path, required=True)
    labels.add_argument("--label-resolutions-manifest", type=Path, required=True)
    labels.add_argument(
        "--replacement-duplicate-decisions-csv", type=Path, required=True
    )
    labels.add_argument(
        "--replacement-duplicate-decisions-manifest", type=Path, required=True
    )
    labels.add_argument(
        "--replacement-label-resolutions-csv", type=Path, required=True
    )
    labels.add_argument(
        "--replacement-label-resolutions-manifest", type=Path, required=True
    )
    labels.add_argument("--max-queue-rank", type=int, required=True)
    labels.add_argument("--reserve-count", type=int, default=0)
    labels.add_argument("--prior-supplemental-labels-csv", type=Path)
    labels.add_argument("--prior-supplemental-labels-manifest", type=Path)
    labels.add_argument("--output-csv", type=Path, required=True)
    labels.add_argument("--output-manifest", type=Path, required=True)

    finalize = commands.add_parser(
        "finalize", help="消费最终人工证据并封存唯一700条 CSV 与 manifest"
    )
    _common(finalize)
    finalize.add_argument("--duplicate-decisions-csv", type=Path, required=True)
    finalize.add_argument("--duplicate-decisions-manifest", type=Path, required=True)
    finalize.add_argument("--label-resolutions-csv", type=Path, required=True)
    finalize.add_argument("--label-resolutions-manifest", type=Path, required=True)
    finalize.add_argument(
        "--replacement-duplicate-decisions-csv", type=Path, required=True
    )
    finalize.add_argument(
        "--replacement-duplicate-decisions-manifest", type=Path, required=True
    )
    finalize.add_argument(
        "--replacement-label-resolutions-csv", type=Path, required=True
    )
    finalize.add_argument(
        "--replacement-label-resolutions-manifest", type=Path, required=True
    )
    finalize.add_argument("--supplemental-labels-csv", type=Path, required=True)
    finalize.add_argument("--supplemental-labels-manifest", type=Path, required=True)
    finalize.add_argument("--output-csv", type=Path, required=True)
    finalize.add_argument("--output-manifest", type=Path, required=True)

    seal = commands.add_parser(
        "seal-duplicate-decisions",
        help="校验人工完成副本并追加 finalized 重复决定 manifest",
    )
    seal.add_argument("--completed-csv", type=Path, required=True)
    seal.add_argument("--pending-manifest", type=Path, required=True)
    seal.add_argument("--output-manifest", type=Path, required=True)

    seal_labels = commands.add_parser(
        "seal-supplemental-labels",
        help="校验补充标签完成副本并追加 finalized manifest",
    )
    seal_labels.add_argument("--completed-csv", type=Path, required=True)
    seal_labels.add_argument("--pending-manifest", type=Path, required=True)
    seal_labels.add_argument("--output-manifest", type=Path, required=True)

    conflict = commands.add_parser(
        "build-label-conflict-review",
        help="根据 finalized 重复决定生成标签冲突人工任务",
    )
    _common(conflict)
    conflict.add_argument("--duplicate-decisions-csv", type=Path, required=True)
    conflict.add_argument("--duplicate-decisions-manifest", type=Path, required=True)
    conflict.add_argument("--output-csv", type=Path, required=True)
    conflict.add_argument("--output-manifest", type=Path, required=True)

    seal_resolutions = commands.add_parser(
        "seal-label-resolutions",
        help="校验标签冲突完成副本并追加 finalized manifest",
    )
    seal_resolutions.add_argument("--completed-csv", type=Path, required=True)
    seal_resolutions.add_argument("--pending-manifest", type=Path, required=True)
    seal_resolutions.add_argument("--output-manifest", type=Path, required=True)
    return parser


def _context(args: argparse.Namespace) -> ReferenceWorkflowContext:
    """从稳定配置构造无输出路径的工作流上下文。

    Args:
        args: 已解析命令行参数。

    Returns:
        标签手册、规范化规则和随机种子已冻结的上下文。
    """

    config, normalization_config = load_cleaning_config_bundle(args.config)
    return ReferenceWorkflowContext(
        legacy_csv=args.legacy_csv,
        legacy_manifest=args.legacy_manifest,
        derived_db=args.derived_db,
        label_guide_id=config.label_guide_version,
        normalization_rule_id=str(config.artifacts["normalization_version_lock"]),
        normalization_config=normalization_config,
        random_seed=config.random_seed,
    )


def main() -> int:
    """执行选定阶段并只输出哈希、计数、状态或稳定失败码。

    Returns:
        成功或幂等复用时为 ``0``；失败关闭时为 ``2``。
    """

    args = _parser().parse_args()
    try:
        if args.command == "seal-duplicate-decisions":
            result = seal_duplicate_decision_artifact(
                args.completed_csv, args.pending_manifest, args.output_manifest
            )
        elif args.command == "seal-supplemental-labels":
            result = seal_supplemental_label_artifact(
                args.completed_csv, args.pending_manifest, args.output_manifest
            )
        elif args.command == "seal-label-resolutions":
            result = seal_label_resolution_artifact(
                args.completed_csv, args.pending_manifest, args.output_manifest
            )
        elif args.command == "build-duplicate-review":
            context = _context(args)
            result = build_duplicate_review_stage(
                context,
                output_csv=args.output_csv,
                output_manifest=args.output_manifest,
            )
        elif args.command == "build-replacement-queue":
            context = _context(args)
            result = build_replacement_queue_stage(
                context,
                duplicate_decisions_csv=args.duplicate_decisions_csv,
                duplicate_decisions_manifest=args.duplicate_decisions_manifest,
                label_resolutions_csv=args.label_resolutions_csv,
                label_resolutions_manifest=args.label_resolutions_manifest,
                output_csv=args.output_csv,
                output_manifest=args.output_manifest,
            )
        elif args.command == "build-label-conflict-review":
            context = _context(args)
            result = build_label_conflict_review_stage(
                context,
                duplicate_decisions_csv=args.duplicate_decisions_csv,
                duplicate_decisions_manifest=args.duplicate_decisions_manifest,
                output_csv=args.output_csv,
                output_manifest=args.output_manifest,
            )
        elif args.command == "build-replacement-duplicate-review":
            context = _context(args)
            result = build_replacement_duplicate_review_stage(
                context,
                duplicate_decisions_csv=args.duplicate_decisions_csv,
                duplicate_decisions_manifest=args.duplicate_decisions_manifest,
                label_resolutions_csv=args.label_resolutions_csv,
                label_resolutions_manifest=args.label_resolutions_manifest,
                max_queue_rank=args.max_queue_rank,
                output_csv=args.output_csv,
                output_manifest=args.output_manifest,
            )
        elif args.command == "build-replacement-label-conflict-review":
            context = _context(args)
            result = build_replacement_label_conflict_review_stage(
                context,
                duplicate_decisions_csv=args.duplicate_decisions_csv,
                duplicate_decisions_manifest=args.duplicate_decisions_manifest,
                label_resolutions_csv=args.label_resolutions_csv,
                label_resolutions_manifest=args.label_resolutions_manifest,
                replacement_duplicate_decisions_csv=(
                    args.replacement_duplicate_decisions_csv
                ),
                replacement_duplicate_decisions_manifest=(
                    args.replacement_duplicate_decisions_manifest
                ),
                max_queue_rank=args.max_queue_rank,
                prior_supplemental_labels_csv=(
                    args.prior_supplemental_labels_csv
                ),
                prior_supplemental_labels_manifest=(
                    args.prior_supplemental_labels_manifest
                ),
                output_csv=args.output_csv,
                output_manifest=args.output_manifest,
            )
        elif args.command == "build-supplemental-labels":
            context = _context(args)
            result = build_supplemental_label_stage(
                context,
                duplicate_decisions_csv=args.duplicate_decisions_csv,
                duplicate_decisions_manifest=args.duplicate_decisions_manifest,
                label_resolutions_csv=args.label_resolutions_csv,
                label_resolutions_manifest=args.label_resolutions_manifest,
                replacement_duplicate_decisions_csv=(
                    args.replacement_duplicate_decisions_csv
                ),
                replacement_duplicate_decisions_manifest=(
                    args.replacement_duplicate_decisions_manifest
                ),
                replacement_label_resolutions_csv=(
                    args.replacement_label_resolutions_csv
                ),
                replacement_label_resolutions_manifest=(
                    args.replacement_label_resolutions_manifest
                ),
                max_queue_rank=args.max_queue_rank,
                reserve_count=args.reserve_count,
                prior_supplemental_labels_csv=args.prior_supplemental_labels_csv,
                prior_supplemental_labels_manifest=(
                    args.prior_supplemental_labels_manifest
                ),
                output_csv=args.output_csv,
                output_manifest=args.output_manifest,
            )
        else:
            context = _context(args)
            result = finalize_reference_stage(
                context,
                duplicate_decisions_csv=args.duplicate_decisions_csv,
                duplicate_decisions_manifest=args.duplicate_decisions_manifest,
                label_resolutions_csv=args.label_resolutions_csv,
                label_resolutions_manifest=args.label_resolutions_manifest,
                replacement_duplicate_decisions_csv=(
                    args.replacement_duplicate_decisions_csv
                ),
                replacement_duplicate_decisions_manifest=(
                    args.replacement_duplicate_decisions_manifest
                ),
                replacement_label_resolutions_csv=(
                    args.replacement_label_resolutions_csv
                ),
                replacement_label_resolutions_manifest=(
                    args.replacement_label_resolutions_manifest
                ),
                supplemental_labels_csv=args.supplemental_labels_csv,
                supplemental_labels_manifest=args.supplemental_labels_manifest,
                output_csv=args.output_csv,
                output_manifest=args.output_manifest,
            )
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
    except (ConfigurationError, ReferenceDatasetError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason_code": getattr(exc, "reason_code", "configuration_invalid"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
