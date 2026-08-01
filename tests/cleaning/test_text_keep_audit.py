"""文本保留集平台比例抽样、双轴事件和质量门禁测试。"""

from __future__ import annotations

from collections import Counter

import pytest

from tourism_ugc_study.cleaning.text_keep_audit import (
    TextKeepObservation,
    TextKeepPopulationItem,
    build_text_keep_audit_sample,
    evaluate_text_keep_audit,
    population_manifest_sha256,
)


def _population(platform_sizes: dict[str, int]) -> list[TextKeepPopulationItem]:
    """按给定平台规模创建无内容、无作者信息的帖子版本人口。"""

    items: list[TextKeepPopulationItem] = []
    source_post_id = 1
    for platform, size in platform_sizes.items():
        for _ in range(size):
            items.append(TextKeepPopulationItem(source_post_id, 1, platform))
            source_post_id += 1
    return items


def _observations(plan, event_labels=()):
    """为冻结计划创建完整观察，并按顺序覆盖少量事件标签。"""

    observations = [
        TextKeepObservation(member.source_post_id, member.source_version, "usable", "related")
        for member in plan.members
    ]
    for index, (structure, tourism) in enumerate(event_labels):
        member = plan.members[index]
        observations[index] = TextKeepObservation(
            member.source_post_id,
            member.source_version,
            structure,
            tourism,
        )
    return observations


def test_platform_proportional_sample_has_300_self_weighting_members() -> None:
    """大人口按平台比例冻结 300 条，且总体一阶概率与权重完全相同。"""

    population = _population({"xhs": 500, "douyin": 300, "bilibili": 200})
    plan = build_text_keep_audit_sample(population, seed=20260801)
    assert len(plan.members) == 300
    assert plan.interval_method == "wilson_one_sided_95"
    assert Counter(member.platform_key for member in plan.members) == {
        "xhs": 150,
        "douyin": 90,
        "bilibili": 60,
    }
    assert {member.inclusion_probability for member in plan.members} == {0.3}
    assert all(member.sampling_weight == pytest.approx(10 / 3) for member in plan.members)

    result = evaluate_text_keep_audit(plan, _observations(plan))
    assert result.evaluation_status == "passed"
    assert result.ht_point_estimate == 0.0
    assert result.one_sided_upper is not None
    assert [item.platform_key for item in result.platform_slices] == [
        "bilibili",
        "douyin",
        "xhs",
    ]


def test_population_below_300_is_a_census() -> None:
    """人口不足 300 时全查，概率/权重为 1，census 上限等于点估计。"""

    plan = build_text_keep_audit_sample(
        _population({"xhs": 120, "douyin": 79}),
        seed=9,
    )
    assert len(plan.members) == plan.source_population_count == 199
    assert plan.interval_method == "census"
    assert all(
        member.inclusion_probability == member.sampling_weight == 1.0
        for member in plan.members
    )
    result = evaluate_text_keep_audit(
        plan,
        _observations(plan, [("invalid", "not_applicable")]),
    )
    assert result.ht_point_estimate == result.one_sided_upper == pytest.approx(1 / 199)
    assert result.evaluation_status == "passed"


def test_thresholds_are_strictly_greater_than_three_and_five_percent() -> None:
    """census 恰好 3% 可通过，超过 3% 才触发点估计失败。"""

    plan = build_text_keep_audit_sample(_population({"xhs": 100}), seed=10)
    at_boundary = evaluate_text_keep_audit(
        plan,
        _observations(plan, [("usable", "unrelated")] * 3),
    )
    assert at_boundary.ht_point_estimate == at_boundary.one_sided_upper == 0.03
    assert at_boundary.evaluation_status == "passed"

    above_boundary = evaluate_text_keep_audit(
        plan,
        _observations(plan, [("usable", "unrelated")] * 4),
    )
    assert above_boundary.evaluation_status == "failed"
    assert above_boundary.failure_reason_codes == (
        "text_keep_audit_point_estimate_above_maximum",
    )


def test_wilson_upper_can_fail_while_point_estimate_is_at_most_three_percent() -> None:
    """300 条中 9 个事件的点估计为 3%，但单侧上限超过 5%。"""

    plan = build_text_keep_audit_sample(_population({"xhs": 1000}), seed=11)
    result = evaluate_text_keep_audit(
        plan,
        _observations(plan, [("usable", "unrelated")] * 9),
    )
    assert result.ht_point_estimate == pytest.approx(0.03)
    assert result.one_sided_upper is not None and result.one_sided_upper > 0.05
    assert result.failure_reason_codes == (
        "text_keep_audit_confidence_upper_above_maximum",
    )


def test_same_population_manifest_is_rejected_and_old_members_are_not_resampled() -> None:
    """同一人口不得换 seed 重抽；变化后人口也不得复抽仍存在的旧成员。"""

    first_population = _population({"xhs": 600})
    first = build_text_keep_audit_sample(first_population, seed=12)
    with pytest.raises(ValueError, match="already audited"):
        build_text_keep_audit_sample(
            list(reversed(first_population)),
            seed=13,
            previous_population_manifest_sha256s={first.population_manifest_sha256},
        )

    changed_population = first_population + [TextKeepPopulationItem(601, 1, "xhs")]
    old_keys = {member.member_key for member in first.members}
    replacement = build_text_keep_audit_sample(
        changed_population,
        seed=13,
        previous_member_keys=old_keys,
    )
    assert not old_keys.intersection(member.member_key for member in replacement.members)
    assert replacement.interval_method == "not_applicable_historical_members_excluded"
    blocked = evaluate_text_keep_audit(replacement, _observations(replacement))
    assert blocked.evaluation_status == "failed"
    assert blocked.failure_reason_codes == (
        "text_keep_audit_historical_evidence_not_combined",
        "text_keep_audit_confidence_upper_not_available",
    )


@pytest.mark.parametrize(
    ("structure", "tourism"),
    [
        ("invalid", "not_applicable"),
        ("uncertain", "related"),
        ("usable", "unrelated"),
        ("usable", "uncertain"),
    ],
)
def test_all_protocol_error_states_count_as_events(structure: str, tourism: str) -> None:
    """结构无效/不确定及旅游不相关/不确定均计为保留集事件。"""

    plan = build_text_keep_audit_sample(_population({"xhs": 20}), seed=14)
    result = evaluate_text_keep_audit(
        plan,
        _observations(plan, [(structure, tourism)]),
    )
    assert result.event_count == 1


@pytest.mark.parametrize(
    ("structure", "tourism"),
    [
        ("invalid", "related"),
        ("usable", "not_applicable"),
        ("uncertain", "not_applicable"),
    ],
)
def test_dual_axis_applicability_is_enforced(structure: str, tourism: str) -> None:
    """invalid 与 not_applicable 必须双向对应，非法证据不能进入统计。"""

    plan = build_text_keep_audit_sample(_population({"xhs": 20}), seed=15)
    member = plan.members[0]
    with pytest.raises(ValueError, match="applicability"):
        evaluate_text_keep_audit(
            plan,
            [
                TextKeepObservation(
                    item.source_post_id,
                    item.source_version,
                    structure if item == member else "usable",
                    tourism if item == member else "related",
                )
                for item in plan.members
            ],
        )


def test_sampling_and_manifests_are_deterministic_and_order_independent() -> None:
    """相同人口、seed 与目标量在输入乱序后仍得到同一成员和 manifest。"""

    population = _population({"xhs": 501, "douyin": 302, "bilibili": 204})
    left = build_text_keep_audit_sample(population, seed=20260729)
    right = build_text_keep_audit_sample(list(reversed(population)), seed=20260729)
    assert left == right
    assert population_manifest_sha256(population) == population_manifest_sha256(
        list(reversed(population))
    )
    assert len(left.population_manifest_sha256) == len(left.sample_manifest_sha256) == 64


def test_incomplete_observations_cannot_produce_a_point_estimate() -> None:
    """未完成的冻结样本只报告完成度与平台切片，不产生可验收总体统计。"""

    plan = build_text_keep_audit_sample(_population({"xhs": 350}), seed=16)
    result = evaluate_text_keep_audit(plan, _observations(plan)[:-1])
    assert result.evaluation_status == "incomplete"
    assert result.ht_point_estimate is result.one_sided_upper is None
    assert result.failure_reason_codes == ("text_keep_audit_annotations_incomplete",)
