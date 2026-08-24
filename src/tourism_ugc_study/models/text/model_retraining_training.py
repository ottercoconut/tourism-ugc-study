"""固定三候选的leakage-group OOF、交叉拟合校准与最终训练。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

from .formal_baseline import (
    FormalBaselineError,
    SigmoidCalibrator,
    evaluate_binary_probabilities,
    fit_sigmoid_calibrator,
    unrelated_margins,
    valid_group_folds,
)
from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_snapshot import RetrainingDocument


class ModelRetrainingTrainingError(RuntimeError):
    """固定候选分组训练、校准或输出无效时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、标签成员或路径的稳定失败码。"""

        super().__init__("formal model retraining failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RetrainingOofObservation:
    """一条成员自身标签未参与模型或校准拟合的折外概率。"""

    member_key: str
    component_id: str
    tourism_label: str
    evidence_origin: str
    analysis_weight: float | None
    historical_test_consumed: bool
    candidate_name: str
    candidate_id: str
    fold_index: int
    calibration_excluded_fold_index: int
    margin: float
    p_unrelated: float


@dataclass(frozen=True)
class FrozenRetrainingCandidate:
    """一个可纯预测的最终候选及其完整训练集校准器。"""

    candidate_name: str
    candidate_id: str
    family: str
    qwen_classifier: LinearSVC
    sparse_pipeline: Pipeline
    fusion_classifier: LogisticRegression | None
    qwen_feature_calibrator: SigmoidCalibrator | None
    sparse_feature_calibrator: SigmoidCalibrator | None
    output_calibrator: SigmoidCalibrator
    embedding_dimension: int
    fit_scope: str = "all_1300_after_oof_evaluation"

    def predict_p_unrelated(
        self, texts: Sequence[str], embeddings: np.ndarray | None = None
    ) -> np.ndarray:
        """不调用fit地生成当前候选的校准无关概率。"""

        if not texts or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise ModelRetrainingTrainingError(
                "model_retraining_prediction_text_invalid"
            )
        if self.candidate_name == "qwen_linear_svc":
            if embeddings is None:
                raise ModelRetrainingTrainingError(
                    "model_retraining_prediction_embeddings_required"
                )
            matrix = _validate_embeddings(
                embeddings,
                dimension=self.embedding_dimension,
                expected_count=len(texts),
            )
            margins = _svc_margins(self.qwen_classifier, matrix)
        elif self.candidate_name == "sparse_linear_svc":
            margins = unrelated_margins(self.sparse_pipeline, texts)
        elif self.candidate_name == "logit_fusion":
            if (
                self.fusion_classifier is None
                or self.qwen_feature_calibrator is None
                or self.sparse_feature_calibrator is None
            ):
                raise ModelRetrainingTrainingError(
                    "model_retraining_fusion_model_invalid"
                )
            if embeddings is None:
                raise ModelRetrainingTrainingError(
                    "model_retraining_prediction_embeddings_required"
                )
            matrix = _validate_embeddings(
                embeddings,
                dimension=self.embedding_dimension,
                expected_count=len(texts),
            )
            qwen_margin = _svc_margins(self.qwen_classifier, matrix)
            sparse_margin = unrelated_margins(self.sparse_pipeline, texts)
            features = np.column_stack(
                [
                    _calibrated_logits(
                        self.qwen_feature_calibrator, qwen_margin
                    ),
                    _calibrated_logits(
                        self.sparse_feature_calibrator, sparse_margin
                    ),
                ]
            )
            margins = _logistic_margins(self.fusion_classifier, features)
        else:
            raise ModelRetrainingTrainingError(
                "model_retraining_candidate_name_invalid"
            )
        probabilities = self.output_calibrator.predict(margins)
        if (
            probabilities.shape != (len(texts),)
            or not np.isfinite(probabilities).all()
        ):
            raise ModelRetrainingTrainingError(
                "model_retraining_prediction_probability_invalid"
            )
        return probabilities


@dataclass(frozen=True)
class ModelRetrainingTrainingResult:
    """三候选OOF证据、最终模型及泄漏审计。"""

    candidates: tuple[FrozenRetrainingCandidate, ...]
    oof_observations: tuple[RetrainingOofObservation, ...]
    fold_assignments: Mapping[str, int]
    candidate_metrics: Mapping[str, Mapping[str, Any]]
    outer_fold_count: int
    fit_call_count: int
    component_cross_fold_violation_count: int
    historical_test_reopened: bool


def _candidate_id(plan: ModelRetrainingPlan, name: str) -> str:
    """由计划和候选参数生成稳定身份。"""

    candidate = next(item for item in plan.candidates if item.name == name)
    payload = {
        "plan_id": plan.plan_id,
        "name": name,
        "family": candidate.family,
        "parameters": dict(candidate.parameters),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def _validate_documents(documents: Sequence[RetrainingDocument]) -> None:
    """要求1,300条身份、正文、标签与component均可训练。"""

    if len(documents) != 1300:
        raise ModelRetrainingTrainingError(
            "model_retraining_training_count_invalid"
        )
    if (
        len({item.member_key for item in documents}) != len(documents)
        or len({item.identity for item in documents}) != len(documents)
        or {item.tourism_label for item in documents}
        != {"related", "unrelated"}
        or any(
            not item.component_id or not item.normalized_model_text.strip()
            for item in documents
        )
    ):
        raise ModelRetrainingTrainingError(
            "model_retraining_training_members_invalid"
        )


def _validate_embeddings(
    embeddings: np.ndarray, *, dimension: int, expected_count: int
) -> np.ndarray:
    """验证Qwen矩阵同序、有限且逐行L2规范化。"""

    matrix = np.asarray(embeddings, dtype=np.float32)
    if (
        matrix.shape != (expected_count, dimension)
        or not np.isfinite(matrix).all()
        or not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    ):
        raise ModelRetrainingTrainingError(
            "model_retraining_training_embeddings_invalid"
        )
    return matrix


def _qwen_classifier(plan: ModelRetrainingPlan) -> LinearSVC:
    """构造冻结Qwen向量线性头。"""

    parameters = plan.candidates[0].parameters
    return LinearSVC(
        C=float(parameters["C"]),
        class_weight=parameters["class_weight"],
        random_state=plan.random_seed,
    )


def _sparse_pipeline(plan: ModelRetrainingPlan) -> Pipeline:
    """构造冻结字符TF-IDF线性头。"""

    parameters = plan.candidates[1].parameters
    return Pipeline(
        [
            (
                "vectorizer",
                TfidfVectorizer(
                    analyzer="char",
                    ngram_range=tuple(parameters["ngram_range"]),
                    min_df=int(parameters["min_df"]),
                    max_df=float(parameters["max_df"]),
                    sublinear_tf=bool(parameters["sublinear_tf"]),
                ),
            ),
            (
                "classifier",
                LinearSVC(
                    C=float(parameters["C"]),
                    class_weight=parameters["class_weight"],
                    random_state=plan.random_seed,
                ),
            ),
        ]
    )


def _fusion_classifier(plan: ModelRetrainingPlan) -> LogisticRegression:
    """构造只读取两个交叉拟合logit的固定融合头。"""

    parameters = plan.candidates[2].parameters
    return LogisticRegression(
        C=float(parameters["C"]),
        class_weight=parameters["class_weight"],
        solver="lbfgs",
        max_iter=2000,
        random_state=plan.random_seed,
    )


def _svc_margins(classifier: LinearSVC, matrix: np.ndarray) -> np.ndarray:
    """统一LinearSVC margin为正方向代表unrelated。"""

    classes = [str(item) for item in classifier.classes_]
    try:
        values = np.asarray(classifier.decision_function(matrix), dtype=float)
    except ValueError as exc:
        raise ModelRetrainingTrainingError(
            "model_retraining_qwen_margin_failed"
        ) from exc
    if (
        set(classes) != {"related", "unrelated"}
        or values.ndim != 1
        or not np.isfinite(values).all()
    ):
        raise ModelRetrainingTrainingError(
            "model_retraining_qwen_margin_invalid"
        )
    return values if classes[1] == "unrelated" else -values


def _logistic_margins(
    classifier: LogisticRegression, features: np.ndarray
) -> np.ndarray:
    """统一融合逻辑回归margin为正方向代表unrelated。"""

    classes = [str(item) for item in classifier.classes_]
    try:
        values = np.asarray(classifier.decision_function(features), dtype=float)
    except ValueError as exc:
        raise ModelRetrainingTrainingError(
            "model_retraining_fusion_margin_failed"
        ) from exc
    if (
        set(classes) != {"related", "unrelated"}
        or values.ndim != 1
        or not np.isfinite(values).all()
    ):
        raise ModelRetrainingTrainingError(
            "model_retraining_fusion_margin_invalid"
        )
    return values if classes[1] == "unrelated" else -values


def _calibrated_logits(
    calibrator: SigmoidCalibrator, margins: np.ndarray
) -> np.ndarray:
    """直接返回Sigmoid的有限logit，避免概率裁剪损失。"""

    values = calibrator.slope * np.asarray(margins) + calibrator.intercept
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ModelRetrainingTrainingError(
            "model_retraining_fusion_feature_invalid"
        )
    return values


def _cross_fitted_probabilities(
    margins: np.ndarray,
    labels: np.ndarray,
    fold_by_index: np.ndarray,
) -> tuple[np.ndarray, SigmoidCalibrator]:
    """逐外折排除成员自身折后拟合Sigmoid，另拟合生产校准器。"""

    probabilities = np.full(len(labels), np.nan, dtype=float)
    encoded = np.asarray(
        [1 if label == "unrelated" else 0 for label in labels], dtype=int
    )
    try:
        for fold_index in sorted(set(int(value) for value in fold_by_index)):
            held = fold_by_index == fold_index
            fitted = ~held
            calibrator = fit_sigmoid_calibrator(
                margins[fitted], encoded[fitted]
            )
            probabilities[held] = calibrator.predict(margins[held])
        final = fit_sigmoid_calibrator(margins, encoded)
    except FormalBaselineError as exc:
        raise ModelRetrainingTrainingError(
            "model_retraining_crossfit_calibration_failed"
        ) from exc
    if not np.isfinite(probabilities).all():
        raise ModelRetrainingTrainingError(
            "model_retraining_crossfit_calibration_incomplete"
        )
    return probabilities, final


def _base_inner_oof(
    texts: Sequence[str],
    embeddings: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    *,
    plan: ModelRetrainingPlan,
    random_seed: int,
) -> tuple[np.ndarray, np.ndarray, int]:
    """在一个外层训练端内生成两个基础模型的分组OOF margin。"""

    try:
        folds = valid_group_folds(
            labels,
            groups,
            desired_splits=plan.outer_folds,
            random_seed=random_seed,
            reason_code="model_retraining_inner_folds_unavailable",
        )
    except FormalBaselineError as exc:
        raise ModelRetrainingTrainingError(
            "model_retraining_inner_folds_unavailable"
        ) from exc
    if len(folds) < plan.minimum_folds:
        raise ModelRetrainingTrainingError(
            "model_retraining_inner_folds_unavailable"
        )
    qwen_oof = np.full(len(labels), np.nan)
    sparse_oof = np.full(len(labels), np.nan)
    fit_count = 0
    for fit_indices, held_indices in folds:
        qwen = _qwen_classifier(plan)
        sparse = _sparse_pipeline(plan)
        try:
            qwen.fit(embeddings[fit_indices], labels[fit_indices])
            sparse.fit(
                [texts[int(index)] for index in fit_indices],
                labels[fit_indices],
            )
            fit_count += 2
            qwen_oof[held_indices] = _svc_margins(
                qwen, embeddings[held_indices]
            )
            sparse_oof[held_indices] = unrelated_margins(
                sparse, [texts[int(index)] for index in held_indices]
            )
        except (ValueError, FormalBaselineError) as exc:
            raise ModelRetrainingTrainingError(
                "model_retraining_inner_fit_failed"
            ) from exc
    if not np.isfinite(qwen_oof).all() or not np.isfinite(sparse_oof).all():
        raise ModelRetrainingTrainingError(
            "model_retraining_inner_oof_incomplete"
        )
    return qwen_oof, sparse_oof, fit_count


def train_fixed_retraining_candidates(
    documents: Sequence[RetrainingDocument],
    embeddings: np.ndarray,
    *,
    plan: ModelRetrainingPlan,
) -> ModelRetrainingTrainingResult:
    """训练固定三候选并生成成员自身完全排除的外层OOF概率。

    Notes:
        Wave A/B分析权重只被复制到OOF记录供后续评价，本函数的所有``fit``均不
        接收``sample_weight``。平台、样本框、旧概率和抽样层不在函数接口中。
    """

    _validate_documents(documents)
    matrix = _validate_embeddings(
        embeddings,
        dimension=plan.qwen_embedding_dimension,
        expected_count=len(documents),
    )
    texts = [item.normalized_model_text for item in documents]
    labels = np.asarray([item.tourism_label for item in documents], dtype=object)
    groups = np.asarray([item.component_id for item in documents], dtype=object)
    try:
        folds = valid_group_folds(
            labels,
            groups,
            desired_splits=plan.outer_folds,
            random_seed=plan.random_seed,
            reason_code="model_retraining_outer_folds_unavailable",
        )
    except FormalBaselineError as exc:
        raise ModelRetrainingTrainingError(
            "model_retraining_outer_folds_unavailable"
        ) from exc
    if len(folds) < plan.minimum_folds:
        raise ModelRetrainingTrainingError(
            "model_retraining_outer_folds_unavailable"
        )
    fold_by_index = np.full(len(documents), -1, dtype=int)
    qwen_oof = np.full(len(documents), np.nan)
    sparse_oof = np.full(len(documents), np.nan)
    fusion_oof = np.full(len(documents), np.nan)
    fit_count = 0
    for fold_index, (fit_indices, held_indices) in enumerate(folds):
        fold_by_index[held_indices] = fold_index
        qwen = _qwen_classifier(plan)
        sparse = _sparse_pipeline(plan)
        try:
            qwen.fit(matrix[fit_indices], labels[fit_indices])
            sparse.fit(
                [texts[int(index)] for index in fit_indices],
                labels[fit_indices],
            )
            fit_count += 2
            held_qwen = _svc_margins(qwen, matrix[held_indices])
            held_sparse = unrelated_margins(
                sparse, [texts[int(index)] for index in held_indices]
            )
        except (ValueError, FormalBaselineError) as exc:
            raise ModelRetrainingTrainingError(
                "model_retraining_outer_fit_failed"
            ) from exc
        qwen_oof[held_indices] = held_qwen
        sparse_oof[held_indices] = held_sparse
        fit_texts = [texts[int(index)] for index in fit_indices]
        inner_qwen, inner_sparse, inner_fit_count = _base_inner_oof(
            fit_texts,
            matrix[fit_indices],
            labels[fit_indices],
            groups[fit_indices],
            plan=plan,
            random_seed=plan.random_seed + fold_index + 1,
        )
        fit_count += inner_fit_count
        encoded_fit = np.asarray(
            [1 if label == "unrelated" else 0 for label in labels[fit_indices]],
            dtype=int,
        )
        try:
            qwen_calibrator = fit_sigmoid_calibrator(
                inner_qwen, encoded_fit
            )
            sparse_calibrator = fit_sigmoid_calibrator(
                inner_sparse, encoded_fit
            )
            fusion_features = np.column_stack(
                [
                    _calibrated_logits(qwen_calibrator, inner_qwen),
                    _calibrated_logits(sparse_calibrator, inner_sparse),
                ]
            )
            fusion = _fusion_classifier(plan)
            fusion.fit(fusion_features, labels[fit_indices])
            fit_count += 1
            held_features = np.column_stack(
                [
                    _calibrated_logits(qwen_calibrator, held_qwen),
                    _calibrated_logits(sparse_calibrator, held_sparse),
                ]
            )
            fusion_oof[held_indices] = _logistic_margins(
                fusion, held_features
            )
        except (ValueError, FormalBaselineError) as exc:
            raise ModelRetrainingTrainingError(
                "model_retraining_fusion_outer_fit_failed"
            ) from exc
    if (
        np.any(fold_by_index < 0)
        or not np.isfinite(qwen_oof).all()
        or not np.isfinite(sparse_oof).all()
        or not np.isfinite(fusion_oof).all()
    ):
        raise ModelRetrainingTrainingError(
            "model_retraining_outer_oof_incomplete"
        )
    component_folds: dict[str, set[int]] = {}
    for index, document in enumerate(documents):
        component_folds.setdefault(document.component_id, set()).add(
            int(fold_by_index[index])
        )
    violation_count = sum(len(values) != 1 for values in component_folds.values())
    if violation_count:
        raise ModelRetrainingTrainingError(
            "model_retraining_component_cross_fold_leakage"
        )

    margins_by_name = {
        "qwen_linear_svc": qwen_oof,
        "sparse_linear_svc": sparse_oof,
        "logit_fusion": fusion_oof,
    }
    probabilities_by_name: dict[str, np.ndarray] = {}
    output_calibrators: dict[str, SigmoidCalibrator] = {}
    for name, margins in margins_by_name.items():
        probabilities, calibrator = _cross_fitted_probabilities(
            margins, labels, fold_by_index
        )
        probabilities_by_name[name] = probabilities
        output_calibrators[name] = calibrator

    # 完整1,300条只在全部OOF证据生成后拟合生产候选；历史测试不再评价。
    final_qwen = _qwen_classifier(plan)
    final_sparse = _sparse_pipeline(plan)
    try:
        final_qwen.fit(matrix, labels)
        final_sparse.fit(texts, labels)
        fit_count += 2
    except ValueError as exc:
        raise ModelRetrainingTrainingError(
            "model_retraining_final_base_fit_failed"
        ) from exc
    encoded_all = np.asarray(
        [1 if label == "unrelated" else 0 for label in labels], dtype=int
    )
    try:
        qwen_feature_calibrator = fit_sigmoid_calibrator(
            qwen_oof, encoded_all
        )
        sparse_feature_calibrator = fit_sigmoid_calibrator(
            sparse_oof, encoded_all
        )
        final_fusion = _fusion_classifier(plan)
        final_fusion.fit(
            np.column_stack(
                [
                    _calibrated_logits(qwen_feature_calibrator, qwen_oof),
                    _calibrated_logits(sparse_feature_calibrator, sparse_oof),
                ]
            ),
            labels,
        )
        fit_count += 1
    except (ValueError, FormalBaselineError) as exc:
        raise ModelRetrainingTrainingError(
            "model_retraining_final_fusion_fit_failed"
        ) from exc

    candidates = tuple(
        FrozenRetrainingCandidate(
            candidate_name=name,
            candidate_id=_candidate_id(plan, name),
            family=next(item.family for item in plan.candidates if item.name == name),
            qwen_classifier=final_qwen,
            sparse_pipeline=final_sparse,
            fusion_classifier=(final_fusion if name == "logit_fusion" else None),
            qwen_feature_calibrator=(
                qwen_feature_calibrator if name == "logit_fusion" else None
            ),
            sparse_feature_calibrator=(
                sparse_feature_calibrator if name == "logit_fusion" else None
            ),
            output_calibrator=output_calibrators[name],
            embedding_dimension=plan.qwen_embedding_dimension,
        )
        for name in ("qwen_linear_svc", "sparse_linear_svc", "logit_fusion")
    )
    oof_rows: list[RetrainingOofObservation] = []
    for candidate in candidates:
        margins = margins_by_name[candidate.candidate_name]
        probabilities = probabilities_by_name[candidate.candidate_name]
        for index, document in enumerate(documents):
            fold_index = int(fold_by_index[index])
            oof_rows.append(
                RetrainingOofObservation(
                    member_key=document.member_key,
                    component_id=document.component_id,
                    tourism_label=document.tourism_label,
                    evidence_origin=document.evidence_origin,
                    analysis_weight=document.analysis_weight,
                    historical_test_consumed=document.historical_test_consumed,
                    candidate_name=candidate.candidate_name,
                    candidate_id=candidate.candidate_id,
                    fold_index=fold_index,
                    calibration_excluded_fold_index=fold_index,
                    margin=float(margins[index]),
                    p_unrelated=float(probabilities[index]),
                )
            )
    metrics = {
        name: evaluate_binary_probabilities(
            [item.tourism_label for item in documents],
            probabilities_by_name[name],
        )
        for name in margins_by_name
    }
    return ModelRetrainingTrainingResult(
        candidates=candidates,
        oof_observations=tuple(oof_rows),
        fold_assignments={
            document.member_key: int(fold_by_index[index])
            for index, document in enumerate(documents)
        },
        candidate_metrics=metrics,
        outer_fold_count=len(folds),
        fit_call_count=fit_count,
        component_cross_fold_violation_count=violation_count,
        historical_test_reopened=False,
    )
