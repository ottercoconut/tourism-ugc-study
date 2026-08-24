"""Wave B 对冻结 Qwen 双阈值策略的独立设计加权评价。"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from .model_reliability_evaluation import LabeledWaveMember, _overall_metrics
from .model_reliability_study import ScoredEvaluationMember
from .model_routing_policy_config import ModelRoutingPolicyPlan
from .model_routing_selection import (
    _bootstrap_multiplicity,
    _interval,
    _strategy_point,
    _tail_computation,
)
from .model_routing_selection_config import ModelRoutingSelectionPlan


class ModelWaveBEvaluationError(RuntimeError):
    """Wave B 标签、权重或冻结策略评价失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员、标签明细或本机路径的稳定失败码。"""

        super().__init__("formal model wave b evaluation failed")
        self.reason_code = reason_code


def _paired_risk_delta(
    qwen_risks: np.ndarray,
    sparse_risks: np.ndarray,
    *,
    qwen_point: float | None,
    sparse_point: float | None,
    confidence: float,
) -> Mapping[str, Any]:
    """在相同 component 重抽样上计算 Qwen−sparse 风险差。"""

    valid = np.isfinite(qwen_risks) & np.isfinite(sparse_risks)
    deltas = np.full(len(valid), np.nan, dtype=float)
    deltas[valid] = qwen_risks[valid] - sparse_risks[valid]
    return {
        "qwen_minus_sparse_weighted_error_risk": (
            None
            if qwen_point is None or sparse_point is None
            else float(qwen_point - sparse_point)
        ),
        "component_bootstrap_interval": _interval(deltas, confidence),
    }


def evaluate_wave_b_policy(
    members: Sequence[LabeledWaveMember],
    scored_population: Sequence[ScoredEvaluationMember],
    *,
    policy: ModelRoutingPolicyPlan,
    evidence_plan: ModelRoutingSelectionPlan,
) -> Mapping[str, Any]:
    """独立评价已冻结 Qwen 策略及等人工量 sparse comparator。

    Args:
        members: 360条 Wave B 完成标签与私有设计权重。
        scored_population: 排除 Wave A 分量后的9,410条封存概率。
        policy: 研究者在读取 Wave B 标签前确认的模型与双阈值。
        evidence_plan: 沿用 Wave A 预设的置信水平、bootstrap次数和警告线。

    Returns:
        总体模型指标、两个固定三段策略、两端风险差与证据充分性状态。

    Raises:
        ModelWaveBEvaluationError: 成员、类别、权重或冻结人口不满足契约。
    """

    if (
        len(members) != policy.wave_b_target_sample_count
        or len(scored_population) != policy.wave_b_eligible_population_count
        or len({item.task_id for item in members}) != len(members)
        or any(
            item.tourism_label not in {"related", "unrelated", "uncertain"}
            or not 0.0 < item.inclusion_probability <= 1.0
            or not math.isclose(
                item.analysis_weight,
                1.0 / item.inclusion_probability,
                rel_tol=1e-12,
            )
            for item in members
        )
    ):
        raise ModelWaveBEvaluationError("model_wave_b_evaluation_members_invalid")
    labels = np.asarray([item.tourism_label for item in members], dtype=str)
    if not {"related", "unrelated"}.issubset(set(labels)):
        raise ModelWaveBEvaluationError(
            "model_wave_b_evaluation_class_support_insufficient"
        )
    multiplicity = _bootstrap_multiplicity(
        members,
        seed=policy.random_seed,
        repetitions=evidence_plan.bootstrap_repetitions,
    )
    related = labels == "related"
    unrelated = labels == "unrelated"
    sample_probabilities = {
        "sparse": np.asarray(
            [item.sparse_p_unrelated for item in members], dtype=float
        ),
        "qwen": np.asarray(
            [item.qwen_p_unrelated for item in members], dtype=float
        ),
    }
    population_probabilities = {
        "sparse": np.asarray(
            [item.sparse_p_unrelated for item in scored_population], dtype=float
        ),
        "qwen": np.asarray(
            [item.qwen_p_unrelated for item in scored_population], dtype=float
        ),
    }
    strategies = {
        "qwen": policy.selected,
        "sparse": policy.comparator,
    }
    overall: dict[str, Mapping[str, Any]] = {}
    points: dict[str, Mapping[str, Any]] = {}
    tail_objects: dict[str, Mapping[str, Any]] = {}
    for model_name in ("sparse", "qwen"):
        strategy = strategies[model_name]
        sample = sample_probabilities[model_name]
        population = population_probabilities[model_name]
        overall[model_name] = _overall_metrics(members, sample)
        keep = _tail_computation(
            members,
            sample,
            sample <= strategy.t_keep,
            unrelated,
            multiplicity,
            threshold=strategy.t_keep,
            tail="auto_keep",
            plan=evidence_plan,
        )
        exclude = _tail_computation(
            members,
            sample,
            sample >= strategy.t_exclude,
            related,
            multiplicity,
            threshold=strategy.t_exclude,
            tail="auto_exclude",
            plan=evidence_plan,
        )
        point = dict(
            _strategy_point(
                members,
                sample,
                population,
                keep,
                exclude,
                model_name=model_name,
                keep_threshold=strategy.t_keep,
                exclude_threshold=strategy.t_exclude,
            )
        )
        point["is_routing_threshold"] = True
        point["point_id"] = strategy.point_id
        points[model_name] = point
        tail_objects[model_name] = {"keep": keep, "exclude": exclude}
    qwen_keep = tail_objects["qwen"]["keep"]
    sparse_keep = tail_objects["sparse"]["keep"]
    qwen_exclude = tail_objects["qwen"]["exclude"]
    sparse_exclude = tail_objects["sparse"]["exclude"]
    paired = {
        "auto_keep_unrelated_retention": _paired_risk_delta(
            qwen_keep.bootstrap_risks,
            sparse_keep.bootstrap_risks,
            qwen_point=qwen_keep.report["weighted_error_risk"],
            sparse_point=sparse_keep.report["weighted_error_risk"],
            confidence=evidence_plan.confidence_level,
        ),
        "auto_exclude_related_loss": _paired_risk_delta(
            qwen_exclude.bootstrap_risks,
            sparse_exclude.bootstrap_risks,
            qwen_point=qwen_exclude.report["weighted_error_risk"],
            sparse_point=sparse_exclude.report["weighted_error_risk"],
            confidence=evidence_plan.confidence_level,
        ),
    }
    warnings = [
        reason
        for model_name in ("qwen", "sparse")
        for tail_name in (
            "auto_keep_unrelated_retention",
            "auto_exclude_related_loss",
        )
        for reason in points[model_name][tail_name]["evidence_warning_reasons"]
    ]
    return {
        "artifact_kind": "formal-cleaning-model-wave-b-evaluation-report",
        "status": "WAVE_B_EVALUATION_COMPLETE",
        "evidence_status": (
            "evidence_insufficient"
            if warnings
            else "sufficient_for_descriptive_independent_evaluation"
        ),
        "role": "frozen_policy_independent_evaluation_only",
        "sample_count": len(members),
        "population_count": len(scored_population),
        "determinate_count": int(np.sum(labels != "uncertain")),
        "uncertain_count": int(np.sum(labels == "uncertain")),
        "overall_metrics": overall,
        "selected_strategy": points["qwen"],
        "matched_workload_comparator": points["sparse"],
        "paired_risk_deltas": paired,
        "evidence_warning_reasons": sorted(set(warnings)),
        "confidence_level": evidence_plan.confidence_level,
        "bootstrap_repetitions": evidence_plan.bootstrap_repetitions,
        "bootstrap_unit": "leakage_component",
        "primary_weighting": "horvitz_thompson_hajek_by_cross_action_stratum",
        "mechanical_acceptance_gate": False,
        "may_change_model_or_threshold": False,
        "labels_entered_fit": False,
        "fit_call_count": 0,
        "test_status": "locked_not_opened",
        "threshold_status": "FROZEN_FOR_WAVE_B_EVALUATION",
        "deployment_status": "NOT_AUTHORIZED",
        "audit_status": "UNSET",
        "auto_cleaning_decisions_present": False,
        "platform_used": False,
    }
