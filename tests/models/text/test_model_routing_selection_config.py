"""三段式双阈值选择分析计划的严格解析测试。"""

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_routing_selection_config import (
    ModelRoutingSelectionConfigError,
    load_model_routing_selection_plan,
)


CONFIG = Path("configs/cleaning-model-routing-selection.yaml")


def test_load_frozen_model_routing_selection_plan() -> None:
    """固定网格、证据绑定和禁止自动冻结边界必须完整加载。"""

    plan = load_model_routing_selection_plan(CONFIG)

    assert len(plan.keep_thresholds) == 16
    assert len(plan.exclude_thresholds) == 16
    assert len(plan.keep_thresholds) * len(plan.exclude_thresholds) == 256
    assert plan.completed_csv_sha256 == "dc77d464" + "8eac5d5fd9f4997915eeb399854ef5084b92170bc72809048faee09a"
    assert plan.minimum_raw_tail_count == 30
    assert plan.minimum_effective_tail_count == 20.0


def test_rejects_threshold_grid_drift(tmp_path: Path) -> None:
    """标签打开后不得通过配置漂移追加更有利的阈值。"""

    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["threshold_grid"]["keep"].append(0.21)
    path = tmp_path / "drift.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ModelRoutingSelectionConfigError) as error:
        load_model_routing_selection_plan(path)

    assert error.value.reason_code == "model_routing_selection_plan_invalid"
