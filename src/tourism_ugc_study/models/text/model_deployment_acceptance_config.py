"""锁定测试判读与部署双尾审计计划的严格配置解析。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ModelDeploymentAcceptanceConfigError(RuntimeError):
    """最终判读或审计计划发生漂移时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露成员、标签、正文或路径的稳定失败码。"""

        super().__init__("formal deployment acceptance configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class LockedTestRule:
    """仅允许一次执行的锁定测试判读规则。"""

    access_count: int
    t_keep: float
    t_exclude: float
    minimum_auto_keep_support: int
    minimum_auto_exclude_support: int
    maximum_auto_keep_unrelated_events: int
    maximum_auto_exclude_related_events: int
    support_failure_status: str
    error_failure_status: str
    pass_status: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class AuditTailRule:
    """一个自动动作尾部的负面事件与失败动作。"""

    action: str
    adverse_labels: tuple[str, ...]
    maximum_adverse_events: int
    failure_action: str


@dataclass(frozen=True)
class DeploymentAuditRule:
    """锁定测试通过后、正式决定前执行的盲法双尾审计。"""

    random_seed: int
    sample_count_per_tail: int
    confidence_level: float
    interval_method: str
    zero_event_upper_at_full_sample: float
    tails: tuple[AuditTailRule, ...]
    task_columns: tuple[str, ...]
    hidden_fields: tuple[str, ...]


@dataclass(frozen=True)
class ModelDeploymentAcceptancePlan:
    """绑定模型、Wave B、锁定测试和审计规则的最终计划。"""

    plan_id: str
    plan_sha256: str
    policy_id: str
    policy_manifest_sha256: str
    wave_b_evaluation_id: str
    wave_b_evaluation_manifest_sha256: str
    wave_b_completed_csv_sha256: str
    qwen_run_id: str
    qwen_model_id: str
    qwen_training_manifest_sha256: str
    qwen_model_artifact_sha256: str
    split_anchor_model_id: str
    split_artifact_sha256: str
    split_manifest_sha256: str
    test_manifest_sha256: str
    test_count: int
    locked_test: LockedTestRule
    audit: DeploymentAuditRule


_TASK_COLUMNS = (
    "task_id",
    "sample_run_id",
    "normalized_model_text",
    "tourism_label",
)
_DIAGNOSTICS = (
    "accuracy_at_0_5",
    "confusion_at_0_5",
    "log_loss",
    "brier_score",
    "pr_auc_unrelated",
    "routing_counts",
)
_HIDDEN_FIELDS = (
    "source_post_id",
    "source_version",
    "platform_key",
    "model_name",
    "model_probability",
    "routing_action",
    "sampling_reason",
)


def _mapping(value: Any, fields: set[str], reason_code: str) -> Mapping[str, Any]:
    """要求映射拥有精确键集合，阻止配置字段静默增删。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelDeploymentAcceptanceConfigError(reason_code)
    return value


def _hex(value: Any, length: int, reason_code: str) -> str:
    """校验内容身份使用的小写定长十六进制字符串。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelDeploymentAcceptanceConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算拒绝 NaN 的规范配置摘要。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_tail(value: Any, *, action: str) -> AuditTailRule:
    """解析一个尾部规则并固定保守事件语义。"""

    item = _mapping(
        value,
        {"adverse_labels", "maximum_adverse_events", "failure_action"},
        "model_deployment_audit_tail_invalid",
    )
    expected_labels = {
        "auto_keep": ("unrelated", "uncertain"),
        "auto_exclude": ("related", "uncertain"),
    }[action]
    expected_failure = {
        "auto_keep": "suspend_auto_keep_and_route_tail_to_manual",
        "auto_exclude": "suspend_auto_exclude_and_route_tail_to_manual",
    }[action]
    labels = item["adverse_labels"]
    if (
        not isinstance(labels, list)
        or tuple(labels) != expected_labels
        or item["maximum_adverse_events"] != 0
        or item["failure_action"] != expected_failure
    ):
        raise ModelDeploymentAcceptanceConfigError(
            "model_deployment_audit_tail_invalid"
        )
    return AuditTailRule(
        action=action,
        adverse_labels=expected_labels,
        maximum_adverse_events=0,
        failure_action=expected_failure,
    )


def load_model_deployment_acceptance_plan(
    path: str | Path,
) -> ModelDeploymentAcceptancePlan:
    """加载并严格校验锁定测试与部署审计计划。

    Args:
        path: 尚未打开锁定测试时提交的冻结 YAML 路径。

    Returns:
        绑定 Wave B、唯一模型、测试身份和双尾审计的不可变计划。

    Raises:
        ModelDeploymentAcceptanceConfigError: 文件、字段或安全门漂移。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelDeploymentAcceptanceConfigError(
            "model_deployment_acceptance_config_unreadable"
        ) from exc
    root = _mapping(
        loaded,
        {
            "artifact_kind",
            "status",
            "random_seed",
            "researcher_decision",
            "bindings",
            "locked_test",
            "deployment_audit",
            "guards",
        },
        "model_deployment_acceptance_config_invalid",
    )
    decision = _mapping(
        root["researcher_decision"],
        {"issue_number", "decision", "risk_preference"},
        "model_deployment_acceptance_decision_invalid",
    )
    bindings = _mapping(
        root["bindings"],
        {
            "routing_policy_id",
            "routing_policy_manifest_sha256",
            "wave_b_evaluation_id",
            "wave_b_evaluation_manifest_sha256",
            "wave_b_completed_csv_sha256",
            "qwen_run_id",
            "qwen_model_id",
            "qwen_training_manifest_sha256",
            "qwen_model_artifact_sha256",
            "split_anchor_model_id",
            "split_artifact_sha256",
            "split_manifest_sha256",
            "test_manifest_sha256",
            "test_count",
        },
        "model_deployment_acceptance_bindings_invalid",
    )
    locked = _mapping(
        root["locked_test"],
        {
            "access_count",
            "prediction_model",
            "T_keep",
            "T_exclude",
            "minimum_auto_keep_support",
            "minimum_auto_exclude_support",
            "maximum_auto_keep_unrelated_events",
            "maximum_auto_exclude_related_events",
            "support_failure_status",
            "error_failure_status",
            "pass_status",
            "diagnostics",
            "diagnostics_are_acceptance_gates",
            "may_compare_or_select_models",
            "may_change_model_or_thresholds_after_open",
        },
        "model_deployment_locked_test_invalid",
    )
    audit = _mapping(
        root["deployment_audit"],
        {
            "status",
            "timing",
            "sampling_unit",
            "sampling_method",
            "sample_count_per_tail",
            "small_tail_rule",
            "tails",
            "confidence_level",
            "interval_method",
            "zero_event_upper_at_full_sample",
            "finite_population_correction",
            "task_columns",
            "task_encoding",
            "human_fills_only",
            "hidden_fields",
            "platform_quota_used",
            "replacement_or_dilution_after_failure_allowed",
            "recovery_rule",
        },
        "model_deployment_audit_invalid",
    )
    guards = _mapping(
        root["guards"],
        {
            "labels_entered_fit",
            "may_retrain",
            "may_open_locked_test_after_plan_freeze",
            "may_generate_provisional_routing_after_test_pass",
            "may_generate_formal_auto_decisions_before_audit_pass",
            "threshold_status",
            "deployment_status",
            "auto_cleaning_decisions_present",
            "platform_used",
        },
        "model_deployment_acceptance_guards_invalid",
    )
    try:
        t_keep = float(locked["T_keep"])
        t_exclude = float(locked["T_exclude"])
        confidence = float(audit["confidence_level"])
        stated_upper = float(audit["zero_event_upper_at_full_sample"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelDeploymentAcceptanceConfigError(
            "model_deployment_acceptance_numeric_invalid"
        ) from exc
    expected_upper = 1.0 - math.pow(1.0 - confidence, 1.0 / 150.0)
    if (
        root["artifact_kind"]
        != "formal-cleaning-model-deployment-acceptance-plan"
        or root["status"] != "frozen_before_locked_test"
        or root["random_seed"] != 20260825
        or decision
        != {
            "issue_number": 46,
            "decision": "freeze_single_locked_test_and_two_tail_audit",
            "risk_preference": "ugc_safety_first",
        }
        or bindings["routing_policy_id"]
        != "30a0806c341546c749034a07597b8d9b"
        or bindings["wave_b_evaluation_id"]
        != "fc5c2721c4e15e8addf7c55ab115950e"
        or bindings["qwen_run_id"] != "bdf73219d584edfcbeea02772716a90a"
        or bindings["qwen_model_id"] != "e24fc7a65a5e0e52f383725e21607837"
        or bindings["split_anchor_model_id"]
        != "9cd30922aabf7fb2e2ba42e5a0396cfd"
        or bindings["test_count"] != 148
        or locked["access_count"] != 1
        or locked["prediction_model"] != "qwen_only"
        or t_keep != 0.14
        or t_exclude != 0.86
        or locked["minimum_auto_keep_support"] != 20
        or locked["minimum_auto_exclude_support"] != 20
        or locked["maximum_auto_keep_unrelated_events"] != 0
        or locked["maximum_auto_exclude_related_events"] != 0
        or locked["support_failure_status"] != "INCONCLUSIVE_MANUAL_ONLY"
        or locked["error_failure_status"] != "FAILED_MANUAL_ONLY"
        or locked["pass_status"] != "PASSED_FOR_PROVISIONAL_ROUTING"
        or tuple(locked["diagnostics"]) != _DIAGNOSTICS
        or locked["diagnostics_are_acceptance_gates"] is not False
        or locked["may_compare_or_select_models"] is not False
        or locked["may_change_model_or_thresholds_after_open"] is not False
        or audit["status"] != "FROZEN_NOT_RUN"
        or audit["timing"]
        != "after_locked_test_pass_before_formal_auto_decisions"
        or audit["sampling_unit"] != "post"
        or audit["sampling_method"]
        != "simple_random_without_replacement_within_action_tail"
        or audit["sample_count_per_tail"] != 150
        or audit["small_tail_rule"]
        != "census_when_population_below_sample_count"
        or confidence != 0.95
        or audit["interval_method"] != "one_sided_clopper_pearson"
        or not math.isclose(stated_upper, expected_upper, rel_tol=0.0, abs_tol=1e-15)
        or audit["finite_population_correction"]
        != "not_used_conservative_binomial_upper"
        or tuple(audit["task_columns"]) != _TASK_COLUMNS
        or audit["task_encoding"] != "utf-8-sig"
        or audit["human_fills_only"] != "tourism_label"
        or tuple(audit["hidden_fields"]) != _HIDDEN_FIELDS
        or audit["platform_quota_used"] is not False
        or audit["replacement_or_dilution_after_failure_allowed"] is not False
        or audit["recovery_rule"]
        != "new_policy_id_and_new_independent_audit_for_failed_tail"
        or guards
        != {
            "labels_entered_fit": False,
            "may_retrain": False,
            "may_open_locked_test_after_plan_freeze": True,
            "may_generate_provisional_routing_after_test_pass": True,
            "may_generate_formal_auto_decisions_before_audit_pass": False,
            "threshold_status": "FROZEN_FOR_LOCKED_TEST",
            "deployment_status": "NOT_AUTHORIZED",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
        }
    ):
        raise ModelDeploymentAcceptanceConfigError(
            "model_deployment_acceptance_plan_invalid"
        )
    for key, length in {
        "routing_policy_id": 32,
        "routing_policy_manifest_sha256": 64,
        "wave_b_evaluation_id": 32,
        "wave_b_evaluation_manifest_sha256": 64,
        "wave_b_completed_csv_sha256": 64,
        "qwen_run_id": 32,
        "qwen_model_id": 32,
        "qwen_training_manifest_sha256": 64,
        "qwen_model_artifact_sha256": 64,
        "split_anchor_model_id": 32,
        "split_artifact_sha256": 64,
        "split_manifest_sha256": 64,
        "test_manifest_sha256": 64,
    }.items():
        _hex(bindings[key], length, "model_deployment_acceptance_bindings_invalid")
    tails = _mapping(
        audit["tails"],
        {"auto_keep", "auto_exclude"},
        "model_deployment_audit_tail_invalid",
    )
    audit_tails = tuple(
        _audit_tail(tails[action], action=action)
        for action in ("auto_keep", "auto_exclude")
    )
    locked_rule = LockedTestRule(
        access_count=1,
        t_keep=t_keep,
        t_exclude=t_exclude,
        minimum_auto_keep_support=20,
        minimum_auto_exclude_support=20,
        maximum_auto_keep_unrelated_events=0,
        maximum_auto_exclude_related_events=0,
        support_failure_status="INCONCLUSIVE_MANUAL_ONLY",
        error_failure_status="FAILED_MANUAL_ONLY",
        pass_status="PASSED_FOR_PROVISIONAL_ROUTING",
        diagnostics=_DIAGNOSTICS,
    )
    audit_rule = DeploymentAuditRule(
        random_seed=20260825,
        sample_count_per_tail=150,
        confidence_level=confidence,
        interval_method="one_sided_clopper_pearson",
        zero_event_upper_at_full_sample=stated_upper,
        tails=audit_tails,
        task_columns=_TASK_COLUMNS,
        hidden_fields=_HIDDEN_FIELDS,
    )
    plan_sha256 = _canonical_sha256(root)
    return ModelDeploymentAcceptancePlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        policy_id=str(bindings["routing_policy_id"]),
        policy_manifest_sha256=str(bindings["routing_policy_manifest_sha256"]),
        wave_b_evaluation_id=str(bindings["wave_b_evaluation_id"]),
        wave_b_evaluation_manifest_sha256=str(
            bindings["wave_b_evaluation_manifest_sha256"]
        ),
        wave_b_completed_csv_sha256=str(bindings["wave_b_completed_csv_sha256"]),
        qwen_run_id=str(bindings["qwen_run_id"]),
        qwen_model_id=str(bindings["qwen_model_id"]),
        qwen_training_manifest_sha256=str(
            bindings["qwen_training_manifest_sha256"]
        ),
        qwen_model_artifact_sha256=str(bindings["qwen_model_artifact_sha256"]),
        split_anchor_model_id=str(bindings["split_anchor_model_id"]),
        split_artifact_sha256=str(bindings["split_artifact_sha256"]),
        split_manifest_sha256=str(bindings["split_manifest_sha256"]),
        test_manifest_sha256=str(bindings["test_manifest_sha256"]),
        test_count=int(bindings["test_count"]),
        locked_test=locked_rule,
        audit=audit_rule,
    )
