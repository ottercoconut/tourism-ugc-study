from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_retraining_config import (
    ModelRetrainingConfigError,
    load_model_retraining_plan,
)


PLAN = Path("configs/cleaning-model-retraining.yaml")


def test_loads_frozen_retraining_plan() -> None:
    plan = load_model_retraining_plan(PLAN)

    assert plan.expected_training_count == 1300
    assert plan.expected_related_count == 585
    assert plan.expected_unrelated_count == 715
    assert [item.name for item in plan.candidates] == [
        "qwen_linear_svc",
        "sparse_linear_svc",
        "logit_fusion",
    ]
    assert len(plan.keep_thresholds) == 49
    assert len(plan.exclude_thresholds) == 49
    assert plan.audit_sample_count_per_tail == 150
    assert plan.audit_auto_exclude_maximum_adverse_events == 3
    assert plan.audit_auto_keep_maximum_adverse_events == 7


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["researcher_decision"].__setitem__(
            "old_outcome_may_be_rewritten", True
        ),
        lambda raw: raw["candidates"].__setitem__(
            "model_family_expansion_allowed", True
        ),
        lambda raw: raw["guards"].__setitem__(
            "old_locked_test_may_reopen", True
        ),
        lambda raw: raw["routing"].__setitem__(
            "maximum_auto_exclude_related_rate", 0.03
        ),
        lambda raw: raw["audit"].__setitem__(
            "replacement_or_dilution_after_failure_allowed", True
        ),
    ],
)
def test_rejects_protocol_drift(tmp_path: Path, mutate) -> None:
    raw = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    mutate(raw)
    changed = tmp_path / "changed.yaml"
    changed.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ModelRetrainingConfigError):
        load_model_retraining_plan(changed)
