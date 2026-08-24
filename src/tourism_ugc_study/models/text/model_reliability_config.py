"""新盲标样本成对评价两个冻结文本模型的严格计划解析。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ModelReliabilityConfigError(RuntimeError):
    """评价计划违反冻结契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("formal model reliability configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class FrozenModelBinding:
    """一个只允许调用 ``predict`` 的既有模型身份。"""

    run_id: str
    model_id: str
    package_manifest_sha256: str


@dataclass(frozen=True)
class ModelReliabilityPlan:
    """内容寻址、禁止提前训练的两波模型评价计划。"""

    plan_id: str
    plan_sha256: str
    random_seed: int
    candidate_build_id: str
    leakage_build_id: str
    reference_csv_sha256: str
    expected_candidate_count: int
    expected_candidate_component_count: int
    expected_reference_count: int
    expected_reference_component_count: int
    expected_eligible_count: int
    expected_eligible_component_count: int
    sparse: FrozenModelBinding
    qwen: FrozenModelBinding
    wave_a_sample_size: int
    wave_a_allocations: Mapping[str, int]
    probability_grid: tuple[float, ...]
    coverage_grid: tuple[float, ...]
    wave_b_maximum_sample_size: int
    wave_b_allocations: Mapping[str, int]
    repeat_wait_days: int
    random_control_fraction: float
    raw_agreement_minimum: float
    per_label_agreement_minimum: float
    confidence_level: float


_EXPECTED_WAVE_A = {
    "both_p_ge_0_90": 35,
    "qwen_only_p_ge_0_90": 45,
    "sparse_only_p_ge_0_90": 45,
    "action_disagreement_at_0_50": 45,
    "both_p_ge_0_50_remainder": 35,
    "both_p_lt_0_50": 35,
}
_EXPECTED_WAVE_B = {
    "both_exclude": 180,
    "selected_only": 100,
    "comparator_only": 60,
    "neither": 20,
}


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑且不接受 NaN 的规范 JSON 摘要。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping(value: Any, fields: set[str], reason_code: str) -> Mapping[str, Any]:
    """要求节点是精确键集合的映射，避免静默接受协议漂移。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelReliabilityConfigError(reason_code)
    return value


def _hex(value: Any, length: int, reason_code: str) -> str:
    """要求固定长度的小写十六进制身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelReliabilityConfigError(reason_code)
    return value


def _model_binding(value: Any, *, qwen: bool) -> FrozenModelBinding:
    """校验 sparse 或 Qwen 运行包的冻结身份。"""

    fields = {"run_id", "model_id", "package_manifest_sha256"}
    if qwen:
        fields.add("role")
    raw = _mapping(value, fields, "model_reliability_model_binding_invalid")
    if qwen and raw.get("role") != "research_comparator_failed_development_gate":
        raise ModelReliabilityConfigError("model_reliability_qwen_role_invalid")
    return FrozenModelBinding(
        run_id=_hex(raw.get("run_id"), 32, "model_reliability_model_binding_invalid"),
        model_id=_hex(
            raw.get("model_id"), 32, "model_reliability_model_binding_invalid"
        ),
        package_manifest_sha256=_hex(
            raw.get("package_manifest_sha256"),
            64,
            "model_reliability_model_binding_invalid",
        ),
    )


def load_model_reliability_plan(path: str | Path) -> ModelReliabilityPlan:
    """加载并严格校验 Issue #46 的新标签评价计划。

    Args:
        path: 冻结 YAML 路径。

    Returns:
        绑定候选人口、两个模型、两波样本量与禁止训练边界的计划。

    Raises:
        ModelReliabilityConfigError: 文件不可读、字段漂移或安全门被放宽。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelReliabilityConfigError(
            "model_reliability_config_unreadable"
        ) from exc
    root = _mapping(
        loaded,
        {
            "artifact_kind",
            "status",
            "random_seed",
            "population",
            "models",
            "wave_a",
            "wave_b",
            "annotation",
            "analysis",
            "guards",
        },
        "model_reliability_config_invalid",
    )
    population = _mapping(
        root["population"],
        {
            "candidate_build_id",
            "leakage_build_id",
            "reference_csv_sha256",
            "exclusion_rule",
            "expected_candidate_count",
            "expected_candidate_component_count",
            "expected_reference_count",
            "expected_reference_component_count",
            "expected_eligible_count",
            "expected_eligible_component_count",
            "platform_used",
        },
        "model_reliability_population_invalid",
    )
    models = _mapping(
        root["models"],
        {"sparse", "qwen", "scoring_contract"},
        "model_reliability_models_invalid",
    )
    wave_a = _mapping(
        root["wave_a"],
        {
            "sample_size",
            "strata",
            "short_stratum_rule",
            "probability_grid",
            "coverage_grid",
            "role",
        },
        "model_reliability_wave_a_invalid",
    )
    wave_b = _mapping(
        root["wave_b"],
        {"maximum_sample_size", "strata", "prerequisite", "confirmation_rule"},
        "model_reliability_wave_b_invalid",
    )
    annotation = _mapping(
        root["annotation"],
        {
            "labels",
            "hidden_fields",
            "repeat_wait_days",
            "random_control_fraction",
            "repeat_completion_minimum",
            "raw_agreement_minimum",
            "per_label_agreement_minimum",
        },
        "model_reliability_annotation_invalid",
    )
    analysis = _mapping(
        root["analysis"],
        {
            "estimand",
            "primary_weighting",
            "paired_comparison",
            "confidence_level",
            "rare_event_interval",
            "variance_sensitivity",
            "uncertain_policy",
        },
        "model_reliability_analysis_invalid",
    )
    guards = _mapping(
        root["guards"],
        {
            "test_status",
            "threshold_status",
            "audit_status",
            "auto_cleaning_decisions_present",
            "labels_may_enter_fit_before_sealed_evaluation",
            "post_evaluation_training_requires_researcher_decision",
        },
        "model_reliability_guards_invalid",
    )
    if (
        root.get("artifact_kind")
        != "formal-cleaning-model-reliability-study-plan"
        or root.get("status") != "frozen_evaluation_only"
        or root.get("random_seed") != 20260824
        or population.get("candidate_build_id")
        != "f58a671d0160ba418b3a1589831fa0d6"
        or population.get("leakage_build_id")
        != "6048c9aba31e020cb2fae9afb0b676e2"
        or population.get("exclusion_rule")
        != "exclude_every_component_touching_final_reference"
        or population.get("expected_candidate_count") != 13858
        or population.get("expected_candidate_component_count") != 8087
        or population.get("expected_reference_count") != 700
        or population.get("expected_reference_component_count") != 587
        or population.get("expected_eligible_count") != 10103
        or population.get("expected_eligible_component_count") != 7500
        or population.get("platform_used") is not False
        or models.get("scoring_contract")
        != "predict_only_no_fit_no_recalibration"
        or wave_a.get("sample_size") != 240
        or dict(wave_a.get("strata", {})) != _EXPECTED_WAVE_A
        or wave_a.get("short_stratum_rule")
        != "census_then_deterministic_proportional_redistribution"
        or wave_a.get("probability_grid") != [0.90, 0.95, 0.975, 0.99]
        or wave_a.get("coverage_grid") != [0.25, 0.40, 0.50, 0.60]
        or wave_a.get("role") != "exploratory_model_comparison_only"
        or wave_b.get("maximum_sample_size") != 360
        or dict(wave_b.get("strata", {})) != _EXPECTED_WAVE_B
        or wave_b.get("prerequisite")
        != "wave_a_sealed_and_researcher_policy_frozen"
        or wave_b.get("confirmation_rule")
        != "absolute_ugc_risk_and_equal_coverage_noninferiority"
        or annotation.get("labels") != ["related", "unrelated", "uncertain"]
        or annotation.get("hidden_fields")
        != ["model_name", "probability", "stratum", "platform", "selection_reason"]
        or annotation.get("repeat_completion_minimum") != 1.0
        or analysis
        != {
            "estimand": "eligible_post_population_model_performance",
            "primary_weighting": "horvitz_thompson_hajek_by_sampling_stratum",
            "paired_comparison": True,
            "confidence_level": 0.95,
            "rare_event_interval": "one_sided_clopper_pearson_descriptive",
            "variance_sensitivity": "leakage_component_cluster_bootstrap",
            "uncertain_policy": "report_separately_no_binary_coercion",
        }
        or guards
        != {
            "test_status": "locked_not_opened",
            "threshold_status": "UNSET",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "labels_may_enter_fit_before_sealed_evaluation": False,
            "post_evaluation_training_requires_researcher_decision": True,
        }
    ):
        raise ModelReliabilityConfigError("model_reliability_plan_invalid")
    reference_sha256 = _hex(
        population.get("reference_csv_sha256"),
        64,
        "model_reliability_population_invalid",
    )
    sparse = _model_binding(models["sparse"], qwen=False)
    qwen = _model_binding(models["qwen"], qwen=True)
    expected_sparse = FrozenModelBinding(
        "ce19406cd132e55b2eb00531f5cc4cd3",
        "1b68baa8bef99d6b9d75b7bf3226cfb4",
        "9769da2175d217833d5183a0d261ea3fae0b809f1483c4a0dd266beb648681d9",
    )
    expected_qwen = FrozenModelBinding(
        "bdf73219d584edfcbeea02772716a90a",
        "e24fc7a65a5e0e52f383725e21607837",
        "e4736d350c4aa7ba5d1cbb8dc03f8c2565d30e31bc2e5389bdffd4dd0b1a625e",
    )
    if sparse != expected_sparse or qwen != expected_qwen:
        raise ModelReliabilityConfigError("model_reliability_model_binding_invalid")
    for key in (
        "repeat_wait_days",
        "random_control_fraction",
        "raw_agreement_minimum",
        "per_label_agreement_minimum",
    ):
        if isinstance(annotation.get(key), bool) or not isinstance(
            annotation.get(key), (int, float)
        ):
            raise ModelReliabilityConfigError(
                "model_reliability_annotation_invalid"
            )
    if (
        annotation["repeat_wait_days"] != 14
        or annotation["random_control_fraction"] != 0.25
        or annotation["raw_agreement_minimum"] != 0.90
        or annotation["per_label_agreement_minimum"] != 0.80
    ):
        raise ModelReliabilityConfigError("model_reliability_annotation_invalid")
    plan_sha256 = _canonical_sha256(root)
    return ModelReliabilityPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=int(root["random_seed"]),
        candidate_build_id=str(population["candidate_build_id"]),
        leakage_build_id=str(population["leakage_build_id"]),
        reference_csv_sha256=reference_sha256,
        expected_candidate_count=13858,
        expected_candidate_component_count=8087,
        expected_reference_count=700,
        expected_reference_component_count=587,
        expected_eligible_count=10103,
        expected_eligible_component_count=7500,
        sparse=sparse,
        qwen=qwen,
        wave_a_sample_size=240,
        wave_a_allocations=dict(_EXPECTED_WAVE_A),
        probability_grid=(0.90, 0.95, 0.975, 0.99),
        coverage_grid=(0.25, 0.40, 0.50, 0.60),
        wave_b_maximum_sample_size=360,
        wave_b_allocations=dict(_EXPECTED_WAVE_B),
        repeat_wait_days=14,
        random_control_fraction=0.25,
        raw_agreement_minimum=0.90,
        per_label_agreement_minimum=0.80,
        confidence_level=0.95,
    )
