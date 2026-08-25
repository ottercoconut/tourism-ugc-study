"""研究者事后阈值选择、交付计数与安全边界的冻结配置。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ModelRetrainingDeliveryConfigError(RuntimeError):
    """交付冻结配置不可读、字段漂移或安全边界放宽时的异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、成员或私有路径的稳定失败码。"""

        super().__init__("formal model retraining delivery configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ResearcherRoutingSelection:
    """研究者明确接受事后风险后冻结的唯一模型与双阈值。"""

    candidate_name: str
    candidate_id: str
    T_keep: float
    T_exclude: float
    acceptance_basis: str
    enabled_automatic_actions: tuple[str, ...]
    historical_audit_status: str
    historical_failed_action: str
    audit_evidence_role: str
    independent_release_evidence: bool


@dataclass(frozen=True)
class ModelRetrainingDeliveryPlan:
    """绑定已有artifact、阈值证据、人工表契约和交付计数的计划。"""

    decision_id: str
    config_sha256: str
    selection: ResearcherRoutingSelection
    bindings: Mapping[str, str]
    selected_point_evidence: Mapping[str, int | float]
    manual_task_filename: str
    manual_task_columns: tuple[str, ...]
    expected_manual_task_count: int
    expected_total_decision_count: int
    expected_action_counts: Mapping[str, int]
    expected_source_counts: Mapping[str, int]


_ROOT_FIELDS = {
    "artifact_kind",
    "status",
    "selection",
    "bindings",
    "selected_point_evidence",
    "delivery",
    "guards",
}
_SELECTION_FIELDS = {
    "candidate_name",
    "candidate_id",
    "T_keep",
    "T_exclude",
    "acceptance_basis",
    "enabled_automatic_actions",
    "historical_audit_outcome_preserved",
    "historical_audit_status",
    "historical_failed_action",
    "audit_evidence_role",
    "independent_release_evidence",
}
_BINDING_FIELDS = {
    "retraining_plan_sha256",
    "snapshot_id",
    "snapshot_manifest_sha256",
    "base_policy_id",
    "base_policy_manifest_sha256",
    "frozen_model_sha256",
    "inference_id",
    "inference_manifest_sha256",
    "inference_records_sha256",
    "threshold_exploration_id",
    "threshold_grid_manifest_sha256",
    "threshold_grid_sha256",
    "audit_assessment_id",
    "audit_assessment_manifest_sha256",
}
_EVIDENCE_FIELDS = {
    "population_count",
    "population_auto_keep_count",
    "population_manual_review_count",
    "population_auto_exclude_count",
    "wave_b_auto_keep_count",
    "wave_b_auto_keep_adverse_count",
    "wave_b_auto_keep_raw_risk",
    "wave_b_auto_keep_weighted_risk",
    "wave_b_auto_keep_cp95_upper",
    "wave_b_auto_exclude_count",
    "wave_b_auto_exclude_adverse_count",
    "wave_b_auto_exclude_raw_risk",
    "wave_b_auto_exclude_weighted_risk",
    "wave_b_auto_exclude_cp95_upper",
    "audit_auto_keep_count",
    "audit_auto_keep_adverse_count",
    "audit_auto_keep_raw_risk",
    "audit_auto_keep_cp95_upper",
    "audit_auto_exclude_count",
    "audit_auto_exclude_adverse_count",
    "audit_auto_exclude_raw_risk",
    "audit_auto_exclude_cp95_upper",
}
_DELIVERY_FIELDS = {
    "manual_task_filename",
    "manual_task_columns",
    "manual_task_encoding",
    "expected_manual_task_count",
    "expected_total_decision_count",
    "expected_action_counts",
    "expected_source_counts",
}
_GUARD_FIELDS = {
    "fit_call_count",
    "predict_call_count",
    "encoder_call_count",
    "source_database_write_count",
    "source_records_deleted",
    "platform_used",
    "old_locked_test_reopened",
    "model_search_stopped",
}
_TASK_COLUMNS = (
    "task_id",
    "sample_run_id",
    "normalized_model_text",
    "tourism_label",
)


def _mapping(value: Any, fields: set[str], reason_code: str) -> Mapping[str, Any]:
    """要求节点恰好包含指定字段，防止协议静默漂移。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelRetrainingDeliveryConfigError(reason_code)
    return value


def _hex(value: Any, length: int, reason_code: str) -> str:
    """验证小写十六进制内容身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelRetrainingDeliveryConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑、禁止NaN的配置摘要。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finite_probability(value: Any, reason_code: str) -> float:
    """读取闭区间概率并拒绝布尔值、NaN和无穷。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelRetrainingDeliveryConfigError(reason_code)
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ModelRetrainingDeliveryConfigError(reason_code)
    return probability


def load_model_retraining_delivery_plan(
    path: str | Path,
) -> ModelRetrainingDeliveryPlan:
    """加载研究者确认的模型、0.31/0.96阈值与交付契约。

    Args:
        path: 仓库内可提交的公开冻结配置。

    Returns:
        绑定已有artifact及预期交付计数的不可变计划。

    Raises:
        ModelRetrainingDeliveryConfigError: 配置不可读、字段漂移或安全门放宽。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelRetrainingDeliveryConfigError(
            "model_retraining_delivery_config_unreadable"
        ) from exc
    root = _mapping(
        loaded, _ROOT_FIELDS, "model_retraining_delivery_config_invalid"
    )
    selection = _mapping(
        root["selection"],
        _SELECTION_FIELDS,
        "model_retraining_delivery_selection_invalid",
    )
    bindings = _mapping(
        root["bindings"],
        _BINDING_FIELDS,
        "model_retraining_delivery_bindings_invalid",
    )
    evidence = _mapping(
        root["selected_point_evidence"],
        _EVIDENCE_FIELDS,
        "model_retraining_delivery_evidence_invalid",
    )
    delivery = _mapping(
        root["delivery"],
        _DELIVERY_FIELDS,
        "model_retraining_delivery_contract_invalid",
    )
    guards = _mapping(
        root["guards"],
        _GUARD_FIELDS,
        "model_retraining_delivery_guards_invalid",
    )
    action_counts = _mapping(
        delivery["expected_action_counts"],
        {"keep", "exclude", "manual_review"},
        "model_retraining_delivery_counts_invalid",
    )
    source_counts = _mapping(
        delivery["expected_source_counts"],
        {
            "human_training_label",
            "human_audit_label",
            "model_released_auto_keep",
            "model_released_auto_exclude",
            "model_middle_band",
        },
        "model_retraining_delivery_counts_invalid",
    )
    T_keep = _finite_probability(
        selection["T_keep"], "model_retraining_delivery_threshold_invalid"
    )
    T_exclude = _finite_probability(
        selection["T_exclude"], "model_retraining_delivery_threshold_invalid"
    )
    if (
        root["artifact_kind"]
        != "formal-cleaning-model-retraining-delivery-decision"
        or root["status"] != "researcher_selected_after_post_hoc_review"
        or selection["candidate_name"] != "logit_fusion"
        or T_keep != 0.31
        or T_exclude != 0.96
        or T_keep >= T_exclude
        or selection["acceptance_basis"]
        != "researcher_accepted_post_hoc_risk"
        or tuple(selection["enabled_automatic_actions"])
        != ("auto_keep", "auto_exclude")
        or selection["historical_audit_outcome_preserved"] is not True
        or selection["historical_audit_status"] != "ONE_TAIL_RELEASED"
        or selection["historical_failed_action"] != "auto_keep"
        or selection["audit_evidence_role"]
        != "post_hoc_threshold_diagnostic_only"
        or selection["independent_release_evidence"] is not False
        or delivery["manual_task_filename"]
        != "manual-review-tourism-relevance-annotation.csv"
        or tuple(delivery["manual_task_columns"]) != _TASK_COLUMNS
        or delivery["manual_task_encoding"] != "utf-8-sig"
        or delivery["expected_manual_task_count"] != 2286
        or delivery["expected_total_decision_count"] != 13858
        or action_counts
        != {"keep": 6835, "exclude": 4737, "manual_review": 2286}
        or source_counts
        != {
            "human_training_label": 1300,
            "human_audit_label": 300,
            "model_released_auto_keep": 6111,
            "model_released_auto_exclude": 3861,
            "model_middle_band": 2286,
        }
        or guards
        != {
            "fit_call_count": 0,
            "predict_call_count": 0,
            "encoder_call_count": 0,
            "source_database_write_count": 0,
            "source_records_deleted": 0,
            "platform_used": False,
            "old_locked_test_reopened": False,
            "model_search_stopped": True,
        }
    ):
        raise ModelRetrainingDeliveryConfigError(
            "model_retraining_delivery_config_invalid"
        )
    if sum(int(value) for value in action_counts.values()) != 13858 or sum(
        int(value) for value in source_counts.values()
    ) != 13858:
        raise ModelRetrainingDeliveryConfigError(
            "model_retraining_delivery_counts_invalid"
        )
    normalized_bindings: dict[str, str] = {}
    for key, value in bindings.items():
        length = 32 if key.endswith("_id") else 64
        normalized_bindings[key] = _hex(
            value, length, "model_retraining_delivery_bindings_invalid"
        )
    normalized_evidence: dict[str, int | float] = {}
    for key, value in evidence.items():
        if key.endswith(("_risk", "_upper")):
            normalized_evidence[key] = _finite_probability(
                value, "model_retraining_delivery_evidence_invalid"
            )
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ModelRetrainingDeliveryConfigError(
                "model_retraining_delivery_evidence_invalid"
            )
        else:
            normalized_evidence[key] = value
    if (
        normalized_evidence["population_count"] != 12558
        or sum(
            int(normalized_evidence[key])
            for key in (
                "population_auto_keep_count",
                "population_manual_review_count",
                "population_auto_exclude_count",
            )
        )
        != 12558
    ):
        raise ModelRetrainingDeliveryConfigError(
            "model_retraining_delivery_evidence_invalid"
        )
    config_sha256 = _canonical_sha256(root)
    return ModelRetrainingDeliveryPlan(
        decision_id=config_sha256[:32],
        config_sha256=config_sha256,
        selection=ResearcherRoutingSelection(
            candidate_name=str(selection["candidate_name"]),
            candidate_id=_hex(
                selection["candidate_id"],
                32,
                "model_retraining_delivery_selection_invalid",
            ),
            T_keep=T_keep,
            T_exclude=T_exclude,
            acceptance_basis=str(selection["acceptance_basis"]),
            enabled_automatic_actions=tuple(
                str(item) for item in selection["enabled_automatic_actions"]
            ),
            historical_audit_status=str(selection["historical_audit_status"]),
            historical_failed_action=str(selection["historical_failed_action"]),
            audit_evidence_role=str(selection["audit_evidence_role"]),
            independent_release_evidence=bool(
                selection["independent_release_evidence"]
            ),
        ),
        bindings=normalized_bindings,
        selected_point_evidence=normalized_evidence,
        manual_task_filename=str(delivery["manual_task_filename"]),
        manual_task_columns=tuple(delivery["manual_task_columns"]),
        expected_manual_task_count=int(delivery["expected_manual_task_count"]),
        expected_total_decision_count=int(
            delivery["expected_total_decision_count"]
        ),
        expected_action_counts={
            str(key): int(value) for key, value in action_counts.items()
        },
        expected_source_counts={
            str(key): int(value) for key, value in source_counts.items()
        },
    )
