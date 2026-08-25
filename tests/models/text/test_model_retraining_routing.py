"""非零风险门、Pareto和唯一策略决胜测试。"""

from __future__ import annotations

from dataclasses import replace

from tourism_ugc_study.models.text.model_retraining_config import (
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_routing import (
    _routing_point,
    analyze_retraining_routing,
)
from tourism_ugc_study.models.text.model_retraining_training import (
    RetrainingOofObservation,
)


def _rows(
    candidate_name: str,
    probabilities: list[float],
    labels: list[str],
) -> list[RetrainingOofObservation]:
    """构造完整Wave B规模的合成交叉拟合证据。"""

    return [
        RetrainingOofObservation(
            member_key=f"{candidate_name}-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            tourism_label=label,
            evidence_origin="wave_b",
            analysis_weight=1.0,
            historical_test_consumed=False,
            candidate_name=candidate_name,
            candidate_id=(candidate_name[0] * 32),
            fold_index=index % 5,
            calibration_excluded_fold_index=index % 5,
            margin=probability,
            p_unrelated=probability,
        )
        for index, (probability, label) in enumerate(zip(probabilities, labels))
    ]


def test_two_and_five_percent_point_risk_gates() -> None:
    """4/202与7/158通过，而5/202与8/158必须失败。"""

    plan = load_model_retraining_plan("configs/cleaning-model-retraining.yaml")
    labels = ["related"] * 155 + ["unrelated"] * 205
    passing_probabilities = (
        [0.99] * 4
        + [0.01] * 151
        + [0.01] * 7
        + [0.99] * 198
    )
    passing = _routing_point(
        _rows("qwen_linear_svc", passing_probabilities, labels),
        T_keep=0.01,
        T_exclude=0.99,
        weighted_log_loss=0.2,
        plan=plan,
    )
    assert passing.auto_exclude_adverse_count == 4
    assert passing.auto_keep_adverse_count == 7
    assert passing.risk_gate_passed is True

    failing_probabilities = (
        [0.99] * 5
        + [0.01] * 150
        + [0.01] * 8
        + [0.99] * 197
    )
    failing = _routing_point(
        _rows("qwen_linear_svc", failing_probabilities, labels),
        T_keep=0.01,
        T_exclude=0.99,
        weighted_log_loss=0.2,
        plan=plan,
    )
    assert failing.auto_exclude_adverse_count == 5
    assert failing.auto_keep_adverse_count == 8
    assert failing.risk_gate_passed is False


def test_selection_minimizes_manual_rate_and_stops_at_fixed_candidates() -> None:
    """风险合格候选中必须选择人工率最低者并输出完整Pareto证据。"""

    plan = replace(
        load_model_retraining_plan("configs/cleaning-model-retraining.yaml"),
        bootstrap_repetitions=50,
    )
    labels = ["related"] * 155 + ["unrelated"] * 205
    qwen = (
        [0.99] * 3
        + [0.01] * 152
        + [0.01] * 7
        + [0.99] * 198
    )
    sparse = [0.01] * 155 + [0.50] * 105 + [0.99] * 100
    fusion = [0.01] * 155 + [0.50] * 40 + [0.99] * 165
    observations = (
        _rows("qwen_linear_svc", qwen, labels)
        + _rows("sparse_linear_svc", sparse, labels)
        + _rows("logit_fusion", fusion, labels)
    )
    result = analyze_retraining_routing(observations, plan=plan)
    assert result.selected.candidate_name == "qwen_linear_svc"
    assert result.selected.manual_review_rate == 0.0
    assert result.selected.risk_gate_passed is True
    assert result.scanned_point_count == 3 * 49 * 49
    assert result.pareto_frontier
    assert result.population_evidence.endswith("with_design_weights")
