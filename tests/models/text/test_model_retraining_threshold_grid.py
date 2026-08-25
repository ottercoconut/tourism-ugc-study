"""双阈值完整事后探索的数量、风险与推荐规则测试。"""

from __future__ import annotations

from tourism_ugc_study.models.text.model_retraining_threshold_grid import (
    AuditThresholdObservation,
    evaluate_threshold_grid,
)
from tourism_ugc_study.models.text.model_retraining_training import (
    RetrainingOofObservation,
)


def _wave_row(
    index: int, probability: float, label: str, weight: float = 1.0
) -> RetrainingOofObservation:
    """生成一个不含平台字段的合成Wave B折外成员。"""

    return RetrainingOofObservation(
        member_key=f"member-{index}",
        component_id=f"component-{index}",
        tourism_label=label,
        evidence_origin="wave_b",
        historical_test_consumed=False,
        analysis_weight=weight,
        candidate_name="logit_fusion",
        candidate_id="candidate",
        fold_index=index % 5,
        calibration_excluded_fold_index=index % 5,
        margin=probability,
        p_unrelated=probability,
    )


def test_grid_outputs_every_pair_and_exact_population_partition() -> None:
    """每个阈值组合都必须出现且三段数量严格覆盖完整人口。"""

    wave = tuple(
        _wave_row(
            index,
            probability=index / 359,
            label="related" if index < 180 else "unrelated",
        )
        for index in range(360)
    )
    audit = tuple(
        AuditThresholdObservation(
            p_unrelated=index / 299,
            tourism_label="related" if index < 150 else "unrelated",
            component_id=f"audit-{index}",
        )
        for index in range(300)
    )
    points, summary = evaluate_threshold_grid(
        [0.05, 0.2, 0.5, 0.8, 0.97],
        wave,
        audit,
        keep_thresholds=(0.1, 0.2),
        exclude_thresholds=(0.8, 0.9),
        original_T_keep=0.2,
        original_T_exclude=0.8,
        minimum_raw_tail_count=1,
        maximum_auto_keep_unrelated_rate=0.05,
        maximum_auto_exclude_related_rate=0.02,
        confidence_level=0.95,
    )
    assert len(points) == 4
    assert summary["point_count"] == 4
    for point in points:
        assert (
            point["population_auto_keep_count"]
            + point["population_manual_review_count"]
            + point["population_auto_exclude_count"]
            == 5
        )


def test_audit_support_rejects_thresholds_expanding_original_tails() -> None:
    """最后300条不能假装覆盖原抽样尾部之外的新扩张区间。"""

    wave = tuple(
        _wave_row(
            index,
            probability=0.1 if index < 180 else 0.9,
            label="related" if index < 180 else "unrelated",
        )
        for index in range(360)
    )
    audit = tuple(
        AuditThresholdObservation(
            p_unrelated=0.1 if index < 150 else 0.9,
            tourism_label="related" if index < 150 else "unrelated",
            component_id=f"audit-{index}",
        )
        for index in range(300)
    )
    points, summary = evaluate_threshold_grid(
        [0.1, 0.4, 0.6, 0.9],
        wave,
        audit,
        keep_thresholds=(0.1, 0.3),
        exclude_thresholds=(0.7, 0.9),
        original_T_keep=0.2,
        original_T_exclude=0.8,
        minimum_raw_tail_count=30,
        maximum_auto_keep_unrelated_rate=0.05,
        maximum_auto_exclude_related_rate=0.02,
        confidence_level=0.95,
    )
    supported = [item for item in points if item["audit_both_tails_supported"]]
    assert len(supported) == 1
    assert supported[0]["T_keep"] == 0.1
    assert supported[0]["T_exclude"] == 0.9
    assert summary["recommendation"]["T_keep"] == 0.1
    assert summary["recommendation"]["T_exclude"] == 0.9
    assert summary["recommendation_is_policy_freeze"] is False


def test_uncertain_is_adverse_in_both_automatic_tails() -> None:
    """uncertain进入任一自动尾部时都按不利事件计算。"""

    wave = tuple(
        _wave_row(
            index,
            probability=0.1 if index < 180 else 0.9,
            label=(
                "uncertain"
                if index in {0, 180}
                else "related"
                if index < 180
                else "unrelated"
            ),
        )
        for index in range(360)
    )
    audit = tuple(
        AuditThresholdObservation(
            p_unrelated=0.1 if index < 150 else 0.9,
            tourism_label=(
                "uncertain"
                if index in {0, 150}
                else "related"
                if index < 150
                else "unrelated"
            ),
            component_id=f"audit-{index}",
        )
        for index in range(300)
    )
    points, _ = evaluate_threshold_grid(
        [0.1, 0.9],
        wave,
        audit,
        keep_thresholds=(0.2,),
        exclude_thresholds=(0.8,),
        original_T_keep=0.2,
        original_T_exclude=0.8,
        minimum_raw_tail_count=30,
        maximum_auto_keep_unrelated_rate=0.05,
        maximum_auto_exclude_related_rate=0.02,
        confidence_level=0.95,
    )
    assert points[0]["wave_b_auto_keep_adverse_count"] == 1
    assert points[0]["wave_b_auto_exclude_adverse_count"] == 1
    assert points[0]["audit_auto_keep_adverse_count"] == 1
    assert points[0]["audit_auto_exclude_adverse_count"] == 1
