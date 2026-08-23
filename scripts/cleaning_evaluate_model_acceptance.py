#!/usr/bin/env python3
"""按冻结的 UGC 安全优先策略评估一个 sparse challenger。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.models.text.model_acceptance import ModelAcceptanceError
from tourism_ugc_study.models.text.model_acceptance_artifacts import (
    evaluate_model_acceptance_artifact,
)


def _parser() -> argparse.ArgumentParser:
    """构造不接收测试、平台或阈值参数的 CLI。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--output-format",
        choices=("human", "json"),
        default="human",
    )
    return parser


def _render(report: Mapping[str, Any]) -> str:
    """渲染安全门优先于总体指标的中文摘要。"""

    deltas = report["candidate_minus_baseline"]
    intervals = report["paired_component_bootstrap_intervals"]
    gates = report["gates"]
    safety = intervals["related_to_unrelated_rate"]
    loss = intervals["log_loss"]
    pr_auc = intervals["pr_auc_unrelated"]
    lines = [
        "UGC 安全优先模型验收",
        "=====================",
        f"策略 ID：{report['policy_id']}",
        f"baseline：{report['baseline_model_id']}",
        f"challenger：{report['candidate_model_id']}",
        f"证据：训练侧配对 nested group OOF，n={report['observation_count']}，"
        f"leakage components={report['component_count']}",
        "",
        "安全硬门（related→unrelated；challenger-baseline）",
        f"  点差：{deltas['related_to_unrelated_rate'] * 100:+.2f} 个百分点",
        f"  单侧90%区间表示：[{safety['lower'] * 100:+.2f}, "
        f"{safety['upper'] * 100:+.2f}] 个百分点",
        f"  点估计不恶化：{'通过' if gates['safety_point_noninferiority'] else '未通过'}",
        f"  上界不超过+2个百分点："
        f"{'通过' if gates['safety_upper_confidence_bound'] else '未通过'}",
        "",
        "总体与概率质量门",
        f"  log loss 点差：{deltas['log_loss']:+.4f}；"
        f"90%区间 [{loss['lower']:+.4f}, {loss['upper']:+.4f}]",
        f"  unrelated PR-AUC 点差：{deltas['pr_auc_unrelated']:+.4f}；"
        f"90%区间 [{pr_auc['lower']:+.4f}, {pr_auc['upper']:+.4f}]",
        f"  Brier 点差：{deltas['brier_score']:+.4f}",
        "",
        f"结论：{report['status']}",
        "边界：通过仅允许唯一胜出者做一次验证方向性复核；"
        "不代表测试、阈值或自动清洗通过。",
        "锁定测试：locked_not_opened；路由阈值：UNSET。",
    ]
    return "\n".join(lines)


def main() -> int:
    """执行聚合验收并只公开稳定失败码。

    Returns:
        成功生成报告为 ``0``；契约或写入失败为 ``2``。
    """

    args = _parser().parse_args()
    try:
        report = dict(
            evaluate_model_acceptance_artifact(
                args.policy,
                args.evidence,
                args.output,
            )
        )
    except ModelAcceptanceError as exc:
        print(
            json.dumps(
                {"status": "failed", "reason_code": exc.reason_code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if args.output_format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(_render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
