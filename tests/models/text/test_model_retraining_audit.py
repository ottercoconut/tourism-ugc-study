"""150条双尾审计3/7门、4/8失败和单尾降级测试。"""

from __future__ import annotations

from dataclasses import replace

from tourism_ugc_study.models.text.model_retraining_audit import (
    assess_audit_labels,
)
from tourism_ugc_study.models.text.model_retraining_config import (
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_decisions import (
    resolve_final_action,
)


def _tail(action: str, adverse_count: int) -> list[dict[str, object]]:
    """构造150条按component成对的合成审计结果。"""

    return [
        {
            "provisional_action": action,
            "component_id": f"{action}-component-{index // 2}",
            "adverse": index < adverse_count,
        }
        for index in range(150)
    ]


def test_three_and_seven_events_pass() -> None:
    """排除端3条、保留端7条必须通过固定事件门。"""

    plan = replace(
        load_model_retraining_plan("configs/cleaning-model-retraining.yaml"),
        bootstrap_repetitions=30,
    )
    keep, exclude = assess_audit_labels(
        _tail("auto_keep", 7) + _tail("auto_exclude", 3), plan=plan
    )
    assert keep.passed is True
    assert exclude.passed is True
    assert keep.adverse_event_count == 7
    assert exclude.adverse_event_count == 3


def test_four_and_eight_events_fail() -> None:
    """排除端4条、保留端8条必须失败且不能被区间覆盖。"""

    plan = replace(
        load_model_retraining_plan("configs/cleaning-model-retraining.yaml"),
        bootstrap_repetitions=30,
    )
    keep, exclude = assess_audit_labels(
        _tail("auto_keep", 8) + _tail("auto_exclude", 4), plan=plan
    )
    assert keep.passed is False
    assert exclude.passed is False


def test_single_tail_failure_only_downgrades_that_action() -> None:
    """只通过保留端时，排除端转人工而保留端仍可自动使用。"""

    enabled = {"auto_keep"}
    assert resolve_final_action(
        "auto_keep", enabled_automatic_actions=enabled
    ) == ("keep", "model_audit_released")
    assert resolve_final_action(
        "auto_exclude", enabled_automatic_actions=enabled
    ) == ("manual_review", "model_tail_downgraded")
    assert resolve_final_action(
        "manual_review", enabled_automatic_actions=enabled
    ) == ("manual_review", "model_middle_band")


def test_audit_human_label_always_overrides_model_action() -> None:
    """即使尾部通过，审计成员仍必须按人工标签而不是模型动作决定。"""

    assert resolve_final_action(
        "auto_exclude",
        enabled_automatic_actions={"auto_exclude"},
        audited_label="related",
    ) == ("keep", "human_audit_label")
    assert resolve_final_action(
        "auto_keep",
        enabled_automatic_actions={"auto_keep"},
        audited_label="uncertain",
    ) == ("manual_review", "human_audit_label")
