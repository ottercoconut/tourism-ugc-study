"""Wave B 三段动作交叉分层与概率抽样测试。"""

from dataclasses import replace
from pathlib import Path

import pytest

from tourism_ugc_study.models.text.model_reliability_study import (
    ScoredEvaluationMember,
)
from tourism_ugc_study.models.text.model_routing_policy_config import (
    WaveBStratumPlan,
    load_model_routing_policy_plan,
)
from tourism_ugc_study.models.text.model_wave_b_study import (
    ModelWaveBStudyError,
    routing_action,
    sample_wave_b,
    wave_b_stratum,
)


PLAN = load_model_routing_policy_plan(
    Path("configs/cleaning-model-routing-policy.yaml")
)


@pytest.mark.parametrize(
    ("probability", "expected"),
    [
        (0.0, "auto_keep"),
        (0.14, "auto_keep"),
        (0.1400001, "manual_review"),
        (0.8599999, "manual_review"),
        (0.86, "auto_exclude"),
        (1.0, "auto_exclude"),
    ],
)
def test_selected_routing_preserves_boundary_equals(
    probability: float, expected: str
) -> None:
    """低端等号属于保留，高端等号属于排除。"""

    assert routing_action(probability, PLAN.selected) == expected


def test_rejects_nonfinite_probability() -> None:
    """NaN 不得静默落入人工中间层。"""

    with pytest.raises(ModelWaveBStudyError) as error:
        routing_action(float("nan"), PLAN.selected)

    assert error.value.reason_code == "model_wave_b_probability_invalid"


def _small_plan():
    """构造每层5条、抽2条的完整九层合成策略。"""

    strata = tuple(
        WaveBStratumPlan(
            name=item.name,
            selected_action=item.selected_action,
            comparator_action=item.comparator_action,
            population_count=5,
            sample_count=2,
        )
        for item in PLAN.wave_b_strata
    )
    return replace(
        PLAN,
        selected=replace(PLAN.selected, population_count=54),
        comparator=replace(PLAN.comparator, population_count=54),
        wave_b_target_sample_count=18,
        wave_b_eligible_population_count=45,
        wave_b_eligible_component_count=45,
        wave_b_strata=strata,
    )


def _synthetic_population():
    """生成9条排除成员和九个动作层各5条。"""

    records: list[ScoredEvaluationMember] = []
    for index in range(9):
        records.append(
            ScoredEvaluationMember(
                source_post_id=index + 1,
                source_version=1,
                component_id=f"excluded-{index}",
                normalized_sha256=f"{index:064x}",
                sparse_p_unrelated=0.5,
                qwen_p_unrelated=0.5,
            )
        )
    action_probability = {
        "auto_keep": 0.1,
        "manual_review": 0.5,
        "auto_exclude": 0.9,
    }
    index = 9
    for selected_action in action_probability:
        for comparator_action in action_probability:
            for _repeat in range(5):
                records.append(
                    ScoredEvaluationMember(
                        source_post_id=index + 1,
                        source_version=1,
                        component_id=f"eligible-{index}",
                        normalized_sha256=f"{index:064x}",
                        sparse_p_unrelated=action_probability[comparator_action],
                        qwen_p_unrelated=action_probability[selected_action],
                    )
                )
                index += 1
    return tuple(records)


def test_wave_b_sample_is_reproducible_and_excludes_wave_a_components() -> None:
    """输入顺序变化不能改变样本，且每层概率与权重必须正确。"""

    plan = _small_plan()
    population = _synthetic_population()
    excluded = frozenset(f"excluded-{index}" for index in range(9))

    first = sample_wave_b(
        population, excluded_wave_a_components=excluded, plan=plan
    )
    second = sample_wave_b(
        tuple(reversed(population)),
        excluded_wave_a_components=excluded,
        plan=plan,
    )

    assert first == second
    assert len(first) == 18
    assert len({item.stratum for item in first}) == 9
    assert all(item.inclusion_probability == 0.4 for item in first)
    assert all(item.analysis_weight == 2.5 for item in first)
    assert all(item.component_id not in excluded for item in first)
    assert all(
        wave_b_stratum(
            ScoredEvaluationMember(
                source_post_id=item.source_post_id,
                source_version=item.source_version,
                component_id=item.component_id,
                normalized_sha256=item.normalized_sha256,
                sparse_p_unrelated=item.sparse_p_unrelated,
                qwen_p_unrelated=item.qwen_p_unrelated,
            ),
            plan=plan,
        )
        == item.stratum
        for item in first
    )
