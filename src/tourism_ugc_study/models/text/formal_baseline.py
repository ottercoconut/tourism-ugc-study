"""正式数据清洗 baseline 的全局切分、折外校准与验证评估。

本模块只接收规范化文本、确定标签、时间和泄漏分量，不接收平台字段。锁定
测试集只形成成员 manifest；训练阶段不会读取测试文本、标签或计算测试指标。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
)
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

from tourism_ugc_study.cleaning.config import StableCleaningConfig


class FormalBaselineError(RuntimeError):
    """正式 baseline 输入或训练不满足失败关闭契约。

    Attributes:
        reason_code: 不包含正文、作者或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化去敏失败。

        Args:
            reason_code: 供 CLI、测试和运行 manifest 使用的稳定失败码。
        """

        super().__init__("formal cleaning baseline failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class BaselineDocument:
    """训练所需且不含平台的单条确定标签投影。

    Attributes:
        source_post_id: 派生库稳定帖子身份。
        source_version: 与完成 CSV 对应的源版本。
        captured_at_sort: 全局时间留出的稳定排序值。
        normalized_model_text: 冻结规则生成的模型文本。
        tourism_label: ``related`` 或 ``unrelated``。
        component_id: 作者、精确重复和确认近重复组成的泄漏分量。
        task_id: 完成 CSV 中的稳定人工任务身份。
    """

    source_post_id: int
    source_version: int
    captured_at_sort: str
    normalized_model_text: str
    tourism_label: str
    component_id: str
    task_id: str


@dataclass(frozen=True)
class BaselineSplitAssignment:
    """单条参考记录的冻结集合归属。

    Attributes:
        source_post_id: 帖子身份。
        source_version: 源版本。
        component_id: 泄漏分量身份。
        split_name: ``train``、``validation`` 或 ``test``。
    """

    source_post_id: int
    source_version: int
    component_id: str
    split_name: str


@dataclass(frozen=True)
class BaselineSplitPlan:
    """全局时间测试与分组分层训练/验证切分的不可变计划。

    Attributes:
        assignments: 全部确定标签记录的集合归属。
        manifest_sha256: 全部集合归属的规范摘要。
        train_manifest_sha256: 训练成员摘要。
        validation_manifest_sha256: 验证成员摘要。
        test_manifest_sha256: 锁定测试成员摘要。
        test_candidate_manifest_sha256: 扩展泄漏分量前的时间候选摘要。
        test_candidate_count: 初始全局时间候选数。
    """

    assignments: tuple[BaselineSplitAssignment, ...]
    manifest_sha256: str
    train_manifest_sha256: str
    validation_manifest_sha256: str
    test_manifest_sha256: str
    test_candidate_manifest_sha256: str
    test_candidate_count: int


@dataclass(frozen=True)
class SigmoidCalibrator:
    """把 SVM margin 映射为 ``p_unrelated`` 的一维逻辑校准器。

    Attributes:
        slope: margin 的逻辑回归系数。
        intercept: 逻辑回归截距。
    """

    slope: float
    intercept: float

    def predict(self, margins: Sequence[float]) -> np.ndarray:
        """计算有限且位于闭区间内的无关概率。

        Args:
            margins: 正方向代表更可能无关的 SVM decision margin。

        Returns:
            与输入顺序一致的 ``p_unrelated`` 数组。

        Raises:
            FormalBaselineError: 输入或输出含非有限数。
        """

        values = np.asarray(margins, dtype=float)
        if values.ndim != 1 or not np.isfinite(values).all():
            raise FormalBaselineError("calibration_margin_not_finite")
        logits = np.clip(self.slope * values + self.intercept, -709.0, 709.0)
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        if not np.isfinite(probabilities).all():
            raise FormalBaselineError("calibrated_probability_not_finite")
        return probabilities


@dataclass(frozen=True)
class FrozenBaselineModel:
    """可独立推理的字符 TF-IDF、线性 SVM 与 Sigmoid 校准器。

    Attributes:
        pipeline: 只在完整训练集合拟合的 sklearn pipeline。
        calibrator: 只使用训练集分组折外 margin 拟合的校准器。
        positive_class: 固定为 ``unrelated``。
        feature_contract: 固定为 ``normalized_model_text``。
        fit_scope: 明确模型未使用验证集和测试集拟合。
    """

    pipeline: Pipeline
    calibrator: SigmoidCalibrator
    positive_class: str = "unrelated"
    feature_contract: str = "normalized_model_text"
    fit_scope: str = "train_only"

    def predict_p_unrelated(self, texts: Sequence[str]) -> np.ndarray:
        """对规范化文本生成校准后的无关概率。

        Args:
            texts: 不含平台或其他元数据的规范化文本序列。

        Returns:
            与输入顺序一致的 ``p_unrelated`` 数组。

        Raises:
            FormalBaselineError: 文本为空、分类器类别不符或概率非法。
        """

        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise FormalBaselineError("prediction_text_invalid")
        margins = unrelated_margins(self.pipeline, texts)
        return self.calibrator.predict(margins)


@dataclass(frozen=True)
class BaselineProbability:
    """供校准审计或后续阈值讨论使用的逐条概率证据。

    Attributes:
        source_post_id: 帖子身份。
        source_version: 源版本。
        split_name: ``train_oof`` 或 ``validation``。
        tourism_label: 冻结人工标签。
        margin: 未校准的 SVM decision margin。
        p_unrelated: Sigmoid 校准后的无关概率。
    """

    source_post_id: int
    source_version: int
    split_name: str
    tourism_label: str
    margin: float
    p_unrelated: float


@dataclass(frozen=True)
class FormalBaselineResult:
    """完成训练但尚未开启锁定测试集的 baseline 结果。

    Attributes:
        model: 可冻结的训练集模型与校准器。
        split_plan: 锁定测试集在内的完整切分计划。
        train_oof_probabilities: 每条训练记录的折外概率。
        validation_probabilities: 独立验证集概率。
        validation_metrics: 只基于验证集的总体诊断指标。
        calibration_fold_count: 实际使用的分组折外折数。
    """

    model: FrozenBaselineModel
    split_plan: BaselineSplitPlan
    train_oof_probabilities: tuple[BaselineProbability, ...]
    validation_probabilities: tuple[BaselineProbability, ...]
    validation_metrics: Mapping[str, Any]
    calibration_fold_count: int


def _sha256(value: object) -> str:
    """计算规范 JSON 的 SHA-256。

    Args:
        value: 可 JSON 序列化对象。

    Returns:
        小写十六进制 SHA-256。
    """

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_binary(documents: Sequence[BaselineDocument], reason_code: str) -> None:
    """要求记录同时包含两个确定标签。

    Args:
        documents: 待检查记录。
        reason_code: 失败时暴露的稳定原因。

    Raises:
        FormalBaselineError: 未同时包含 ``related`` 和 ``unrelated``。
    """

    if {item.tourism_label for item in documents} != {"related", "unrelated"}:
        raise FormalBaselineError(reason_code)


def _time_key(document: BaselineDocument, random_seed: int) -> tuple[str, str]:
    """构造平台无关的稳定全局时间排序键。

    Args:
        document: 待排序记录。
        random_seed: 同时间值的稳定打散种子。

    Returns:
        时间字符串与去标识化稳定哈希组成的排序键。
    """

    tie = _sha256(
        [random_seed, "global-temporal-test", document.source_post_id, document.source_version]
    )
    return document.captured_at_sort, tie


def _valid_group_folds(
    labels: np.ndarray,
    groups: np.ndarray,
    *,
    desired_splits: int,
    random_seed: int,
    reason_code: str,
) -> tuple[tuple[np.ndarray, np.ndarray], ...]:
    """寻找每个训练端与留出端都含双类的分组分层折。

    Args:
        labels: 二分类字符串标签。
        groups: 与标签等长的泄漏分量。
        desired_splits: 希望使用的最大折数。
        random_seed: 分组分层打散种子。
        reason_code: 无可行折时使用的稳定失败码。

    Returns:
        可复现的训练索引与留出索引元组。

    Raises:
        FormalBaselineError: 少于两组或不存在双类有效折。
    """

    maximum = min(desired_splits, len(set(str(value) for value in groups)))
    for split_count in range(maximum, 1, -1):
        splitter = StratifiedGroupKFold(
            n_splits=split_count,
            shuffle=True,
            random_state=random_seed,
        )
        try:
            folds = tuple(splitter.split(np.arange(len(labels)), labels, groups))
        except ValueError:
            continue
        if all(
            set(labels[train_indices]) == {"related", "unrelated"}
            and set(labels[held_indices]) == {"related", "unrelated"}
            for train_indices, held_indices in folds
        ):
            return folds
    raise FormalBaselineError(reason_code)


def build_global_split_plan(
    documents: Sequence[BaselineDocument],
    *,
    random_seed: int,
    temporal_test_fraction: float,
    validation_fraction: float,
) -> BaselineSplitPlan:
    """建立平台无关的全局时间测试和分组分层训练/验证切分。

    Args:
        documents: 全部确定标签参考记录。
        random_seed: 可复现随机种子。
        temporal_test_fraction: 扩展泄漏分量前的全局时间候选比例。
        validation_fraction: 剩余开发记录中的目标验证比例。

    Returns:
        不含任何平台配额或平台字段的冻结切分计划。

    Raises:
        FormalBaselineError: 身份重复、标签非法、分组不足、集合缺类或发生泄漏。
    """

    if not documents:
        raise FormalBaselineError("baseline_reference_empty")
    identities = [(item.source_post_id, item.source_version) for item in documents]
    if len(identities) != len(set(identities)):
        raise FormalBaselineError("baseline_reference_identity_duplicate")
    if any(
        item.tourism_label not in {"related", "unrelated"}
        or not item.component_id
        or not item.normalized_model_text.strip()
        for item in documents
    ):
        raise FormalBaselineError("baseline_reference_record_invalid")
    _require_binary(documents, "baseline_reference_missing_class")
    candidate_count = math.ceil(len(documents) * temporal_test_fraction)
    if candidate_count <= 0 or candidate_count >= len(documents):
        raise FormalBaselineError("global_temporal_test_capacity_invalid")
    ordered = sorted(documents, key=lambda item: _time_key(item, random_seed))
    test_candidates = ordered[-candidate_count:]
    test_candidate_identities = tuple(
        sorted((item.source_post_id, item.source_version) for item in test_candidates)
    )
    test_components = {item.component_id for item in test_candidates}
    remaining = [item for item in documents if item.component_id not in test_components]
    _require_binary(remaining, "global_development_missing_class")

    labels = np.asarray([item.tourism_label for item in remaining], dtype=object)
    groups = np.asarray([item.component_id for item in remaining], dtype=object)
    desired_splits = max(2, round(1.0 / validation_fraction))
    candidates: list[tuple[float, np.ndarray, np.ndarray]] = []
    for train_indices, validation_indices in _valid_group_folds(
        labels,
        groups,
        desired_splits=desired_splits,
        random_seed=random_seed,
        reason_code="global_validation_split_unavailable",
    ):
        validation_labels = labels[validation_indices]
        fraction_error = abs(len(validation_indices) / len(remaining) - validation_fraction)
        class_error = abs(
            float(np.mean(validation_labels == "unrelated"))
            - float(np.mean(labels == "unrelated"))
        )
        candidates.append((fraction_error + class_error, train_indices, validation_indices))
    _, train_indices, validation_indices = min(candidates, key=lambda item: item[0])
    train_components = {remaining[int(index)].component_id for index in train_indices}
    validation_components = {
        remaining[int(index)].component_id for index in validation_indices
    }
    if (
        train_components & validation_components
        or test_components & train_components
        or test_components & validation_components
    ):
        raise FormalBaselineError("baseline_component_leakage_detected")
    split_by_component = {
        **{component: "train" for component in train_components},
        **{component: "validation" for component in validation_components},
        **{component: "test" for component in test_components},
    }
    assignments = tuple(
        BaselineSplitAssignment(
            item.source_post_id,
            item.source_version,
            item.component_id,
            split_by_component[item.component_id],
        )
        for item in sorted(
            documents, key=lambda value: (value.source_post_id, value.source_version)
        )
    )
    assignment_by_identity = {
        (item.source_post_id, item.source_version): item.split_name for item in assignments
    }
    if any(assignment_by_identity[identity] != "test" for identity in test_candidate_identities):
        raise FormalBaselineError("global_temporal_candidate_not_in_test")

    def split_hash(split_name: str) -> str:
        """计算单一集合成员的规范摘要。

        Args:
            split_name: ``train``、``validation`` 或 ``test``。

        Returns:
            该集合排序成员的 SHA-256。
        """

        return _sha256(
            [asdict(item) for item in assignments if item.split_name == split_name]
        )

    return BaselineSplitPlan(
        assignments=assignments,
        manifest_sha256=_sha256([asdict(item) for item in assignments]),
        train_manifest_sha256=split_hash("train"),
        validation_manifest_sha256=split_hash("validation"),
        test_manifest_sha256=split_hash("test"),
        test_candidate_manifest_sha256=_sha256(test_candidate_identities),
        test_candidate_count=candidate_count,
    )


def _pipeline(config: StableCleaningConfig, random_seed: int) -> Pipeline:
    """构造尚未拟合的冻结 baseline pipeline。

    Args:
        config: 已严格校验的稳定清洗配置。
        random_seed: 线性 SVM 的可复现种子。

    Returns:
        字符 TF-IDF 与 ``LinearSVC`` 组成的 sklearn pipeline。
    """

    return Pipeline(
        [
            (
                "vectorizer",
                TfidfVectorizer(
                    analyzer=str(config.text["analyzer"]),
                    ngram_range=tuple(config.text["ngram_range"]),
                    min_df=int(config.text["min_df"]),
                    max_df=float(config.text["max_df"]),
                    sublinear_tf=bool(config.text["sublinear_tf"]),
                ),
            ),
            (
                "classifier",
                LinearSVC(
                    C=float(config.text["svm_c"]),
                    class_weight=str(config.text["class_weight"]),
                    random_state=random_seed,
                ),
            ),
        ]
    )


def unrelated_margins(pipeline: Pipeline, texts: Sequence[str]) -> np.ndarray:
    """把 sklearn 类别顺序统一为正方向更可能 ``unrelated``。

    Args:
        pipeline: 已拟合且含 ``classifier`` 步骤的 pipeline。
        texts: 规范化文本序列。

    Returns:
        一维无关方向 decision margin。

    Raises:
        FormalBaselineError: 分类器类别或 margin 形状、有限性不符合二分类契约。
    """

    classifier = pipeline.named_steps["classifier"]
    classes = [str(value) for value in classifier.classes_]
    if set(classes) != {"related", "unrelated"}:
        raise FormalBaselineError("baseline_classifier_classes_invalid")
    raw = np.asarray(pipeline.decision_function(texts), dtype=float)
    if raw.ndim != 1 or not np.isfinite(raw).all():
        raise FormalBaselineError("baseline_margin_invalid")
    return raw if classes[1] == "unrelated" else -raw


def _fit_sigmoid(margins: np.ndarray, labels: np.ndarray) -> SigmoidCalibrator:
    """用训练集折外 margin 拟合无正则的一维 Sigmoid。

    Args:
        margins: 每条训练记录恰好一次的折外 margin。
        labels: 与 margin 等长的二元数值标签。

    Returns:
        可序列化的两参数 Sigmoid 校准器。

    Raises:
        FormalBaselineError: 输入非法或数值优化未收敛。
    """

    if (
        margins.ndim != 1
        or labels.ndim != 1
        or len(margins) != len(labels)
        or set(int(value) for value in labels) != {0, 1}
        or not np.isfinite(margins).all()
    ):
        raise FormalBaselineError("sigmoid_calibration_input_invalid")
    prevalence = float(np.mean(labels))
    initial = np.asarray([1.0, math.log(prevalence / (1.0 - prevalence))])

    def objective(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        """返回平均逻辑损失及解析梯度。

        Args:
            parameters: 当前 slope 与 intercept。

        Returns:
            平均逻辑损失和同顺序解析梯度。
        """

        logits = parameters[0] * margins + parameters[1]
        loss = float(np.mean(np.logaddexp(0.0, logits) - labels * logits))
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -709.0, 709.0)))
        residuals = probabilities - labels
        gradient = np.asarray(
            [float(np.mean(residuals * margins)), float(np.mean(residuals))]
        )
        return loss, gradient

    fitted = minimize(
        objective,
        initial,
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": 1000, "ftol": 1e-12},
    )
    if not fitted.success or not np.isfinite(fitted.x).all():
        raise FormalBaselineError("sigmoid_calibration_fit_failed")
    return SigmoidCalibrator(float(fitted.x[0]), float(fitted.x[1]))


def _metrics(labels: Sequence[str], probabilities: Sequence[float]) -> dict[str, Any]:
    """计算验证集总体分类与概率校准诊断。

    Args:
        labels: 冻结二分类人工标签。
        probabilities: 校准后的 ``p_unrelated``。

    Returns:
        以无关为正类、0.5仅作诊断分界的总体指标。
    """

    encoded = np.asarray([1 if label == "unrelated" else 0 for label in labels], dtype=int)
    values = np.asarray(probabilities, dtype=float)
    predictions = (values >= 0.5).astype(int)
    matrix = confusion_matrix(encoded, predictions, labels=[0, 1])
    return {
        "count": len(labels),
        "related_count": int(np.sum(encoded == 0)),
        "unrelated_count": int(np.sum(encoded == 1)),
        "diagnostic_cutoff": 0.5,
        "diagnostic_cutoff_is_routing_threshold": False,
        "precision_unrelated": float(precision_score(encoded, predictions, zero_division=0)),
        "recall_unrelated": float(recall_score(encoded, predictions, zero_division=0)),
        "f1_unrelated": float(f1_score(encoded, predictions, zero_division=0)),
        "pr_auc_unrelated": float(average_precision_score(encoded, values)),
        "brier_score": float(brier_score_loss(encoded, values)),
        "log_loss": float(log_loss(encoded, values, labels=[0, 1])),
        "confusion": {
            "related_as_related": int(matrix[0, 0]),
            "related_as_unrelated": int(matrix[0, 1]),
            "unrelated_as_related": int(matrix[1, 0]),
            "unrelated_as_unrelated": int(matrix[1, 1]),
        },
    }


def fit_formal_baseline(
    documents: Sequence[BaselineDocument],
    *,
    split_plan: BaselineSplitPlan,
    config: StableCleaningConfig,
) -> FormalBaselineResult:
    """拟合训练集模型、分组折外 Sigmoid，并只评估验证集。

    Args:
        documents: 与切分计划身份完全一致的确定标签记录。
        split_plan: 已在任何拟合前冻结的全局切分计划。
        config: 严格校验且阈值仍为 ``UNSET`` 的稳定配置。

    Returns:
        可冻结模型、折外/验证概率和验证集总体指标。

    Raises:
        FormalBaselineError: 切分不一致、训练折无双类、拟合失败或概率非法。

    Notes:
        本函数不会读取测试集合的文本或标签进行预测，也不会选择任何路由阈值。
    """

    by_identity = {
        (item.source_post_id, item.source_version): item for item in documents
    }
    assignments = {
        (item.source_post_id, item.source_version): item.split_name
        for item in split_plan.assignments
    }
    if len(by_identity) != len(documents) or set(by_identity) != set(assignments):
        raise FormalBaselineError("baseline_split_reference_mismatch")
    splits = {
        name: [
            document
            for identity, document in by_identity.items()
            if assignments[identity] == name
        ]
        for name in ("train", "validation", "test")
    }
    for name in ("train", "validation"):
        items = splits[name]
        _require_binary(items, f"baseline_{name}_missing_class")
    train = splits["train"]
    labels = np.asarray([item.tourism_label for item in train], dtype=object)
    groups = np.asarray([item.component_id for item in train], dtype=object)
    folds = _valid_group_folds(
        labels,
        groups,
        desired_splits=int(config.text["calibration_folds"]),
        random_seed=config.random_seed,
        reason_code="baseline_calibration_folds_unavailable",
    )
    oof_margins = np.full(len(train), np.nan, dtype=float)
    for fit_indices, held_indices in folds:
        pipeline = _pipeline(config, config.random_seed)
        try:
            pipeline.fit(
                [train[int(index)].normalized_model_text for index in fit_indices],
                labels[fit_indices],
            )
        except ValueError as exc:
            raise FormalBaselineError("baseline_fold_fit_failed") from exc
        oof_margins[held_indices] = unrelated_margins(
            pipeline,
            [train[int(index)].normalized_model_text for index in held_indices],
        )
    if not np.isfinite(oof_margins).all():
        raise FormalBaselineError("baseline_oof_incomplete")
    encoded = np.asarray([1 if label == "unrelated" else 0 for label in labels], dtype=int)
    calibrator = _fit_sigmoid(oof_margins, encoded)
    oof_probabilities = calibrator.predict(oof_margins)

    final_pipeline = _pipeline(config, config.random_seed)
    try:
        final_pipeline.fit(
            [item.normalized_model_text for item in train],
            [item.tourism_label for item in train],
        )
    except ValueError as exc:
        raise FormalBaselineError("baseline_final_fit_failed") from exc
    model = FrozenBaselineModel(final_pipeline, calibrator)
    validation = splits["validation"]
    validation_margins = unrelated_margins(
        final_pipeline, [item.normalized_model_text for item in validation]
    )
    validation_probabilities = calibrator.predict(validation_margins)
    train_oof_rows = tuple(
        BaselineProbability(
            item.source_post_id,
            item.source_version,
            "train_oof",
            item.tourism_label,
            float(oof_margins[index]),
            float(oof_probabilities[index]),
        )
        for index, item in enumerate(train)
    )
    validation_rows = tuple(
        BaselineProbability(
            item.source_post_id,
            item.source_version,
            "validation",
            item.tourism_label,
            float(validation_margins[index]),
            float(validation_probabilities[index]),
        )
        for index, item in enumerate(validation)
    )
    return FormalBaselineResult(
        model=model,
        split_plan=split_plan,
        train_oof_probabilities=train_oof_rows,
        validation_probabilities=validation_rows,
        validation_metrics=_metrics(
            [item.tourism_label for item in validation], validation_probabilities
        ),
        calibration_fold_count=len(folds),
    )


def split_counts(plan: BaselineSplitPlan) -> Mapping[str, int]:
    """汇总切分计划的三集合数量。

    Args:
        plan: 已冻结切分计划。

    Returns:
        按 ``train``、``validation``、``test`` 排列的计数映射。
    """

    counts = Counter(item.split_name for item in plan.assignments)
    return {name: int(counts[name]) for name in ("train", "validation", "test")}


def component_split_names(plan: BaselineSplitPlan) -> Mapping[str, tuple[str, ...]]:
    """为泄漏验收汇总每个分量出现的集合。

    Args:
        plan: 已冻结切分计划。

    Returns:
        分量身份到排序后集合名元组的映射。
    """

    names: dict[str, set[str]] = defaultdict(set)
    for item in plan.assignments:
        names[item.component_id].add(item.split_name)
    return {key: tuple(sorted(value)) for key, value in sorted(names.items())}
