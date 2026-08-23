"""UGC 安全优先的稀疏文本 challenger 嵌套分组比较。

本模块只接收训练集合的规范化文本、人工标签与泄漏分量。候选选择、SVM
概率校准和 NB log-count ratio 均限制在每个外层折的训练端；平台、验证集和
锁定测试集不属于输入契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import sparse
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC
from sklearn.utils.validation import check_is_fitted

from .formal_baseline import (
    SigmoidCalibrator,
    evaluate_binary_probabilities,
    fit_sigmoid_calibrator,
    unrelated_margins,
    valid_group_folds,
)
from .model_acceptance import PairedOofObservation
from .sparse_challenger_config import (
    SparseCandidateSpec,
    SparseChallengerPlan,
)


class SparseChallengerError(RuntimeError):
    """challenger 训练违反输入或防泄漏契约时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、帖子身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: 供 CLI、测试和 artifact 使用的失败码。
        """

        super().__init__("formal sparse challenger training failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ChallengerDocument:
    """不含平台与集合身份的单条训练侧 challenger 投影。

    Attributes:
        member_key: 当前训练包内唯一的去标识成员键。
        component_id: 作者、精确重复和确认近重复形成的泄漏分量。
        normalized_model_text: 冻结规范化规则生成的模型文本。
        tourism_label: ``related`` 或 ``unrelated``。
    """

    member_key: str
    component_id: str
    normalized_model_text: str
    tourism_label: str


@dataclass(frozen=True)
class CandidateDevelopmentScore:
    """一个候选在同一组内层 OOF 证据上的选择记录。

    Attributes:
        candidate_id: 内容寻址的候选身份。
        family: 候选模型族。
        status: ``evaluated`` 或 ``fit_failed``。
        eligible_by_related_safety: 是否未劣于同折 baseline 的 UGC 安全点估计。
        fold_count: 实际使用的分组折数；失败时为零。
        related_as_unrelated_rate: 相关 UGC 被诊断为无关的比例。
        log_loss: 无关概率的 log loss。
        pr_auc_unrelated: 以无关为正类的 PR-AUC。
        brier_score: 无关概率的 Brier score。
        failure_reason_code: 候选失败的稳定原因；成功时为 ``None``。
    """

    candidate_id: str
    family: str
    status: str
    eligible_by_related_safety: bool
    fold_count: int
    related_as_unrelated_rate: float | None
    log_loss: float | None
    pr_auc_unrelated: float | None
    brier_score: float | None
    failure_reason_code: str | None


@dataclass(frozen=True)
class OuterFoldSelection:
    """一个外层折中完全由训练端决定的候选选择摘要。

    Attributes:
        fold_index: 从零开始的外层折序号。
        fit_count: 外层训练端记录数。
        held_count: 外层留出端记录数。
        inner_fold_count: 选择和校准实际使用的内层折数。
        selected_candidate_id: 选中候选或 baseline anchor 身份。
        selected_family: 选中模型族。
        baseline_related_as_unrelated_rate: 同折 baseline 的安全诊断率。
        selected_related_as_unrelated_rate: 所选模型的安全诊断率。
        baseline_log_loss: 同折 baseline 内层 OOF log loss。
        selected_log_loss: 所选模型内层 OOF log loss。
        baseline_fallback: 无安全合格 challenger 时是否回退 baseline。
    """

    fold_index: int
    fit_count: int
    held_count: int
    inner_fold_count: int
    selected_candidate_id: str
    selected_family: str
    baseline_related_as_unrelated_rate: float
    selected_related_as_unrelated_rate: float
    baseline_log_loss: float
    selected_log_loss: float
    baseline_fallback: bool


@dataclass(frozen=True)
class FrozenSparseCandidateModel:
    """一个仅在给定训练端拟合、可生成无关概率的稀疏模型。

    Attributes:
        spec: 冻结候选规范。
        pipeline: 已在完整训练端拟合的向量器、可选 NB 比率和分类器。
        calibrator: SVM 的分组 OOF Sigmoid；逻辑回归为 ``None``。
        fit_scope: 明确模型只使用调用方给定的训练端。
    """

    spec: SparseCandidateSpec
    pipeline: Pipeline
    calibrator: SigmoidCalibrator | None
    fit_scope: str = "provided_train_only"

    def predict_p_unrelated(self, texts: Sequence[str]) -> np.ndarray:
        """生成与输入顺序一致的有限无关概率。

        Args:
            texts: 冻结规范化文本，不得附加平台字段。

        Returns:
            闭区间内的一维 ``p_unrelated``。

        Raises:
            SparseChallengerError: 文本、分类器类别或输出概率非法。
        """

        if not texts or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise SparseChallengerError("sparse_challenger_prediction_text_invalid")
        if self.spec.classifier == "linear_svc":
            if self.calibrator is None:
                raise SparseChallengerError("sparse_challenger_calibrator_missing")
            try:
                values = self.calibrator.predict(
                    unrelated_margins(self.pipeline, texts)
                )
            except RuntimeError as exc:
                raise SparseChallengerError(
                    "sparse_challenger_margin_prediction_failed"
                ) from exc
        else:
            classifier = self.pipeline.named_steps["classifier"]
            classes = [str(value) for value in classifier.classes_]
            if set(classes) != {"related", "unrelated"}:
                raise SparseChallengerError(
                    "sparse_challenger_classifier_classes_invalid"
                )
            probabilities = np.asarray(
                self.pipeline.predict_proba(texts), dtype=float
            )
            values = probabilities[:, classes.index("unrelated")]
        values = np.asarray(values, dtype=float)
        if (
            values.ndim != 1
            or len(values) != len(texts)
            or not np.isfinite(values).all()
            or np.any(values < 0.0)
            or np.any(values > 1.0)
        ):
            raise SparseChallengerError(
                "sparse_challenger_probability_prediction_invalid"
            )
        return values


@dataclass(frozen=True)
class SparseChallengerResult:
    """完成训练侧嵌套比较但未读取验证或测试集合的结果。

    Attributes:
        selected_spec: 全训练集合内层 OOF 选择的唯一最终规范。
        selected_model: 只在完整训练集合拟合的最终候选模型。
        paired_outer_oof: baseline 与选择程序逐条配对的外层 OOF 证据。
        outer_selections: 各外层折的选择摘要。
        full_training_baseline_score: 全训练集合的 baseline anchor 内层 OOF 摘要。
        full_training_candidate_scores: 54 个预登记候选的内层 OOF 摘要。
        outer_fold_count: 实际外层分组折数。
        final_inner_fold_count: 最终选择实际内层分组折数。
        baseline_fallback: 最终是否因无安全候选而保留 baseline。
    """

    selected_spec: SparseCandidateSpec
    selected_model: FrozenSparseCandidateModel
    paired_outer_oof: tuple[PairedOofObservation, ...]
    outer_selections: tuple[OuterFoldSelection, ...]
    full_training_baseline_score: CandidateDevelopmentScore
    full_training_candidate_scores: tuple[CandidateDevelopmentScore, ...]
    outer_fold_count: int
    final_inner_fold_count: int
    baseline_fallback: bool


@dataclass(frozen=True)
class SparseCandidatePartitionFit:
    """供融合模型复用的固定 sparse 候选分区拟合结果。"""

    model: FrozenSparseCandidateModel
    oof_probabilities: np.ndarray
    fold_count: int


class NBLogCountRatioTransformer(TransformerMixin, BaseEstimator):
    """按训练端标签估计 Wang–Manning 风格 NB log-count ratio。

    二值文档特征分别按类别求和，加 ``alpha`` 后做类别内 L1 归一化，最终
    使用 ``log(P(feature|unrelated) / P(feature|related))`` 缩放稀疏矩阵。
    ``fit`` 必须位于 sklearn pipeline 内，以保证每个折独立估计比率。
    """

    def __init__(self, alpha: float = 1.0) -> None:
        """保存固定平滑常数。

        Args:
            alpha: 每个类别、每个特征的加性平滑值，必须为正有限数。
        """

        self.alpha = alpha

    def fit(self, X, y):
        """从当前训练端估计无关相对相关的 log-count ratio。

        Args:
            X: 二值稀疏文档特征矩阵。
            y: 与行等长的 ``related``/``unrelated`` 标签。

        Returns:
            已记录一维 ``log_count_ratio_`` 的自身。

        Raises:
            ValueError: 平滑值、矩阵或标签不满足二分类契约。
        """

        alpha = float(self.alpha)
        labels = np.asarray(y, dtype=object)
        if (
            not np.isfinite(alpha)
            or alpha <= 0.0
            or getattr(X, "ndim", None) != 2
            or X.shape[0] != len(labels)
            or X.shape[1] <= 0
            or set(str(value) for value in labels) != {"related", "unrelated"}
        ):
            raise ValueError("nb log-count ratio input invalid")
        unrelated = alpha + np.asarray(
            X[labels == "unrelated"].sum(axis=0), dtype=float
        ).ravel()
        related = alpha + np.asarray(
            X[labels == "related"].sum(axis=0), dtype=float
        ).ravel()
        self.log_count_ratio_ = np.log(
            (unrelated / unrelated.sum()) / (related / related.sum())
        )
        if not np.isfinite(self.log_count_ratio_).all():
            raise ValueError("nb log-count ratio is not finite")
        return self

    def transform(self, X):
        """用已拟合比率逐列缩放稀疏矩阵。

        Args:
            X: 与拟合词表列数相同的稀疏特征矩阵。

        Returns:
            保持稀疏表示的逐列缩放矩阵。

        Raises:
            ValueError: 尚未拟合或列数漂移。
        """

        check_is_fitted(self, "log_count_ratio_")
        if X.ndim != 2 or X.shape[1] != len(self.log_count_ratio_):
            raise ValueError("nb log-count ratio feature mismatch")
        return sparse.csr_matrix(X).multiply(self.log_count_ratio_)


@dataclass(frozen=True)
class _CrossFitResult:
    """一个候选的分组折外概率与可复用校准器。"""

    probabilities: np.ndarray
    calibrator: SigmoidCalibrator | None
    fold_count: int


@dataclass(frozen=True)
class _InnerSelectionResult:
    """一次内层选择及其完整可审计证据。"""

    selected_spec: SparseCandidateSpec
    selected_model: FrozenSparseCandidateModel
    baseline_model: FrozenSparseCandidateModel
    baseline_score: CandidateDevelopmentScore
    candidate_scores: tuple[CandidateDevelopmentScore, ...]
    inner_fold_count: int
    baseline_fallback: bool


def _pipeline(spec: SparseCandidateSpec, random_seed: int) -> Pipeline:
    """按冻结候选规范构造一个尚未拟合的 pipeline。"""

    if spec.representation == "tfidf":
        vectorizer = TfidfVectorizer(
            analyzer="char",
            ngram_range=spec.ngram_range,
            min_df=spec.min_df,
            max_df=0.995,
            sublinear_tf=spec.sublinear_tf,
        )
        steps: list[tuple[str, Any]] = [("vectorizer", vectorizer)]
    elif spec.representation == "binary_count_nb_log_ratio":
        if not spec.binary or spec.alpha is None:
            raise SparseChallengerError("sparse_challenger_nb_spec_invalid")
        steps = [
            (
                "vectorizer",
                CountVectorizer(
                    analyzer="char",
                    ngram_range=spec.ngram_range,
                    min_df=spec.min_df,
                    max_df=0.995,
                    binary=True,
                    dtype=np.float64,
                ),
            ),
            ("nb_log_count_ratio", NBLogCountRatioTransformer(spec.alpha)),
        ]
    else:
        raise SparseChallengerError("sparse_challenger_representation_invalid")
    if spec.classifier == "linear_svc":
        classifier: Any = LinearSVC(
            C=spec.C,
            class_weight="balanced",
            random_state=random_seed,
        )
    elif spec.classifier == "logistic_regression" and spec.solver == "liblinear":
        classifier = LogisticRegression(
            C=spec.C,
            class_weight="balanced",
            solver="liblinear",
            random_state=random_seed,
            max_iter=5000,
        )
    else:
        raise SparseChallengerError("sparse_challenger_classifier_invalid")
    return Pipeline([*steps, ("classifier", classifier)])


def _validate_documents(documents: Sequence[ChallengerDocument]) -> None:
    """校验训练侧身份、文本、标签与泄漏分量。"""

    if not documents:
        raise SparseChallengerError("sparse_challenger_documents_empty")
    member_keys = [item.member_key for item in documents]
    if (
        len(member_keys) != len(set(member_keys))
        or any(
            not item.member_key
            or not item.component_id
            or not isinstance(item.normalized_model_text, str)
            or not item.normalized_model_text.strip()
            or item.tourism_label not in {"related", "unrelated"}
            for item in documents
        )
    ):
        raise SparseChallengerError("sparse_challenger_document_invalid")
    if {item.tourism_label for item in documents} != {"related", "unrelated"}:
        raise SparseChallengerError("sparse_challenger_documents_missing_class")


def _probabilities_from_pipeline(
    spec: SparseCandidateSpec,
    pipeline: Pipeline,
    texts: Sequence[str],
) -> np.ndarray:
    """读取一个未外部校准候选的原生无关概率。"""

    if spec.classifier != "logistic_regression":
        raise SparseChallengerError("sparse_challenger_native_probability_invalid")
    classifier = pipeline.named_steps["classifier"]
    classes = [str(value) for value in classifier.classes_]
    if set(classes) != {"related", "unrelated"}:
        raise SparseChallengerError("sparse_challenger_classifier_classes_invalid")
    values = np.asarray(pipeline.predict_proba(texts), dtype=float)
    return values[:, classes.index("unrelated")]


def _cross_fit(
    documents: Sequence[ChallengerDocument],
    spec: SparseCandidateSpec,
    *,
    desired_folds: int,
    minimum_folds: int,
    random_seed: int,
) -> _CrossFitResult:
    """在同一组分组折上生成候选概率并拟合可选 Sigmoid。"""

    labels = np.asarray([item.tourism_label for item in documents], dtype=object)
    groups = np.asarray([item.component_id for item in documents], dtype=object)
    try:
        folds = valid_group_folds(
            labels,
            groups,
            desired_splits=desired_folds,
            random_seed=random_seed,
            reason_code="sparse_challenger_group_folds_unavailable",
        )
    except RuntimeError as exc:
        raise SparseChallengerError(
            "sparse_challenger_group_folds_unavailable"
        ) from exc
    if len(folds) < minimum_folds:
        raise SparseChallengerError("sparse_challenger_group_folds_below_minimum")
    raw = np.full(len(documents), np.nan, dtype=float)
    for fit_indices, held_indices in folds:
        pipeline = _pipeline(spec, random_seed)
        try:
            pipeline.fit(
                [documents[int(index)].normalized_model_text for index in fit_indices],
                labels[fit_indices],
            )
            texts = [
                documents[int(index)].normalized_model_text for index in held_indices
            ]
            if spec.classifier == "linear_svc":
                raw[held_indices] = unrelated_margins(pipeline, texts)
            else:
                raw[held_indices] = _probabilities_from_pipeline(
                    spec, pipeline, texts
                )
        except (RuntimeError, ValueError) as exc:
            raise SparseChallengerError(
                "sparse_challenger_candidate_fold_fit_failed"
            ) from exc
    if not np.isfinite(raw).all():
        raise SparseChallengerError("sparse_challenger_oof_incomplete")
    if spec.classifier == "linear_svc":
        encoded = np.asarray(
            [1 if label == "unrelated" else 0 for label in labels], dtype=int
        )
        try:
            calibrator = fit_sigmoid_calibrator(raw, encoded)
            probabilities = calibrator.predict(raw)
        except RuntimeError as exc:
            raise SparseChallengerError(
                "sparse_challenger_calibration_failed"
            ) from exc
    else:
        calibrator = None
        probabilities = raw
    if (
        not np.isfinite(probabilities).all()
        or np.any(probabilities < 0.0)
        or np.any(probabilities > 1.0)
    ):
        raise SparseChallengerError("sparse_challenger_oof_probability_invalid")
    return _CrossFitResult(probabilities, calibrator, len(folds))


def _fit_full(
    documents: Sequence[ChallengerDocument],
    spec: SparseCandidateSpec,
    calibrator: SigmoidCalibrator | None,
    *,
    random_seed: int,
) -> FrozenSparseCandidateModel:
    """在当前调用方提供的完整训练端拟合一个已选规范。"""

    pipeline = _pipeline(spec, random_seed)
    try:
        pipeline.fit(
            [item.normalized_model_text for item in documents],
            [item.tourism_label for item in documents],
        )
    except (RuntimeError, ValueError) as exc:
        raise SparseChallengerError("sparse_challenger_final_fit_failed") from exc
    return FrozenSparseCandidateModel(spec, pipeline, calibrator)


def fit_sparse_candidate_partition(
    documents: Sequence[ChallengerDocument],
    spec: SparseCandidateSpec,
    *,
    desired_folds: int,
    minimum_folds: int,
    random_seed: int,
) -> SparseCandidatePartitionFit:
    """仅在调用方给定训练分区内交叉拟合固定 sparse 候选。

    Args:
        documents: 当前分区的规范化文本、标签与 leakage component。
        spec: 已由既有正式运行选定并重新绑定的固定 sparse 规范。
        desired_folds: 当前分区期望的分组折数。
        minimum_folds: 仍允许训练的最少有效折数。
        random_seed: 由外层折确定的稳定随机种子。

    Returns:
        当前分区的逐成员 OOF 概率和完整分区拟合模型。

    Raises:
        SparseChallengerError: 文档、分组折、校准或拟合非法。

    Notes:
        本接口不重新选择 sparse 网格。它只为融合层在每个外层训练端重建
        固定 comparator，避免使用可能由外层留出标签参与拟合的全局 OOF。
    """

    _validate_documents(documents)
    cross_fit = _cross_fit(
        documents,
        spec,
        desired_folds=desired_folds,
        minimum_folds=minimum_folds,
        random_seed=random_seed,
    )
    model = _fit_full(
        documents, spec, cross_fit.calibrator, random_seed=random_seed
    )
    return SparseCandidatePartitionFit(
        model=model,
        oof_probabilities=np.asarray(cross_fit.probabilities, dtype=float),
        fold_count=cross_fit.fold_count,
    )


def _score(
    spec: SparseCandidateSpec,
    labels: Sequence[str],
    cross_fit: _CrossFitResult,
    *,
    baseline_related_as_unrelated_rate: float | None,
) -> CandidateDevelopmentScore:
    """把折外概率转成安全优先的候选选择摘要。"""

    try:
        metrics = evaluate_binary_probabilities(labels, cross_fit.probabilities)
    except ValueError as exc:
        raise SparseChallengerError("sparse_challenger_metrics_invalid") from exc
    related_count = int(metrics["related_count"])
    if related_count <= 0:
        raise SparseChallengerError("sparse_challenger_metrics_missing_related")
    rate = float(metrics["confusion"]["related_as_unrelated"]) / related_count
    eligible = (
        baseline_related_as_unrelated_rate is None
        or rate <= baseline_related_as_unrelated_rate + 1e-15
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
    spec: SparseCandidateSpec, reason_code: str
) -> CandidateDevelopmentScore:
    """生成不泄露异常内容的候选失败摘要。"""

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
    documents: Sequence[ChallengerDocument],
    plan: SparseChallengerPlan,
    *,
    random_seed: int,
) -> _InnerSelectionResult:
    """仅用当前训练端的分组 OOF 证据选择安全候选。"""

    labels = [item.tourism_label for item in documents]
    baseline_cross_fit = _cross_fit(
        documents,
        plan.baseline_spec,
        desired_folds=plan.inner_folds,
        minimum_folds=plan.minimum_folds,
        random_seed=random_seed,
    )
    baseline_score = _score(
        plan.baseline_spec,
        labels,
        baseline_cross_fit,
        baseline_related_as_unrelated_rate=None,
    )
    baseline_rate = baseline_score.related_as_unrelated_rate
    if baseline_rate is None:
        raise SparseChallengerError("sparse_challenger_baseline_score_invalid")
    scores: list[CandidateDevelopmentScore] = []
    cross_fits: dict[str, _CrossFitResult] = {}
    for spec in plan.candidates:
        try:
            cross_fit = _cross_fit(
                documents,
                spec,
                desired_folds=plan.inner_folds,
                minimum_folds=plan.minimum_folds,
                random_seed=random_seed,
            )
            score = _score(
                spec,
                labels,
                cross_fit,
                baseline_related_as_unrelated_rate=baseline_rate,
            )
        except SparseChallengerError as exc:
            scores.append(_failed_score(spec, exc.reason_code))
            continue
        scores.append(score)
        cross_fits[spec.candidate_id] = cross_fit
    eligible = [
        score
        for score in scores
        if score.status == "evaluated" and score.eligible_by_related_safety
    ]
    baseline_model = _fit_full(
        documents,
        plan.baseline_spec,
        baseline_cross_fit.calibrator,
        random_seed=random_seed,
    )
    if not eligible:
        return _InnerSelectionResult(
            selected_spec=plan.baseline_spec,
            selected_model=baseline_model,
            baseline_model=baseline_model,
            baseline_score=baseline_score,
            candidate_scores=tuple(scores),
            inner_fold_count=baseline_cross_fit.fold_count,
            baseline_fallback=True,
        )
    selected_score = min(
        eligible,
        key=lambda score: (
            float(score.log_loss),
            -float(score.pr_auc_unrelated),
            score.candidate_id,
        ),
    )
    selected_spec = next(
        spec
        for spec in plan.candidates
        if spec.candidate_id == selected_score.candidate_id
    )
    selected_cross_fit = cross_fits[selected_spec.candidate_id]
    selected_model = _fit_full(
        documents,
        selected_spec,
        selected_cross_fit.calibrator,
        random_seed=random_seed,
    )
    return _InnerSelectionResult(
        selected_spec=selected_spec,
        selected_model=selected_model,
        baseline_model=baseline_model,
        baseline_score=baseline_score,
        candidate_scores=tuple(scores),
        inner_fold_count=baseline_cross_fit.fold_count,
        baseline_fallback=False,
    )


def fit_sparse_challenger_nested(
    documents: Sequence[ChallengerDocument],
    *,
    plan: SparseChallengerPlan,
) -> SparseChallengerResult:
    """执行训练侧嵌套分组比较并拟合唯一最终候选。

    Args:
        documents: 仅包含冻结训练成员的规范化文本、标签与泄漏分量。
        plan: 绑定当前 baseline、训练 manifest 和验收策略的冻结计划。

    Returns:
        逐条配对外层 OOF、每折选择证据和全训练集合唯一候选模型。

    Raises:
        SparseChallengerError: 输入、分组折、baseline 或最终拟合失败。

    Notes:
        本函数不接受集合字段，因此无法读取验证集或锁定测试集；也不选择
        ``T_keep``/``T_exclude`` 等路由阈值。
    """

    _validate_documents(documents)
    labels = np.asarray([item.tourism_label for item in documents], dtype=object)
    groups = np.asarray([item.component_id for item in documents], dtype=object)
    try:
        outer_folds = valid_group_folds(
            labels,
            groups,
            desired_splits=plan.outer_folds,
            random_seed=plan.random_seed,
            reason_code="sparse_challenger_outer_folds_unavailable",
        )
    except RuntimeError as exc:
        raise SparseChallengerError(
            "sparse_challenger_outer_folds_unavailable"
        ) from exc
    if len(outer_folds) < plan.minimum_folds:
        raise SparseChallengerError("sparse_challenger_outer_folds_below_minimum")
    observations: list[PairedOofObservation] = []
    selections: list[OuterFoldSelection] = []
    seen: set[str] = set()
    for fold_index, (fit_indices, held_indices) in enumerate(outer_folds):
        fit_documents = [documents[int(index)] for index in fit_indices]
        held_documents = [documents[int(index)] for index in held_indices]
        selection = _select_inner(
            fit_documents,
            plan,
            random_seed=plan.random_seed + (fold_index + 1) * 1009,
        )
        texts = [item.normalized_model_text for item in held_documents]
        baseline_probabilities = selection.baseline_model.predict_p_unrelated(texts)
        candidate_probabilities = selection.selected_model.predict_p_unrelated(texts)
        selected_score = (
            selection.baseline_score
            if selection.baseline_fallback
            else next(
                score
                for score in selection.candidate_scores
                if score.candidate_id == selection.selected_spec.candidate_id
            )
        )
        if (
            selection.baseline_score.related_as_unrelated_rate is None
            or selection.baseline_score.log_loss is None
            or selected_score.related_as_unrelated_rate is None
            or selected_score.log_loss is None
        ):
            raise SparseChallengerError("sparse_challenger_selection_score_invalid")
        selections.append(
            OuterFoldSelection(
                fold_index=fold_index,
                fit_count=len(fit_documents),
                held_count=len(held_documents),
                inner_fold_count=selection.inner_fold_count,
                selected_candidate_id=selection.selected_spec.candidate_id,
                selected_family=selection.selected_spec.family,
                baseline_related_as_unrelated_rate=(
                    selection.baseline_score.related_as_unrelated_rate
                ),
                selected_related_as_unrelated_rate=(
                    selected_score.related_as_unrelated_rate
                ),
                baseline_log_loss=selection.baseline_score.log_loss,
                selected_log_loss=selected_score.log_loss,
                baseline_fallback=selection.baseline_fallback,
            )
        )
        for index, item in enumerate(held_documents):
            if item.member_key in seen:
                raise SparseChallengerError("sparse_challenger_outer_oof_duplicate")
            seen.add(item.member_key)
            observations.append(
                PairedOofObservation(
                    member_key=item.member_key,
                    component_id=item.component_id,
                    tourism_label=item.tourism_label,
                    baseline_p_unrelated=float(baseline_probabilities[index]),
                    candidate_p_unrelated=float(candidate_probabilities[index]),
                )
            )
    if seen != {item.member_key for item in documents}:
        raise SparseChallengerError("sparse_challenger_outer_oof_incomplete")
    final_selection = _select_inner(
        documents,
        plan,
        random_seed=plan.random_seed + 900001,
    )
    return SparseChallengerResult(
        selected_spec=final_selection.selected_spec,
        selected_model=final_selection.selected_model,
        paired_outer_oof=tuple(
            sorted(observations, key=lambda item: item.member_key)
        ),
        outer_selections=tuple(selections),
        full_training_baseline_score=final_selection.baseline_score,
        full_training_candidate_scores=final_selection.candidate_scores,
        outer_fold_count=len(outer_folds),
        final_inner_fold_count=final_selection.inner_fold_count,
        baseline_fallback=final_selection.baseline_fallback,
    )
