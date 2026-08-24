"""1300条标签重训、非零风险路由与前瞻性审计计划解析。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ModelRetrainingConfigError(RuntimeError):
    """重训计划字段或安全边界漂移时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、成员、标签或私有路径的稳定失败码。"""

        super().__init__("formal model retraining configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RetrainingCandidatePlan:
    """一个固定候选的公开算法参数。"""

    name: str
    family: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ModelRetrainingPlan:
    """绑定训练证据、固定候选、路由风险与新审计的不可变计划。"""

    plan_id: str
    plan_sha256: str
    random_seed: int
    candidate_build_id: str
    leakage_build_id: str
    reference_csv_sha256: str
    wave_a_completed_csv_sha256: str
    wave_a_private_map_sha256: str
    wave_b_completed_csv_sha256: str
    wave_b_private_map_sha256: str
    wave_b_evaluation_id: str
    wave_b_evaluation_manifest_sha256: str
    old_qwen_model_id: str
    old_locked_test_run_id: str
    old_locked_test_manifest_sha256: str
    expected_training_count: int
    expected_related_count: int
    expected_unrelated_count: int
    expected_origin_counts: Mapping[str, int]
    qwen_base_plan_sha256: str
    qwen_repository: str
    qwen_revision: str
    qwen_instruction: str
    qwen_prompt_template: str
    qwen_max_length: int
    qwen_embedding_dimension: int
    qwen_device: str
    qwen_batch_size: int
    candidates: tuple[RetrainingCandidatePlan, ...]
    outer_folds: int
    minimum_folds: int
    confidence_level: float
    bootstrap_repetitions: int
    keep_thresholds: tuple[float, ...]
    exclude_thresholds: tuple[float, ...]
    minimum_raw_tail_count: int
    maximum_auto_exclude_related_rate: float
    maximum_auto_keep_unrelated_rate: float
    historical_manual_rate: float
    material_improvement_percentage_points: float
    selection_order: tuple[str, ...]
    audit_sample_count_per_tail: int
    audit_auto_exclude_maximum_adverse_events: int
    audit_auto_keep_maximum_adverse_events: int
    audit_task_columns: tuple[str, ...]


_ROOT_FIELDS = {
    "artifact_kind",
    "status",
    "random_seed",
    "researcher_decision",
    "bindings",
    "training_snapshot",
    "encoder",
    "candidates",
    "evaluation",
    "routing",
    "audit",
    "guards",
}
_TASK_COLUMNS = (
    "task_id",
    "sample_run_id",
    "normalized_model_text",
    "tourism_label",
)
_SELECTION_ORDER = (
    "manual_review_rate_minimize",
    "auto_exclude_related_rate_minimize",
    "auto_keep_unrelated_rate_minimize",
    "log_loss_minimize",
    "complexity_minimize",
    "candidate_id_ascending",
)


def _mapping(value: Any, fields: set[str], reason_code: str) -> Mapping[str, Any]:
    """要求节点使用精确字段集合，阻止协议静默漂移。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelRetrainingConfigError(reason_code)
    return value


def _hex(value: Any, length: int, reason_code: str) -> str:
    """要求内容身份是固定长度的小写十六进制字符串。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelRetrainingConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑且拒绝NaN的配置摘要。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _thresholds(start: float, stop: float, step: float) -> tuple[float, ...]:
    """按十进制定点语义生成包含终点的概率网格。"""

    count = round((stop - start) / step)
    return tuple(round(start + index * step, 2) for index in range(count + 1))


def load_model_retraining_plan(path: str | Path) -> ModelRetrainingPlan:
    """加载Issue #49冻结的重训与审计计划。

    Args:
        path: 仓库内冻结YAML配置。

    Returns:
        经严格字段、算法和安全门校验的不可变计划。

    Raises:
        ModelRetrainingConfigError: 配置不可读、字段漂移或放宽安全边界。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelRetrainingConfigError(
            "model_retraining_config_unreadable"
        ) from exc
    root = _mapping(
        loaded, _ROOT_FIELDS, "model_retraining_config_invalid"
    )
    decision = _mapping(
        root["researcher_decision"],
        {
            "parent_issue_number",
            "issue_number",
            "old_locked_test_outcome",
            "old_outcome_may_be_rewritten",
            "stop_after_fixed_candidates",
        },
        "model_retraining_decision_invalid",
    )
    bindings = _mapping(
        root["bindings"],
        {
            "candidate_build_id",
            "leakage_build_id",
            "reference_csv_sha256",
            "wave_a_completed_csv_sha256",
            "wave_a_private_map_sha256",
            "wave_b_completed_csv_sha256",
            "wave_b_private_map_sha256",
            "wave_b_evaluation_id",
            "wave_b_evaluation_manifest_sha256",
            "old_qwen_model_id",
            "old_locked_test_run_id",
            "old_locked_test_manifest_sha256",
        },
        "model_retraining_bindings_invalid",
    )
    snapshot = _mapping(
        root["training_snapshot"],
        {
            "expected_count",
            "expected_related_count",
            "expected_unrelated_count",
            "origins",
            "old_test_role",
            "sampling_weights_enter_fit",
            "platform_enters_fit",
        },
        "model_retraining_snapshot_plan_invalid",
    )
    origins = _mapping(
        snapshot["origins"],
        {"final_reference", "wave_a", "wave_b"},
        "model_retraining_snapshot_plan_invalid",
    )
    encoder = _mapping(
        root["encoder"],
        {
            "base_plan_sha256",
            "repository",
            "revision",
            "instruction",
            "prompt_template",
            "max_length",
            "projection",
            "aggregation",
            "omitted_token_count_required",
            "device",
            "batch_size",
        },
        "model_retraining_encoder_invalid",
    )
    candidates = _mapping(
        root["candidates"],
        {
            "qwen_linear_svc",
            "sparse_linear_svc",
            "logit_fusion",
            "model_family_expansion_allowed",
        },
        "model_retraining_candidates_invalid",
    )
    qwen = _mapping(
        candidates["qwen_linear_svc"],
        {"family", "embedding_dimension", "C", "class_weight"},
        "model_retraining_qwen_candidate_invalid",
    )
    sparse = _mapping(
        candidates["sparse_linear_svc"],
        {
            "family",
            "ngram_range",
            "min_df",
            "max_df",
            "sublinear_tf",
            "C",
            "class_weight",
        },
        "model_retraining_sparse_candidate_invalid",
    )
    fusion = _mapping(
        candidates["logit_fusion"],
        {"family", "C", "class_weight"},
        "model_retraining_fusion_candidate_invalid",
    )
    evaluation = _mapping(
        root["evaluation"],
        {
            "outer_folds",
            "minimum_folds",
            "split_unit",
            "calibration",
            "population_evidence",
            "confidence_level",
            "component_bootstrap_repetitions",
        },
        "model_retraining_evaluation_invalid",
    )
    routing = _mapping(
        root["routing"],
        {
            "keep_thresholds",
            "exclude_thresholds",
            "minimum_raw_tail_count",
            "maximum_auto_exclude_related_rate",
            "maximum_auto_keep_unrelated_rate",
            "uncertain_policy",
            "historical_manual_rate",
            "material_improvement_percentage_points",
            "selection_order",
        },
        "model_retraining_routing_invalid",
    )
    keep_grid = _mapping(
        routing["keep_thresholds"],
        {"start", "stop", "step"},
        "model_retraining_threshold_grid_invalid",
    )
    exclude_grid = _mapping(
        routing["exclude_thresholds"],
        {"start", "stop", "step"},
        "model_retraining_threshold_grid_invalid",
    )
    audit = _mapping(
        root["audit"],
        {
            "sample_count_per_tail",
            "sampling_method",
            "auto_exclude_maximum_adverse_events",
            "auto_keep_maximum_adverse_events",
            "confidence_level",
            "interval_method",
            "replacement_or_dilution_after_failure_allowed",
            "failed_tail_action",
            "task_columns",
            "task_encoding",
        },
        "model_retraining_audit_invalid",
    )
    guards = _mapping(
        root["guards"],
        {
            "source_database_read_only",
            "private_artifacts_may_enter_git",
            "formal_inference_fit_call_count",
            "old_locked_test_may_reopen",
            "audit_is_only_independent_release_evidence",
            "automatic_source_deletion_allowed",
            "platform_used",
        },
        "model_retraining_guards_invalid",
    )
    expected_decision = {
        "parent_issue_number": 46,
        "issue_number": 49,
        "old_locked_test_outcome": "FAILED_MANUAL_ONLY",
        "old_outcome_may_be_rewritten": False,
        "stop_after_fixed_candidates": True,
    }
    expected_snapshot = {
        "expected_count": 1300,
        "expected_related_count": 585,
        "expected_unrelated_count": 715,
        "origins": {
            "final_reference": 700,
            "wave_a": 240,
            "wave_b": 360,
        },
        "old_test_role": "historical_test_consumed_may_enter_fit_not_evaluation",
        "sampling_weights_enter_fit": False,
        "platform_enters_fit": False,
    }
    if (
        root["artifact_kind"] != "formal-cleaning-model-retraining-plan"
        or root["status"] != "frozen_before_retraining"
        or root["random_seed"] != 20260825
        or decision != expected_decision
        or snapshot != expected_snapshot
        or encoder["repository"] != "Qwen/Qwen3-Embedding-4B"
        or encoder["revision"] != "5cf2132abc99cad020ac570b19d031efec650f2b"
        or encoder["max_length"] != 2048
        or encoder["projection"] != "continuous_complete_chunks"
        or encoder["aggregation"] != "arithmetic_mean_then_l2_normalize"
        or encoder["omitted_token_count_required"] != 0
        or encoder["device"] != "mps"
        or encoder["batch_size"] != 1
        or qwen != {
            "family": "qwen_complete_chunks_linear_svc",
            "embedding_dimension": 2560,
            "C": 1.0,
            "class_weight": None,
        }
        or sparse != {
            "family": "char_tfidf_linear_svc",
            "ngram_range": [2, 5],
            "min_df": 2,
            "max_df": 0.995,
            "sublinear_tf": True,
            "C": 3.0,
            "class_weight": "balanced",
        }
        or fusion != {
            "family": "qwen_sparse_logit_fusion",
            "C": 1.0,
            "class_weight": None,
        }
        or candidates["model_family_expansion_allowed"] is not False
        or evaluation != {
            "outer_folds": 5,
            "minimum_folds": 2,
            "split_unit": "finalized_leakage_component",
            "calibration": "cross_fitted_sigmoid_on_oof_margin",
            "population_evidence": "wave_b_cross_fitted_predictions_with_design_weights",
            "confidence_level": 0.95,
            "component_bootstrap_repetitions": 2000,
        }
        or keep_grid != {"start": 0.01, "stop": 0.49, "step": 0.01}
        or exclude_grid != {"start": 0.51, "stop": 0.99, "step": 0.01}
        or routing["minimum_raw_tail_count"] != 30
        or routing["maximum_auto_exclude_related_rate"] != 0.02
        or routing["maximum_auto_keep_unrelated_rate"] != 0.05
        or routing["uncertain_policy"] != "adverse_in_automatic_tail"
        or routing["historical_manual_rate"] != 0.2036
        or routing["material_improvement_percentage_points"] != 3.0
        or tuple(routing["selection_order"]) != _SELECTION_ORDER
        or audit
        != {
            "sample_count_per_tail": 150,
            "sampling_method": "simple_random_without_replacement_within_action_tail",
            "auto_exclude_maximum_adverse_events": 3,
            "auto_keep_maximum_adverse_events": 7,
            "confidence_level": 0.95,
            "interval_method": "one_sided_clopper_pearson_and_component_bootstrap",
            "replacement_or_dilution_after_failure_allowed": False,
            "failed_tail_action": "route_failed_tail_to_manual",
            "task_columns": list(_TASK_COLUMNS),
            "task_encoding": "utf-8-sig",
        }
        or guards
        != {
            "source_database_read_only": True,
            "private_artifacts_may_enter_git": False,
            "formal_inference_fit_call_count": 0,
            "old_locked_test_may_reopen": False,
            "audit_is_only_independent_release_evidence": True,
            "automatic_source_deletion_allowed": False,
            "platform_used": False,
        }
    ):
        raise ModelRetrainingConfigError("model_retraining_plan_invalid")
    for key, length in {
        "candidate_build_id": 32,
        "leakage_build_id": 32,
        "reference_csv_sha256": 64,
        "wave_a_completed_csv_sha256": 64,
        "wave_a_private_map_sha256": 64,
        "wave_b_completed_csv_sha256": 64,
        "wave_b_private_map_sha256": 64,
        "wave_b_evaluation_id": 32,
        "wave_b_evaluation_manifest_sha256": 64,
        "old_qwen_model_id": 32,
        "old_locked_test_run_id": 32,
        "old_locked_test_manifest_sha256": 64,
    }.items():
        _hex(bindings.get(key), length, "model_retraining_bindings_invalid")
    _hex(
        encoder.get("base_plan_sha256"),
        64,
        "model_retraining_encoder_invalid",
    )
    keep_thresholds = _thresholds(0.01, 0.49, 0.01)
    exclude_thresholds = _thresholds(0.51, 0.99, 0.01)
    plan_sha256 = _canonical_sha256(root)
    fixed_candidates = (
        RetrainingCandidatePlan(
            "qwen_linear_svc", str(qwen["family"]), dict(qwen)
        ),
        RetrainingCandidatePlan(
            "sparse_linear_svc", str(sparse["family"]), dict(sparse)
        ),
        RetrainingCandidatePlan(
            "logit_fusion", str(fusion["family"]), dict(fusion)
        ),
    )
    return ModelRetrainingPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=20260825,
        candidate_build_id=str(bindings["candidate_build_id"]),
        leakage_build_id=str(bindings["leakage_build_id"]),
        reference_csv_sha256=str(bindings["reference_csv_sha256"]),
        wave_a_completed_csv_sha256=str(
            bindings["wave_a_completed_csv_sha256"]
        ),
        wave_a_private_map_sha256=str(bindings["wave_a_private_map_sha256"]),
        wave_b_completed_csv_sha256=str(
            bindings["wave_b_completed_csv_sha256"]
        ),
        wave_b_private_map_sha256=str(bindings["wave_b_private_map_sha256"]),
        wave_b_evaluation_id=str(bindings["wave_b_evaluation_id"]),
        wave_b_evaluation_manifest_sha256=str(
            bindings["wave_b_evaluation_manifest_sha256"]
        ),
        old_qwen_model_id=str(bindings["old_qwen_model_id"]),
        old_locked_test_run_id=str(bindings["old_locked_test_run_id"]),
        old_locked_test_manifest_sha256=str(
            bindings["old_locked_test_manifest_sha256"]
        ),
        expected_training_count=1300,
        expected_related_count=585,
        expected_unrelated_count=715,
        expected_origin_counts={key: int(value) for key, value in origins.items()},
        qwen_base_plan_sha256=str(encoder["base_plan_sha256"]),
        qwen_repository=str(encoder["repository"]),
        qwen_revision=str(encoder["revision"]),
        qwen_instruction=str(encoder["instruction"]),
        qwen_prompt_template=str(encoder["prompt_template"]),
        qwen_max_length=2048,
        qwen_embedding_dimension=2560,
        qwen_device="mps",
        qwen_batch_size=1,
        candidates=fixed_candidates,
        outer_folds=5,
        minimum_folds=2,
        confidence_level=0.95,
        bootstrap_repetitions=2000,
        keep_thresholds=keep_thresholds,
        exclude_thresholds=exclude_thresholds,
        minimum_raw_tail_count=30,
        maximum_auto_exclude_related_rate=0.02,
        maximum_auto_keep_unrelated_rate=0.05,
        historical_manual_rate=0.2036,
        material_improvement_percentage_points=3.0,
        selection_order=_SELECTION_ORDER,
        audit_sample_count_per_tail=150,
        audit_auto_exclude_maximum_adverse_events=3,
        audit_auto_keep_maximum_adverse_events=7,
        audit_task_columns=_TASK_COLUMNS,
    )
