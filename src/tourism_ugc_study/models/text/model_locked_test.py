"""冻结 Qwen 双阈值在唯一锁定测试上的纯评价与判读。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from .formal_baseline import evaluate_binary_probabilities
from .model_deployment_acceptance_config import ModelDeploymentAcceptancePlan


class ModelLockedTestError(RuntimeError):
    """锁定测试观察或判读违反冻结契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露测试成员、标签、正文或概率的稳定失败码。"""

        super().__init__("formal locked test evaluation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class LockedTestObservation:
    """一个去标识测试成员的冻结标签与 Qwen 概率。"""

    member_key: str
    component_id: str
    tourism_label: str
    p_unrelated: float


def routing_action(probability: float, *, t_keep: float, t_exclude: float) -> str:
    """按冻结闭区间边界把一个概率路由到三个动作之一。

    Args:
        probability: 有限的无关概率。
        t_keep: 自动保留上界，包含边界。
        t_exclude: 自动排除下界，包含边界。

    Returns:
        ``auto_keep``、``manual_review`` 或 ``auto_exclude``。

    Raises:
        ModelLockedTestError: 概率或阈值无效。
    """

    if (
        not np.isfinite(probability)
        or not 0.0 <= probability <= 1.0
        or not 0.0 <= t_keep < t_exclude <= 1.0
    ):
        raise ModelLockedTestError("model_locked_test_probability_invalid")
    if probability <= t_keep:
        return "auto_keep"
    if probability >= t_exclude:
        return "auto_exclude"
    return "manual_review"


def evaluate_locked_test(
    observations: Sequence[LockedTestObservation],
    *,
    plan: ModelDeploymentAcceptancePlan,
) -> Mapping[str, Any]:
    """一次性评价固定 Qwen 概率并应用预冻结方向性错误门。

    Args:
        observations: 与148条测试成员一一对应的标签、分量和概率。
        plan: 测试开启前封存的最终判读与审计计划。

    Returns:
        三段动作、总体诊断、验收状态和后续授权边界。

    Raises:
        ModelLockedTestError: 数量、身份、标签、概率或分量发生漂移。

    Notes:
        0.5总体指标仅作完整披露；正式门只使用两个自动尾部的支持数和
        方向性错误数。函数不含任何拟合、调参或模型比较。
    """

    items = tuple(observations)
    if len(items) != plan.test_count:
        raise ModelLockedTestError("model_locked_test_count_invalid")
    member_keys = [item.member_key for item in items]
    if (
        len(set(member_keys)) != len(member_keys)
        or any(
            not isinstance(item.member_key, str)
            or len(item.member_key) != 64
            or any(character not in "0123456789abcdef" for character in item.member_key)
            or not isinstance(item.component_id, str)
            or not item.component_id
            or item.tourism_label not in {"related", "unrelated"}
            or not np.isfinite(item.p_unrelated)
            or not 0.0 <= item.p_unrelated <= 1.0
            for item in items
        )
    ):
        raise ModelLockedTestError("model_locked_test_observation_invalid")
    actions = tuple(
        routing_action(
            float(item.p_unrelated),
            t_keep=plan.locked_test.t_keep,
            t_exclude=plan.locked_test.t_exclude,
        )
        for item in items
    )
    auto_keep_count = sum(action == "auto_keep" for action in actions)
    manual_review_count = sum(action == "manual_review" for action in actions)
    auto_exclude_count = sum(action == "auto_exclude" for action in actions)
    auto_keep_unrelated_events = sum(
        action == "auto_keep" and item.tourism_label == "unrelated"
        for item, action in zip(items, actions, strict=True)
    )
    auto_exclude_related_events = sum(
        action == "auto_exclude" and item.tourism_label == "related"
        for item, action in zip(items, actions, strict=True)
    )
    error_gate_passed = (
        auto_keep_unrelated_events
        <= plan.locked_test.maximum_auto_keep_unrelated_events
        and auto_exclude_related_events
        <= plan.locked_test.maximum_auto_exclude_related_events
    )
    support_gate_passed = (
        auto_keep_count >= plan.locked_test.minimum_auto_keep_support
        and auto_exclude_count >= plan.locked_test.minimum_auto_exclude_support
    )
    if not error_gate_passed:
        status = plan.locked_test.error_failure_status
        deployment_status = "MANUAL_ONLY"
    elif not support_gate_passed:
        status = plan.locked_test.support_failure_status
        deployment_status = "MANUAL_ONLY"
    else:
        status = plan.locked_test.pass_status
        deployment_status = "PROVISIONAL_ROUTING_AUTHORIZED"
    labels = [item.tourism_label for item in items]
    probabilities = [float(item.p_unrelated) for item in items]
    overall_metrics = dict(evaluate_binary_probabilities(labels, probabilities))
    return {
        "artifact_kind": "formal-cleaning-model-locked-test-report",
        "status": status,
        "test_count": len(items),
        "test_access_count": 1,
        "model": "qwen",
        "model_id": plan.qwen_model_id,
        "t_keep": plan.locked_test.t_keep,
        "t_exclude": plan.locked_test.t_exclude,
        "routing": {
            "auto_keep_count": auto_keep_count,
            "manual_review_count": manual_review_count,
            "auto_exclude_count": auto_exclude_count,
            "automatic_coverage_rate": (
                (auto_keep_count + auto_exclude_count) / len(items)
            ),
            "auto_keep_unrelated_events": auto_keep_unrelated_events,
            "auto_exclude_related_events": auto_exclude_related_events,
        },
        "gates": {
            "error_gate_passed": error_gate_passed,
            "support_gate_passed": support_gate_passed,
            "minimum_auto_keep_support": (
                plan.locked_test.minimum_auto_keep_support
            ),
            "minimum_auto_exclude_support": (
                plan.locked_test.minimum_auto_exclude_support
            ),
            "maximum_auto_keep_unrelated_events": (
                plan.locked_test.maximum_auto_keep_unrelated_events
            ),
            "maximum_auto_exclude_related_events": (
                plan.locked_test.maximum_auto_exclude_related_events
            ),
        },
        "overall_metrics": overall_metrics,
        "diagnostics_are_acceptance_gates": False,
        "model_comparison_performed": False,
        "threshold_search_performed": False,
        "fit_call_count": 0,
        "prediction_call_count": 1,
        "test_status": "opened_once",
        "audit_status": "FROZEN_NOT_RUN",
        "deployment_status": deployment_status,
        "may_generate_provisional_routing": (
            status == plan.locked_test.pass_status
        ),
        "may_generate_formal_auto_decisions": False,
        "auto_cleaning_decisions_present": False,
        "platform_used": False,
    }
