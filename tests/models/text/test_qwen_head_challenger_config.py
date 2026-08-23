"""Qwen 缓存向量分类头 challenger 冻结配置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    QwenHeadChallengerConfigError,
    load_qwen_head_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "configs/cleaning-qwen-head-challenger.yaml"


def test_frozen_plan_expands_exactly_fifty_six_candidates() -> None:
    plan = load_qwen_head_challenger_plan(PLAN_PATH)

    assert len(plan.candidates) == 56
    assert len({item.candidate_id for item in plan.candidates}) == 56
    assert {item.embedding_dimension for item in plan.candidates} == {
        256,
        512,
        1024,
        2560,
    }
    anchor = next(
        item
        for item in plan.candidates
        if item.candidate_id == plan.safety_anchor_candidate_id
    )
    assert anchor.family == "logistic_regression"
    assert anchor.embedding_dimension == 2560
    assert anchor.C == 1.0
    assert anchor.class_weight == "balanced"
    assert plan.outer_folds == 5
    assert plan.inner_folds == 4


def test_plan_rejects_unregistered_dimension(tmp_path: Path) -> None:
    raw = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    raw["search"]["mrl_dimensions"].append(128)
    changed = tmp_path / "changed.yaml"
    changed.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(QwenHeadChallengerConfigError) as error:
        load_qwen_head_challenger_plan(changed)

    assert error.value.reason_code == "qwen_head_challenger_plan_invalid"
