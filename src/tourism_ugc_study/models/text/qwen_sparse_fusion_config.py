"""Qwen 与固定 sparse comparator 无泄漏融合的冻结配置解析。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class QwenSparseFusionConfigError(RuntimeError):
    """融合配置违反冻结契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("qwen sparse fusion configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class FusionWeightSpec:
    """一个固定 Qwen logit 权重及其内容身份。"""

    weight_id: str
    qwen_weight: float


@dataclass(frozen=True)
class QwenSparseFusionPlan:
    """内容寻址的第二层 nested OOF 融合计划。"""

    plan_id: str
    plan_sha256: str
    random_seed: int
    reference_csv_sha256: str
    train_manifest_sha256: str
    validation_manifest_sha256: str
    test_manifest_sha256: str
    split_anchor_model_id: str
    qwen_source_run_id: str
    qwen_source_model_id: str
    qwen_source_package_manifest_sha256: str
    qwen_source_embeddings_sha256: str
    qwen_source_training_oof_sha256: str
    qwen_base_plan_sha256: str
    head_run_id: str
    head_model_id: str
    head_package_manifest_sha256: str
    head_plan_sha256: str
    sparse_run_id: str
    sparse_model_id: str
    sparse_package_manifest_sha256: str
    sparse_candidate_id: str
    sparse_plan_sha256: str
    acceptance_policy_sha256: str
    weights: tuple[FusionWeightSpec, ...]
    probability_clip: float
    outer_folds: int
    inner_folds: int
    minimum_folds: int
    diagnostic_cutoff: float


_ROOT_FIELDS = frozenset(
    {
        "artifact_kind",
        "status",
        "random_seed",
        "data",
        "source",
        "acceptance",
        "fusion",
        "evaluation",
        "failure_behavior",
    }
)
_DATA_FIELDS = frozenset(
    {
        "reference_csv_sha256",
        "train_manifest_sha256",
        "validation_manifest_sha256",
        "test_manifest_sha256",
        "split_anchor_model_id",
    }
)
_SOURCE_FIELDS = frozenset({"qwen_baseline", "head_challenger", "sparse"})
_QWEN_SOURCE_FIELDS = frozenset(
    {
        "run_id",
        "model_id",
        "package_manifest_sha256",
        "embeddings_sha256",
        "training_oof_sha256",
        "plan_sha256",
    }
)
_HEAD_SOURCE_FIELDS = frozenset(
    {
        "run_id",
        "model_id",
        "package_manifest_sha256",
        "plan_sha256",
        "acceptance_status",
        "validation_status",
    }
)
_SPARSE_SOURCE_FIELDS = frozenset(
    {
        "run_id",
        "model_id",
        "package_manifest_sha256",
        "selected_candidate_id",
        "plan_sha256",
    }
)
_ACCEPTANCE_FIELDS = frozenset({"policy_sha256", "decision_rule"})
_FUSION_FIELDS = frozenset(
    {
        "qwen_weights",
        "probability_clip",
        "space",
        "weight_zero_role",
        "weight_one_role",
        "sparse_search",
        "qwen_head_selection",
        "selection_order",
    }
)
_EVALUATION_FIELDS = frozenset(
    {
        "scope",
        "outer_folds",
        "inner_folds",
        "minimum_folds",
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
        raise QwenSparseFusionConfigError(reason_code)
    return value


def _require_hex(value: Any, length: int, reason_code: str) -> str:
    """要求固定长度的小写十六进制身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QwenSparseFusionConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑 JSON 的 SHA-256。"""

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _weight_spec(value: Any) -> FusionWeightSpec:
    """校验闭区间融合权重并生成稳定身份。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QwenSparseFusionConfigError("qwen_sparse_fusion_weight_invalid")
    weight = float(value)
    if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
        raise QwenSparseFusionConfigError("qwen_sparse_fusion_weight_invalid")
    weight_id = _canonical_sha256(
        {
            "space": "logit",
            "qwen_weight": weight,
            "probability_clip": 1e-6,
        }
    )[:32]
    return FusionWeightSpec(weight_id=weight_id, qwen_weight=weight)


def load_qwen_sparse_fusion_plan(path: str | Path) -> QwenSparseFusionPlan:
    """加载并严格校验第二层无泄漏融合计划。

    Args:
        path: 预登记 YAML 路径。

    Returns:
        绑定三份既有运行和五个固定权重的内容寻址计划。

    Raises:
        QwenSparseFusionConfigError: 文件、字段或任一冻结值漂移。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise QwenSparseFusionConfigError(
            "qwen_sparse_fusion_config_unreadable"
        ) from exc
    raw = _exact_mapping(
        loaded, _ROOT_FIELDS, "qwen_sparse_fusion_config_invalid"
    )
    data = _exact_mapping(
        raw["data"], _DATA_FIELDS, "qwen_sparse_fusion_data_invalid"
    )
    source = _exact_mapping(
        raw["source"], _SOURCE_FIELDS, "qwen_sparse_fusion_source_invalid"
    )
    qwen = _exact_mapping(
        source["qwen_baseline"],
        _QWEN_SOURCE_FIELDS,
        "qwen_sparse_fusion_qwen_source_invalid",
    )
    head = _exact_mapping(
        source["head_challenger"],
        _HEAD_SOURCE_FIELDS,
        "qwen_sparse_fusion_head_source_invalid",
    )
    sparse = _exact_mapping(
        source["sparse"],
        _SPARSE_SOURCE_FIELDS,
        "qwen_sparse_fusion_sparse_source_invalid",
    )
    acceptance = _exact_mapping(
        raw["acceptance"],
        _ACCEPTANCE_FIELDS,
        "qwen_sparse_fusion_acceptance_invalid",
    )
    fusion = _exact_mapping(
        raw["fusion"], _FUSION_FIELDS, "qwen_sparse_fusion_search_invalid"
    )
    evaluation = _exact_mapping(
        raw["evaluation"],
        _EVALUATION_FIELDS,
        "qwen_sparse_fusion_evaluation_invalid",
    )
    expected_data = {
        "reference_csv_sha256": "3eb3ea233c05bf76b5bdda548701895c9551726d74b6f9742523e695aeac73fd",
        "train_manifest_sha256": "4365348070336ff60a27383c451aa9ae565bb537eac22282a1a5495716d8f80c",
        "validation_manifest_sha256": "d103058cfaf8de5faa69248a8e7825035b25cb44d23aff041ace533b3bf51800",
        "test_manifest_sha256": "e8cbcbc306ff5faedf9b94fcac9a88ff0e3885ab4d74ea99b12c63eb52721ae8",
        "split_anchor_model_id": "9cd30922aabf7fb2e2ba42e5a0396cfd",
    }
    expected_qwen = {
        "run_id": "14feebc04a7a61b8b97f959a998d14dc",
        "model_id": "cb5bad8cd27c2c5df9edea3a2ca20834",
        "package_manifest_sha256": "2e4e8b34688e1bdd4d968bb4c56a22a17d19236a7076789c5c9aa7584712cbf1",
        "embeddings_sha256": "0517a29e4a14ddadfaa36fc4f69d803227f84a2e746f4864bb641d1acfc423ec",
        "training_oof_sha256": "ae537f5648848ca94d4c5bcdc2617b9dde8a27f58480eb97f3218b3bc7c047af",
        "plan_sha256": "56a5900909834c0877725bf3367d295ef5c94bc39527e7737bb5fe802b2b461a",
    }
    expected_head = {
        "run_id": "3d23bc1b9b8c2957856851fa1454eccd",
        "model_id": "a2f919b09d1a78e7b8a8b749524013a0",
        "package_manifest_sha256": "bf5936977f4712b556e2b0cb41cdb092330ebbb1c46e543bc58516970ae135ee",
        "plan_sha256": "f9a1cccab81b2ea9ba8c26b5d539781cbb54ba991e185d9a16f19323ae492fb9",
        "acceptance_status": "failed_retain_baseline",
        "validation_status": "not_allowed",
    }
    expected_sparse = {
        "run_id": "ce19406cd132e55b2eb00531f5cc4cd3",
        "model_id": "1b68baa8bef99d6b9d75b7bf3226cfb4",
        "package_manifest_sha256": "9769da2175d217833d5183a0d261ea3fae0b809f1483c4a0dd266beb648681d9",
        "selected_candidate_id": "1ed77d39ddf8f7c8d75c98412d3eb073",
        "plan_sha256": "ad515917c735ce77ed5231f0fa25bd536e8395115f22b9bf4e5035f4df199301",
    }
    expected_fusion = {
        "qwen_weights": [0.0, 0.25, 0.5, 0.75, 1.0],
        "probability_clip": 1e-6,
        "space": "logit",
        "weight_zero_role": "sparse_audit_anchor",
        "weight_one_role": "qwen_audit_anchor",
        "sparse_search": False,
        "qwen_head_selection": "repeat_frozen_head_plan_inside_each_outer_fit",
        "selection_order": [
            "not_worse_than_inner_sparse_ugc_safety",
            "minimum_log_loss",
            "maximum_pr_auc_unrelated",
            "stable_weight_id",
        ],
    }
    expected_evaluation = {
        "scope": "train_nested_group_oof",
        "outer_folds": 5,
        "inner_folds": 4,
        "minimum_folds": 2,
        "diagnostic_cutoff": 0.5,
        "diagnostic_cutoff_is_routing_threshold": False,
        "validation_status": "not_allowed_until_training_gate_passes",
        "test_status": "locked_not_opened",
    }
    if (
        raw.get("artifact_kind")
        != "formal-cleaning-qwen-sparse-fusion-plan"
        or raw.get("status") != "frozen_not_fit"
        or raw.get("random_seed") != 20260728
        or raw.get("failure_behavior") != "advance_to_english_head_tail"
        or dict(data) != expected_data
        or dict(qwen) != expected_qwen
        or dict(head) != expected_head
        or dict(sparse) != expected_sparse
        or dict(acceptance)
        != {
            "policy_sha256": "42b1770fe0241fb7aa69303878141330cedcd7cb54053ed82908d8fad64d7836",
            "decision_rule": "ugc_safety_first",
        }
        or dict(fusion) != expected_fusion
        or dict(evaluation) != expected_evaluation
    ):
        raise QwenSparseFusionConfigError("qwen_sparse_fusion_plan_invalid")
    for mapping, fields in (
        (data, tuple(field for field in _DATA_FIELDS if field.endswith("sha256"))),
        (
            qwen,
            (
                "package_manifest_sha256",
                "embeddings_sha256",
                "training_oof_sha256",
                "plan_sha256",
            ),
        ),
        (head, ("package_manifest_sha256", "plan_sha256")),
        (sparse, ("package_manifest_sha256", "plan_sha256")),
        (acceptance, ("policy_sha256",)),
    ):
        for field in fields:
            _require_hex(
                mapping.get(field), 64, "qwen_sparse_fusion_identity_invalid"
            )
    weights = tuple(_weight_spec(value) for value in fusion["qwen_weights"])
    if len(weights) != 5 or len({item.weight_id for item in weights}) != 5:
        raise QwenSparseFusionConfigError("qwen_sparse_fusion_weight_invalid")
    plan_sha256 = _canonical_sha256(raw)
    return QwenSparseFusionPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=int(raw["random_seed"]),
        reference_csv_sha256=str(data["reference_csv_sha256"]),
        train_manifest_sha256=str(data["train_manifest_sha256"]),
        validation_manifest_sha256=str(data["validation_manifest_sha256"]),
        test_manifest_sha256=str(data["test_manifest_sha256"]),
        split_anchor_model_id=str(data["split_anchor_model_id"]),
        qwen_source_run_id=str(qwen["run_id"]),
        qwen_source_model_id=str(qwen["model_id"]),
        qwen_source_package_manifest_sha256=str(qwen["package_manifest_sha256"]),
        qwen_source_embeddings_sha256=str(qwen["embeddings_sha256"]),
        qwen_source_training_oof_sha256=str(qwen["training_oof_sha256"]),
        qwen_base_plan_sha256=str(qwen["plan_sha256"]),
        head_run_id=str(head["run_id"]),
        head_model_id=str(head["model_id"]),
        head_package_manifest_sha256=str(head["package_manifest_sha256"]),
        head_plan_sha256=str(head["plan_sha256"]),
        sparse_run_id=str(sparse["run_id"]),
        sparse_model_id=str(sparse["model_id"]),
        sparse_package_manifest_sha256=str(sparse["package_manifest_sha256"]),
        sparse_candidate_id=str(sparse["selected_candidate_id"]),
        sparse_plan_sha256=str(sparse["plan_sha256"]),
        acceptance_policy_sha256=str(acceptance["policy_sha256"]),
        weights=weights,
        probability_clip=float(fusion["probability_clip"]),
        outer_folds=int(evaluation["outer_folds"]),
        inner_folds=int(evaluation["inner_folds"]),
        minimum_folds=int(evaluation["minimum_folds"]),
        diagnostic_cutoff=float(evaluation["diagnostic_cutoff"]),
    )
