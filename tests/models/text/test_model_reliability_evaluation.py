"""Wave A 设计加权成对评价与探索性边界测试。"""

from dataclasses import replace
from pathlib import Path

import pytest

from tourism_ugc_study.models.text.model_reliability_config import (
    load_model_reliability_plan,
)
from tourism_ugc_study.models.text.model_reliability_evaluation import (
    LabeledWaveMember,
    ModelReliabilityEvaluationError,
    evaluate_wave_a,
)
from tourism_ugc_study.models.text.model_reliability_study import (
    ScoredEvaluationMember,
)


PLAN = load_model_reliability_plan(
    Path("configs/cleaning-model-reliability-study.yaml")
)


def _labeled(index: int, *, label: str | None = None) -> LabeledWaveMember:
    """生成含设计权重的成对合成标签。"""

    tourism_label = label or ("related" if index % 2 == 0 else "unrelated")
    return LabeledWaveMember(
        task_id=f"task-{index}",
        source_post_id=index + 1,
        source_version=1,
        component_id=f"component-{index // 2}",
        stratum="synthetic",
        inclusion_probability=0.5,
        analysis_weight=2.0,
        sparse_p_unrelated=0.15 if tourism_label == "related" else 0.85,
        qwen_p_unrelated=0.10 if tourism_label == "related" else 0.90,
        tourism_label=tourism_label,
        reason_code="synthetic_reason",
        evidence_note="合成证据",
    )


def _population(count: int = 300) -> tuple[ScoredEvaluationMember, ...]:
    return tuple(
        ScoredEvaluationMember(
            source_post_id=index + 1,
            source_version=1,
            component_id=f"component-{index // 2}",
            normalized_sha256=f"{index:064x}",
            sparse_p_unrelated=(index + 1) / (count + 2),
            qwen_p_unrelated=(index + 2) / (count + 3),
        )
        for index in range(count)
    )


def test_wave_a_reports_uncertain_separately_and_never_selects_model() -> None:
    members = tuple(
        _labeled(index, label="uncertain" if index == 0 else None)
        for index in range(40)
    )

    report = evaluate_wave_a(
        members,
        _population(),
        probability_grid=(0.90,),
        coverage_grid=(0.25,),
        random_seed=PLAN.random_seed,
        confidence_level=0.95,
    )

    assert report["uncertain_count"] == 1
    assert report["determinate_count"] == 39
    assert report["may_select_model"] is False
    assert report["may_freeze_threshold"] is False
    assert report["labels_entered_fit"] is False
    assert report["overall_metrics"]["qwen"]["weighted_log_loss"] < report[
        "overall_metrics"
    ]["sparse"]["weighted_log_loss"]


def test_wave_a_rejects_missing_reason_code() -> None:
    members = list(_labeled(index) for index in range(10))
    members[0] = replace(members[0], reason_code="")

    with pytest.raises(ModelReliabilityEvaluationError) as error:
        evaluate_wave_a(
            members,
            _population(),
            probability_grid=(0.90,),
            coverage_grid=(0.25,),
            random_seed=PLAN.random_seed,
            confidence_level=0.95,
        )

    assert error.value.reason_code == "model_reliability_completed_labels_invalid"
