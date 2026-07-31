#!/usr/bin/env python3
"""执行图片技术噪声复核、决定、SHA 传播与保留集审计。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.config import ConfigurationError, load_config  # noqa: E402
from tourism_ugc_study.cleaning.image_decision_repository import (  # noqa: E402
    ImageDecisionRepositoryError,
    build_image_decisions,
    propagate_exact_sha_labels,
)
from tourism_ugc_study.cleaning.image_keep_audit_repository import (  # noqa: E402
    ImageKeepAuditRepositoryError,
    create_keep_audit_round,
    evaluate_keep_audit_round,
    export_keep_audit_tasks,
    import_keep_audit_annotations,
)
from tourism_ugc_study.cleaning.image_review_repository import (  # noqa: E402
    ImageReviewRepositoryError,
    create_boundary_supplement,
    create_double_label_plan,
    create_image_review_run,
    evaluate_image_agreement,
    export_image_annotation_tasks,
    import_image_annotations,
    record_image_adjudication,
)


def build_parser() -> argparse.ArgumentParser:
    """构造图片人工复核的薄命令行契约。

    所有子命令显式接收派生库和配置；导出/导入路径只作为进程输入，不进入
    JSON 回执。返回的解析器只负责类型、必填项和枚举选择；解析器本身不连接
    数据库、不打开图片、不执行网络请求。参数错误由 argparse 以退出码 2
    终止，不会被领域错误处理器改写。
    """

    parser = argparse.ArgumentParser(description="处理图片技术噪声人工复核证据。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="主清洗配置")
    commands = parser.add_subparsers(dest="command", required=True)

    create_review = commands.add_parser("create-review", help="冻结试标、候选或边界复核")
    create_review.add_argument("--candidate-build-id", required=True)
    create_review.add_argument(
        "--kind", required=True, choices=("pilot", "candidate_review", "boundary")
    )

    supplement = commands.add_parser("create-boundary-supplement", help="冻结至多五十张边界补充")
    supplement.add_argument("--candidate-build-id", required=True)
    supplement.add_argument("--prior-review-run-id", action="append", required=True)

    export_review = commands.add_parser("export-review", help="导出盲标槽位 CSV")
    export_review.add_argument("--review-run-id", required=True)
    export_review.add_argument("--assignment-slot", required=True, type=int, choices=(1, 2))
    export_review.add_argument("--output", required=True)

    import_review = commands.add_parser("import-review", help="追加导入盲标 CSV")
    import_review.add_argument("--input", required=True)
    import_review.add_argument("--imported-by-hash", required=True)

    double_plan = commands.add_parser("create-double-plan", help="冻结边界或拟排除第二槽计划")
    double_plan.add_argument("--review-run-id", required=True)
    double_plan.add_argument(
        "--kind", required=True, choices=("boundary", "proposed_exclusion")
    )

    agreement = commands.add_parser("agreement", help="评估完整边界双标计划")
    agreement.add_argument("--review-run-id", required=True)

    adjudicate = commands.add_parser("adjudicate", help="追加一条第三人仲裁")
    adjudicate.add_argument("--review-run-id", required=True)
    adjudicate.add_argument("--fingerprint-id", required=True)
    adjudicate.add_argument("--left-annotation-id", required=True)
    adjudicate.add_argument("--right-annotation-id", required=True)
    adjudicate.add_argument("--adjudicator-hash", required=True)
    adjudicate.add_argument("--label", required=True)
    adjudicate.add_argument("--reason-code", action="append", default=[])
    adjudicate.add_argument("--annotated-at-utc", required=True)

    decisions = commands.add_parser("build-decisions", help="封存代表决定快照")
    decisions.add_argument("--candidate-build-id", required=True)
    decisions.add_argument(
        "--candidate-review-run-id",
        help="修订决定时显式选择同 build、同手册的候选复核运行",
    )

    propagate = commands.add_parser("propagate-sha", help="仅沿 SHA 精确簇传播人工标签")
    propagate.add_argument("--decision-build-id", required=True)

    create_audit = commands.add_parser("create-audit", help="冻结两层保留集审计样本")
    create_audit.add_argument("--decision-build-id", required=True)
    create_audit.add_argument("--round-number", required=True, type=int)

    export_audit = commands.add_parser("export-audit", help="导出保留集审计 CSV")
    export_audit.add_argument("--audit-round-id", required=True)
    export_audit.add_argument("--output", required=True)

    import_audit = commands.add_parser("import-audit", help="追加导入保留集审计标签")
    import_audit.add_argument("--input", required=True)

    evaluate_audit = commands.add_parser("evaluate-audit", help="评估审计事件率和 Wilson 上限")
    evaluate_audit.add_argument("--audit-round-id", required=True)
    return parser


def _payload(args: argparse.Namespace) -> dict[str, object]:
    """调用解耦核心 API，并返回只含 ID、计数、状态和哈希的字典。"""

    config = load_config(args.config)
    if args.command == "create-review":
        return asdict(
            create_image_review_run(
                args.derived_db,
                candidate_build_id=args.candidate_build_id,
                review_kind=args.kind,
                config=config,
            )
        )
    if args.command == "create-boundary-supplement":
        review, plan = create_boundary_supplement(
            args.derived_db,
            candidate_build_id=args.candidate_build_id,
            prior_review_run_ids=args.prior_review_run_id,
            config=config,
        )
        return {"review": asdict(review), "double_plan": asdict(plan)}
    if args.command == "export-review":
        count = export_image_annotation_tasks(
            args.derived_db,
            review_run_id=args.review_run_id,
            assignment_slot=args.assignment_slot,
            output_path=args.output,
        )
        return {"review_run_id": args.review_run_id, "assignment_slot": args.assignment_slot, "task_count": count}
    if args.command == "import-review":
        return asdict(
            import_image_annotations(
                args.derived_db,
                csv_path=args.input,
                imported_by_hash=args.imported_by_hash,
            )
        )
    if args.command == "create-double-plan":
        return asdict(
            create_double_label_plan(
                args.derived_db,
                review_run_id=args.review_run_id,
                plan_kind=args.kind,
            )
        )
    if args.command == "agreement":
        return asdict(
            evaluate_image_agreement(
                args.derived_db, review_run_id=args.review_run_id, config=config
            )
        )
    if args.command == "adjudicate":
        return asdict(
            record_image_adjudication(
                args.derived_db,
                review_run_id=args.review_run_id,
                fingerprint_id=args.fingerprint_id,
                left_annotation_id=args.left_annotation_id,
                right_annotation_id=args.right_annotation_id,
                adjudicator_hash=args.adjudicator_hash,
                guide_version=config.image_label_guide_version,
                technical_noise_label=args.label,
                reason_codes=args.reason_code,
                adjudicated_at_utc=args.annotated_at_utc,
            )
        )
    if args.command == "build-decisions":
        return asdict(
            build_image_decisions(
                args.derived_db,
                candidate_build_id=args.candidate_build_id,
                config=config,
                candidate_review_run_id=args.candidate_review_run_id,
            )
        )
    if args.command == "propagate-sha":
        return asdict(
            propagate_exact_sha_labels(
                args.derived_db, decision_build_id=args.decision_build_id
            )
        )
    if args.command == "create-audit":
        return asdict(
            create_keep_audit_round(
                args.derived_db,
                decision_build_id=args.decision_build_id,
                round_number=args.round_number,
                config=config,
            )
        )
    if args.command == "export-audit":
        count = export_keep_audit_tasks(
            args.derived_db,
            audit_round_id=args.audit_round_id,
            output_path=args.output,
        )
        return {"audit_round_id": args.audit_round_id, "task_count": count}
    if args.command == "import-audit":
        return asdict(import_keep_audit_annotations(args.derived_db, csv_path=args.input))
    return asdict(
        evaluate_keep_audit_round(
            args.derived_db, audit_round_id=args.audit_round_id, config=config
        )
    )


def main(argv: list[str] | None = None) -> int:
    """执行单个图片复核操作并输出去敏单行 JSON。

    成功返回 0；配置/复核/决定/审计领域错误返回 1 和稳定 reason_code。错误
    回执不含路径、URL、图片、CSV 内容或 traceback。命令不联网、不打开原图、
    不回写正式采集库；argparse 参数错误保持退出码 2。
    """

    args = build_parser().parse_args(argv)
    try:
        payload = _payload(args)
    except (
        ConfigurationError,
        ImageReviewRepositoryError,
        ImageDecisionRepositoryError,
        ImageKeepAuditRepositoryError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", "configuration_invalid")
        print(json.dumps({"status": "failed", "reason_code": reason_code}), file=sys.stderr)
        return 1
    payload["status"] = payload.get("evaluation_status", "completed")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
