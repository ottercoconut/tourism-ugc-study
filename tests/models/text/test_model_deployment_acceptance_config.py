"""锁定测试判读与部署双尾审计计划的配置测试。"""

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_deployment_acceptance_config import (
    ModelDeploymentAcceptanceConfigError,
    load_model_deployment_acceptance_plan,
)


CONFIG = Path("configs/cleaning-model-deployment-acceptance.yaml")


def test_loads_frozen_locked_test_and_two_tail_audit() -> None:
    """冻结计划必须绑定唯一测试、零事件门和每尾150条审计。"""

    plan = load_model_deployment_acceptance_plan(CONFIG)

    assert plan.test_count == 148
    assert plan.locked_test.access_count == 1
    assert (plan.locked_test.t_keep, plan.locked_test.t_exclude) == (0.14, 0.86)
    assert plan.locked_test.maximum_auto_keep_unrelated_events == 0
    assert plan.locked_test.maximum_auto_exclude_related_events == 0
    assert plan.audit.sample_count_per_tail == 150
    assert tuple(tail.action for tail in plan.audit.tails) == (
        "auto_keep",
        "auto_exclude",
    )
    assert plan.audit.zero_event_upper_at_full_sample < 0.02


def test_rejects_locked_test_threshold_drift(tmp_path: Path) -> None:
    """测试开启前后都不得通过配置改写已确认阈值。"""

    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["locked_test"]["T_exclude"] = 0.85
    path = tmp_path / "drift.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ModelDeploymentAcceptanceConfigError) as error:
        load_model_deployment_acceptance_plan(path)

    assert error.value.reason_code == "model_deployment_acceptance_plan_invalid"


def test_rejects_one_event_audit_dilution(tmp_path: Path) -> None:
    """不得允许用补抽样把已经出现的审计事件稀释成通过。"""

    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["deployment_audit"]["tails"]["auto_keep"][
        "maximum_adverse_events"
    ] = 1
    path = tmp_path / "dilution.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ModelDeploymentAcceptanceConfigError) as error:
        load_model_deployment_acceptance_plan(path)

    assert error.value.reason_code == "model_deployment_audit_tail_invalid"
