"""UGC 安全优先的开发阶段模型验收策略与配对统计计算。

本模块只比较同一训练侧嵌套分组折外记录上的 baseline 与 challenger。
固定 ``0.5`` 仅用于安全诊断，既不是 ``T_keep`` 也不是 ``T_exclude``；
锁定测试、平台字段和验证集均不进入本模块的模型选择计算。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import yaml
from sklearn.metrics import average_precision_score


class ModelAcceptanceError(RuntimeError):
    """模型验收策略或配对证据违反契约时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: 供 CLI、测试和 artifact 使用的失败码。
        """

        super().__init__("formal cleaning model acceptance failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ModelAcceptancePolicy:
    """内容寻址的 UGC 安全优先模型验收策略。

    Attributes:
        policy_id: 规范化完整策略的截断 SHA-256 身份。
        policy_sha256: 规范化完整策略的 SHA-256。
        baseline_model_id: 唯一允许比较的当前 baseline。
        reference_csv_sha256: baseline 所绑定的最终参考 CSV。
        train_manifest_sha256: 当前训练成员摘要。
        validation_manifest_sha256: 当前验证成员摘要；只作方向性复核绑定。
        test_manifest_sha256: 锁定测试成员摘要；不得读取成员或概率。
        confidence_level: 单侧配对 bootstrap 置信水平。
        bootstrap_repetitions: 固定 bootstrap 重复次数。
        random_seed: 固定重采样种子。
        diagnostic_cutoff: 安全诊断分界，固定为 ``0.5``。
        safety_maximum_point_delta: 候选误排率相对 baseline 的最大点差。
        safety_maximum_upper_delta: 误排率点差单侧上界允许值。
        log_loss_minimum_improvement: log loss 所需最小点改善。
        pr_auc_maximum_point_degradation: PR-AUC 点估计最大允许退化。
        pr_auc_maximum_lower_degradation: PR-AUC 单侧下界最大允许退化。
        brier_maximum_point_delta: Brier score 最大允许点差。
    """

    policy_id: str
    policy_sha256: str
    baseline_model_id: str
    reference_csv_sha256: str
    train_manifest_sha256: str
    validation_manifest_sha256: str
    test_manifest_sha256: str
    confidence_level: float
    bootstrap_repetitions: int
    random_seed: int
    diagnostic_cutoff: float
    safety_maximum_point_delta: float
    safety_maximum_upper_delta: float
    log_loss_minimum_improvement: float
    pr_auc_maximum_point_degradation: float
    pr_auc_maximum_lower_degradation: float
    brier_maximum_point_delta: float


@dataclass(frozen=True)
class PairedOofObservation:
    """同一外层折中 baseline 与 challenger 的单条配对 OOF 证据。

    Attributes:
        member_key: 不可逆且在本次比较中唯一的成员键。
        component_id: 作者与重复关系闭包形成的泄漏分量。
        tourism_label: 冻结人工标签。
        baseline_p_unrelated: baseline 折外无关概率。
        candidate_p_unrelated: challenger 折外无关概率。
    """

    member_key: str
    component_id: str
    tourism_label: str
    baseline_p_unrelated: float
    candidate_p_unrelated: float


_ROOT_FIELDS = frozenset(
    {
        "artifact_kind",
        "status",
        "decision_rule",
        "baseline",
        "evidence",
        "gates",
        "validation",
        "failure_behavior",
    }
)
_BASELINE_FIELDS = frozenset(
    {
        "model_id",
        "reference_csv_sha256",
        "train_manifest_sha256",
        "validation_manifest_sha256",
        "test_manifest_sha256",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "scope",
        "paired_outer_folds",
        "resampling_unit",
        "confidence_level",
        "bootstrap_repetitions",
        "random_seed",
        "diagnostic_cutoff",
        "diagnostic_cutoff_is_routing_threshold",
    }
)
_GATE_FIELDS = frozenset({"safety", "primary", "ranking", "calibration"})
_SAFETY_FIELDS = frozenset(
    {"metric", "maximum_point_delta", "maximum_upper_confidence_delta"}
)
_PRIMARY_FIELDS = frozenset(
    {"metric", "minimum_point_improvement", "require_upper_confidence_below_zero"}
)
_RANKING_FIELDS = frozenset(
    {
        "metric",
        "maximum_point_degradation",
        "maximum_lower_confidence_degradation",
    }
)
_CALIBRATION_FIELDS = frozenset({"metric", "maximum_point_delta"})
_VALIDATION_FIELDS = frozenset(
    {"role", "unique_winner_only", "may_expand_search"}
)


def _exact_mapping(
    value: Any,
    fields: frozenset[str],
    reason_code: str,
) -> Mapping[str, Any]:
    """校验映射类型及其精确键集合。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelAcceptanceError(reason_code)
    return value


def _finite_number(value: Any, reason_code: str) -> float:
    """解析有限数并拒绝布尔值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelAcceptanceError(reason_code)
    result = float(value)
    if not math.isfinite(result):
        raise ModelAcceptanceError(reason_code)
    return result


def _sha256_text(raw: Mapping[str, Any]) -> str:
    """计算规范 JSON 策略摘要。"""

    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_hash(value: Any) -> str:
    """校验小写 SHA-256 字符串。"""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelAcceptanceError("model_acceptance_policy_hash_invalid")
    return value


def load_model_acceptance_policy(path: str | Path) -> ModelAcceptancePolicy:
    """加载并严格校验独立的模型验收策略。

    Args:
        path: ``cleaning-model-acceptance.yaml`` 文件。

    Returns:
        内容寻址且可直接执行的冻结策略。

    Raises:
        ModelAcceptanceError: 文件不可读、字段未知或任一数值门违反冻结契约。
    """

    try:
        raw_value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelAcceptanceError("model_acceptance_policy_unreadable") from exc
    raw = _exact_mapping(raw_value, _ROOT_FIELDS, "model_acceptance_policy_invalid")
    if (
        raw.get("artifact_kind") != "formal-cleaning-model-acceptance-policy"
        or raw.get("status") != "frozen"
        or raw.get("decision_rule") != "ugc_safety_first"
        or raw.get("failure_behavior") != "retain_baseline"
    ):
        raise ModelAcceptanceError("model_acceptance_policy_invalid")
    baseline = _exact_mapping(
        raw["baseline"], _BASELINE_FIELDS, "model_acceptance_baseline_invalid"
    )
    model_id = baseline.get("model_id")
    if not isinstance(model_id, str) or len(model_id) != 32:
        raise ModelAcceptanceError("model_acceptance_baseline_invalid")
    reference_hash = _require_hash(baseline.get("reference_csv_sha256"))
    train_hash = _require_hash(baseline.get("train_manifest_sha256"))
    validation_hash = _require_hash(baseline.get("validation_manifest_sha256"))
    test_hash = _require_hash(baseline.get("test_manifest_sha256"))

    evidence = _exact_mapping(
        raw["evidence"], _EVIDENCE_FIELDS, "model_acceptance_evidence_invalid"
    )
    confidence = _finite_number(
        evidence.get("confidence_level"), "model_acceptance_evidence_invalid"
    )
    repetitions = evidence.get("bootstrap_repetitions")
    seed = evidence.get("random_seed")
    cutoff = _finite_number(
        evidence.get("diagnostic_cutoff"), "model_acceptance_evidence_invalid"
    )
    if (
        evidence.get("scope") != "train_nested_group_oof"
        or evidence.get("paired_outer_folds") is not True
        or evidence.get("resampling_unit") != "leakage_component"
        or confidence != 0.90
        or isinstance(repetitions, bool)
        or repetitions != 5000
        or isinstance(seed, bool)
        or seed != 20260728
        or cutoff != 0.50
        or evidence.get("diagnostic_cutoff_is_routing_threshold") is not False
    ):
        raise ModelAcceptanceError("model_acceptance_evidence_invalid")

    gates = _exact_mapping(raw["gates"], _GATE_FIELDS, "model_acceptance_gates_invalid")
    safety = _exact_mapping(
        gates["safety"], _SAFETY_FIELDS, "model_acceptance_safety_gate_invalid"
    )
    primary = _exact_mapping(
        gates["primary"], _PRIMARY_FIELDS, "model_acceptance_primary_gate_invalid"
    )
    ranking = _exact_mapping(
        gates["ranking"], _RANKING_FIELDS, "model_acceptance_ranking_gate_invalid"
    )
    calibration = _exact_mapping(
        gates["calibration"],
        _CALIBRATION_FIELDS,
        "model_acceptance_calibration_gate_invalid",
    )
    safety_point = _finite_number(
        safety.get("maximum_point_delta"), "model_acceptance_safety_gate_invalid"
    )
    safety_upper = _finite_number(
        safety.get("maximum_upper_confidence_delta"),
        "model_acceptance_safety_gate_invalid",
    )
    primary_improvement = _finite_number(
        primary.get("minimum_point_improvement"),
        "model_acceptance_primary_gate_invalid",
    )
    ranking_point = _finite_number(
        ranking.get("maximum_point_degradation"),
        "model_acceptance_ranking_gate_invalid",
    )
    ranking_lower = _finite_number(
        ranking.get("maximum_lower_confidence_degradation"),
        "model_acceptance_ranking_gate_invalid",
    )
    brier_delta = _finite_number(
        calibration.get("maximum_point_delta"),
        "model_acceptance_calibration_gate_invalid",
    )
    if (
        safety.get("metric") != "related_to_unrelated_rate"
        or safety_point != 0.0
        or safety_upper != 0.02
        or primary.get("metric") != "log_loss"
        or primary_improvement != 0.01
        or primary.get("require_upper_confidence_below_zero") is not True
        or ranking.get("metric") != "pr_auc_unrelated"
        or ranking_point != 0.005
        or ranking_lower != 0.01
        or calibration.get("metric") != "brier_score"
        or brier_delta != 0.0
    ):
        raise ModelAcceptanceError("model_acceptance_gates_invalid")
    validation = _exact_mapping(
        raw["validation"],
        _VALIDATION_FIELDS,
        "model_acceptance_validation_invalid",
    )
    if dict(validation) != {
        "role": "directional_check_only",
        "unique_winner_only": True,
        "may_expand_search": False,
    }:
        raise ModelAcceptanceError("model_acceptance_validation_invalid")
    sha256 = _sha256_text(raw)
    return ModelAcceptancePolicy(
        policy_id=sha256[:32],
        policy_sha256=sha256,
        baseline_model_id=model_id,
        reference_csv_sha256=reference_hash,
        train_manifest_sha256=train_hash,
        validation_manifest_sha256=validation_hash,
        test_manifest_sha256=test_hash,
        confidence_level=confidence,
        bootstrap_repetitions=repetitions,
        random_seed=seed,
        diagnostic_cutoff=cutoff,
        safety_maximum_point_delta=safety_point,
        safety_maximum_upper_delta=safety_upper,
        log_loss_minimum_improvement=primary_improvement,
        pr_auc_maximum_point_degradation=ranking_point,
        pr_auc_maximum_lower_degradation=ranking_lower,
        brier_maximum_point_delta=brier_delta,
    )


def _validated_arrays(
    observations: Sequence[PairedOofObservation],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[str, ...]]:
    """校验配对 OOF 并转换为指标数组。"""

    if not observations:
        raise ModelAcceptanceError("model_acceptance_observations_empty")
    member_keys: set[str] = set()
    labels: list[int] = []
    baseline: list[float] = []
    candidate: list[float] = []
    components: list[str] = []
    for record in observations:
        if (
            not record.member_key
            or record.member_key in member_keys
            or not record.component_id
            or record.tourism_label not in {"related", "unrelated"}
        ):
            raise ModelAcceptanceError("model_acceptance_observation_invalid")
        member_keys.add(record.member_key)
        probabilities = (record.baseline_p_unrelated, record.candidate_p_unrelated)
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in probabilities):
            raise ModelAcceptanceError("model_acceptance_probability_invalid")
        labels.append(int(record.tourism_label == "unrelated"))
        baseline.append(float(record.baseline_p_unrelated))
        candidate.append(float(record.candidate_p_unrelated))
        components.append(record.component_id)
    y_true = np.asarray(labels, dtype=int)
    if len(set(labels)) != 2:
        raise ModelAcceptanceError("model_acceptance_classes_incomplete")
    return (
        y_true,
        np.asarray(baseline, dtype=float),
        np.asarray(candidate, dtype=float),
        tuple(components),
    )


def _metric_values(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    cutoff: float,
) -> Mapping[str, float]:
    """计算验收门需要的四个总体指标。"""

    clipped = np.clip(probabilities, 1e-15, 1.0 - 1e-15)
    losses = -(y_true * np.log(clipped) + (1 - y_true) * np.log(1.0 - clipped))
    related = y_true == 0
    return {
        "related_to_unrelated_rate": float(np.mean(probabilities[related] >= cutoff)),
        "log_loss": float(np.mean(losses)),
        "pr_auc_unrelated": float(average_precision_score(y_true, probabilities)),
        "brier_score": float(np.mean((probabilities - y_true) ** 2)),
    }


def _paired_bootstrap_deltas(
    y_true: np.ndarray,
    baseline: np.ndarray,
    candidate: np.ndarray,
    components: tuple[str, ...],
    policy: ModelAcceptancePolicy,
) -> Mapping[str, np.ndarray]:
    """以 leakage component 为单位生成配对指标差分分布。"""

    component_names = tuple(sorted(set(components)))
    if len(component_names) < 2:
        raise ModelAcceptanceError("model_acceptance_components_insufficient")
    index_by_component = {
        name: np.asarray(
            [index for index, component in enumerate(components) if component == name],
            dtype=int,
        )
        for name in component_names
    }
    rng = np.random.default_rng(policy.random_seed)
    distributions: dict[str, list[float]] = {
        "related_to_unrelated_rate": [],
        "log_loss": [],
        "pr_auc_unrelated": [],
    }
    for _ in range(policy.bootstrap_repetitions):
        sampled = rng.choice(component_names, size=len(component_names), replace=True)
        indices = np.concatenate([index_by_component[str(name)] for name in sampled])
        sampled_y = y_true[indices]
        if len(set(sampled_y.tolist())) != 2:
            continue
        baseline_metrics = _metric_values(
            sampled_y, baseline[indices], policy.diagnostic_cutoff
        )
        candidate_metrics = _metric_values(
            sampled_y, candidate[indices], policy.diagnostic_cutoff
        )
        for metric in distributions:
            distributions[metric].append(
                candidate_metrics[metric] - baseline_metrics[metric]
            )
    minimum_valid = math.ceil(policy.bootstrap_repetitions * 0.95)
    if any(len(values) < minimum_valid for values in distributions.values()):
        raise ModelAcceptanceError("model_acceptance_bootstrap_invalid")
    return {
        metric: np.asarray(values, dtype=float)
        for metric, values in distributions.items()
    }


def evaluate_model_acceptance(
    observations: Sequence[PairedOofObservation],
    policy: ModelAcceptancePolicy,
    *,
    candidate_model_id: str,
) -> Mapping[str, Any]:
    """执行 UGC 安全优先的训练侧配对模型验收。

    Args:
        observations: 同一外层折、同一成员顺序的 baseline/challenger OOF 证据。
        policy: 已冻结且绑定当前 baseline 的验收策略。
        candidate_model_id: 当前 challenger 的稳定模型身份。

    Returns:
        不含成员、正文、平台或测试信息的聚合验收报告。

    Raises:
        ModelAcceptanceError: 候选身份、配对证据或 bootstrap 不满足契约。

    Notes:
        通过表示 challenger 有资格成为唯一验证候选，不表示已通过锁定测试、
        路由阈值或生产自动清洗门。
    """

    if (
        not isinstance(candidate_model_id, str)
        or not candidate_model_id.strip()
        or candidate_model_id == policy.baseline_model_id
    ):
        raise ModelAcceptanceError("model_acceptance_candidate_id_invalid")
    y_true, baseline, candidate, components = _validated_arrays(observations)
    baseline_metrics = _metric_values(y_true, baseline, policy.diagnostic_cutoff)
    candidate_metrics = _metric_values(y_true, candidate, policy.diagnostic_cutoff)
    deltas = {
        metric: candidate_metrics[metric] - baseline_metrics[metric]
        for metric in baseline_metrics
    }
    bootstrap = _paired_bootstrap_deltas(
        y_true, baseline, candidate, components, policy
    )
    upper_quantile = policy.confidence_level
    lower_quantile = 1.0 - policy.confidence_level
    intervals = {
        "related_to_unrelated_rate": {
            "lower": float(np.quantile(bootstrap["related_to_unrelated_rate"], lower_quantile)),
            "upper": float(np.quantile(bootstrap["related_to_unrelated_rate"], upper_quantile)),
        },
        "log_loss": {
            "lower": float(np.quantile(bootstrap["log_loss"], lower_quantile)),
            "upper": float(np.quantile(bootstrap["log_loss"], upper_quantile)),
        },
        "pr_auc_unrelated": {
            "lower": float(np.quantile(bootstrap["pr_auc_unrelated"], lower_quantile)),
            "upper": float(np.quantile(bootstrap["pr_auc_unrelated"], upper_quantile)),
        },
    }
    gates = {
        "safety_point_noninferiority": (
            deltas["related_to_unrelated_rate"]
            <= policy.safety_maximum_point_delta
        ),
        "safety_upper_confidence_bound": (
            intervals["related_to_unrelated_rate"]["upper"]
            <= policy.safety_maximum_upper_delta
        ),
        "log_loss_minimum_improvement": (
            deltas["log_loss"] <= -policy.log_loss_minimum_improvement
        ),
        "log_loss_upper_confidence_below_zero": (
            intervals["log_loss"]["upper"] < 0.0
        ),
        "pr_auc_point_noninferiority": (
            deltas["pr_auc_unrelated"]
            >= -policy.pr_auc_maximum_point_degradation
        ),
        "pr_auc_lower_confidence_noninferiority": (
            intervals["pr_auc_unrelated"]["lower"]
            >= -policy.pr_auc_maximum_lower_degradation
        ),
        "brier_point_noninferiority": (
            deltas["brier_score"] <= policy.brier_maximum_point_delta
        ),
    }
    passed = all(gates.values())
    return {
        "artifact_kind": "formal-cleaning-model-acceptance-report",
        "status": "passed" if passed else "failed_retain_baseline",
        "decision_rule": "ugc_safety_first",
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "baseline_model_id": policy.baseline_model_id,
        "candidate_model_id": candidate_model_id,
        "evidence_scope": "train_nested_group_oof",
        "paired_outer_folds": True,
        "resampling_unit": "leakage_component",
        "observation_count": len(observations),
        "component_count": len(set(components)),
        "confidence_level": policy.confidence_level,
        "bootstrap_repetitions": policy.bootstrap_repetitions,
        "diagnostic_cutoff": policy.diagnostic_cutoff,
        "diagnostic_cutoff_is_routing_threshold": False,
        "baseline_metrics": dict(baseline_metrics),
        "candidate_metrics": dict(candidate_metrics),
        "candidate_minus_baseline": deltas,
        "paired_component_bootstrap_intervals": intervals,
        "gates": gates,
        "all_gates_passed": passed,
        "failure_behavior": "retain_baseline",
        "validation_role": "directional_check_only",
        "test_status": "locked_not_opened",
        "test_members_read": False,
        "test_probabilities_present": False,
        "platform_used": False,
        "threshold_status": "UNSET",
    }
