"""冻结路由策略与 Wave B 抽样配置的严格解析测试。"""

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_routing_policy_config import (
    ModelRoutingPolicyConfigError,
    load_model_routing_policy_plan,
)


CONFIG = Path("configs/cleaning-model-routing-policy.yaml")


def test_load_frozen_wave_b_policy() -> None:
    """策略、等人工量 comparator 和九层分配必须完整加载。"""

    plan = load_model_routing_policy_plan(CONFIG)

    assert plan.selected.model == "qwen"
    assert (plan.selected.t_keep, plan.selected.t_exclude) == (0.14, 0.86)
    assert plan.comparator.model == "sparse"
    assert (plan.comparator.t_keep, plan.comparator.t_exclude) == (0.20, 0.80)
    assert len(plan.wave_b_strata) == 9
    assert sum(item.population_count for item in plan.wave_b_strata) == 9410
    assert sum(item.sample_count for item in plan.wave_b_strata) == 360


def test_rejects_post_decision_threshold_drift(tmp_path: Path) -> None:
    """研究者确认后不得通过配置静默改变 Qwen 阈值。"""

    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["selected_strategy"]["T_keep"] = 0.15
    path = tmp_path / "drift.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ModelRoutingPolicyConfigError) as error:
        load_model_routing_policy_plan(path)

    assert error.value.reason_code == "model_routing_policy_plan_invalid"


def test_rejects_wave_b_allocation_over_capacity(tmp_path: Path) -> None:
    """任何动作层都不得抽取超过已冻结的人口容量。"""

    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["wave_b"]["strata"]["qwen_auto_keep__sparse_auto_exclude"][
        "sample_count"
    ] = 26
    path = tmp_path / "overflow.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ModelRoutingPolicyConfigError) as error:
        load_model_routing_policy_plan(path)

    assert error.value.reason_code == "model_routing_policy_strata_invalid"
