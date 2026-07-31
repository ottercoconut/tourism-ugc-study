"""两层图片保留集审计、Wilson 上限和失败门测试。"""

from __future__ import annotations

import pytest

from tourism_ugc_study.cleaning.image_keep_audit import (
    AuditObservation,
    AuditPopulationItem,
    build_keep_audit_sample,
    evaluate_keep_audit,
    wilson_one_sided_upper,
)


def _population(platform_sizes: dict[str, int]):
    items = []
    index = 1
    for platform, size in platform_sizes.items():
        for _ in range(size):
            items.append(AuditPopulationItem(f"fp-{index:04d}", platform))
            index += 1
    return items


def test_primary_is_equal_probability_and_supplement_fills_small_platforms() -> None:
    population = _population({"large": 230, "small": 20, "tiny": 10})
    plan = build_keep_audit_sample(
        population,
        seed=20260728,
        primary_size=200,
        platform_supplement_min=30,
    )
    assert len(plan.primary_members) == 200
    assert {round(item.inclusion_probability, 12) for item in plan.primary_members} == {
        round(200 / 260, 12)
    }
    observed = {"small": 0, "tiny": 0}
    for item in (*plan.primary_members, *plan.supplement_members):
        if item.platform_key in observed:
            observed[item.platform_key] += 1
    assert observed == {"small": 20, "tiny": 10}
    assert all(item.sampling_layer == "platform_supplement" for item in plan.supplement_members)
    assert plan.interval_method == "wilson_one_sided_95"


def test_three_platform_population_300_has_primary_200_plus_supplement_20() -> None:
    """冻结 300 人口/3 平台夹具，回归 200 主样本＋20 平台补充设计。"""

    plan = build_keep_audit_sample(
        _population({"large": 240, "small": 30, "tiny": 30}),
        seed=5,
        primary_size=200,
        platform_supplement_min=30,
    )
    assert plan.population_count == 300
    assert len(plan.primary_members) == 200
    assert len(plan.supplement_members) == 20
    observed = {"small": 0, "tiny": 0}
    for member in (*plan.primary_members, *plan.supplement_members):
        if member.platform_key in observed:
            observed[member.platform_key] += 1
    assert observed == {"small": 30, "tiny": 30}
    assert plan.interval_method == "wilson_one_sided_95"


def test_census_and_nonoverlapping_rounds() -> None:
    population = _population({"only": 100})
    first = build_keep_audit_sample(
        population, seed=1, primary_size=200, platform_supplement_min=30
    )
    assert first.interval_method == "census"
    assert len(first.primary_members) == 100
    with pytest.raises(ValueError):
        build_keep_audit_sample(
            population,
            seed=2,
            primary_size=200,
            platform_supplement_min=30,
            excluded_fingerprint_ids={item.fingerprint_id for item in first.primary_members},
        )


def test_census_uses_current_population_not_irrelevant_historical_exclusions() -> None:
    """旧轮身份已完全离开当前人口时，新轮完整主样本仍是合法 census。"""

    current = _population({"replacement": 20})
    replaced_ids = {f"old-{index:04d}" for index in range(20)}
    census = build_keep_audit_sample(
        current,
        seed=2,
        primary_size=200,
        platform_supplement_min=30,
        excluded_fingerprint_ids=replaced_ids,
    )
    assert census.interval_method == "census"
    assert len(census.primary_members) == len(current)
    assert not census.supplement_members
    assert all(
        member.inclusion_probability == member.sampling_weight == 1.0
        for member in census.primary_members
    )

    # 一旦历史样本仍与当前人口相交，该成员不得被复抽，主样本就没有全查当前
    # 人口；即使剩余人口小于 200，也必须使用抽样轮身份。
    overlapping = build_keep_audit_sample(
        current,
        seed=2,
        primary_size=200,
        platform_supplement_min=30,
        excluded_fingerprint_ids={current[0].fingerprint_id},
    )
    assert overlapping.interval_method == "wilson_one_sided_95"
    assert len(overlapping.primary_members) == len(current) - 1


def test_wilson_examples_match_protocol_and_one_event_fails() -> None:
    assert wilson_one_sided_upper(0, 200) == pytest.approx(0.013347, abs=1e-6)
    assert wilson_one_sided_upper(1, 200) == pytest.approx(0.022098, abs=1e-6)
    # 人口必须大于主样本上限，才能验证抽样轮的 Wilson 而不是 census。
    population = _population({"one": 201})
    plan = build_keep_audit_sample(
        population, seed=3, primary_size=200, platform_supplement_min=30
    )
    observations = [
        AuditObservation(item.fingerprint_id, item.sampling_layer, "valid_content")
        for item in plan.primary_members
    ]
    observations[0] = AuditObservation(
        observations[0].fingerprint_id, "primary", "placeholder_or_error"
    )
    result = evaluate_keep_audit(
        plan,
        observations,
        residual_noise_rate_max=0.02,
        confidence_level=0.95,
    )
    assert result.evaluation_status == "failed"
    assert result.primary_event_count == 1


def test_supplement_noise_and_uncertain_fail_without_entering_primary_estimate() -> None:
    plan = build_keep_audit_sample(
        _population({"large": 230, "small": 5}),
        seed=4,
        primary_size=200,
        platform_supplement_min=30,
    )
    observations = [
        AuditObservation(item.fingerprint_id, item.sampling_layer, "valid_content")
        for item in (*plan.primary_members, *plan.supplement_members)
    ]
    if plan.supplement_members:
        target = plan.supplement_members[0]
        for index, observation in enumerate(observations):
            if observation.fingerprint_id == target.fingerprint_id:
                observations[index] = AuditObservation(
                    target.fingerprint_id, target.sampling_layer, "uncertain"
                )
                break
    result = evaluate_keep_audit(
        plan,
        observations,
        residual_noise_rate_max=0.02,
        confidence_level=0.95,
    )
    assert result.primary_event_count == 0
    assert result.supplement_event_count == 1
    assert result.primary_point_estimate == 0.0
    assert result.evaluation_status == "failed"


def test_incomplete_audit_cannot_pass() -> None:
    plan = build_keep_audit_sample(
        _population({"one": 10}), seed=5, primary_size=200, platform_supplement_min=30
    )
    result = evaluate_keep_audit(
        plan,
        [],
        residual_noise_rate_max=0.02,
        confidence_level=0.95,
    )
    assert result.evaluation_status == "incomplete"
    assert result.one_sided_upper is None


def test_census_uses_observed_rate_instead_of_zero_event_rule() -> None:
    """全查人口允许阈值内事件率，超过 2% 才失败。"""

    plan = build_keep_audit_sample(
        _population({"only": 100}),
        seed=6,
        primary_size=200,
        platform_supplement_min=30,
    )
    observations = [
        AuditObservation(item.fingerprint_id, item.sampling_layer, "valid_content")
        for item in plan.primary_members
    ]
    observations[0] = AuditObservation(
        observations[0].fingerprint_id, "primary", "site_ui"
    )
    accepted = evaluate_keep_audit(
        plan,
        observations,
        residual_noise_rate_max=0.02,
        confidence_level=0.95,
    )
    assert accepted.primary_event_count == 1
    assert accepted.primary_point_estimate == accepted.one_sided_upper == 0.01
    assert accepted.evaluation_status == "passed"

    for index in (1, 2):
        observations[index] = AuditObservation(
            observations[index].fingerprint_id,
            "primary",
            "placeholder_or_error",
        )
    rejected = evaluate_keep_audit(
        plan,
        observations,
        residual_noise_rate_max=0.02,
        confidence_level=0.95,
    )
    assert rejected.primary_point_estimate == 0.03
    assert rejected.evaluation_status == "failed"
