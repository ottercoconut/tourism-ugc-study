"""Wave B交叉拟合概率上的风险门、Pareto前沿与唯一策略选择。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.special import xlogy
from scipy.stats import beta

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_training import RetrainingOofObservation


class ModelRetrainingRoutingError(RuntimeError):
    """Wave B OOF评价、阈值扫描或策略选择失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含成员、标签或概率明细的稳定失败码。"""

        super().__init__("formal model retraining routing failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RoutingRiskPoint:
    """一个候选与双阈值的设计加权风险—人工率证据。"""

    candidate_name: str
    candidate_id: str
    T_keep: float
    T_exclude: float
    raw_auto_keep_count: int
    raw_manual_review_count: int
    raw_auto_exclude_count: int
    auto_keep_adverse_count: int
    auto_exclude_adverse_count: int
    auto_keep_unrelated_rate: float
    auto_exclude_related_rate: float
    manual_review_rate: float
    auto_keep_raw_clopper_pearson_lower: float
    auto_keep_raw_clopper_pearson_upper: float
    auto_exclude_raw_clopper_pearson_lower: float
    auto_exclude_raw_clopper_pearson_upper: float
    wave_b_weighted_log_loss: float
    minimum_tail_support_passed: bool
    risk_gate_passed: bool


@dataclass(frozen=True)
class RoutingSelectionResult:
    """完整扫描中的Pareto前沿、唯一选择与区间敏感性。"""

    selected: RoutingRiskPoint
    pareto_frontier: tuple[RoutingRiskPoint, ...]
    scanned_point_count: int
    support_eligible_point_count: int
    risk_eligible_point_count: int
    historical_manual_rate: float
    manual_rate_reduction_percentage_points: float
    material_improvement: bool
    selected_component_bootstrap_intervals: Mapping[str, Mapping[str, float]]
    selection_order: tuple[str, ...]
    population_evidence: str


def _clopper_pearson(
    events: int, count: int, confidence_level: float
) -> tuple[float, float]:
    """计算原始事件率的双侧Clopper–Pearson区间。"""

    if count < 1 or events < 0 or events > count:
        return float("nan"), float("nan")
    alpha = 1.0 - confidence_level
    lower = 0.0 if events == 0 else float(beta.ppf(alpha / 2, events, count - events + 1))
    upper = 1.0 if events == count else float(beta.ppf(1 - alpha / 2, events + 1, count - events))
    return lower, upper


def _weighted_log_loss(labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray) -> float:
    """计算Wave B设计加权二元log loss。"""

    encoded = np.asarray(labels == "unrelated", dtype=float)
    clipped = np.clip(probabilities, 1e-15, 1 - 1e-15)
    losses = -(xlogy(encoded, clipped) + xlogy(1 - encoded, 1 - clipped))
    return float(np.sum(weights * losses) / np.sum(weights))


def _routing_point(
    rows: Sequence[RetrainingOofObservation],
    *,
    T_keep: float,
    T_exclude: float,
    weighted_log_loss: float,
    plan: ModelRetrainingPlan,
) -> RoutingRiskPoint:
    """评价一个双阈值，uncertain在相应自动尾部按不利事件计。"""

    probabilities = np.asarray([item.p_unrelated for item in rows], dtype=float)
    labels = np.asarray([item.tourism_label for item in rows], dtype=object)
    weights = np.asarray([item.analysis_weight for item in rows], dtype=float)
    keep = probabilities <= T_keep
    exclude = probabilities >= T_exclude
    manual = ~(keep | exclude)
    keep_adverse = keep & np.isin(labels, ["unrelated", "uncertain"])
    exclude_adverse = exclude & np.isin(labels, ["related", "uncertain"])
    keep_count = int(np.sum(keep))
    exclude_count = int(np.sum(exclude))
    keep_rate = (
        float(np.sum(weights[keep_adverse]) / np.sum(weights[keep]))
        if keep_count
        else float("nan")
    )
    exclude_rate = (
        float(np.sum(weights[exclude_adverse]) / np.sum(weights[exclude]))
        if exclude_count
        else float("nan")
    )
    manual_rate = float(np.sum(weights[manual]) / np.sum(weights))
    support = (
        keep_count >= plan.minimum_raw_tail_count
        and exclude_count >= plan.minimum_raw_tail_count
    )
    risk = (
        support
        and keep_rate <= plan.maximum_auto_keep_unrelated_rate
        and exclude_rate <= plan.maximum_auto_exclude_related_rate
    )
    keep_events = int(np.sum(keep_adverse))
    exclude_events = int(np.sum(exclude_adverse))
    keep_interval = _clopper_pearson(
        keep_events, keep_count, plan.confidence_level
    )
    exclude_interval = _clopper_pearson(
        exclude_events, exclude_count, plan.confidence_level
    )
    first = rows[0]
    return RoutingRiskPoint(
        candidate_name=first.candidate_name,
        candidate_id=first.candidate_id,
        T_keep=T_keep,
        T_exclude=T_exclude,
        raw_auto_keep_count=keep_count,
        raw_manual_review_count=int(np.sum(manual)),
        raw_auto_exclude_count=exclude_count,
        auto_keep_adverse_count=keep_events,
        auto_exclude_adverse_count=exclude_events,
        auto_keep_unrelated_rate=keep_rate,
        auto_exclude_related_rate=exclude_rate,
        manual_review_rate=manual_rate,
        auto_keep_raw_clopper_pearson_lower=keep_interval[0],
        auto_keep_raw_clopper_pearson_upper=keep_interval[1],
        auto_exclude_raw_clopper_pearson_lower=exclude_interval[0],
        auto_exclude_raw_clopper_pearson_upper=exclude_interval[1],
        wave_b_weighted_log_loss=weighted_log_loss,
        minimum_tail_support_passed=support,
        risk_gate_passed=risk,
    )


def _pareto_frontier(points: Sequence[RoutingRiskPoint]) -> tuple[RoutingRiskPoint, ...]:
    """保留在人工率和两端风险上不被另一点同时支配的组合。"""

    if not points:
        return ()
    values = np.asarray(
        [
            [
                item.manual_review_rate,
                item.auto_exclude_related_rate,
                item.auto_keep_unrelated_rate,
            ]
            for item in points
        ],
        dtype=float,
    )
    keep: list[RoutingRiskPoint] = []
    for index, point in enumerate(points):
        no_worse = np.all(values <= values[index], axis=1)
        strictly_better = np.any(values < values[index], axis=1)
        dominated = bool(np.any(no_worse & strictly_better))
        if not dominated:
            keep.append(point)
    return tuple(
        sorted(
            keep,
            key=lambda item: (
                item.manual_review_rate,
                item.auto_exclude_related_rate,
                item.auto_keep_unrelated_rate,
                item.candidate_id,
                item.T_keep,
                item.T_exclude,
            ),
        )
    )


def _selection_key(point: RoutingRiskPoint) -> tuple[Any, ...]:
    """实现冻结的人工量、安全风险、概率质量和简洁性决胜顺序。"""

    complexity = {
        "sparse_linear_svc": 1,
        "qwen_linear_svc": 2,
        "logit_fusion": 3,
    }
    try:
        rank = complexity[point.candidate_name]
    except KeyError as exc:
        raise ModelRetrainingRoutingError(
            "model_retraining_routing_candidate_invalid"
        ) from exc
    return (
        point.manual_review_rate,
        point.auto_exclude_related_rate,
        point.auto_keep_unrelated_rate,
        point.wave_b_weighted_log_loss,
        rank,
        point.candidate_id,
        point.T_keep,
        point.T_exclude,
    )


def _component_bootstrap(
    rows: Sequence[RetrainingOofObservation],
    selected: RoutingRiskPoint,
    *,
    plan: ModelRetrainingPlan,
) -> Mapping[str, Mapping[str, float]]:
    """按leakage component聚类重抽并报告两端加权风险敏感性区间。"""

    by_component: dict[str, list[RetrainingOofObservation]] = {}
    for row in rows:
        by_component.setdefault(row.component_id, []).append(row)
    component_ids = sorted(by_component)
    seed_payload = (
        f"{plan.random_seed}|{selected.candidate_id}|"
        f"{selected.T_keep:.2f}|{selected.T_exclude:.2f}"
    )
    seed = int(hashlib.sha256(seed_payload.encode()).hexdigest()[:16], 16)
    generator = np.random.default_rng(seed)
    keep_rates: list[float] = []
    exclude_rates: list[float] = []
    for _ in range(plan.bootstrap_repetitions):
        sampled = generator.choice(component_ids, size=len(component_ids), replace=True)
        sampled_rows = [row for component in sampled for row in by_component[str(component)]]
        probabilities = np.asarray([row.p_unrelated for row in sampled_rows])
        labels = np.asarray([row.tourism_label for row in sampled_rows], dtype=object)
        weights = np.asarray([row.analysis_weight for row in sampled_rows], dtype=float)
        keep = probabilities <= selected.T_keep
        exclude = probabilities >= selected.T_exclude
        if np.any(keep):
            adverse = keep & np.isin(labels, ["unrelated", "uncertain"])
            keep_rates.append(float(np.sum(weights[adverse]) / np.sum(weights[keep])))
        if np.any(exclude):
            adverse = exclude & np.isin(labels, ["related", "uncertain"])
            exclude_rates.append(float(np.sum(weights[adverse]) / np.sum(weights[exclude])))
    alpha = (1.0 - plan.confidence_level) / 2

    def interval(values: Sequence[float]) -> Mapping[str, float]:
        if not values:
            raise ModelRetrainingRoutingError(
                "model_retraining_routing_bootstrap_empty"
            )
        return {
            "lower": float(np.quantile(values, alpha)),
            "upper": float(np.quantile(values, 1 - alpha)),
            "repetitions": float(len(values)),
        }

    return {
        "auto_keep_unrelated_rate": interval(keep_rates),
        "auto_exclude_related_rate": interval(exclude_rates),
    }


def analyze_retraining_routing(
    observations: Sequence[RetrainingOofObservation],
    *,
    plan: ModelRetrainingPlan,
) -> RoutingSelectionResult:
    """扫描固定三候选和完整双阈值网格并选择唯一风险合格策略。"""

    candidate_names = {item.name for item in plan.candidates}
    by_candidate: dict[str, list[RetrainingOofObservation]] = {}
    for row in observations:
        if row.evidence_origin == "wave_b":
            by_candidate.setdefault(row.candidate_name, []).append(row)
    if set(by_candidate) != candidate_names:
        raise ModelRetrainingRoutingError(
            "model_retraining_routing_candidate_evidence_missing"
        )
    points: list[RoutingRiskPoint] = []
    for candidate_name in sorted(by_candidate):
        rows = by_candidate[candidate_name]
        if (
            len(rows) != plan.expected_origin_counts["wave_b"]
            or len({item.member_key for item in rows}) != len(rows)
            or any(
                item.analysis_weight is None
                or not np.isfinite(item.analysis_weight)
                or item.analysis_weight <= 0
                or not 0 <= item.p_unrelated <= 1
                for item in rows
            )
        ):
            raise ModelRetrainingRoutingError(
                "model_retraining_routing_wave_b_evidence_invalid"
            )
        labels = np.asarray([item.tourism_label for item in rows], dtype=object)
        probabilities = np.asarray([item.p_unrelated for item in rows], dtype=float)
        weights = np.asarray([item.analysis_weight for item in rows], dtype=float)
        log_loss = _weighted_log_loss(labels, probabilities, weights)
        for T_keep in plan.keep_thresholds:
            for T_exclude in plan.exclude_thresholds:
                points.append(
                    _routing_point(
                        rows,
                        T_keep=T_keep,
                        T_exclude=T_exclude,
                        weighted_log_loss=log_loss,
                        plan=plan,
                    )
                )
    support_eligible = [item for item in points if item.minimum_tail_support_passed]
    risk_eligible = [item for item in support_eligible if item.risk_gate_passed]
    if not risk_eligible:
        raise ModelRetrainingRoutingError(
            "model_retraining_routing_no_acceptable_strategy"
        )
    selected = min(risk_eligible, key=_selection_key)
    selected_rows = by_candidate[selected.candidate_name]
    reduction = (plan.historical_manual_rate - selected.manual_review_rate) * 100
    return RoutingSelectionResult(
        selected=selected,
        pareto_frontier=_pareto_frontier(support_eligible),
        scanned_point_count=len(points),
        support_eligible_point_count=len(support_eligible),
        risk_eligible_point_count=len(risk_eligible),
        historical_manual_rate=plan.historical_manual_rate,
        manual_rate_reduction_percentage_points=reduction,
        material_improvement=(
            reduction >= plan.material_improvement_percentage_points
        ),
        selected_component_bootstrap_intervals=_component_bootstrap(
            selected_rows, selected, plan=plan
        ),
        selection_order=plan.selection_order,
        population_evidence=(
            "wave_b_cross_fitted_predictions_with_design_weights"
        ),
    )


def routing_selection_as_dict(result: RoutingSelectionResult) -> Mapping[str, Any]:
    """把选择结果转换为禁止NaN的JSON友好映射。"""

    return asdict(result)
