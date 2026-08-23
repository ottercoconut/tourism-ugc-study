"""Qwen 缓存 embedding 上的安全优先嵌套分类头搜索。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC

from .formal_baseline import (
    SigmoidCalibrator,
    evaluate_binary_probabilities,
    fit_sigmoid_calibrator,
    valid_group_folds,
)
from .qwen_head_challenger_config import (
    QwenHeadCandidateSpec,
    QwenHeadChallengerPlan,
)
from .sparse_challenger import CandidateDevelopmentScore


class QwenHeadChallengerError(RuntimeError):
    """分类头搜索输入、分组拟合或概率非法时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含文本、成员身份或本机路径的稳定失败码。"""

        super().__init__("qwen head challenger failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenHeadMember:
    """不含正文的缓存向量训练成员契约。"""

    member_key: str
    component_id: str
    tourism_label: str


@dataclass(frozen=True)
class QwenHeadOofProbability:
    """训练成员的一次外层 leakage-group OOF 概率。"""

    member_key: str
    component_id: str
    tourism_label: str
    p_unrelated: float
    fold_index: int


@dataclass(frozen=True)
class QwenHeadOuterSelection:
    """一个外层折中仅由训练端决定的分类头选择摘要。"""

    fold_index: int
    fit_count: int
    held_count: int
    inner_fold_count: int
    selected_candidate_id: str
    selected_family: str
    selected_embedding_dimension: int
    selected_C: float
    selected_class_weight: str | None
    anchor_related_as_unrelated_rate: float
    selected_related_as_unrelated_rate: float
    anchor_log_loss: float
    selected_log_loss: float
    selected_is_anchor: bool


@dataclass(frozen=True)
class FrozenQwenHeadModel:
    """只在调用方训练端拟合的 MRL 投影、线性头与 OOF Sigmoid。"""

    spec: QwenHeadCandidateSpec
    classifier: LogisticRegression | LinearSVC
    calibrator: SigmoidCalibrator
    source_embedding_dimension: int = 2560
    fit_scope: str = "provided_train_only"

    def predict_p_unrelated(self, embeddings: np.ndarray) -> np.ndarray:
        """对同源完整 embedding 生成校准后的无关概率。

        Args:
            embeddings: 二维、有限且逐行 L2 归一化的2560维向量。

        Returns:
            与输入等长、位于闭区间的 ``p_unrelated``。

        Raises:
            QwenHeadChallengerError: 向量、类别、margin 或概率非法。
        """

        matrix = _validated_embedding_matrix(
            embeddings,
            expected_dimension=self.source_embedding_dimension,
            expected_count=None,
        )
        projected = _mrl_projection(matrix, self.spec.embedding_dimension)
        margins = _unrelated_margins(self.classifier, projected)
        try:
            probabilities = np.asarray(
                self.calibrator.predict(margins), dtype=float
            )
        except RuntimeError as exc:
            raise QwenHeadChallengerError(
                "qwen_head_probability_calibration_failed"
            ) from exc
        if (
            probabilities.ndim != 1
            or len(probabilities) != len(matrix)
            or not np.isfinite(probabilities).all()
            or np.any(probabilities < 0.0)
            or np.any(probabilities > 1.0)
        ):
            raise QwenHeadChallengerError(
                "qwen_head_probability_invalid"
            )
        return probabilities


@dataclass(frozen=True)
class QwenHeadChallengerResult:
    """完成训练侧 nested OOF、但未读取验证或测试的第一层结果。"""

    selected_spec: QwenHeadCandidateSpec
    selected_model: FrozenQwenHeadModel
    oof_probabilities: tuple[QwenHeadOofProbability, ...]
    outer_selections: tuple[QwenHeadOuterSelection, ...]
    full_training_anchor_score: CandidateDevelopmentScore
    full_training_candidate_scores: tuple[CandidateDevelopmentScore, ...]
    outer_fold_count: int
    final_inner_fold_count: int
    training_metrics: Mapping[str, Any]


@dataclass(frozen=True)
class _CrossFitResult:
    """一个分类头的分组折外概率、校准器与实际折数。"""

    probabilities: np.ndarray
    calibrator: SigmoidCalibrator
    fold_count: int


@dataclass(frozen=True)
class _InnerSelectionResult:
    """一次内层安全优先选择及其完整评分证据。"""

    selected_spec: QwenHeadCandidateSpec
    selected_model: FrozenQwenHeadModel
    anchor_score: CandidateDevelopmentScore
    candidate_scores: tuple[CandidateDevelopmentScore, ...]
    inner_fold_count: int


def _validated_members(members: Sequence[QwenHeadMember]) -> None:
    """校验去标识成员、标签与 leakage component。"""

    if not members:
        raise QwenHeadChallengerError("qwen_head_members_empty")
    keys = [item.member_key for item in members]
    if (
        len(keys) != len(set(keys))
        or any(
            not item.member_key
            or not item.component_id
            or item.tourism_label not in {"related", "unrelated"}
            for item in members
        )
    ):
        raise QwenHeadChallengerError("qwen_head_member_invalid")
    if {item.tourism_label for item in members} != {"related", "unrelated"}:
        raise QwenHeadChallengerError("qwen_head_members_missing_class")


def _validated_embedding_matrix(
    embeddings: np.ndarray,
    *,
    expected_dimension: int,
    expected_count: int | None,
) -> np.ndarray:
    """要求二维有限、维数匹配且逐行 L2 归一化的 float32 矩阵。"""

    matrix = np.asarray(embeddings, dtype=np.float32)
    if (
        matrix.ndim != 2
        or matrix.shape[1] != expected_dimension
        or matrix.shape[0] < 1
        or (expected_count is not None and matrix.shape[0] != expected_count)
        or not np.isfinite(matrix).all()
    ):
        raise QwenHeadChallengerError("qwen_head_embedding_matrix_invalid")
    if not np.allclose(
        np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5, rtol=1e-5
    ):
        raise QwenHeadChallengerError(
            "qwen_head_embedding_matrix_not_normalized"
        )
    return matrix


def _mrl_projection(matrix: np.ndarray, dimension: int) -> np.ndarray:
    """截取 MRL 前缀并逐行重新 L2 归一化。"""

    if dimension < 1 or dimension > matrix.shape[1]:
        raise QwenHeadChallengerError("qwen_head_mrl_dimension_invalid")
    projected = np.asarray(matrix[:, :dimension], dtype=np.float32)
    norms = np.linalg.norm(projected, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or np.any(norms <= 0.0):
        raise QwenHeadChallengerError("qwen_head_mrl_norm_invalid")
    result = projected / norms
    if not np.isfinite(result).all():
        raise QwenHeadChallengerError("qwen_head_mrl_projection_invalid")
    return result


def _classifier(
    spec: QwenHeadCandidateSpec, random_seed: int
) -> LogisticRegression | LinearSVC:
    """按冻结候选规范构造尚未拟合的线性分类器。"""

    if spec.family == "logistic_regression" and spec.solver == "liblinear":
        return LogisticRegression(
            C=spec.C,
            class_weight=spec.class_weight,
            solver=spec.solver,
            max_iter=spec.max_iter,
            random_state=random_seed,
        )
    if spec.family == "linear_svc" and spec.solver is None:
        return LinearSVC(
            C=spec.C,
            class_weight=spec.class_weight,
            max_iter=spec.max_iter,
            random_state=random_seed,
        )
    raise QwenHeadChallengerError("qwen_head_candidate_spec_invalid")


def _unrelated_margins(
    classifier: LogisticRegression | LinearSVC, matrix: np.ndarray
) -> np.ndarray:
    """把分类器 margin 方向统一为正值更可能 ``unrelated``。"""

    classes = [str(value) for value in classifier.classes_]
    if set(classes) != {"related", "unrelated"} or len(classes) != 2:
        raise QwenHeadChallengerError("qwen_head_classifier_classes_invalid")
    try:
        raw = np.asarray(classifier.decision_function(matrix), dtype=float)
    except ValueError as exc:
        raise QwenHeadChallengerError(
            "qwen_head_margin_prediction_failed"
        ) from exc
    if raw.ndim != 1 or len(raw) != len(matrix) or not np.isfinite(raw).all():
        raise QwenHeadChallengerError("qwen_head_margin_invalid")
    return raw if classes[1] == "unrelated" else -raw


def _group_folds(
    members: Sequence[QwenHeadMember],
    *,
    desired_folds: int,
    minimum_folds: int,
    random_seed: int,
    stage: str,
) -> tuple[tuple[np.ndarray, np.ndarray], ...]:
    """生成满足类别与 leakage component 约束的稳定分组折。"""

    labels = np.asarray([item.tourism_label for item in members], dtype=object)
    groups = np.asarray([item.component_id for item in members], dtype=object)
    try:
        folds = tuple(
            valid_group_folds(
                labels,
                groups,
                desired_splits=desired_folds,
                random_seed=random_seed,
                reason_code=f"qwen_head_{stage}_folds_unavailable",
            )
        )
    except RuntimeError as exc:
        raise QwenHeadChallengerError(
            f"qwen_head_{stage}_folds_unavailable"
        ) from exc
    if len(folds) < minimum_folds:
        raise QwenHeadChallengerError(
            f"qwen_head_{stage}_folds_below_minimum"
        )
    return folds


def _cross_fit(
    members: Sequence[QwenHeadMember],
    embeddings: np.ndarray,
    spec: QwenHeadCandidateSpec,
    *,
    desired_folds: int,
    minimum_folds: int,
    random_seed: int,
) -> _CrossFitResult:
    """在同一分组折上生成 margin，并仅以这些 OOF margin 拟合 Sigmoid。"""

    projected = _mrl_projection(embeddings, spec.embedding_dimension)
    labels = np.asarray([item.tourism_label for item in members], dtype=object)
    encoded = np.asarray(labels == "unrelated", dtype=int)
    folds = _group_folds(
        members,
        desired_folds=desired_folds,
        minimum_folds=minimum_folds,
        random_seed=random_seed,
        stage="inner",
    )
    margins = np.full(len(members), np.nan, dtype=float)
    for fit_indices, held_indices in folds:
        classifier = _classifier(spec, random_seed)
        try:
            classifier.fit(projected[fit_indices], labels[fit_indices])
            margins[held_indices] = _unrelated_margins(
                classifier, projected[held_indices]
            )
        except (RuntimeError, ValueError) as exc:
            raise QwenHeadChallengerError(
                "qwen_head_candidate_fold_fit_failed"
            ) from exc
    if not np.isfinite(margins).all():
        raise QwenHeadChallengerError("qwen_head_candidate_oof_incomplete")
    try:
        calibrator = fit_sigmoid_calibrator(margins, encoded)
        probabilities = np.asarray(calibrator.predict(margins), dtype=float)
    except RuntimeError as exc:
        raise QwenHeadChallengerError(
            "qwen_head_candidate_calibration_failed"
        ) from exc
    if (
        not np.isfinite(probabilities).all()
        or np.any(probabilities < 0.0)
        or np.any(probabilities > 1.0)
    ):
        raise QwenHeadChallengerError(
            "qwen_head_candidate_probability_invalid"
        )
    return _CrossFitResult(probabilities, calibrator, len(folds))


def _fit_full(
    members: Sequence[QwenHeadMember],
    embeddings: np.ndarray,
    spec: QwenHeadCandidateSpec,
    calibrator: SigmoidCalibrator,
    *,
    random_seed: int,
) -> FrozenQwenHeadModel:
    """在调用方提供的完整训练端拟合已选分类头。"""

    labels = np.asarray([item.tourism_label for item in members], dtype=object)
    classifier = _classifier(spec, random_seed)
    try:
        classifier.fit(
            _mrl_projection(embeddings, spec.embedding_dimension), labels
        )
    except (RuntimeError, ValueError) as exc:
        raise QwenHeadChallengerError("qwen_head_final_fit_failed") from exc
    return FrozenQwenHeadModel(spec, classifier, calibrator)


def _score(
    spec: QwenHeadCandidateSpec,
    labels: Sequence[str],
    cross_fit: _CrossFitResult,
    *,
    anchor_related_as_unrelated_rate: float | None,
) -> CandidateDevelopmentScore:
    """把候选 OOF 概率转成安全优先选择摘要。"""

    try:
        metrics = evaluate_binary_probabilities(labels, cross_fit.probabilities)
    except ValueError as exc:
        raise QwenHeadChallengerError("qwen_head_metrics_invalid") from exc
    related_count = int(metrics["related_count"])
    if related_count <= 0:
        raise QwenHeadChallengerError("qwen_head_metrics_missing_related")
    rate = float(metrics["confusion"]["related_as_unrelated"]) / related_count
    eligible = (
        anchor_related_as_unrelated_rate is None
        or rate <= anchor_related_as_unrelated_rate + 1e-15
    )
    return CandidateDevelopmentScore(
        candidate_id=spec.candidate_id,
        family=spec.family,
        status="evaluated",
        eligible_by_related_safety=eligible,
        fold_count=cross_fit.fold_count,
        related_as_unrelated_rate=rate,
        log_loss=float(metrics["log_loss"]),
        pr_auc_unrelated=float(metrics["pr_auc_unrelated"]),
        brier_score=float(metrics["brier_score"]),
        failure_reason_code=None,
    )


def _failed_score(
    spec: QwenHeadCandidateSpec, reason_code: str
) -> CandidateDevelopmentScore:
    """生成不暴露异常文本或成员的候选失败摘要。"""

    return CandidateDevelopmentScore(
        candidate_id=spec.candidate_id,
        family=spec.family,
        status="fit_failed",
        eligible_by_related_safety=False,
        fold_count=0,
        related_as_unrelated_rate=None,
        log_loss=None,
        pr_auc_unrelated=None,
        brier_score=None,
        failure_reason_code=reason_code,
    )


def _select_inner(
    members: Sequence[QwenHeadMember],
    embeddings: np.ndarray,
    plan: QwenHeadChallengerPlan,
    *,
    random_seed: int,
) -> _InnerSelectionResult:
    """仅用当前训练端 OOF 证据执行锚点安全约束和概率质量选择。"""

    labels = [item.tourism_label for item in members]
    by_id = {item.candidate_id: item for item in plan.candidates}
    anchor_spec = by_id[plan.safety_anchor_candidate_id]
    anchor_cross_fit = _cross_fit(
        members,
        embeddings,
        anchor_spec,
        desired_folds=plan.inner_folds,
        minimum_folds=plan.minimum_folds,
        random_seed=random_seed,
    )
    anchor_score = _score(
        anchor_spec,
        labels,
        anchor_cross_fit,
        anchor_related_as_unrelated_rate=None,
    )
    anchor_rate = anchor_score.related_as_unrelated_rate
    if anchor_rate is None:
        raise QwenHeadChallengerError("qwen_head_anchor_score_invalid")
    scores: list[CandidateDevelopmentScore] = []
    cross_fits: dict[str, _CrossFitResult] = {}
    for spec in plan.candidates:
        try:
            cross_fit = (
                anchor_cross_fit
                if spec.candidate_id == anchor_spec.candidate_id
                else _cross_fit(
                    members,
                    embeddings,
                    spec,
                    desired_folds=plan.inner_folds,
                    minimum_folds=plan.minimum_folds,
                    random_seed=random_seed,
                )
            )
            score = _score(
                spec,
                labels,
                cross_fit,
                anchor_related_as_unrelated_rate=anchor_rate,
            )
        except QwenHeadChallengerError as exc:
            scores.append(_failed_score(spec, exc.reason_code))
            continue
        scores.append(score)
        cross_fits[spec.candidate_id] = cross_fit
    eligible = [
        item
        for item in scores
        if item.status == "evaluated" and item.eligible_by_related_safety
    ]
    if not eligible:
        raise QwenHeadChallengerError("qwen_head_no_safe_candidate")
    selected_score = min(
        eligible,
        key=lambda item: (
            float(item.log_loss),
            -float(item.pr_auc_unrelated),
            item.candidate_id,
        ),
    )
    selected_spec = by_id[selected_score.candidate_id]
    selected_cross_fit = cross_fits[selected_spec.candidate_id]
    selected_model = _fit_full(
        members,
        embeddings,
        selected_spec,
        selected_cross_fit.calibrator,
        random_seed=random_seed,
    )
    return _InnerSelectionResult(
        selected_spec=selected_spec,
        selected_model=selected_model,
        anchor_score=anchor_score,
        candidate_scores=tuple(scores),
        inner_fold_count=anchor_cross_fit.fold_count,
    )


def fit_qwen_head_challenger_nested(
    members: Sequence[QwenHeadMember],
    embeddings: np.ndarray,
    *,
    plan: QwenHeadChallengerPlan,
) -> QwenHeadChallengerResult:
    """执行第一层缓存 embedding 分类头 nested group OOF。

    Args:
        members: 仅含训练成员键、人工标签和 leakage component 的序列。
        embeddings: 与成员顺序一致的冻结2560维 L2 向量。
        plan: Issue #45 预登记的56候选与折叠计划。

    Returns:
        唯一最终分类头、逐成员外层 OOF、折内选择证据与训练指标。

    Raises:
        QwenHeadChallengerError: 成员、向量、分组折、拟合或概率非法。

    Notes:
        函数不接受正文、平台、验证、测试、阈值或审计输入。每个外层折的
        分类头与校准器完全由其训练端的内层分组 OOF 决定。
    """

    _validated_members(members)
    matrix = _validated_embedding_matrix(
        embeddings, expected_dimension=2560, expected_count=len(members)
    )
    outer_folds = _group_folds(
        members,
        desired_folds=plan.outer_folds,
        minimum_folds=plan.minimum_folds,
        random_seed=plan.random_seed,
        stage="outer",
    )
    oof = np.full(len(members), np.nan, dtype=float)
    fold_indices = np.full(len(members), -1, dtype=int)
    selections: list[QwenHeadOuterSelection] = []
    for fold_index, (fit_indices, held_indices) in enumerate(outer_folds):
        fit_members = [members[int(index)] for index in fit_indices]
        selection = _select_inner(
            fit_members,
            matrix[fit_indices],
            plan,
            random_seed=plan.random_seed + (fold_index + 1) * 1009,
        )
        probabilities = selection.selected_model.predict_p_unrelated(
            matrix[held_indices]
        )
        oof[held_indices] = probabilities
        fold_indices[held_indices] = fold_index
        selected_score = next(
            item
            for item in selection.candidate_scores
            if item.candidate_id == selection.selected_spec.candidate_id
        )
        if (
            selection.anchor_score.related_as_unrelated_rate is None
            or selection.anchor_score.log_loss is None
            or selected_score.related_as_unrelated_rate is None
            or selected_score.log_loss is None
        ):
            raise QwenHeadChallengerError("qwen_head_selection_score_invalid")
        selections.append(
            QwenHeadOuterSelection(
                fold_index=fold_index,
                fit_count=len(fit_indices),
                held_count=len(held_indices),
                inner_fold_count=selection.inner_fold_count,
                selected_candidate_id=selection.selected_spec.candidate_id,
                selected_family=selection.selected_spec.family,
                selected_embedding_dimension=(
                    selection.selected_spec.embedding_dimension
                ),
                selected_C=selection.selected_spec.C,
                selected_class_weight=selection.selected_spec.class_weight,
                anchor_related_as_unrelated_rate=(
                    selection.anchor_score.related_as_unrelated_rate
                ),
                selected_related_as_unrelated_rate=(
                    selected_score.related_as_unrelated_rate
                ),
                anchor_log_loss=selection.anchor_score.log_loss,
                selected_log_loss=selected_score.log_loss,
                selected_is_anchor=(
                    selection.selected_spec.candidate_id
                    == plan.safety_anchor_candidate_id
                ),
            )
        )
    if not np.isfinite(oof).all() or np.any(fold_indices < 0):
        raise QwenHeadChallengerError("qwen_head_outer_oof_incomplete")
    final_selection = _select_inner(
        members,
        matrix,
        plan,
        random_seed=plan.random_seed + 900001,
    )
    rows = tuple(
        sorted(
            (
                QwenHeadOofProbability(
                    member_key=item.member_key,
                    component_id=item.component_id,
                    tourism_label=item.tourism_label,
                    p_unrelated=float(oof[index]),
                    fold_index=int(fold_indices[index]),
                )
                for index, item in enumerate(members)
            ),
            key=lambda item: item.member_key,
        )
    )
    return QwenHeadChallengerResult(
        selected_spec=final_selection.selected_spec,
        selected_model=final_selection.selected_model,
        oof_probabilities=rows,
        outer_selections=tuple(selections),
        full_training_anchor_score=final_selection.anchor_score,
        full_training_candidate_scores=final_selection.candidate_scores,
        outer_fold_count=len(outer_folds),
        final_inner_fold_count=final_selection.inner_fold_count,
        training_metrics=evaluate_binary_probabilities(
            [item.tourism_label for item in members], oof
        ),
    )
