"""固定 sparse 与 Qwen 分类头的无泄漏 logit 融合。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from .formal_baseline import evaluate_binary_probabilities, valid_group_folds
from .model_acceptance import PairedOofObservation
from .qwen_head_challenger import (
    FrozenQwenHeadModel,
    QwenHeadMember,
    QwenHeadPartitionFit,
    fit_qwen_head_partition,
)
from .qwen_head_challenger_config import QwenHeadChallengerPlan
from .qwen_sparse_fusion_config import (
    FusionWeightSpec,
    QwenSparseFusionPlan,
)
from .sparse_challenger import (
    ChallengerDocument,
    FrozenSparseCandidateModel,
    SparseCandidatePartitionFit,
    fit_sparse_candidate_partition,
)
from .sparse_challenger_config import SparseCandidateSpec


class QwenSparseFusionError(RuntimeError):
    """融合输入、分组拟合、选择或概率非法时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("qwen sparse fusion failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class FusionWeightScore:
    """同一内层 OOF 证据上的一个固定融合权重评分。"""

    weight_id: str
    qwen_weight: float
    eligible_by_related_safety: bool
    related_as_unrelated_rate: float
    log_loss: float
    pr_auc_unrelated: float
    brier_score: float


@dataclass(frozen=True)
class FusionOuterSelection:
    """一个外层折中完全由训练端决定的融合摘要。"""

    fold_index: int
    fit_count: int
    held_count: int
    inner_fold_count: int
    selected_weight_id: str
    selected_qwen_weight: float
    selected_is_sparse_anchor: bool
    selected_is_qwen_anchor: bool
    qwen_head_candidate_id: str
    qwen_head_family: str
    qwen_head_dimension: int
    sparse_related_as_unrelated_rate: float
    selected_related_as_unrelated_rate: float
    sparse_log_loss: float
    selected_log_loss: float


@dataclass(frozen=True)
class FrozenQwenSparseFusionModel:
    """固定 sparse、Qwen 分类头与一个 logit 融合权重。"""

    sparse_model: FrozenSparseCandidateModel
    qwen_model: FrozenQwenHeadModel
    weight: FusionWeightSpec
    probability_clip: float
    fit_scope: str = "provided_train_only"

    def predict_p_unrelated(
        self, texts: Sequence[str], embeddings: np.ndarray
    ) -> np.ndarray:
        """对同顺序文本和 Qwen embedding 生成融合概率。

        Args:
            texts: 冻结规范化文本，不含平台或集合身份。
            embeddings: 与文本同顺序的完整2560维归一化 Qwen 向量。

        Returns:
            与输入等长的有限 ``p_unrelated``。

        Raises:
            QwenSparseFusionError: 数量、基模型输出或融合概率非法。
        """

        if len(texts) != len(embeddings):
            raise QwenSparseFusionError(
                "qwen_sparse_fusion_prediction_count_mismatch"
            )
        sparse_probabilities = self.sparse_model.predict_p_unrelated(texts)
        qwen_probabilities = self.qwen_model.predict_p_unrelated(embeddings)
        return blend_logit_probabilities(
            sparse_probabilities,
            qwen_probabilities,
            qwen_weight=self.weight.qwen_weight,
            probability_clip=self.probability_clip,
        )


@dataclass(frozen=True)
class QwenSparseFusionResult:
    """完成第二层训练侧 nested OOF、但未读取验证或测试的结果。"""

    selected_weight: FusionWeightSpec
    selected_model: FrozenQwenSparseFusionModel
    paired_outer_oof: tuple[PairedOofObservation, ...]
    outer_selections: tuple[FusionOuterSelection, ...]
    full_training_weight_scores: tuple[FusionWeightScore, ...]
    full_training_qwen_head_candidate_id: str
    outer_fold_count: int
    final_inner_fold_count: int
    baseline_training_metrics: Mapping[str, Any]
    candidate_training_metrics: Mapping[str, Any]


@dataclass(frozen=True)
class _PartitionSelection:
    """一个外层训练端内的两基模型、权重与内层评分。"""

    selected_weight: FusionWeightSpec
    selected_model: FrozenQwenSparseFusionModel
    weight_scores: tuple[FusionWeightScore, ...]
    sparse_fit: SparseCandidatePartitionFit
    qwen_fit: QwenHeadPartitionFit
    inner_fold_count: int


def blend_logit_probabilities(
    sparse_probabilities: Sequence[float],
    qwen_probabilities: Sequence[float],
    *,
    qwen_weight: float,
    probability_clip: float,
) -> np.ndarray:
    """在 logit 空间按固定 Qwen 权重融合两组概率。

    Args:
        sparse_probabilities: sparse 模型的无关概率。
        qwen_probabilities: Qwen 模型的无关概率。
        qwen_weight: Qwen logit 权重；0为纯 sparse，1为纯 Qwen。
        probability_clip: 计算 logit 前的数值裁剪量。

    Returns:
        与输入等长且位于开区间的融合概率。

    Raises:
        QwenSparseFusionError: 输入形状、数值、权重或裁剪量非法。
    """

    sparse_values = np.asarray(sparse_probabilities, dtype=float)
    qwen_values = np.asarray(qwen_probabilities, dtype=float)
    if (
        sparse_values.ndim != 1
        or qwen_values.ndim != 1
        or len(sparse_values) != len(qwen_values)
        or len(sparse_values) < 1
        or not np.isfinite(sparse_values).all()
        or not np.isfinite(qwen_values).all()
        or np.any(sparse_values < 0.0)
        or np.any(sparse_values > 1.0)
        or np.any(qwen_values < 0.0)
        or np.any(qwen_values > 1.0)
        or not np.isfinite(qwen_weight)
        or not 0.0 <= qwen_weight <= 1.0
        or not np.isfinite(probability_clip)
        or not 0.0 < probability_clip < 0.5
    ):
        raise QwenSparseFusionError("qwen_sparse_fusion_probability_invalid")
    sparse_clipped = np.clip(
        sparse_values, probability_clip, 1.0 - probability_clip
    )
    qwen_clipped = np.clip(
        qwen_values, probability_clip, 1.0 - probability_clip
    )
    sparse_logits = np.log(sparse_clipped / (1.0 - sparse_clipped))
    qwen_logits = np.log(qwen_clipped / (1.0 - qwen_clipped))
    logits = (1.0 - qwen_weight) * sparse_logits + qwen_weight * qwen_logits
    probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -709.0, 709.0)))
    if not np.isfinite(probabilities).all():
        raise QwenSparseFusionError("qwen_sparse_fusion_probability_invalid")
    return probabilities


def _validate_aligned_inputs(
    documents: Sequence[ChallengerDocument],
    members: Sequence[QwenHeadMember],
    embeddings: np.ndarray,
) -> np.ndarray:
    """要求文本、去标识成员和 embedding 在同一顺序严格对齐。"""

    matrix = np.asarray(embeddings, dtype=np.float32)
    if (
        not documents
        or len(documents) != len(members)
        or matrix.shape != (len(documents), 2560)
        or not np.isfinite(matrix).all()
        or not np.allclose(
            np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5, rtol=1e-5
        )
    ):
        raise QwenSparseFusionError("qwen_sparse_fusion_inputs_invalid")
    seen: set[str] = set()
    for document, member in zip(documents, members, strict=True):
        if (
            document.member_key != member.member_key
            or document.component_id != member.component_id
            or document.tourism_label != member.tourism_label
            or document.member_key in seen
            or not document.normalized_model_text.strip()
            or document.tourism_label not in {"related", "unrelated"}
        ):
            raise QwenSparseFusionError("qwen_sparse_fusion_lineage_mismatch")
        seen.add(document.member_key)
    if {item.tourism_label for item in documents} != {"related", "unrelated"}:
        raise QwenSparseFusionError("qwen_sparse_fusion_classes_incomplete")
    return matrix


def _score_weights(
    labels: Sequence[str],
    sparse_probabilities: np.ndarray,
    qwen_probabilities: np.ndarray,
    *,
    plan: QwenSparseFusionPlan,
) -> tuple[tuple[FusionWeightScore, ...], FusionWeightSpec]:
    """以内层 sparse 安全率为锚点评分并选择一个固定融合权重。"""

    try:
        sparse_metrics = evaluate_binary_probabilities(
            labels, sparse_probabilities
        )
    except ValueError as exc:
        raise QwenSparseFusionError(
            "qwen_sparse_fusion_metrics_invalid"
        ) from exc
    related_count = int(sparse_metrics["related_count"])
    if related_count <= 0:
        raise QwenSparseFusionError(
            "qwen_sparse_fusion_metrics_missing_related"
        )
    sparse_rate = (
        float(sparse_metrics["confusion"]["related_as_unrelated"])
        / related_count
    )
    scores: list[FusionWeightScore] = []
    for weight in plan.weights:
        probabilities = blend_logit_probabilities(
            sparse_probabilities,
            qwen_probabilities,
            qwen_weight=weight.qwen_weight,
            probability_clip=plan.probability_clip,
        )
        try:
            metrics = evaluate_binary_probabilities(labels, probabilities)
        except ValueError as exc:
            raise QwenSparseFusionError(
                "qwen_sparse_fusion_metrics_invalid"
            ) from exc
        rate = float(metrics["confusion"]["related_as_unrelated"]) / related_count
        scores.append(
            FusionWeightScore(
                weight_id=weight.weight_id,
                qwen_weight=weight.qwen_weight,
                eligible_by_related_safety=rate <= sparse_rate + 1e-15,
                related_as_unrelated_rate=rate,
                log_loss=float(metrics["log_loss"]),
                pr_auc_unrelated=float(metrics["pr_auc_unrelated"]),
                brier_score=float(metrics["brier_score"]),
            )
        )
    eligible = [item for item in scores if item.eligible_by_related_safety]
    if not eligible:
        raise QwenSparseFusionError("qwen_sparse_fusion_no_safe_weight")
    selected_score = min(
        eligible,
        key=lambda item: (
            item.log_loss,
            -item.pr_auc_unrelated,
            item.weight_id,
        ),
    )
    selected_weight = next(
        item for item in plan.weights if item.weight_id == selected_score.weight_id
    )
    return tuple(scores), selected_weight


def _fit_partition(
    documents: Sequence[ChallengerDocument],
    members: Sequence[QwenHeadMember],
    embeddings: np.ndarray,
    *,
    sparse_spec: SparseCandidateSpec,
    head_plan: QwenHeadChallengerPlan,
    fusion_plan: QwenSparseFusionPlan,
    random_seed: int,
) -> _PartitionSelection:
    """只在一个外层训练端重建两基模型、内层 OOF 和融合权重。"""

    sparse_fit = fit_sparse_candidate_partition(
        documents,
        sparse_spec,
        desired_folds=fusion_plan.inner_folds,
        minimum_folds=fusion_plan.minimum_folds,
        random_seed=random_seed,
    )
    qwen_fit = fit_qwen_head_partition(
        members, embeddings, plan=head_plan, random_seed=random_seed
    )
    if sparse_fit.fold_count != qwen_fit.inner_fold_count:
        raise QwenSparseFusionError(
            "qwen_sparse_fusion_inner_fold_count_mismatch"
        )
    scores, selected_weight = _score_weights(
        [item.tourism_label for item in documents],
        sparse_fit.oof_probabilities,
        qwen_fit.selected_oof_probabilities,
        plan=fusion_plan,
    )
    model = FrozenQwenSparseFusionModel(
        sparse_model=sparse_fit.model,
        qwen_model=qwen_fit.selected_model,
        weight=selected_weight,
        probability_clip=fusion_plan.probability_clip,
    )
    return _PartitionSelection(
        selected_weight=selected_weight,
        selected_model=model,
        weight_scores=scores,
        sparse_fit=sparse_fit,
        qwen_fit=qwen_fit,
        inner_fold_count=sparse_fit.fold_count,
    )


def fit_qwen_sparse_fusion_nested(
    documents: Sequence[ChallengerDocument],
    members: Sequence[QwenHeadMember],
    embeddings: np.ndarray,
    *,
    sparse_spec: SparseCandidateSpec,
    head_plan: QwenHeadChallengerPlan,
    fusion_plan: QwenSparseFusionPlan,
) -> QwenSparseFusionResult:
    """执行第二层 sparse＋Qwen 的外5/内4无泄漏融合。

    Args:
        documents: 仅含冻结训练442条的规范化文本与 leakage component。
        members: 与文档同顺序、不含正文的 Qwen 成员投影。
        embeddings: 与成员同顺序的冻结4B训练 embedding。
        sparse_spec: 既有 sparse 正式运行唯一选定规范，不重新搜索。
        head_plan: 第一层已冻结的56候选分类头计划。
        fusion_plan: 五个固定 logit 权重与折叠计划。

    Returns:
        同外层折的 sparse/fusion 配对 OOF、唯一最终权重与可推理模型。

    Raises:
        QwenSparseFusionError: 对齐、折叠、基模型拟合、选择或概率非法。

    Notes:
        每个外层折都在训练端重做 sparse 交叉校准、Qwen 分类头选择与权重
        选择；外层留出只用于一次预测。函数不接受平台、验证、测试或阈值。
    """

    matrix = _validate_aligned_inputs(documents, members, embeddings)
    labels = np.asarray([item.tourism_label for item in documents], dtype=object)
    groups = np.asarray([item.component_id for item in documents], dtype=object)
    try:
        outer_folds = tuple(
            valid_group_folds(
                labels,
                groups,
                desired_splits=fusion_plan.outer_folds,
                random_seed=fusion_plan.random_seed,
                reason_code="qwen_sparse_fusion_outer_folds_unavailable",
            )
        )
    except RuntimeError as exc:
        raise QwenSparseFusionError(
            "qwen_sparse_fusion_outer_folds_unavailable"
        ) from exc
    if len(outer_folds) < fusion_plan.minimum_folds:
        raise QwenSparseFusionError(
            "qwen_sparse_fusion_outer_folds_below_minimum"
        )
    observations: list[PairedOofObservation] = []
    selections: list[FusionOuterSelection] = []
    seen: set[str] = set()
    for fold_index, (fit_indices, held_indices) in enumerate(outer_folds):
        fit_documents = [documents[int(index)] for index in fit_indices]
        fit_members = [members[int(index)] for index in fit_indices]
        held_documents = [documents[int(index)] for index in held_indices]
        selection = _fit_partition(
            fit_documents,
            fit_members,
            matrix[fit_indices],
            sparse_spec=sparse_spec,
            head_plan=head_plan,
            fusion_plan=fusion_plan,
            random_seed=fusion_plan.random_seed + (fold_index + 1) * 1009,
        )
        held_texts = [item.normalized_model_text for item in held_documents]
        sparse_probabilities = selection.sparse_fit.model.predict_p_unrelated(
            held_texts
        )
        candidate_probabilities = selection.selected_model.predict_p_unrelated(
            held_texts, matrix[held_indices]
        )
        sparse_score = next(
            item for item in selection.weight_scores if item.qwen_weight == 0.0
        )
        selected_score = next(
            item
            for item in selection.weight_scores
            if item.weight_id == selection.selected_weight.weight_id
        )
        selections.append(
            FusionOuterSelection(
                fold_index=fold_index,
                fit_count=len(fit_indices),
                held_count=len(held_indices),
                inner_fold_count=selection.inner_fold_count,
                selected_weight_id=selection.selected_weight.weight_id,
                selected_qwen_weight=selection.selected_weight.qwen_weight,
                selected_is_sparse_anchor=(
                    selection.selected_weight.qwen_weight == 0.0
                ),
                selected_is_qwen_anchor=(
                    selection.selected_weight.qwen_weight == 1.0
                ),
                qwen_head_candidate_id=(
                    selection.qwen_fit.selected_spec.candidate_id
                ),
                qwen_head_family=selection.qwen_fit.selected_spec.family,
                qwen_head_dimension=(
                    selection.qwen_fit.selected_spec.embedding_dimension
                ),
                sparse_related_as_unrelated_rate=(
                    sparse_score.related_as_unrelated_rate
                ),
                selected_related_as_unrelated_rate=(
                    selected_score.related_as_unrelated_rate
                ),
                sparse_log_loss=sparse_score.log_loss,
                selected_log_loss=selected_score.log_loss,
            )
        )
        for local_index, document in enumerate(held_documents):
            if document.member_key in seen:
                raise QwenSparseFusionError(
                    "qwen_sparse_fusion_outer_oof_duplicate"
                )
            seen.add(document.member_key)
            observations.append(
                PairedOofObservation(
                    member_key=document.member_key,
                    component_id=document.component_id,
                    tourism_label=document.tourism_label,
                    baseline_p_unrelated=float(sparse_probabilities[local_index]),
                    candidate_p_unrelated=float(
                        candidate_probabilities[local_index]
                    ),
                )
            )
    if seen != {item.member_key for item in documents}:
        raise QwenSparseFusionError("qwen_sparse_fusion_outer_oof_incomplete")
    final_selection = _fit_partition(
        documents,
        members,
        matrix,
        sparse_spec=sparse_spec,
        head_plan=head_plan,
        fusion_plan=fusion_plan,
        random_seed=fusion_plan.random_seed + 900001,
    )
    paired = tuple(sorted(observations, key=lambda item: item.member_key))
    return QwenSparseFusionResult(
        selected_weight=final_selection.selected_weight,
        selected_model=final_selection.selected_model,
        paired_outer_oof=paired,
        outer_selections=tuple(selections),
        full_training_weight_scores=final_selection.weight_scores,
        full_training_qwen_head_candidate_id=(
            final_selection.qwen_fit.selected_spec.candidate_id
        ),
        outer_fold_count=len(outer_folds),
        final_inner_fold_count=final_selection.inner_fold_count,
        baseline_training_metrics=evaluate_binary_probabilities(
            [item.tourism_label for item in paired],
            [item.baseline_p_unrelated for item in paired],
        ),
        candidate_training_metrics=evaluate_binary_probabilities(
            [item.tourism_label for item in paired],
            [item.candidate_p_unrelated for item in paired],
        ),
    )
