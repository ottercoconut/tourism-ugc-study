"""Wave A 完成后的三段式双阈值选择分析计划解析。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ModelRoutingSelectionConfigError(RuntimeError):
    """双阈值分析计划字段、身份或安全边界无效时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含标签、正文、成员身份或私有路径的稳定失败码。"""

        super().__init__("formal model routing selection configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ModelRoutingSelectionPlan:
    """绑定 Wave A 证据、固定网格与禁止自动冻结边界的分析计划。"""

    plan_id: str
    plan_sha256: str
    random_seed: int
    prelabel_protocol_commit: str
    scored_frame_id: str
    scored_manifest_sha256: str
    wave_id: str
    wave_manifest_sha256: str
    completed_csv_sha256: str
    base_evaluation_id: str
    base_evaluation_manifest_sha256: str
    sparse_model_id: str
    qwen_model_id: str
    keep_thresholds: tuple[float, ...]
    exclude_thresholds: tuple[float, ...]
    confidence_level: float
    bootstrap_repetitions: int
    workload_targets: tuple[float, ...]
    minimum_raw_tail_count: int
    minimum_effective_tail_count: float


_KEEP_THRESHOLDS = tuple(round(0.05 + index * 0.01, 2) for index in range(16))
_EXCLUDE_THRESHOLDS = tuple(round(0.80 + index * 0.01, 2) for index in range(16))
_WORKLOAD_TARGETS = (0.10, 0.20, 0.30, 0.40, 0.50)


def _mapping(value: Any, fields: set[str], reason_code: str) -> Mapping[str, Any]:
    """要求配置节点使用精确键集合，拒绝静默协议漂移。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelRoutingSelectionConfigError(reason_code)
    return value


def _hex(value: Any, length: int, reason_code: str) -> str:
    """要求字段是固定长度的小写十六进制身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelRoutingSelectionConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑且拒绝 NaN 的规范配置摘要。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_model_routing_selection_plan(
    path: str | Path,
) -> ModelRoutingSelectionPlan:
    """加载严格绑定已封存 Wave A 证据的双阈值分析计划。

    Args:
        path: 冻结 YAML 配置路径。

    Returns:
        只允许生成选择分析、不能训练或自动冻结策略的不可变计划。

    Raises:
        ModelRoutingSelectionConfigError: 文件不可读、字段漂移或安全边界放宽。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelRoutingSelectionConfigError(
            "model_routing_selection_config_unreadable"
        ) from exc
    root = _mapping(
        loaded,
        {
            "artifact_kind",
            "status",
            "random_seed",
            "prelabel_protocol",
            "bindings",
            "threshold_grid",
            "analysis",
            "guards",
        },
        "model_routing_selection_config_invalid",
    )
    protocol = _mapping(
        root["prelabel_protocol"],
        {"commit", "grid_rule"},
        "model_routing_selection_protocol_invalid",
    )
    bindings = _mapping(
        root["bindings"],
        {
            "scored_frame_id",
            "scored_manifest_sha256",
            "wave_id",
            "wave_manifest_sha256",
            "completed_csv_sha256",
            "base_evaluation_id",
            "base_evaluation_manifest_sha256",
            "sparse_model_id",
            "qwen_model_id",
        },
        "model_routing_selection_bindings_invalid",
    )
    grid = _mapping(
        root["threshold_grid"],
        {"keep", "exclude", "pair_count", "probability_role"},
        "model_routing_selection_grid_invalid",
    )
    analysis = _mapping(
        root["analysis"],
        {
            "estimand",
            "primary_weighting",
            "confidence_level",
            "bootstrap_repetitions",
            "bootstrap_unit",
            "workload_targets",
            "equal_workload_comparison",
            "uncertain_policy",
            "evidence_warning",
            "pareto_objectives",
        },
        "model_routing_selection_analysis_invalid",
    )
    evidence = _mapping(
        analysis["evidence_warning"],
        {
            "minimum_raw_tail_count",
            "minimum_effective_tail_count",
            "use_as_acceptance_gate",
        },
        "model_routing_selection_evidence_warning_invalid",
    )
    guards = _mapping(
        root["guards"],
        {
            "labels_entered_fit",
            "may_retrain",
            "may_open_locked_test",
            "may_freeze_threshold_automatically",
            "threshold_status",
            "audit_status",
            "auto_cleaning_decisions_present",
            "platform_used",
        },
        "model_routing_selection_guards_invalid",
    )
    if (
        root["artifact_kind"] != "formal-cleaning-model-routing-selection-plan"
        or root["status"] != "frozen_analysis_only"
        or root["random_seed"] != 20260824
        or protocol
        != {
            "commit": "71610b92cabbcbb9dc84d3f8262dee526f8cae7e",
            "grid_rule": "documented_before_wave_a_labels_opened",
        }
        or tuple(grid["keep"]) != _KEEP_THRESHOLDS
        or tuple(grid["exclude"]) != _EXCLUDE_THRESHOLDS
        or grid["pair_count"] != 256
        or grid["probability_role"]
        != "selection_diagnostic_not_population_posterior"
        or analysis["estimand"]
        != "eligible_post_population_three_way_routing_performance"
        or analysis["primary_weighting"]
        != "horvitz_thompson_hajek_by_sampling_stratum"
        or analysis["confidence_level"] != 0.95
        or analysis["bootstrap_repetitions"] != 2000
        or analysis["bootstrap_unit"] != "leakage_component"
        or tuple(analysis["workload_targets"]) != _WORKLOAD_TARGETS
        or analysis["equal_workload_comparison"] is not True
        or analysis["uncertain_policy"]
        != "report_separately_no_binary_coercion"
        or evidence
        != {
            "minimum_raw_tail_count": 30,
            "minimum_effective_tail_count": 20.0,
            "use_as_acceptance_gate": False,
        }
        or analysis["pareto_objectives"]
        != [
            "manual_review_rate_minimize",
            "conservative_related_loss_in_auto_exclude_minimize",
            "conservative_unrelated_retention_in_auto_keep_minimize",
        ]
        or guards
        != {
            "labels_entered_fit": False,
            "may_retrain": False,
            "may_open_locked_test": False,
            "may_freeze_threshold_automatically": False,
            "threshold_status": "UNSET",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
        }
    ):
        raise ModelRoutingSelectionConfigError(
            "model_routing_selection_plan_invalid"
        )
    for key, length in {
        "scored_frame_id": 32,
        "scored_manifest_sha256": 64,
        "wave_id": 32,
        "wave_manifest_sha256": 64,
        "completed_csv_sha256": 64,
        "base_evaluation_id": 32,
        "base_evaluation_manifest_sha256": 64,
        "sparse_model_id": 32,
        "qwen_model_id": 32,
    }.items():
        _hex(bindings.get(key), length, "model_routing_selection_bindings_invalid")
    plan_sha256 = _canonical_sha256(root)
    return ModelRoutingSelectionPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=20260824,
        prelabel_protocol_commit=str(protocol["commit"]),
        scored_frame_id=str(bindings["scored_frame_id"]),
        scored_manifest_sha256=str(bindings["scored_manifest_sha256"]),
        wave_id=str(bindings["wave_id"]),
        wave_manifest_sha256=str(bindings["wave_manifest_sha256"]),
        completed_csv_sha256=str(bindings["completed_csv_sha256"]),
        base_evaluation_id=str(bindings["base_evaluation_id"]),
        base_evaluation_manifest_sha256=str(
            bindings["base_evaluation_manifest_sha256"]
        ),
        sparse_model_id=str(bindings["sparse_model_id"]),
        qwen_model_id=str(bindings["qwen_model_id"]),
        keep_thresholds=_KEEP_THRESHOLDS,
        exclude_thresholds=_EXCLUDE_THRESHOLDS,
        confidence_level=0.95,
        bootstrap_repetitions=2000,
        workload_targets=_WORKLOAD_TARGETS,
        minimum_raw_tail_count=30,
        minimum_effective_tail_count=20.0,
    )
