"""字符 TF-IDF 与 LinearSVC 的旅游无关内容基线和正式评估。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import average_precision_score, confusion_matrix
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

from .config import RelevanceConfig
from .split import SplitPlan
from .thresholds import ThresholdPlan, select_thresholds


class RelevanceModelError(RuntimeError):
    """参考记录、切分或特征无法支持二分类训练时抛出。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("relevance model training failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class GoldDocument:
    """最终审核参考记录与冻结规范化文本的训练投影。

    类名为既有模型产物兼容标识，不代表记录来自多人仲裁或没有测量误差。
    """

    source_post_id: int
    source_version: int
    platform_key: str
    captured_at_sort: str
    normalized_model_text: str
    tourism_label: str
    component_id: str
    adjudication_id: str


@dataclass(frozen=True)
class TrainingResult:
    """训练集拟合的最佳 pipeline、验证阈值和冻结测试报告。"""

    pipeline: Pipeline
    chosen_c: float
    thresholds: ThresholdPlan
    metrics: Mapping[str, object]


def unrelated_margins(pipeline: Pipeline, texts: Sequence[str]) -> np.ndarray:
    """把任意 sklearn 类别顺序统一为正方向更可能 `unrelated`。"""

    classifier = pipeline.named_steps["classifier"]
    classes = [str(value) for value in classifier.classes_]
    if set(classes) != {"related", "unrelated"}:
        raise RelevanceModelError("unexpected_classifier_classes")
    raw = np.asarray(pipeline.decision_function(texts), dtype=float)
    return raw if classes[1] == "unrelated" else -raw


def _binary_metrics(labels: Sequence[str], margins: Sequence[float]) -> dict[str, object]:
    """计算以 unrelated 为正类的指标，并显式表示零分母/单类别空值。"""

    encoded = np.asarray([1 if label == "unrelated" else 0 for label in labels], dtype=int)
    predictions = np.asarray([1 if margin >= 0.0 else 0 for margin in margins], dtype=int)
    matrix = confusion_matrix(encoded, predictions, labels=[0, 1])
    pr_auc = float(average_precision_score(encoded, margins)) if len(set(encoded)) == 2 else None
    true_positive = int(matrix[1, 1])
    false_positive = int(matrix[0, 1])
    false_negative = int(matrix[1, 0])
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    return {
        "count": len(labels),
        "unrelated_count": int(encoded.sum()),
        "pr_auc_unrelated": pr_auc,
        "pr_auc_unrelated_status": (
            "defined" if pr_auc is not None else "undefined_single_class"
        ),
        "precision_unrelated": (
            true_positive / precision_denominator if precision_denominator else None
        ),
        "precision_unrelated_status": (
            "defined" if precision_denominator else "undefined_no_predicted_unrelated"
        ),
        "recall_unrelated": (
            true_positive / recall_denominator if recall_denominator else None
        ),
        "recall_unrelated_status": (
            "defined" if recall_denominator else "undefined_no_unrelated_labels"
        ),
        "confusion": {
            "related_as_related": int(matrix[0, 0]),
            "related_as_unrelated": int(matrix[0, 1]),
            "unrelated_as_related": int(matrix[1, 0]),
            "unrelated_as_unrelated": int(matrix[1, 1]),
        },
    }


def _test_slices(
    documents: Sequence[GoldDocument],
    margins: Sequence[float],
    *,
    stable_negative_min: int,
) -> dict[str, object]:
    """报告平台和时间切片；负例不足时抑制稳定平台结论。"""

    by_platform: dict[str, dict[str, object]] = {}
    for platform in sorted({item.platform_key for item in documents}):
        indices = [index for index, item in enumerate(documents) if item.platform_key == platform]
        metrics = _binary_metrics(
            [documents[index].tourism_label for index in indices],
            [margins[index] for index in indices],
        )
        metrics["stable_conclusion_allowed"] = (
            int(metrics["unrelated_count"]) >= stable_negative_min
        )
        if not metrics["stable_conclusion_allowed"]:
            metrics["suppression_reason"] = "platform_unrelated_count_below_minimum"
        by_platform[platform] = metrics

    ordered = sorted(
        range(len(documents)),
        key=lambda index: (
            documents[index].captured_at_sort,
            documents[index].source_post_id,
        ),
    )
    midpoint = max(1, len(ordered) // 2)
    time_slices: dict[str, object] = {}
    for name, indices in (("earlier", ordered[:midpoint]), ("later", ordered[midpoint:])):
        if not indices:
            continue
        time_slices[name] = _binary_metrics(
            [documents[index].tourism_label for index in indices],
            [margins[index] for index in indices],
        )
    return {"platform": by_platform, "time": time_slices}


def fit_relevance_model(
    documents: Sequence[GoldDocument],
    *,
    split_plan: SplitPlan,
    config: RelevanceConfig,
    random_seed: int,
    smoke_only: bool = False,
) -> TrainingResult:
    """仅以最终审核二分类参考记录训练，并仅用验证集选择 C 和阈值。

    向量器与每个候选 SVM 都只在训练集拟合。测试集只在最佳 C 和阈值均
    冻结后评估一次，不参与超参数、阈值或低风险启用判断。
    """

    by_identity = {
        (item.source_post_id, item.source_version): item for item in documents
    }
    if len(by_identity) != len(documents):
        raise RelevanceModelError("duplicate_gold_document")
    split_by_identity = {
        (item.source_post_id, item.source_version): item.split_name
        for item in split_plan.assignments
    }
    if set(by_identity) != set(split_by_identity):
        raise RelevanceModelError("split_gold_manifest_mismatch")
    if any(
        item.tourism_label not in {"related", "unrelated"}
        or not item.normalized_model_text.strip()
        for item in documents
    ):
        raise RelevanceModelError("invalid_training_document")
    splits = {
        name: [item for item in documents if split_by_identity[
            (item.source_post_id, item.source_version)
        ] == name]
        for name in ("train", "validation", "test")
    }
    for name, items in splits.items():
        if Counter(item.tourism_label for item in items).keys() != {"related", "unrelated"}:
            raise RelevanceModelError(f"{name}_split_missing_class")

    train_texts = [item.normalized_model_text for item in splits["train"]]
    train_labels = [item.tourism_label for item in splits["train"]]
    validation_texts = [item.normalized_model_text for item in splits["validation"]]
    validation_labels = [item.tourism_label for item in splits["validation"]]
    candidates: list[tuple[float, float, Pipeline, np.ndarray]] = []
    validation_c_metrics: dict[str, object] = {}
    for c_value in config.c_grid:
        pipeline = Pipeline(
            [
                (
                    "vectorizer",
                    TfidfVectorizer(
                        analyzer=config.analyzer,
                        ngram_range=config.ngram_range,
                        min_df=config.min_df,
                        max_df=config.max_df,
                        sublinear_tf=config.sublinear_tf,
                    ),
                ),
                (
                    "classifier",
                    LinearSVC(
                        C=c_value,
                        class_weight=config.class_weight,
                        random_state=random_seed,
                    ),
                ),
            ]
        )
        try:
            pipeline.fit(train_texts, train_labels)
        except ValueError as exc:
            raise RelevanceModelError("tfidf_or_svm_fit_failed") from exc
        margins = unrelated_margins(pipeline, validation_texts)
        encoded = [1 if label == "unrelated" else 0 for label in validation_labels]
        pr_auc = float(average_precision_score(encoded, margins))
        validation_c_metrics[str(c_value)] = _binary_metrics(validation_labels, margins)
        candidates.append((pr_auc, c_value, pipeline, margins))
    # PR-AUC 相同优先较小 C，以降低不必要的模型复杂度。
    _, chosen_c, pipeline, validation_margins = max(
        candidates, key=lambda item: (item[0], -item[1])
    )
    thresholds = select_thresholds(
        validation_labels,
        validation_margins,
        high_risk_precision_min=config.high_risk_unrelated_precision_min,
        high_risk_recall_min=config.high_risk_unrelated_recall_min,
        low_risk_related_precision_min=config.low_risk_related_precision_min,
        low_risk_related_recall_min=config.low_risk_related_recall_min,
    )
    test_documents = splits["test"]
    test_margins = unrelated_margins(
        pipeline, [item.normalized_model_text for item in test_documents]
    )
    metrics = {
        "run_mode": "smoke" if smoke_only else "formal",
        "smoke_only": smoke_only,
        "positive_class": "unrelated",
        "margin_direction": "higher_is_more_unrelated",
        "selection_dataset": "validation",
        "validation_by_c": validation_c_metrics,
        "test": _binary_metrics(
            [item.tourism_label for item in test_documents], test_margins
        ),
        "test_slices": _test_slices(
            test_documents,
            test_margins,
            stable_negative_min=config.platform_stable_negative_min,
        ),
        "split_counts": {name: len(items) for name, items in splits.items()},
    }
    return TrainingResult(pipeline, chosen_c, thresholds, metrics)
