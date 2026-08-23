"""唯一 sparse challenger 的无拟合验证方向性复核。

本模块只调用已冻结候选的概率预测接口，并与 baseline 已封存的验证概率配对。
它不导入训练配置、候选矩阵或分类器，不提供任何模型拟合、校准或调参入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

import numpy as np

from .formal_baseline import evaluate_binary_probabilities


class SparseChallengerValidationError(RuntimeError):
    """验证输入或概率违反一次性方向性复核契约时抛出的异常。

    Attributes:
        reason_code: 不含正文、身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: 供 CLI、测试和 artifact 使用的失败码。
        """

        super().__init__("sparse challenger directional validation failed")
        self.reason_code = reason_code


class FrozenProbabilityPredictor(Protocol):
    """验证阶段唯一允许调用的冻结概率预测接口。"""

    def predict_p_unrelated(self, texts: Sequence[str]) -> np.ndarray:
        """返回与输入顺序一致的无关概率。"""


@dataclass(frozen=True)
class ChallengerValidationDocument:
    """一条不含平台、测试或超参数的验证输入。

    Attributes:
        member_key: 当前验证 artifact 内唯一的去标识成员键。
        component_id: 冻结 leakage component，仅用于谱系和后续误差审计。
        normalized_model_text: 最终参考 CSV 中的冻结模型文本。
        tourism_label: ``related`` 或 ``unrelated``。
        baseline_p_unrelated: baseline 已封存的验证无关概率。
    """

    member_key: str
    component_id: str
    normalized_model_text: str
    tourism_label: str
    baseline_p_unrelated: float


@dataclass(frozen=True)
class ChallengerValidationObservation:
    """验证集合上的逐成员配对概率。

    Attributes:
        member_key: 去标识成员键。
        component_id: 冻结 leakage component。
        tourism_label: 冻结人工标签。
        baseline_p_unrelated: baseline 验证概率。
        candidate_p_unrelated: 唯一 challenger 验证概率。
    """

    member_key: str
    component_id: str
    tourism_label: str
    baseline_p_unrelated: float
    candidate_p_unrelated: float


@dataclass(frozen=True)
class SparseChallengerValidationResult:
    """不承担调参或最终验收功能的验证方向性结果。

    Attributes:
        observations: 逐成员配对验证概率。
        baseline_metrics: baseline 的验证总体诊断。
        candidate_metrics: challenger 的验证总体诊断。
        candidate_minus_baseline: 同方向定义的点估计差。
        direction_checks: 四项预声明方向是否一致。
        direction_status: ``directionally_consistent`` 或
            ``mixed_or_reversed``。
        validation_count: 验证记录数。
        diagnostic_cutoff: 固定为0.5且不是路由阈值。
    """

    observations: tuple[ChallengerValidationObservation, ...]
    baseline_metrics: Mapping[str, Any]
    candidate_metrics: Mapping[str, Any]
    candidate_minus_baseline: Mapping[str, float]
    direction_checks: Mapping[str, bool]
    direction_status: str
    validation_count: int
    diagnostic_cutoff: float = 0.5


def _validate_documents(
    documents: Sequence[ChallengerValidationDocument],
) -> None:
    """要求验证输入身份唯一、双类完整且概率有限。"""

    if not documents:
        raise SparseChallengerValidationError(
            "sparse_challenger_validation_empty"
        )
    member_keys = [item.member_key for item in documents]
    probabilities = np.asarray(
        [item.baseline_p_unrelated for item in documents], dtype=float
    )
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
        or {item.tourism_label for item in documents}
        != {"related", "unrelated"}
        or not np.isfinite(probabilities).all()
        or np.any(probabilities < 0.0)
        or np.any(probabilities > 1.0)
    ):
        raise SparseChallengerValidationError(
            "sparse_challenger_validation_document_invalid"
        )


def _validation_metrics(
    labels: Sequence[str], probabilities: Sequence[float]
) -> dict[str, Any]:
    """补充 Accuracy 与 UGC 误排率的验证诊断。"""

    try:
        metrics = evaluate_binary_probabilities(labels, probabilities)
    except (RuntimeError, ValueError) as exc:
        raise SparseChallengerValidationError(
            "sparse_challenger_validation_metrics_invalid"
        ) from exc
    confusion = metrics["confusion"]
    related_count = int(metrics["related_count"])
    if related_count <= 0 or int(metrics["count"]) <= 0:
        raise SparseChallengerValidationError(
            "sparse_challenger_validation_metrics_invalid"
        )
    metrics["accuracy"] = float(
        (
            int(confusion["related_as_related"])
            + int(confusion["unrelated_as_unrelated"])
        )
        / int(metrics["count"])
    )
    metrics["related_to_unrelated_rate"] = float(
        int(confusion["related_as_unrelated"]) / related_count
    )
    return metrics


def evaluate_sparse_challenger_validation(
    documents: Sequence[ChallengerValidationDocument],
    *,
    frozen_candidate: FrozenProbabilityPredictor,
) -> SparseChallengerValidationResult:
    """只预测唯一候选并与 baseline 验证概率做方向性比较。

    Args:
        documents: 仅含冻结验证成员、人工标签和 baseline 已有概率的输入。
        frozen_candidate: 只暴露 ``predict_p_unrelated`` 的冻结候选。

    Returns:
        逐成员配对概率、总体指标和四项方向一致性描述。

    Raises:
        SparseChallengerValidationError: 输入、预测概率或指标非法。

    Notes:
        本函数没有模型训练、校准、候选选择或阈值参数。方向一致只表示
        challenger 相对 baseline 的点估计方向没有反转，不是锁定测试通过。
    """

    _validate_documents(documents)
    texts = [item.normalized_model_text for item in documents]
    try:
        candidate = np.asarray(
            frozen_candidate.predict_p_unrelated(texts), dtype=float
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise SparseChallengerValidationError(
            "sparse_challenger_validation_prediction_failed"
        ) from exc
    if (
        candidate.ndim != 1
        or len(candidate) != len(documents)
        or not np.isfinite(candidate).all()
        or np.any(candidate < 0.0)
        or np.any(candidate > 1.0)
    ):
        raise SparseChallengerValidationError(
            "sparse_challenger_validation_probability_invalid"
        )
    labels = [item.tourism_label for item in documents]
    baseline = np.asarray(
        [item.baseline_p_unrelated for item in documents], dtype=float
    )
    baseline_metrics = _validation_metrics(labels, baseline)
    candidate_metrics = _validation_metrics(labels, candidate)
    metric_names = (
        "related_to_unrelated_rate",
        "log_loss",
        "pr_auc_unrelated",
        "brier_score",
        "accuracy",
    )
    deltas = {
        name: float(candidate_metrics[name] - baseline_metrics[name])
        for name in metric_names
    }
    directions = {
        "related_safety_not_worse": deltas["related_to_unrelated_rate"] <= 0.0,
        "log_loss_not_worse": deltas["log_loss"] <= 0.0,
        "pr_auc_not_worse": deltas["pr_auc_unrelated"] >= 0.0,
        "brier_not_worse": deltas["brier_score"] <= 0.0,
    }
    observations = tuple(
        ChallengerValidationObservation(
            member_key=item.member_key,
            component_id=item.component_id,
            tourism_label=item.tourism_label,
            baseline_p_unrelated=float(item.baseline_p_unrelated),
            candidate_p_unrelated=float(candidate[index]),
        )
        for index, item in enumerate(documents)
    )
    return SparseChallengerValidationResult(
        observations=observations,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        candidate_minus_baseline=deltas,
        direction_checks=directions,
        direction_status=(
            "directionally_consistent"
            if all(directions.values())
            else "mixed_or_reversed"
        ),
        validation_count=len(documents),
    )

