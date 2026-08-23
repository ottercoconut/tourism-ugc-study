"""冻结 Qwen 语义向量上的分组折外线性概率 baseline。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression

from .formal_baseline import evaluate_binary_probabilities, valid_group_folds
from .model_acceptance import PairedOofObservation
from .qwen_embedding_config import QwenEmbeddingPlan
from .sparse_challenger import ChallengerDocument


class QwenEmbeddingBaselineError(RuntimeError):
    """语义 baseline 输入、折外拟合或概率非法时抛出的去敏异常。

    Attributes:
        reason_code: 不含文本、成员身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。"""

        super().__init__("qwen embedding baseline failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenOofProbability:
    """训练成员的一次 leakage-group 折外概率。"""

    member_key: str
    component_id: str
    tourism_label: str
    p_unrelated: float
    fold_index: int


@dataclass(frozen=True)
class FrozenQwenEmbeddingClassifier:
    """冻结编码器向量之上的已拟合线性概率头。

    模型对象不包含 Qwen 权重；推理调用方必须先用 manifest 绑定的本地编码器
    生成同维、L2 归一化向量，再调用本类。这样公开权重不会被复制进私有包。
    """

    classifier: LogisticRegression
    plan_id: str
    encoder_revision: str
    embedding_dimension: int
    fit_scope: str = "frozen_train_only"

    def predict_p_unrelated(self, embeddings: np.ndarray) -> np.ndarray:
        """对冻结语义向量生成原生逻辑回归概率。

        Args:
            embeddings: 二维、有限且逐行 L2 归一化的语义向量。

        Returns:
            与输入等长的 ``p_unrelated``。

        Raises:
            QwenEmbeddingBaselineError: 维数、归一化、类别或概率非法。
        """

        matrix = _validated_embedding_matrix(
            embeddings, self.embedding_dimension, expected_count=None
        )
        classes = [str(value) for value in self.classifier.classes_]
        if set(classes) != {"related", "unrelated"}:
            raise QwenEmbeddingBaselineError(
                "qwen_embedding_classifier_classes_invalid"
            )
        try:
            probabilities = np.asarray(
                self.classifier.predict_proba(matrix), dtype=float
            )[:, classes.index("unrelated")]
        except (ValueError, IndexError) as exc:
            raise QwenEmbeddingBaselineError(
                "qwen_embedding_probability_prediction_failed"
            ) from exc
        if (
            probabilities.ndim != 1
            or len(probabilities) != len(matrix)
            or not np.isfinite(probabilities).all()
            or np.any(probabilities < 0.0)
            or np.any(probabilities > 1.0)
        ):
            raise QwenEmbeddingBaselineError(
                "qwen_embedding_probability_invalid"
            )
        return probabilities


@dataclass(frozen=True)
class QwenEmbeddingBaselineResult:
    """完成训练侧固定候选分组 OOF、但未读取验证或测试的结果。"""

    model: FrozenQwenEmbeddingClassifier
    oof_probabilities: tuple[QwenOofProbability, ...]
    fold_count: int
    training_metrics: Mapping[str, Any]
    confidence_band_diagnostics: Mapping[str, Any]
    risk_coverage_diagnostics: tuple[Mapping[str, Any], ...]


def _validated_documents(documents: Sequence[ChallengerDocument]) -> None:
    """校验训练成员、标签与 leakage component 契约。"""

    if not documents:
        raise QwenEmbeddingBaselineError("qwen_embedding_documents_empty")
    member_keys: set[str] = set()
    labels: set[str] = set()
    for document in documents:
        if (
            not document.member_key
            or document.member_key in member_keys
            or not document.component_id
            or not document.normalized_model_text.strip()
            or document.tourism_label not in {"related", "unrelated"}
        ):
            raise QwenEmbeddingBaselineError("qwen_embedding_document_invalid")
        member_keys.add(document.member_key)
        labels.add(document.tourism_label)
    if labels != {"related", "unrelated"}:
        raise QwenEmbeddingBaselineError("qwen_embedding_documents_missing_class")


def _validated_embedding_matrix(
    embeddings: np.ndarray,
    dimension: int,
    *,
    expected_count: int | None,
) -> np.ndarray:
    """要求二维有限、维数匹配且逐行归一化的 float32 矩阵。"""

    matrix = np.asarray(embeddings, dtype=np.float32)
    if (
        matrix.ndim != 2
        or matrix.shape[1] != dimension
        or (expected_count is not None and matrix.shape[0] != expected_count)
        or matrix.shape[0] < 1
        or not np.isfinite(matrix).all()
    ):
        raise QwenEmbeddingBaselineError("qwen_embedding_matrix_invalid")
    if not np.allclose(
        np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5, rtol=1e-5
    ):
        raise QwenEmbeddingBaselineError("qwen_embedding_matrix_not_normalized")
    return matrix


def _classifier(plan: QwenEmbeddingPlan) -> LogisticRegression:
    """构造唯一预登记的原生概率线性头。"""

    return LogisticRegression(
        C=plan.classifier.C,
        class_weight=plan.classifier.class_weight,
        solver=plan.classifier.solver,
        max_iter=plan.classifier.max_iter,
        random_state=plan.random_seed,
    )


def probability_band_diagnostics(
    labels: Sequence[str],
    probabilities: np.ndarray,
    *,
    low: float,
    high: float,
) -> Mapping[str, Any]:
    """报告固定概率带的开发工作量代理，不把它解释为路由阈值。"""

    encoded = np.asarray([label == "unrelated" for label in labels], dtype=bool)
    low_mask = probabilities <= low
    high_mask = probabilities >= high
    middle_mask = ~(low_mask | high_mask)
    return {
        "low": low,
        "high": high,
        "is_routing_threshold": False,
        "auto_like_count": int(np.sum(low_mask | high_mask)),
        "middle_count": int(np.sum(middle_mask)),
        "middle_rate": float(np.mean(middle_mask)),
        "low_count": int(np.sum(low_mask)),
        "low_unrelated_count": int(np.sum(low_mask & encoded)),
        "high_count": int(np.sum(high_mask)),
        "high_related_count": int(np.sum(high_mask & ~encoded)),
    }


def risk_coverage_diagnostics(
    labels: Sequence[str],
    probabilities: np.ndarray,
    *,
    confidence_grid: Sequence[float],
) -> tuple[Mapping[str, Any], ...]:
    """报告冻结置信度网格上的覆盖与错误，不把网格当作路由阈值。"""

    encoded = np.asarray([label == "unrelated" for label in labels], dtype=bool)
    rows: list[Mapping[str, Any]] = []
    for confidence in confidence_grid:
        low = 1.0 - confidence
        selected = (probabilities <= low) | (probabilities >= confidence)
        predicted_unrelated = probabilities >= confidence
        errors = selected & (predicted_unrelated != encoded)
        covered_count = int(np.sum(selected))
        error_count = int(np.sum(errors))
        rows.append(
            {
                "confidence": float(confidence),
                "is_routing_threshold": False,
                "covered_count": covered_count,
                "coverage": float(np.mean(selected)),
                "error_count": error_count,
                "selective_risk": (
                    float(error_count / covered_count)
                    if covered_count
                    else None
                ),
                "related_to_unrelated_count": int(
                    np.sum(selected & predicted_unrelated & ~encoded)
                ),
                "unrelated_to_related_count": int(
                    np.sum(selected & ~predicted_unrelated & encoded)
                ),
            }
        )
    return tuple(rows)


def fit_qwen_embedding_baseline(
    documents: Sequence[ChallengerDocument],
    embeddings: np.ndarray,
    *,
    plan: QwenEmbeddingPlan,
) -> QwenEmbeddingBaselineResult:
    """对唯一冻结候选执行 leakage-group OOF 并拟合最终线性头。

    Args:
        documents: 仅含训练442条的冻结文本、标签与 leakage component。
        embeddings: 编码器一次生成且与文档顺序一致的归一化向量。
        plan: 不含超参数搜索空间的预登记计划。

    Returns:
        每条成员恰好一次的 OOF 概率、聚合诊断与完整训练线性头。

    Raises:
        QwenEmbeddingBaselineError: 数据、分组折、拟合或概率不满足契约。

    Notes:
        本函数没有验证/测试输入，也不选择路由阈值。固定 ``0.1/0.9``
        概率带只用于比较潜在人工工作量，不会生成任何自动决定。
    """

    _validated_documents(documents)
    matrix = _validated_embedding_matrix(
        embeddings,
        plan.encoder.embedding_dimension,
        expected_count=len(documents),
    )
    labels = np.asarray([item.tourism_label for item in documents], dtype=object)
    groups = np.asarray([item.component_id for item in documents], dtype=object)
    try:
        folds = valid_group_folds(
            labels,
            groups,
            desired_splits=plan.outer_folds,
            random_seed=plan.random_seed,
            reason_code="qwen_embedding_group_folds_unavailable",
        )
    except RuntimeError as exc:
        raise QwenEmbeddingBaselineError(
            "qwen_embedding_group_folds_unavailable"
        ) from exc
    if len(folds) < plan.minimum_folds:
        raise QwenEmbeddingBaselineError(
            "qwen_embedding_group_folds_below_minimum"
        )
    oof = np.full(len(documents), np.nan, dtype=float)
    fold_indices = np.full(len(documents), -1, dtype=int)
    for fold_index, (fit_indices, held_indices) in enumerate(folds):
        classifier = _classifier(plan)
        try:
            classifier.fit(matrix[fit_indices], labels[fit_indices])
        except ValueError as exc:
            raise QwenEmbeddingBaselineError(
                "qwen_embedding_fold_fit_failed"
            ) from exc
        fold_model = FrozenQwenEmbeddingClassifier(
            classifier=classifier,
            plan_id=plan.plan_id,
            encoder_revision=plan.encoder.revision,
            embedding_dimension=plan.encoder.embedding_dimension,
            fit_scope="group_fold_train_only",
        )
        oof[held_indices] = fold_model.predict_p_unrelated(matrix[held_indices])
        fold_indices[held_indices] = fold_index
    if not np.isfinite(oof).all() or np.any(fold_indices < 0):
        raise QwenEmbeddingBaselineError("qwen_embedding_oof_incomplete")
    final_classifier = _classifier(plan)
    try:
        final_classifier.fit(matrix, labels)
    except ValueError as exc:
        raise QwenEmbeddingBaselineError("qwen_embedding_final_fit_failed") from exc
    model = FrozenQwenEmbeddingClassifier(
        classifier=final_classifier,
        plan_id=plan.plan_id,
        encoder_revision=plan.encoder.revision,
        embedding_dimension=plan.encoder.embedding_dimension,
    )
    rows = tuple(
        sorted(
            (
                QwenOofProbability(
                    member_key=item.member_key,
                    component_id=item.component_id,
                    tourism_label=item.tourism_label,
                    p_unrelated=float(oof[index]),
                    fold_index=int(fold_indices[index]),
                )
                for index, item in enumerate(documents)
            ),
            key=lambda row: row.member_key,
        )
    )
    return QwenEmbeddingBaselineResult(
        model=model,
        oof_probabilities=rows,
        fold_count=len(folds),
        training_metrics=evaluate_binary_probabilities(labels, oof),
        confidence_band_diagnostics=probability_band_diagnostics(
            labels,
            oof,
            low=plan.confidence_band_low,
            high=plan.confidence_band_high,
        ),
        risk_coverage_diagnostics=risk_coverage_diagnostics(
            labels,
            oof,
            confidence_grid=plan.risk_coverage_confidence_grid,
        ),
    )


def pair_qwen_with_sparse_comparator(
    qwen_oof: Sequence[QwenOofProbability],
    sparse_oof: Sequence[PairedOofObservation],
) -> tuple[PairedOofObservation, ...]:
    """把 sparse candidate 概率作为 baseline 与 Qwen OOF 逐成员配对。

    Args:
        qwen_oof: 新语义 baseline 的固定候选 OOF。
        sparse_oof: 已封存 sparse 运行中的 paired OOF；读取 candidate 概率。

    Returns:
        可交给通用 UGC 安全验收器的逐成员配对证据。

    Raises:
        QwenEmbeddingBaselineError: 成员、标签或 leakage component 不一致。
    """

    qwen_by_key = {row.member_key: row for row in qwen_oof}
    sparse_by_key = {row.member_key: row for row in sparse_oof}
    if (
        len(qwen_by_key) != len(qwen_oof)
        or len(sparse_by_key) != len(sparse_oof)
        or set(qwen_by_key) != set(sparse_by_key)
    ):
        raise QwenEmbeddingBaselineError(
            "qwen_embedding_comparator_members_mismatch"
        )
    paired: list[PairedOofObservation] = []
    for member_key in sorted(qwen_by_key):
        qwen = qwen_by_key[member_key]
        sparse = sparse_by_key[member_key]
        if (
            qwen.component_id != sparse.component_id
            or qwen.tourism_label != sparse.tourism_label
        ):
            raise QwenEmbeddingBaselineError(
                "qwen_embedding_comparator_lineage_mismatch"
            )
        paired.append(
            PairedOofObservation(
                member_key=member_key,
                component_id=qwen.component_id,
                tourism_label=qwen.tourism_label,
                baseline_p_unrelated=sparse.candidate_p_unrelated,
                candidate_p_unrelated=qwen.p_unrelated,
            )
        )
    return tuple(paired)
