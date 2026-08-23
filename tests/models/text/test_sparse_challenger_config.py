"""首轮 sparse challenger 预登记配置的契约测试。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.sparse_challenger_config import (
    SparseChallengerConfigError,
    load_sparse_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "configs/cleaning-text-challenger.yaml"


def _write_changed_config(tmp_path: Path, change) -> Path:
    """复制冻结配置并应用一个测试漂移。"""

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "changed.yaml"
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_frozen_plan_expands_exact_candidate_matrix() -> None:
    plan = load_sparse_challenger_plan(CONFIG_PATH)

    assert plan.plan_sha256 == (
        "ad515917c735ce77ed5231f0fa25bd536e8395115f22b9bf4e5035f4df199301"
    )
    assert plan.plan_id == plan.plan_sha256[:32]
    assert len(plan.candidates) == 54
    assert len({candidate.candidate_id for candidate in plan.candidates}) == 54
    assert Counter(candidate.family for candidate in plan.candidates) == {
        "tfidf_linear_svc": 18,
        "nbsvm": 18,
        "tfidf_logistic_regression": 18,
    }
    assert plan.baseline_spec.family == "baseline_anchor"
    assert plan.baseline_spec.ngram_range == (2, 5)
    assert plan.baseline_spec.C == 1.0
    assert plan.baseline_spec.probability == "group_oof_sigmoid"


@pytest.mark.parametrize(
    ("change", "reason_code"),
    [
        (
            lambda raw: raw.update({"unknown": True}),
            "sparse_challenger_config_invalid",
        ),
        (
            lambda raw: raw["families"]["nbsvm"].update({"alpha": 0.5}),
            "sparse_challenger_families_invalid",
        ),
        (
            lambda raw: raw["common"].update({"title_body_channels": True}),
            "sparse_challenger_plan_invalid",
        ),
        (
            lambda raw: raw["common"].update({"platform_used": True}),
            "sparse_challenger_plan_invalid",
        ),
        (
            lambda raw: raw["evaluation"].update({"outer_folds": 4}),
            "sparse_challenger_plan_invalid",
        ),
    ],
)
def test_frozen_plan_rejects_unregistered_changes(
    tmp_path: Path, change, reason_code: str
) -> None:
    path = _write_changed_config(tmp_path, change)

    with pytest.raises(SparseChallengerConfigError) as error:
        load_sparse_challenger_plan(path)

    assert error.value.reason_code == reason_code

