#!/usr/bin/env python3
"""生成或汇总隐藏模型答案的 baseline 标签一致性复核任务。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.models.text.blind_label_review import BlindLabelReviewError
from tourism_ugc_study.models.text.blind_label_review_artifacts import (
    apply_approved_blind_label_corrections,
    prepare_blind_label_review_package,
    write_blind_label_review_summary,
)


def _parser() -> argparse.ArgumentParser:
    """构造盲化任务生成与汇总子命令。"""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="从训练 OOF 与验证开发概率生成私有盲化任务包",
    )
    prepare.add_argument("--package-dir", type=Path, required=True)
    prepare.add_argument("--reference-csv", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--random-seed", type=int, default=20260728)
    prepare.add_argument("--probability-cutoff", type=float, default=0.90)
    prepare.add_argument("--short-text-max", type=int, default=600)
    prepare.add_argument("--control-ratio", type=int, default=1)
    prepare.add_argument(
        "--output-format",
        choices=("human", "json"),
        default="human",
    )

    summarize = subparsers.add_parser(
        "summarize",
        help="校验已填写任务并分别汇总目标与随机正确对照",
    )
    summarize.add_argument("--review-dir", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    summarize.add_argument(
        "--output-format",
        choices=("human", "json"),
        default="human",
    )

    apply = subparsers.add_parser(
        "apply",
        help="把用户明确批准的改标原位应用到最终参考 CSV＋manifest",
    )
    apply.add_argument("--review-dir", type=Path, required=True)
    apply.add_argument("--summary", type=Path, required=True)
    apply.add_argument("--reference-csv", type=Path, required=True)
    apply.add_argument("--reference-manifest", type=Path, required=True)
    apply.add_argument("--output-receipt", type=Path, required=True)
    apply.add_argument(
        "--approved-review-key",
        action="append",
        required=True,
        help="用户批准应用的 review key；每个 key 重复传入一次",
    )
    apply.add_argument("--expected-reference-csv-sha256", required=True)
    apply.add_argument(
        "--execute-reference-update",
        action="store_true",
        help="显式确认原位更新最终参考证据；缺少时失败关闭",
    )
    apply.add_argument(
        "--output-format",
        choices=("human", "json"),
        default="human",
    )
    return parser


def _render_prepare(result: Mapping[str, Any]) -> str:
    """渲染不泄露任务成员或本机路径的生成摘要。"""

    return "\n".join(
        (
            "隐藏模型答案的标签一致性复核任务已准备",
            "========================================",
            f"复核 ID：{result['review_id']}",
            f"模型 ID：{result['model_id']}",
            f"模型矛盾目标：{result['target_count']} 条",
            f"匹配随机正确对照：{result['control_count']} 条",
            f"  同长度层精确匹配：{result['exact_match_control_count']} 条",
            f"  同标签/切分内最近长度匹配：{result['relaxed_match_control_count']} 条",
            f"任务总数：{result['total_count']} 条",
            "测试状态：locked_not_opened（未读取测试成员或概率）",
            "人工操作：只填写 review_label；review_note 可选。",
            "允许标签：related / unrelated / uncertain。",
        )
    )


def _render_summary(summary: Mapping[str, Any]) -> str:
    """渲染解除盲化后的分组汇总与推断边界。"""

    lines = [
        "隐藏模型答案的标签一致性复核汇总",
        "================================",
        f"复核 ID：{summary['review_id']}",
        f"模型 ID：{summary['model_id']}",
        "状态：completed_not_applied（未修改最终 CSV）",
    ]
    labels = (
        ("model_conflict_target", "模型矛盾目标"),
        ("matched_correct_control", "随机正确对照"),
    )
    for key, label in labels:
        values = summary["group_summaries"][key]
        rate = values["change_rate_among_determinate"]
        rendered_rate = "不可计算" if rate is None else f"{rate * 100:.1f}%"
        lines.extend(
            (
                "",
                f"{label}（n={values['count']}）",
                f"  维持：{values['maintained_count']} 条",
                f"  修改：{values['changed_count']} 条",
                f"  不确定：{values['uncertain_count']} 条",
                f"  确定结果中的修改率：{rendered_rate}",
            )
        )
    lines.extend(
        (
            "",
            "边界：定向矛盾组不能估计700条或候选人口总体标签错误率。",
            "任何标签修改仍需用户明确批准，程序没有自动回写最终 CSV。",
        )
    )
    return "\n".join(lines)


def _render_apply(receipt: Mapping[str, Any]) -> str:
    """渲染标签复核应用后的新证据身份。"""

    directions = receipt["approved_direction_counts"]
    return "\n".join(
        (
            "隐藏模型答案的标签复核裁决已应用",
            "================================",
            f"复核 ID：{receipt['review_id']}",
            f"批准修改：{receipt['approved_change_count']} 条",
            f"否决修改：{receipt['rejected_change_count']} 条",
            "修改方向："
            f"related→unrelated {directions.get('related_to_unrelated', 0)} 条；"
            f"unrelated→related {directions.get('unrelated_to_related', 0)} 条",
            f"新标签：related={receipt['label_counts']['related']}；"
            f"unrelated={receipt['label_counts']['unrelated']}",
            f"新 CSV SHA-256：{receipt['reference_csv_sha256']}",
            f"新 manifest SHA-256：{receipt['reference_manifest_sha256']}",
            f"新成员 SHA-256：{receipt['reference_member_sha256']}",
            "测试状态：locked_not_opened；数据库、模型和阈值未修改。",
        )
    )


def main() -> int:
    """执行选定子命令，并只公开稳定失败码。

    Returns:
        成功为 ``0``；契约或本地写入失败为 ``2``。
    """

    args = _parser().parse_args()
    try:
        if args.command == "prepare":
            result = prepare_blind_label_review_package(
                args.package_dir,
                args.reference_csv,
                args.output_dir,
                random_seed=args.random_seed,
                probability_cutoff=args.probability_cutoff,
                short_text_max=args.short_text_max,
                control_ratio=args.control_ratio,
            )
            payload = asdict(result)
            rendered = _render_prepare(payload)
        elif args.command == "summarize":
            payload = dict(
                write_blind_label_review_summary(args.review_dir, args.output)
            )
            rendered = _render_summary(payload)
        else:
            if not args.execute_reference_update:
                raise BlindLabelReviewError(
                    "blind_review_apply_explicit_execution_required"
                )
            payload = dict(
                apply_approved_blind_label_corrections(
                    args.review_dir,
                    args.summary,
                    args.reference_csv,
                    args.reference_manifest,
                    args.output_receipt,
                    approved_review_keys=args.approved_review_key,
                    expected_reference_csv_sha256=(
                        args.expected_reference_csv_sha256
                    ),
                )
            )
            rendered = _render_apply(payload)
        if args.output_format == "json":
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(rendered)
    except BlindLabelReviewError as exc:
        print(
            json.dumps(
                {"status": "failed", "reason_code": exc.reason_code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
