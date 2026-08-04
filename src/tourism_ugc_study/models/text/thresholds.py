"""只使用验证集选择 SVM margin 阈值并构建人工复核队列。"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ThresholdPlan:
    """正方向为 `unrelated` 的高风险和低风险边界。"""

    high_risk_threshold: float | None
    low_risk_threshold: float | None
    low_risk_enabled: bool
    high_risk_precision: float | None
    high_risk_recall: float | None
    low_risk_related_precision: float | None
    low_risk_related_recall: float | None


@dataclass(frozen=True)
class PredictionInput:
    """分流所需的帖子身份、平台和正向 margin。"""

    source_post_id: int
    source_version: int
    platform_key: str
    margin: float


@dataclass(frozen=True)
class RoutedPrediction:
    """模型建议及人工复核/低风险抽审要求；不是最终清洗决定。"""

    source_post_id: int
    source_version: int
    margin: float
    suggested_action: str
    requires_human_review: bool
    low_risk_audit_selected: bool


def _precision_recall(
    labels: Sequence[str],
    selected: Sequence[bool],
    positive_label: str,
) -> tuple[float, float] | None:
    selected_count = sum(selected)
    positive_count = sum(label == positive_label for label in labels)
    if selected_count == 0 or positive_count == 0:
        return None
    true_positive = sum(
        include and label == positive_label
        for label, include in zip(labels, selected, strict=True)
    )
    return true_positive / selected_count, true_positive / positive_count


def select_thresholds(
    labels: Sequence[str],
    margins: Sequence[float],
    *,
    high_risk_precision_min: float,
    high_risk_recall_min: float,
    low_risk_related_precision_min: float,
    low_risk_related_recall_min: float,
) -> ThresholdPlan:
    """在验证集上按预声明的精确率/召回率目标选择两个边界。

    高风险边界选择满足 `unrelated` 目标的最低 margin，以保留尽可能多的
    人工高风险召回；低风险边界选择满足 `related` 目标的最高 margin。
    任一目标无解时不伪造阈值，特别是低风险自动保留会被禁用。
    """

    if len(labels) != len(margins) or not labels:
        raise ValueError("non-empty paired labels and margins are required")
    if set(labels) != {"related", "unrelated"}:
        raise ValueError("validation set must contain both relevance classes")
    thresholds = sorted(set(float(value) for value in margins))
    high_candidates: list[tuple[float, float, float]] = []
    low_candidates: list[tuple[float, float, float]] = []
    for threshold in thresholds:
        high_metrics = _precision_recall(
            labels,
            [margin >= threshold for margin in margins],
            "unrelated",
        )
        if (
            high_metrics is not None
            and high_metrics[0] >= high_risk_precision_min
            and high_metrics[1] >= high_risk_recall_min
        ):
            high_candidates.append((threshold, *high_metrics))
        low_metrics = _precision_recall(
            labels,
            [margin <= threshold for margin in margins],
            "related",
        )
        if (
            low_metrics is not None
            and low_metrics[0] >= low_risk_related_precision_min
            and low_metrics[1] >= low_risk_related_recall_min
        ):
            low_candidates.append((threshold, *low_metrics))
    high = min(high_candidates, key=lambda item: item[0]) if high_candidates else None
    low = max(low_candidates, key=lambda item: item[0]) if low_candidates else None
    low_enabled = low is not None and (high is None or low[0] < high[0])
    return ThresholdPlan(
        high_risk_threshold=high[0] if high else None,
        low_risk_threshold=low[0] if low_enabled and low else None,
        low_risk_enabled=low_enabled,
        high_risk_precision=high[1] if high else None,
        high_risk_recall=high[2] if high else None,
        low_risk_related_precision=low[1] if low_enabled and low else None,
        low_risk_related_recall=low[2] if low_enabled and low else None,
    )


def _audit_rank(random_seed: int, model_run_id: str, item: PredictionInput) -> str:
    payload = (
        f"{random_seed}:{model_run_id}:{item.platform_key}:"
        f"{item.source_post_id}:{item.source_version}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def route_predictions(
    predictions: Sequence[PredictionInput],
    *,
    thresholds: ThresholdPlan,
    random_seed: int,
    model_run_id: str,
    low_risk_audit_fraction: float,
    low_risk_audit_min_per_platform: int,
) -> tuple[RoutedPrediction, ...]:
    """把模型 margin 转为候选动作，并固定各平台低风险抽审清单。

    高风险和中间区间全部要求人工复核；低风险候选按
    `min(Np, max(ceil(f*Np), minimum))` 抽审。输出不含 `exclude`，因此模型
    无法越过人工仲裁直接形成清洗决定。
    """

    actions: dict[tuple[int, int], str] = {}
    low_by_platform: dict[str, list[PredictionInput]] = defaultdict(list)
    for item in predictions:
        if (
            thresholds.high_risk_threshold is not None
            and item.margin >= thresholds.high_risk_threshold
        ):
            action = "high_risk_review"
        elif (
            thresholds.low_risk_enabled
            and thresholds.low_risk_threshold is not None
            and item.margin <= thresholds.low_risk_threshold
        ):
            action = "low_risk_keep_candidate"
            low_by_platform[item.platform_key].append(item)
        else:
            action = "manual_review"
        actions[(item.source_post_id, item.source_version)] = action

    audited: set[tuple[int, int]] = set()
    for platform, items in low_by_platform.items():
        sample_size = min(
            len(items),
            max(math.ceil(low_risk_audit_fraction * len(items)), low_risk_audit_min_per_platform),
        )
        selected = sorted(
            items,
            key=lambda item: _audit_rank(random_seed, model_run_id, item),
        )[:sample_size]
        audited.update((item.source_post_id, item.source_version) for item in selected)
    return tuple(
        RoutedPrediction(
            source_post_id=item.source_post_id,
            source_version=item.source_version,
            margin=item.margin,
            suggested_action=actions[(item.source_post_id, item.source_version)],
            requires_human_review=(
                actions[(item.source_post_id, item.source_version)]
                != "low_risk_keep_candidate"
                or (item.source_post_id, item.source_version) in audited
            ),
            low_risk_audit_selected=(item.source_post_id, item.source_version) in audited,
        )
        for item in sorted(
            predictions, key=lambda value: (value.source_post_id, value.source_version)
        )
    )
