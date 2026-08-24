"""双模型新标签评价的人口配对与概率抽样测试。"""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tourism_ugc_study.models.text.model_reliability_config import (
    load_model_reliability_plan,
)
from tourism_ugc_study.models.text.model_reliability_study import (
    EligibleEvaluationMember,
    ModelReliabilityStudyError,
    ScoredEvaluationMember,
    _redistributed_allocations,
    pair_model_scores,
    sample_wave_a,
    wave_a_stratum,
)


PLAN = load_model_reliability_plan(
    Path("configs/cleaning-model-reliability-study.yaml")
)


def _scored(index: int, sparse: float, qwen: float) -> ScoredEvaluationMember:
    return ScoredEvaluationMember(
        source_post_id=index + 1,
        source_version=1,
        component_id=f"component-{index // 2}",
        normalized_sha256=f"{index:064x}",
        sparse_p_unrelated=sparse,
        qwen_p_unrelated=qwen,
    )


@pytest.mark.parametrize(
    ("sparse", "qwen", "expected"),
    [
        (0.95, 0.91, "both_p_ge_0_90"),
        (0.89, 0.91, "qwen_only_p_ge_0_90"),
        (0.91, 0.89, "sparse_only_p_ge_0_90"),
        (0.80, 0.20, "action_disagreement_at_0_50"),
        (0.80, 0.70, "both_p_ge_0_50_remainder"),
        (0.20, 0.30, "both_p_lt_0_50"),
    ],
)
def test_wave_a_strata_are_mutually_exclusive(
    sparse: float, qwen: float, expected: str
) -> None:
    assert wave_a_stratum(_scored(0, sparse, qwen)) == expected


def test_pair_model_scores_rejects_nonfinite_probability() -> None:
    members = (
        EligibleEvaluationMember(
            1, 1, "component", "synthetic", "text", "0" * 64
        ),
    )

    with pytest.raises(ModelReliabilityStudyError) as error:
        pair_model_scores(members, [np.nan], [0.5])

    assert error.value.reason_code == "model_reliability_prediction_invalid"


def test_short_stratum_is_census_and_budget_is_redistributed() -> None:
    counts = {
        "both_p_ge_0_90": 5,
        "qwen_only_p_ge_0_90": 100,
        "sparse_only_p_ge_0_90": 100,
        "action_disagreement_at_0_50": 100,
        "both_p_ge_0_50_remainder": 100,
        "both_p_lt_0_50": 100,
    }
    result = _redistributed_allocations(counts, PLAN.wave_a_allocations)

    assert result["both_p_ge_0_90"] == 5
    assert sum(result.values()) == 240
    assert all(result[key] <= counts[key] for key in result)


def test_wave_a_sample_is_reproducible_and_has_design_weights() -> None:
    patterns = [
        (0.95, 0.95),
        (0.20, 0.95),
        (0.95, 0.20),
        (0.80, 0.20),
        (0.80, 0.70),
        (0.20, 0.30),
    ]
    scored = tuple(
        _scored(index, *patterns[index % len(patterns)])
        for index in range(600)
    )
    test_plan = replace(PLAN, expected_eligible_count=len(scored))

    first = sample_wave_a(scored, plan=test_plan)
    second = sample_wave_a(tuple(reversed(scored)), plan=test_plan)

    assert first == second
    assert len(first) == 240
    assert all(item.analysis_weight == 1 / item.inclusion_probability for item in first)
    assert all(item.stratum_population_count == 100 for item in first)
