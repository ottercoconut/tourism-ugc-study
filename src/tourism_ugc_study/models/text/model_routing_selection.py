"""Wave A 标签上的三段式双阈值风险、工作量与 Pareto 选择分析。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.stats import beta

from .model_reliability_evaluation import LabeledWaveMember
from .model_reliability_study import ScoredEvaluationMember
from .model_routing_selection_config import ModelRoutingSelectionPlan


class ModelRoutingSelectionError(RuntimeError):
    """双阈值分析输入或统计不变量失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、标签明细或成员身份的稳定失败码。"""

        super().__init__("formal model routing selection failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class _TailComputation:
    """一个模型尾部阈值的公开诊断与 bootstrap 风险向量。"""

    report: Mapping[str, Any]
    bootstrap_risks: np.ndarray


def _canonical_id(value: object) -> str:
    """为路由点生成不依赖平台或成员顺序的短内容身份。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _ratio(
    numerator: np.ndarray, denominator: np.ndarray, weights: np.ndarray
) -> float | None:
    """计算 Hájek 加权比例；有效分母为空时返回 ``None``。"""

    denominator_weight = float(np.sum(weights * denominator))
    if denominator_weight <= 0.0:
        return None
    return float(np.sum(weights * numerator) / denominator_weight)


def _effective_sample_size(mask: np.ndarray, weights: np.ndarray) -> float:
    """计算动作尾部的 Kish 有效样本量。"""

    selected = weights[mask]
    if len(selected) == 0:
        return 0.0
    squared_sum = float(np.sum(selected**2))
    if squared_sum <= 0.0:
        return 0.0
    return float(np.sum(selected) ** 2 / squared_sum)


def _one_sided_upper(errors: int, total: int, confidence: float) -> float | None:
    """计算原始尾部错误计数的一侧 Clopper–Pearson 描述上界。"""

    if total <= 0 or errors < 0 or errors > total:
        return None
    if errors == total:
        return 1.0
    return float(beta.ppf(confidence, errors + 1, total - errors))


def _bootstrap_multiplicity(
    members: Sequence[LabeledWaveMember], *, seed: int, repetitions: int
) -> np.ndarray:
    """一次性生成 component cluster bootstrap 成员倍数矩阵。

    所有模型和阈值共享同一重抽样矩阵，既减少重复计算，也保持成对差值
    真正使用相同的 leakage component 重抽样。
    """

    components = sorted({item.component_id for item in members})
    if len(components) < 2 or repetitions < 1:
        raise ModelRoutingSelectionError(
            "model_routing_selection_bootstrap_support_invalid"
        )
    component_index = {value: index for index, value in enumerate(components)}
    member_components = np.asarray(
        [component_index[item.component_id] for item in members], dtype=int
    )
    generator = np.random.default_rng(seed)
    draws = generator.integers(
        0, len(components), size=(repetitions, len(components)), endpoint=False
    )
    counts = np.zeros((repetitions, len(components)), dtype=np.int16)
    rows = np.repeat(np.arange(repetitions), len(components))
    np.add.at(counts, (rows, draws.ravel()), 1)
    return counts[:, member_components].astype(float, copy=False)


def _bootstrap_ratios(
    multiplicity: np.ndarray,
    numerator: np.ndarray,
    denominator: np.ndarray,
    weights: np.ndarray,
) -> np.ndarray:
    """在共享 component 重抽样上计算一组加权比例。"""

    numerator_values = multiplicity @ (weights * numerator)
    denominator_values = multiplicity @ (weights * denominator)
    values = np.full(len(numerator_values), np.nan, dtype=float)
    valid = denominator_values > 0.0
    values[valid] = numerator_values[valid] / denominator_values[valid]
    return values


def _interval(values: np.ndarray, confidence: float) -> Mapping[str, float | int | None]:
    """从有效 bootstrap 风险向量生成百分位区间。"""

    valid = values[np.isfinite(values)]
    if len(valid) == 0:
        return {"lower": None, "upper": None, "valid_repetitions": 0}
    alpha = 1.0 - confidence
    return {
        "lower": float(np.quantile(valid, alpha / 2.0)),
        "upper": float(np.quantile(valid, 1.0 - alpha / 2.0)),
        "valid_repetitions": int(len(valid)),
    }


def _tail_computation(
    members: Sequence[LabeledWaveMember],
    probabilities: np.ndarray,
    action_mask: np.ndarray,
    error_mask: np.ndarray,
    multiplicity: np.ndarray,
    *,
    threshold: float,
    tail: str,
    plan: ModelRoutingSelectionPlan,
) -> _TailComputation:
    """计算一个保留端或排除端阈值的风险、区间与证据警告。"""

    del probabilities  # 动作掩码已由调用方按冻结阈值生成。
    weights = np.asarray([item.analysis_weight for item in members], dtype=float)
    labels = np.asarray([item.tourism_label for item in members], dtype=str)
    determinate = labels != "uncertain"
    denominator = action_mask & determinate
    numerator = denominator & error_mask
    risks = _bootstrap_ratios(
        multiplicity,
        numerator.astype(float),
        denominator.astype(float),
        weights,
    )
    interval = _interval(risks, plan.confidence_level)
    raw_count = int(np.sum(denominator))
    raw_error_count = int(np.sum(numerator))
    effective_count = _effective_sample_size(denominator, weights)
    raw_upper = _one_sided_upper(
        raw_error_count, raw_count, plan.confidence_level
    )
    bootstrap_upper = interval["upper"]
    conservative_upper = max(
        value
        for value in (raw_upper, bootstrap_upper)
        if isinstance(value, (int, float)) and math.isfinite(value)
    ) if raw_upper is not None or bootstrap_upper is not None else None
    warning_reasons = []
    if raw_count < plan.minimum_raw_tail_count:
        warning_reasons.append("raw_tail_count_below_diagnostic_minimum")
    if effective_count < plan.minimum_effective_tail_count:
        warning_reasons.append("effective_tail_count_below_diagnostic_minimum")
    report = {
        "tail": tail,
        "threshold": float(threshold),
        "raw_count": raw_count,
        "raw_error_count": raw_error_count,
        "effective_sample_size": effective_count,
        "weighted_error_risk": _ratio(
            numerator.astype(float), denominator.astype(float), weights
        ),
        "component_bootstrap_interval": interval,
        "raw_one_sided_clopper_pearson_upper": raw_upper,
        "conservative_descriptive_upper": conservative_upper,
        "evidence_warning": bool(warning_reasons),
        "evidence_warning_reasons": warning_reasons,
        "warning_is_acceptance_gate": False,
    }
    return _TailComputation(report=report, bootstrap_risks=risks)


def _paired_tail_deltas(
    sparse: Mapping[float, _TailComputation],
    qwen: Mapping[float, _TailComputation],
    *,
    confidence: float,
) -> list[Mapping[str, Any]]:
    """报告同一阈值、同一 component 重抽样上的 Qwen−sparse 风险差。"""

    results = []
    for threshold in sorted(set(sparse) & set(qwen)):
        sparse_point = sparse[threshold]
        qwen_point = qwen[threshold]
        sparse_risk = sparse_point.report["weighted_error_risk"]
        qwen_risk = qwen_point.report["weighted_error_risk"]
        valid = np.isfinite(sparse_point.bootstrap_risks) & np.isfinite(
            qwen_point.bootstrap_risks
        )
        deltas = np.full(len(valid), np.nan, dtype=float)
        deltas[valid] = (
            qwen_point.bootstrap_risks[valid]
            - sparse_point.bootstrap_risks[valid]
        )
        results.append(
            {
                "threshold": float(threshold),
                "qwen_minus_sparse_weighted_error_risk": (
                    None
                    if sparse_risk is None or qwen_risk is None
                    else float(qwen_risk - sparse_risk)
                ),
                "component_bootstrap_interval": _interval(deltas, confidence),
            }
        )
    return results


def _strategy_point(
    members: Sequence[LabeledWaveMember],
    sample_probabilities: np.ndarray,
    population_probabilities: np.ndarray,
    keep: _TailComputation,
    exclude: _TailComputation,
    *,
    model_name: str,
    keep_threshold: float,
    exclude_threshold: float,
) -> Mapping[str, Any]:
    """组合两个尾部阈值，计算三段式人口工作量与自动决策错误。"""

    if keep_threshold >= exclude_threshold:
        raise ModelRoutingSelectionError(
            "model_routing_selection_threshold_order_invalid"
        )
    labels = np.asarray([item.tourism_label for item in members], dtype=str)
    weights = np.asarray([item.analysis_weight for item in members], dtype=float)
    determinate = labels != "uncertain"
    related = labels == "related"
    unrelated = labels == "unrelated"
    sample_keep = sample_probabilities <= keep_threshold
    sample_exclude = sample_probabilities >= exclude_threshold
    sample_auto = (sample_keep | sample_exclude) & determinate
    sample_error = (sample_keep & unrelated) | (sample_exclude & related)
    population_keep = population_probabilities <= keep_threshold
    population_exclude = population_probabilities >= exclude_threshold
    population_manual = ~(population_keep | population_exclude)
    population_count = len(population_probabilities)
    auto_error = _ratio(
        (sample_error & determinate).astype(float),
        sample_auto.astype(float),
        weights,
    )
    return {
        "point_id": _canonical_id(
            {
                "model": model_name,
                "t_keep": keep_threshold,
                "t_exclude": exclude_threshold,
            }
        ),
        "model": model_name,
        "t_keep": float(keep_threshold),
        "t_exclude": float(exclude_threshold),
        "population_count": population_count,
        "auto_keep_count": int(np.sum(population_keep)),
        "manual_review_count": int(np.sum(population_manual)),
        "auto_exclude_count": int(np.sum(population_exclude)),
        "auto_keep_rate": float(np.mean(population_keep)),
        "manual_review_rate": float(np.mean(population_manual)),
        "auto_exclude_rate": float(np.mean(population_exclude)),
        "automatic_coverage_rate": float(np.mean(~population_manual)),
        "auto_decision_raw_count": int(np.sum(sample_auto)),
        "auto_decision_raw_error_count": int(np.sum(sample_error & determinate)),
        "weighted_auto_decision_error_risk": auto_error,
        "weighted_auto_decision_accuracy": (
            None if auto_error is None else float(1.0 - auto_error)
        ),
        "auto_keep_unrelated_retention": dict(keep.report),
        "auto_exclude_related_loss": dict(exclude.report),
        "uncertain_count": int(np.sum(~determinate)),
        "is_routing_threshold": False,
    }


def _pareto_frontier(points: Sequence[Mapping[str, Any]]) -> list[str]:
    """按人工率和两端保守风险删除被支配点，不产生唯一推荐。"""

    objectives = []
    for point in points:
        keep_upper = point["auto_keep_unrelated_retention"][
            "conservative_descriptive_upper"
        ]
        exclude_upper = point["auto_exclude_related_loss"][
            "conservative_descriptive_upper"
        ]
        objectives.append(
            (
                float(point["manual_review_rate"]),
                1.0 if exclude_upper is None else float(exclude_upper),
                1.0 if keep_upper is None else float(keep_upper),
            )
        )
    frontier = []
    for index, point in enumerate(points):
        target = objectives[index]
        dominated = any(
            other_index != index
            and all(left <= right for left, right in zip(other, target))
            and any(left < right for left, right in zip(other, target))
            for other_index, other in enumerate(objectives)
        )
        if not dominated:
            frontier.append(str(point["point_id"]))
    return sorted(frontier)


def _workload_representatives(
    points: Sequence[Mapping[str, Any]], targets: Sequence[float]
) -> list[Mapping[str, Any]]:
    """为固定人工率目标选择最近点；不把它解释为最终阈值推荐。"""

    representatives = []
    for target in targets:
        point = min(
            points,
            key=lambda item: (
                abs(float(item["manual_review_rate"]) - target),
                float(
                    item["auto_exclude_related_loss"][
                        "conservative_descriptive_upper"
                    ]
                ),
                float(
                    item["auto_keep_unrelated_retention"][
                        "conservative_descriptive_upper"
                    ]
                ),
                float(item["t_keep"]),
                float(item["t_exclude"]),
            ),
        )
        representatives.append(
            {
                "target_manual_review_rate": float(target),
                "point_id": point["point_id"],
                "realized_manual_review_rate": point["manual_review_rate"],
                "t_keep": point["t_keep"],
                "t_exclude": point["t_exclude"],
                "auto_keep_rate": point["auto_keep_rate"],
                "auto_exclude_rate": point["auto_exclude_rate"],
                "weighted_keep_error_risk": point[
                    "auto_keep_unrelated_retention"
                ]["weighted_error_risk"],
                "weighted_exclude_error_risk": point[
                    "auto_exclude_related_loss"
                ]["weighted_error_risk"],
                "weighted_auto_decision_accuracy": point[
                    "weighted_auto_decision_accuracy"
                ],
            }
        )
    return representatives


def _equal_workload_comparison(
    sparse: Sequence[Mapping[str, Any]], qwen: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """在相同预登记人工率目标下报告 Qwen−sparse 描述差。"""

    results = []
    for sparse_point, qwen_point in zip(sparse, qwen, strict=True):
        results.append(
            {
                "target_manual_review_rate": qwen_point[
                    "target_manual_review_rate"
                ],
                "sparse": dict(sparse_point),
                "qwen": dict(qwen_point),
                "qwen_minus_sparse_realized_manual_review_rate": float(
                    qwen_point["realized_manual_review_rate"]
                    - sparse_point["realized_manual_review_rate"]
                ),
                "qwen_minus_sparse_weighted_keep_error_risk": float(
                    qwen_point["weighted_keep_error_risk"]
                    - sparse_point["weighted_keep_error_risk"]
                ),
                "qwen_minus_sparse_weighted_exclude_error_risk": float(
                    qwen_point["weighted_exclude_error_risk"]
                    - sparse_point["weighted_exclude_error_risk"]
                ),
                "qwen_minus_sparse_weighted_auto_decision_accuracy": float(
                    qwen_point["weighted_auto_decision_accuracy"]
                    - sparse_point["weighted_auto_decision_accuracy"]
                ),
            }
        )
    return results


def analyze_model_routing_grid(
    members: Sequence[LabeledWaveMember],
    scored_population: Sequence[ScoredEvaluationMember],
    *,
    plan: ModelRoutingSelectionPlan,
    overall_metrics: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """分析两个冻结模型的256组三段式路由策略。

    Args:
        members: Wave A 的240条设计加权完成标签。
        scored_population: 两模型对10,103条评价人口的封存概率。
        plan: 标签打开前网格及证据绑定的独立分析计划。
        overall_metrics: 已封存基础评价的两模型总体概率指标。

    Returns:
        完整策略表、尾部风险、成对差、等工作量比较和 Pareto 前沿。

    Raises:
        ModelRoutingSelectionError: 成员、概率、权重或类别支持无效。
    """

    if (
        len(members) == 0
        or len(scored_population) == 0
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
        raise ModelRoutingSelectionError(
            "model_routing_selection_members_invalid"
        )
    labels = {item.tourism_label for item in members}
    if not {"related", "unrelated"}.issubset(labels):
        raise ModelRoutingSelectionError(
            "model_routing_selection_class_support_insufficient"
        )
    multiplicity = _bootstrap_multiplicity(
        members,
        seed=plan.random_seed,
        repetitions=plan.bootstrap_repetitions,
    )
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
    labels_array = np.asarray([item.tourism_label for item in members], dtype=str)
    related = labels_array == "related"
    unrelated = labels_array == "unrelated"
    keep_diagnostics: dict[str, dict[float, _TailComputation]] = {}
    exclude_diagnostics: dict[str, dict[float, _TailComputation]] = {}
    strategies: dict[str, list[Mapping[str, Any]]] = {}
    representatives: dict[str, list[Mapping[str, Any]]] = {}
    for model_name in ("sparse", "qwen"):
        sample = sample_probabilities[model_name]
        population = population_probabilities[model_name]
        keep_diagnostics[model_name] = {
            threshold: _tail_computation(
                members,
                sample,
                sample <= threshold,
                unrelated,
                multiplicity,
                threshold=threshold,
                tail="auto_keep",
                plan=plan,
            )
            for threshold in plan.keep_thresholds
        }
        exclude_diagnostics[model_name] = {
            threshold: _tail_computation(
                members,
                sample,
                sample >= threshold,
                related,
                multiplicity,
                threshold=threshold,
                tail="auto_exclude",
                plan=plan,
            )
            for threshold in plan.exclude_thresholds
        }
        strategies[model_name] = [
            _strategy_point(
                members,
                sample,
                population,
                keep_diagnostics[model_name][keep_threshold],
                exclude_diagnostics[model_name][exclude_threshold],
                model_name=model_name,
                keep_threshold=keep_threshold,
                exclude_threshold=exclude_threshold,
            )
            for keep_threshold in plan.keep_thresholds
            for exclude_threshold in plan.exclude_thresholds
        ]
        representatives[model_name] = _workload_representatives(
            strategies[model_name], plan.workload_targets
        )
    all_points = strategies["sparse"] + strategies["qwen"]
    return {
        "artifact_kind": "formal-cleaning-model-routing-selection-report",
        "status": "WAVE_A_SELECTION_READY",
        "role": "model_and_dual_threshold_selection_evidence_only",
        "sample_count": len(members),
        "population_count": len(scored_population),
        "determinate_count": sum(
            item.tourism_label != "uncertain" for item in members
        ),
        "uncertain_count": sum(
            item.tourism_label == "uncertain" for item in members
        ),
        "threshold_pair_count_per_model": len(plan.keep_thresholds)
        * len(plan.exclude_thresholds),
        "overall_metrics": {name: dict(value) for name, value in overall_metrics.items()},
        "tail_diagnostics": {
            model_name: {
                "auto_keep": [
                    dict(keep_diagnostics[model_name][threshold].report)
                    for threshold in plan.keep_thresholds
                ],
                "auto_exclude": [
                    dict(exclude_diagnostics[model_name][threshold].report)
                    for threshold in plan.exclude_thresholds
                ],
            }
            for model_name in ("sparse", "qwen")
        },
        "paired_tail_risk_deltas": {
            "auto_keep": _paired_tail_deltas(
                keep_diagnostics["sparse"],
                keep_diagnostics["qwen"],
                confidence=plan.confidence_level,
            ),
            "auto_exclude": _paired_tail_deltas(
                exclude_diagnostics["sparse"],
                exclude_diagnostics["qwen"],
                confidence=plan.confidence_level,
            ),
        },
        "strategies": strategies,
        "workload_representatives": representatives,
        "equal_workload_comparison": _equal_workload_comparison(
            representatives["sparse"], representatives["qwen"]
        ),
        "pareto_frontier_point_ids": _pareto_frontier(all_points),
        "pareto_uses_conservative_descriptive_upper": True,
        "evidence_warning_is_acceptance_gate": False,
        "may_support_researcher_selection": True,
        "selected_model": None,
        "selected_t_keep": None,
        "selected_t_exclude": None,
        "may_freeze_threshold_automatically": False,
        "labels_entered_fit": False,
        "test_status": "locked_not_opened",
        "threshold_status": "UNSET",
        "audit_status": "UNSET",
        "auto_cleaning_decisions_present": False,
        "platform_used": False,
    }
