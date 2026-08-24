"""Wave A 新标签的设计加权成对模型评价与不确定性估计。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from scipy.stats import beta
from sklearn.metrics import average_precision_score, log_loss

from .model_reliability_study import ScoredEvaluationMember, WaveSampleMember


class ModelReliabilityEvaluationError(RuntimeError):
    """标签、权重或统计评价违反冻结契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("formal model reliability evaluation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class LabeledWaveMember:
    """一条已完成初标并与隐藏模型映射绑定的 Wave A 成员。"""

    task_id: str
    source_post_id: int
    source_version: int
    component_id: str
    stratum: str
    inclusion_probability: float
    analysis_weight: float
    sparse_p_unrelated: float
    qwen_p_unrelated: float
    tourism_label: str


def _ratio(numerator: np.ndarray, denominator: np.ndarray, weights: np.ndarray) -> float | None:
    """计算 Hájek 加权比例；无有效分母时返回 ``None``。"""

    denominator_weight = float(np.sum(weights * denominator))
    if denominator_weight <= 0.0:
        return None
    return float(np.sum(weights * numerator) / denominator_weight)


def _one_sided_clopper_pearson_upper(errors: int, total: int, confidence: float) -> float | None:
    """给出原始罕见事件计数的一侧 Clopper–Pearson 描述上界。"""

    if total <= 0 or errors < 0 or errors > total:
        return None
    if errors == total:
        return 1.0
    return float(beta.ppf(confidence, errors + 1, total - errors))


def _component_bootstrap_interval(
    members: Sequence[LabeledWaveMember],
    statistic: Callable[[np.ndarray], float | None],
    *,
    seed: int,
    repetitions: int = 2000,
    confidence: float = 0.95,
) -> Mapping[str, float | int | None]:
    """按 leakage component 重抽样，报告统计量的百分位敏感性区间。"""

    components = sorted({item.component_id for item in members})
    indices_by_component = {
        component: np.asarray(
            [index for index, item in enumerate(members) if item.component_id == component],
            dtype=int,
        )
        for component in components
    }
    if len(components) < 2:
        return {"lower": None, "upper": None, "valid_repetitions": 0}
    generator = np.random.default_rng(seed)
    estimates: list[float] = []
    for _ in range(repetitions):
        sampled_components = generator.choice(
            components, size=len(components), replace=True
        )
        indices = np.concatenate(
            [indices_by_component[str(component)] for component in sampled_components]
        )
        value = statistic(indices)
        if value is not None and math.isfinite(value):
            estimates.append(float(value))
    if not estimates:
        return {"lower": None, "upper": None, "valid_repetitions": 0}
    alpha = 1.0 - confidence
    return {
        "lower": float(np.quantile(estimates, alpha / 2.0)),
        "upper": float(np.quantile(estimates, 1.0 - alpha / 2.0)),
        "valid_repetitions": len(estimates),
    }


def _threshold_diagnostics(
    members: Sequence[LabeledWaveMember],
    probabilities: np.ndarray,
    threshold: float,
    *,
    seed: int,
    confidence: float,
) -> Mapping[str, Any]:
    """计算一个冻结概率阈值下的加权覆盖率与 UGC 误排风险。"""

    weights = np.asarray([item.analysis_weight for item in members], dtype=float)
    labels = np.asarray([item.tourism_label for item in members], dtype=str)
    determinate = labels != "uncertain"
    selected = probabilities >= threshold
    related = labels == "related"
    selected_determinate = selected & determinate
    selected_count = int(np.sum(selected_determinate))
    related_error_count = int(np.sum(selected & related))

    def statistic(indices: np.ndarray) -> float | None:
        selected_indices = selected_determinate[indices].astype(float)
        error_indices = (selected & related)[indices].astype(float)
        return _ratio(error_indices, selected_indices, weights[indices])

    return {
        "threshold": float(threshold),
        "is_routing_threshold": False,
        "determinate_count": int(np.sum(determinate)),
        "selected_count": selected_count,
        "related_error_count": related_error_count,
        "weighted_coverage": _ratio(
            selected_determinate.astype(float), determinate.astype(float), weights
        ),
        "weighted_related_risk_in_selected": statistic(
            np.arange(len(members), dtype=int)
        ),
        "raw_one_sided_clopper_pearson_upper": _one_sided_clopper_pearson_upper(
            related_error_count, selected_count, confidence
        ),
        "component_bootstrap_interval": _component_bootstrap_interval(
            members,
            statistic,
            seed=seed,
            confidence=confidence,
        ),
    }


def _overall_metrics(
    members: Sequence[LabeledWaveMember], probabilities: np.ndarray
) -> Mapping[str, Any]:
    """计算排除 uncertain 后的加权概率、排序与0.5诊断指标。"""

    labels = np.asarray([item.tourism_label for item in members], dtype=str)
    weights = np.asarray([item.analysis_weight for item in members], dtype=float)
    determinate = labels != "uncertain"
    y = (labels[determinate] == "unrelated").astype(int)
    p = probabilities[determinate]
    w = weights[determinate]
    if len(y) == 0 or set(y.tolist()) != {0, 1}:
        raise ModelReliabilityEvaluationError(
            "model_reliability_evaluation_class_support_insufficient"
        )
    predicted = p >= 0.50
    related = y == 0
    unrelated = y == 1
    total_weight = float(np.sum(w))
    return {
        "determinate_count": int(np.sum(determinate)),
        "uncertain_count": int(np.sum(~determinate)),
        "weighted_log_loss": float(log_loss(y, p, sample_weight=w, labels=[0, 1])),
        "weighted_brier_score": float(np.average((p - y) ** 2, weights=w)),
        "weighted_pr_auc_unrelated": float(
            average_precision_score(y, p, sample_weight=w)
        ),
        "diagnostic_cutoff": 0.50,
        "diagnostic_cutoff_is_routing_threshold": False,
        "weighted_accuracy": float(np.sum(w * (predicted == y)) / total_weight),
        "weighted_related_to_unrelated_rate": float(
            np.sum(w * (predicted & related)) / np.sum(w * related)
        ),
        "weighted_unrelated_to_related_rate": float(
            np.sum(w * (~predicted & unrelated)) / np.sum(w * unrelated)
        ),
    }


def _coverage_threshold(probabilities: Sequence[float], coverage: float) -> float:
    """在完整合格人口上确定至少达到目标覆盖率的稳定概率切点。"""

    values = np.asarray(probabilities, dtype=float)
    if values.ndim != 1 or len(values) == 0 or not 0.0 < coverage < 1.0:
        raise ModelReliabilityEvaluationError(
            "model_reliability_coverage_grid_invalid"
        )
    # method=higher 选择经验分布上的实际分数；nextafter 向下包含该分数并在
    # 大量同分时如实产生略高于目标的覆盖率，而不随机拆分同分成员。
    threshold = float(np.quantile(values, 1.0 - coverage, method="higher"))
    return float(np.nextafter(threshold, -np.inf))


def evaluate_wave_a(
    members: Sequence[LabeledWaveMember],
    scored_population: Sequence[ScoredEvaluationMember],
    *,
    probability_grid: Sequence[float],
    coverage_grid: Sequence[float],
    random_seed: int,
    confidence_level: float,
) -> Mapping[str, Any]:
    """生成 Wave A 的探索性设计加权成对模型比较报告。

    Args:
        members: 240 条已完成初标、含隐藏纳入概率的同一批成员。
        scored_population: 两模型对完整合格人口的封存概率。
        probability_grid: 预登记固定概率诊断网格。
        coverage_grid: 预登记固定人口覆盖率网格。
        random_seed: component bootstrap 固定随机种子。
        confidence_level: 描述区间置信水平。

    Returns:
        不改变模型、阈值或训练状态的探索性报告。
    """

    if (
        not members
        or len({item.task_id for item in members}) != len(members)
        or any(
            item.tourism_label not in {"related", "unrelated", "uncertain"}
            or not 0.0 < item.inclusion_probability <= 1.0
            or not math.isclose(
                item.analysis_weight, 1.0 / item.inclusion_probability, rel_tol=1e-12
            )
            for item in members
        )
    ):
        raise ModelReliabilityEvaluationError(
            "model_reliability_completed_labels_invalid"
        )
    sparse = np.asarray([item.sparse_p_unrelated for item in members], dtype=float)
    qwen = np.asarray([item.qwen_p_unrelated for item in members], dtype=float)
    population_sparse = [item.sparse_p_unrelated for item in scored_population]
    population_qwen = [item.qwen_p_unrelated for item in scored_population]
    models = {
        "sparse": (sparse, population_sparse),
        "qwen": (qwen, population_qwen),
    }
    probability_results: dict[str, list[Mapping[str, Any]]] = {}
    coverage_results: dict[str, list[Mapping[str, Any]]] = {}
    overall: dict[str, Mapping[str, Any]] = {}
    for model_index, (name, (sample_probabilities, population_probabilities)) in enumerate(
        models.items()
    ):
        overall[name] = _overall_metrics(members, sample_probabilities)
        probability_results[name] = [
            _threshold_diagnostics(
                members,
                sample_probabilities,
                threshold,
                seed=random_seed + model_index * 10000 + index,
                confidence=confidence_level,
            )
            for index, threshold in enumerate(probability_grid)
        ]
        coverage_results[name] = []
        for index, coverage in enumerate(coverage_grid):
            threshold = _coverage_threshold(population_probabilities, coverage)
            item = dict(
                _threshold_diagnostics(
                    members,
                    sample_probabilities,
                    threshold,
                    seed=random_seed + model_index * 10000 + 100 + index,
                    confidence=confidence_level,
                )
            )
            item["target_population_coverage"] = float(coverage)
            item["realized_population_coverage"] = float(
                np.mean(np.asarray(population_probabilities) >= threshold)
            )
            coverage_results[name].append(item)
    paired_probability_deltas = []
    labels = np.asarray([item.tourism_label for item in members], dtype=str)
    weights = np.asarray([item.analysis_weight for item in members], dtype=float)
    determinate = labels != "uncertain"
    related = labels == "related"
    for index, threshold in enumerate(probability_grid):
        sparse_selected = (sparse >= threshold) & determinate
        qwen_selected = (qwen >= threshold) & determinate

        def delta_statistic(indices: np.ndarray) -> float | None:
            sparse_risk = _ratio(
                (sparse_selected & related)[indices].astype(float),
                sparse_selected[indices].astype(float),
                weights[indices],
            )
            qwen_risk = _ratio(
                (qwen_selected & related)[indices].astype(float),
                qwen_selected[indices].astype(float),
                weights[indices],
            )
            if sparse_risk is None or qwen_risk is None:
                return None
            return qwen_risk - sparse_risk

        paired_probability_deltas.append(
            {
                "threshold": float(threshold),
                "qwen_minus_sparse_weighted_related_risk": delta_statistic(
                    np.arange(len(members), dtype=int)
                ),
                "component_bootstrap_interval": _component_bootstrap_interval(
                    members,
                    delta_statistic,
                    seed=random_seed + 20000 + index,
                    confidence=confidence_level,
                ),
            }
        )
    return {
        "artifact_kind": "formal-cleaning-model-reliability-wave-a-report",
        "role": "exploratory_model_comparison_only",
        "sample_count": len(members),
        "determinate_count": sum(item.tourism_label != "uncertain" for item in members),
        "uncertain_count": sum(item.tourism_label == "uncertain" for item in members),
        "overall_metrics": overall,
        "fixed_probability_grid": probability_results,
        "fixed_coverage_grid": coverage_results,
        "paired_probability_risk_deltas": paired_probability_deltas,
        "confidence_level": confidence_level,
        "primary_weighting": "horvitz_thompson_hajek_by_sampling_stratum",
        "variance_sensitivity": "leakage_component_cluster_bootstrap",
        "rare_event_interval": "one_sided_clopper_pearson_descriptive_raw_count",
        "may_select_model": False,
        "may_freeze_threshold": False,
        "labels_entered_fit": False,
        "test_status": "locked_not_opened",
        "threshold_status": "UNSET",
        "audit_status": "UNSET",
    }
