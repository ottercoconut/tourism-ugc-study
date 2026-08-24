"""Qwen 英文 head-tail 第三层冻结配置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.qwen_head_tail_config import (
    QwenHeadTailConfigError,
    load_qwen_head_tail_plan,
)


ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "configs/cleaning-qwen-english-head-tail.yaml"


def test_head_tail_plan_freezes_english_two_view_contract() -> None:
    plan = load_qwen_head_tail_plan(PLAN_PATH)

    assert plan.projection == "head_tail_two_view"
    assert plan.aggregation == "arithmetic_mean_then_l2_normalize"
    assert plan.max_length == 2048
    assert plan.instruction.startswith("Classify whether")
    assert plan.layer2_run_id == "eba8568816309688f1f85e6092a57b1d"
    assert plan.outer_folds == 5
    assert plan.inner_folds == 4


def test_head_tail_plan_rejects_instruction_drift(tmp_path: Path) -> None:
    raw = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    raw["encoder_change"]["instruction"] += " Changed."
    changed = tmp_path / "changed.yaml"
    changed.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(QwenHeadTailConfigError) as error:
        load_qwen_head_tail_plan(changed)

    assert error.value.reason_code == "qwen_head_tail_plan_invalid"
