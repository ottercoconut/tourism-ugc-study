"""Qwen 缓存向量分类头 challenger 的冻结配置解析。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class QwenHeadChallengerConfigError(RuntimeError):
    """分类头 challenger 配置违反冻结契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含路径或成员身份的稳定失败码。"""

        super().__init__("qwen head challenger configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenHeadCandidateSpec:
    """一个可审计的 MRL 维度与线性分类头组合。"""

    candidate_id: str
    family: str
    embedding_dimension: int
    C: float
    class_weight: str | None
    solver: str | None
    max_iter: int


@dataclass(frozen=True)
class QwenHeadChallengerPlan:
    """内容寻址的缓存向量 nested OOF 搜索计划。"""

    plan_id: str
    plan_sha256: str
    random_seed: int
    source_run_id: str
    source_model_id: str
    source_package_manifest_sha256: str
    source_embeddings_sha256: str
    source_training_oof_sha256: str
    source_plan_sha256: str
    candidates: tuple[QwenHeadCandidateSpec, ...]
    safety_anchor_candidate_id: str
    outer_folds: int
    inner_folds: int
    minimum_folds: int
    diagnostic_cutoff: float


_ROOT_FIELDS = frozenset(
    {
        "artifact_kind",
        "status",
        "random_seed",
        "source",
        "search",
        "evaluation",
        "failure_behavior",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "run_id",
        "model_id",
        "package_manifest_sha256",
        "embeddings_sha256",
        "training_oof_sha256",
        "plan_sha256",
    }
)
_SEARCH_FIELDS = frozenset(
    {
        "mrl_dimensions",
        "logistic_regression",
        "linear_svc",
        "calibration",
        "safety_anchor",
    }
)
_LOGISTIC_FIELDS = frozenset({"C", "class_weights", "solver", "max_iter"})
_SVC_FIELDS = frozenset({"C", "class_weights", "max_iter"})
_CALIBRATION_FIELDS = frozenset({"method", "scope"})
_ANCHOR_FIELDS = frozenset(
    {"family", "embedding_dimension", "C", "class_weight"}
)
_EVALUATION_FIELDS = frozenset(
    {
        "scope",
        "outer_folds",
        "inner_folds",
        "minimum_folds",
        "selection_order",
        "diagnostic_cutoff",
        "diagnostic_cutoff_is_routing_threshold",
        "validation_status",
        "test_status",
    }
)


def _exact_mapping(
    value: Any, fields: frozenset[str], reason_code: str
) -> Mapping[str, Any]:
    """要求节点为具有精确字段集合的映射。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise QwenHeadChallengerConfigError(reason_code)
    return value


def _require_hex(value: Any, length: int, reason_code: str) -> str:
    """要求固定长度的小写十六进制身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QwenHeadChallengerConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑 JSON 的 SHA-256。"""

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finite_positive(value: Any, reason_code: str) -> float:
    """解析严格为正的有限超参数。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QwenHeadChallengerConfigError(reason_code)
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise QwenHeadChallengerConfigError(reason_code)
    return result


def _candidate_id(payload: Mapping[str, Any]) -> str:
    """为候选规范生成稳定的截断内容摘要。"""

    return _canonical_sha256(payload)[:32]


def _candidate(
    *,
    family: str,
    dimension: int,
    C: float,
    class_weight: str | None,
    solver: str | None,
    max_iter: int,
) -> QwenHeadCandidateSpec:
    """从规范字段构造内容寻址候选。"""

    payload = {
        "family": family,
        "embedding_dimension": dimension,
        "C": C,
        "class_weight": class_weight,
        "solver": solver,
        "max_iter": max_iter,
        "probability": "group_oof_sigmoid",
        "mrl_renormalize": True,
    }
    return QwenHeadCandidateSpec(
        candidate_id=_candidate_id(payload),
        family=family,
        embedding_dimension=dimension,
        C=C,
        class_weight=class_weight,
        solver=solver,
        max_iter=max_iter,
    )


def load_qwen_head_challenger_plan(
    path: str | Path,
) -> QwenHeadChallengerPlan:
    """加载并严格校验第一层冻结搜索计划。

    Args:
        path: 预登记 YAML 路径。

    Returns:
        含56个固定候选和单一安全锚点的内容寻址计划。

    Raises:
        QwenHeadChallengerConfigError: 文件、字段或任一冻结值漂移。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise QwenHeadChallengerConfigError(
            "qwen_head_challenger_config_unreadable"
        ) from exc
    raw = _exact_mapping(
        loaded, _ROOT_FIELDS, "qwen_head_challenger_config_invalid"
    )
    source = _exact_mapping(
        raw["source"], _SOURCE_FIELDS, "qwen_head_challenger_source_invalid"
    )
    search = _exact_mapping(
        raw["search"], _SEARCH_FIELDS, "qwen_head_challenger_search_invalid"
    )
    logistic = _exact_mapping(
        search["logistic_regression"],
        _LOGISTIC_FIELDS,
        "qwen_head_challenger_logistic_invalid",
    )
    svc = _exact_mapping(
        search["linear_svc"],
        _SVC_FIELDS,
        "qwen_head_challenger_svc_invalid",
    )
    calibration = _exact_mapping(
        search["calibration"],
        _CALIBRATION_FIELDS,
        "qwen_head_challenger_calibration_invalid",
    )
    anchor = _exact_mapping(
        search["safety_anchor"],
        _ANCHOR_FIELDS,
        "qwen_head_challenger_anchor_invalid",
    )
    evaluation = _exact_mapping(
        raw["evaluation"],
        _EVALUATION_FIELDS,
        "qwen_head_challenger_evaluation_invalid",
    )
    expected_source = {
        "run_id": "14feebc04a7a61b8b97f959a998d14dc",
        "model_id": "cb5bad8cd27c2c5df9edea3a2ca20834",
        "package_manifest_sha256": (
            "2e4e8b34688e1bdd4d968bb4c56a22a17d19236a7076789c5c9aa7584712cbf1"
        ),
        "embeddings_sha256": (
            "0517a29e4a14ddadfaa36fc4f69d803227f84a2e746f4864bb641d1acfc423ec"
        ),
        "training_oof_sha256": (
            "ae537f5648848ca94d4c5bcdc2617b9dde8a27f58480eb97f3218b3bc7c047af"
        ),
        "plan_sha256": (
            "56a5900909834c0877725bf3367d295ef5c94bc39527e7737bb5fe802b2b461a"
        ),
    }
    expected_evaluation = {
        "scope": "train_nested_group_oof_cached_embeddings",
        "outer_folds": 5,
        "inner_folds": 4,
        "minimum_folds": 2,
        "selection_order": [
            "not_worse_than_internal_ugc_safety_anchor",
            "minimum_log_loss",
            "maximum_pr_auc_unrelated",
            "stable_candidate_id",
        ],
        "diagnostic_cutoff": 0.5,
        "diagnostic_cutoff_is_routing_threshold": False,
        "validation_status": "not_allowed_until_training_gate_passes",
        "test_status": "locked_not_opened",
    }
    if (
        raw.get("artifact_kind")
        != "formal-cleaning-qwen-head-challenger-plan"
        or raw.get("status") != "frozen_not_fit"
        or raw.get("random_seed") != 20260728
        or raw.get("failure_behavior") != "advance_to_leakage_safe_fusion"
        or dict(source) != expected_source
        or search.get("mrl_dimensions") != [256, 512, 1024, 2560]
        or dict(logistic)
        != {
            "C": [0.1, 1.0, 10.0, 100.0],
            "class_weights": ["none", "balanced"],
            "solver": "liblinear",
            "max_iter": 5000,
        }
        or dict(svc)
        != {
            "C": [0.1, 1.0, 10.0],
            "class_weights": ["none", "balanced"],
            "max_iter": 5000,
        }
        or dict(calibration)
        != {
            "method": "sigmoid",
            "scope": "same_fit_partition_group_oof_margins",
        }
        or dict(anchor)
        != {
            "family": "logistic_regression",
            "embedding_dimension": 2560,
            "C": 1.0,
            "class_weight": "balanced",
        }
        or dict(evaluation) != expected_evaluation
    ):
        raise QwenHeadChallengerConfigError(
            "qwen_head_challenger_plan_invalid"
        )
    for field in (
        "package_manifest_sha256",
        "embeddings_sha256",
        "training_oof_sha256",
        "plan_sha256",
    ):
        _require_hex(
            source.get(field), 64, "qwen_head_challenger_source_invalid"
        )
    dimensions = [int(value) for value in search["mrl_dimensions"]]
    candidates: list[QwenHeadCandidateSpec] = []
    for dimension in dimensions:
        for class_weight_value in logistic["class_weights"]:
            class_weight = (
                None if class_weight_value == "none" else str(class_weight_value)
            )
            for C_value in logistic["C"]:
                candidates.append(
                    _candidate(
                        family="logistic_regression",
                        dimension=dimension,
                        C=_finite_positive(
                            C_value, "qwen_head_challenger_logistic_invalid"
                        ),
                        class_weight=class_weight,
                        solver=str(logistic["solver"]),
                        max_iter=int(logistic["max_iter"]),
                    )
                )
        for class_weight_value in svc["class_weights"]:
            class_weight = (
                None if class_weight_value == "none" else str(class_weight_value)
            )
            for C_value in svc["C"]:
                candidates.append(
                    _candidate(
                        family="linear_svc",
                        dimension=dimension,
                        C=_finite_positive(
                            C_value, "qwen_head_challenger_svc_invalid"
                        ),
                        class_weight=class_weight,
                        solver=None,
                        max_iter=int(svc["max_iter"]),
                    )
                )
    if len(candidates) != 56 or len({item.candidate_id for item in candidates}) != 56:
        raise QwenHeadChallengerConfigError(
            "qwen_head_challenger_candidates_invalid"
        )
    anchor_spec = next(
        (
            item
            for item in candidates
            if item.family == anchor["family"]
            and item.embedding_dimension == anchor["embedding_dimension"]
            and item.C == anchor["C"]
            and item.class_weight == anchor["class_weight"]
        ),
        None,
    )
    if anchor_spec is None:
        raise QwenHeadChallengerConfigError(
            "qwen_head_challenger_anchor_invalid"
        )
    plan_sha256 = _canonical_sha256(raw)
    return QwenHeadChallengerPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=int(raw["random_seed"]),
        source_run_id=str(source["run_id"]),
        source_model_id=str(source["model_id"]),
        source_package_manifest_sha256=str(source["package_manifest_sha256"]),
        source_embeddings_sha256=str(source["embeddings_sha256"]),
        source_training_oof_sha256=str(source["training_oof_sha256"]),
        source_plan_sha256=str(source["plan_sha256"]),
        candidates=tuple(candidates),
        safety_anchor_candidate_id=anchor_spec.candidate_id,
        outer_folds=int(evaluation["outer_folds"]),
        inner_folds=int(evaluation["inner_folds"]),
        minimum_folds=int(evaluation["minimum_folds"]),
        diagnostic_cutoff=float(evaluation["diagnostic_cutoff"]),
    )
