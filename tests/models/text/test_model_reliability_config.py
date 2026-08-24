"""新盲标模型评价计划的冻结边界测试。"""

from pathlib import Path

import pytest

from tourism_ugc_study.models.text.model_reliability_config import (
    ModelReliabilityConfigError,
    load_model_reliability_plan,
)


CONFIG = Path("configs/cleaning-model-reliability-study.yaml")


def test_plan_binds_evaluation_only_models_and_sample_budget() -> None:
    """计划必须固定两个旧模型，并禁止新标签提前进入 fit。"""

    plan = load_model_reliability_plan(CONFIG)

    assert plan.wave_a_sample_size == 240
    assert plan.wave_b_maximum_sample_size == 360
    assert sum(plan.wave_a_allocations.values()) == 240
    assert sum(plan.wave_b_allocations.values()) == 360
    assert plan.sparse.model_id == "1b68baa8bef99d6b9d75b7bf3226cfb4"
    assert plan.qwen.model_id == "e24fc7a65a5e0e52f383725e21607837"


def test_plan_rejects_training_before_evaluation_seal(tmp_path: Path) -> None:
    """任何把 evaluation-only 改成可训练的配置都必须失败关闭。"""

    changed = CONFIG.read_text(encoding="utf-8").replace(
        "labels_may_enter_fit_before_sealed_evaluation: false",
        "labels_may_enter_fit_before_sealed_evaluation: true",
    )
    path = tmp_path / "changed.yaml"
    path.write_text(changed, encoding="utf-8")

    with pytest.raises(ModelReliabilityConfigError) as error:
        load_model_reliability_plan(path)

    assert error.value.reason_code == "model_reliability_plan_invalid"
