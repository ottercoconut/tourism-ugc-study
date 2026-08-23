"""Qwen 英文 instruction 与 head-tail 双视图计划的严格解析。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class QwenHeadTailConfigError(RuntimeError):
    """第三层表示计划违反冻结契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("qwen head tail configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenHeadTailPlan:
    """内容寻址的第三层英文双视图编码与评测计划。"""

    plan_id: str
    plan_sha256: str
    random_seed: int
    reference_csv_sha256: str
    train_manifest_sha256: str
    validation_manifest_sha256: str
    test_manifest_sha256: str
    split_anchor_model_id: str
    qwen_base_plan_sha256: str
    head_plan_sha256: str
    sparse_run_id: str
    sparse_model_id: str
    sparse_package_manifest_sha256: str
    sparse_paired_oof_sha256: str
    layer2_run_id: str
    layer2_model_id: str
    layer2_package_manifest_sha256: str
    acceptance_policy_sha256: str
    instruction: str
    prompt_template: str
    max_length: int
    projection: str
    aggregation: str
    device: str
    batch_size: int
    parameter_dtype: str
    output_dtype: str
    outer_folds: int
    inner_folds: int
    minimum_folds: int


_ROOT_FIELDS = frozenset(
    {
        "artifact_kind",
        "status",
        "random_seed",
        "data",
        "source",
        "acceptance",
        "encoder_change",
        "execution",
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
_SOURCE_FIELDS = frozenset(
    {
        "qwen_base_plan_sha256",
        "head_plan_sha256",
        "sparse_run_id",
        "sparse_model_id",
        "sparse_package_manifest_sha256",
        "sparse_paired_oof_sha256",
        "layer2_run_id",
        "layer2_model_id",
        "layer2_package_manifest_sha256",
        "layer2_acceptance_status",
        "layer2_validation_status",
    }
)
_ACCEPTANCE_FIELDS = frozenset({"policy_sha256", "decision_rule"})
_ENCODER_FIELDS = frozenset(
    {
        "instruction",
        "prompt_template",
        "max_length",
        "projection",
        "content_budget",
        "short_text_views",
        "long_text_views",
        "head_window",
        "tail_window",
        "aggregation",
        "middle_policy",
        "platform_used",
        "fine_tuning",
    }
)
_EXECUTION_FIELDS = frozenset(
    {"device", "batch_size", "parameter_dtype", "output_dtype"}
)
_EVALUATION_FIELDS = frozenset(
    {
        "scope",
        "head_plan",
        "outer_folds",
        "inner_folds",
        "minimum_folds",
        "length_subgroups",
        "validation_status",
        "test_status",
    }
)


def _exact_mapping(
    value: Any, fields: frozenset[str], reason_code: str
) -> Mapping[str, Any]:
    """要求节点为具有精确字段集合的映射。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise QwenHeadTailConfigError(reason_code)
    return value


def _require_hex(value: Any, length: int, reason_code: str) -> str:
    """要求固定长度的小写十六进制身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QwenHeadTailConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑 JSON 的 SHA-256。"""

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_qwen_head_tail_plan(path: str | Path) -> QwenHeadTailPlan:
    """加载并严格校验第三层英文 head-tail 计划。

    Args:
        path: 预登记 YAML 路径。

    Returns:
        绑定第二层失败、固定 instruction 和两视图算法的内容寻址计划。

    Raises:
        QwenHeadTailConfigError: 文件、字段或任一冻结值漂移。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise QwenHeadTailConfigError(
            "qwen_head_tail_config_unreadable"
        ) from exc
    raw = _exact_mapping(loaded, _ROOT_FIELDS, "qwen_head_tail_config_invalid")
    data = _exact_mapping(raw["data"], _DATA_FIELDS, "qwen_head_tail_data_invalid")
    source = _exact_mapping(
        raw["source"], _SOURCE_FIELDS, "qwen_head_tail_source_invalid"
    )
    acceptance = _exact_mapping(
        raw["acceptance"],
        _ACCEPTANCE_FIELDS,
        "qwen_head_tail_acceptance_invalid",
    )
    encoder = _exact_mapping(
        raw["encoder_change"],
        _ENCODER_FIELDS,
        "qwen_head_tail_encoder_invalid",
    )
    execution = _exact_mapping(
        raw["execution"],
        _EXECUTION_FIELDS,
        "qwen_head_tail_execution_invalid",
    )
    evaluation = _exact_mapping(
        raw["evaluation"],
        _EVALUATION_FIELDS,
        "qwen_head_tail_evaluation_invalid",
    )
    expected_data = {
        "reference_csv_sha256": "3eb3ea233c05bf76b5bdda548701895c9551726d74b6f9742523e695aeac73fd",
        "train_manifest_sha256": "4365348070336ff60a27383c451aa9ae565bb537eac22282a1a5495716d8f80c",
        "validation_manifest_sha256": "d103058cfaf8de5faa69248a8e7825035b25cb44d23aff041ace533b3bf51800",
        "test_manifest_sha256": "e8cbcbc306ff5faedf9b94fcac9a88ff0e3885ab4d74ea99b12c63eb52721ae8",
        "split_anchor_model_id": "9cd30922aabf7fb2e2ba42e5a0396cfd",
    }
    expected_source = {
        "qwen_base_plan_sha256": "56a5900909834c0877725bf3367d295ef5c94bc39527e7737bb5fe802b2b461a",
        "head_plan_sha256": "f9a1cccab81b2ea9ba8c26b5d539781cbb54ba991e185d9a16f19323ae492fb9",
        "sparse_run_id": "ce19406cd132e55b2eb00531f5cc4cd3",
        "sparse_model_id": "1b68baa8bef99d6b9d75b7bf3226cfb4",
        "sparse_package_manifest_sha256": "9769da2175d217833d5183a0d261ea3fae0b809f1483c4a0dd266beb648681d9",
        "sparse_paired_oof_sha256": "95d5624336d2336c26ea3d044f879b32485a0e2dcd98e6b11dd48ae592aa03c6",
        "layer2_run_id": "eba8568816309688f1f85e6092a57b1d",
        "layer2_model_id": "4eb42812a7d9d68388418a6d58442c2b",
        "layer2_package_manifest_sha256": "37fcdc206ba4c7c43afe2820ad2c47292925a91a8bf191084dc13b9c8ed261d3",
        "layer2_acceptance_status": "failed_retain_baseline",
        "layer2_validation_status": "not_allowed",
    }
    expected_acceptance = {
        "policy_sha256": "3d201246b06fa58f48f88083c81f0de16588da9520e30944c3bf5354ab06ff7a",
        "decision_rule": "ugc_safety_first",
    }
    expected_encoder = {
        "instruction": "Classify whether the following social-media post is tourist-generated content about a personal visit to Qingdao. Pure advertising, business promotion, local non-tourist content, and general city information are unrelated.",
        "prompt_template": "Instruct: {instruction}\nQuery: ",
        "max_length": 2048,
        "projection": "head_tail_two_view",
        "content_budget": "max_length_minus_prompt_and_special_tokens",
        "short_text_views": 1,
        "long_text_views": 2,
        "head_window": "first_content_budget_tokens",
        "tail_window": "last_content_budget_tokens",
        "aggregation": "arithmetic_mean_then_l2_normalize",
        "middle_policy": "omitted_and_aggregately_reported",
        "platform_used": False,
        "fine_tuning": False,
    }
    expected_execution = {
        "device": "mps",
        "batch_size": 1,
        "parameter_dtype": "bfloat16",
        "output_dtype": "float32",
    }
    expected_evaluation = {
        "scope": "train_nested_group_oof_new_representation",
        "head_plan": "repeat_frozen_56_candidate_plan",
        "outer_folds": 5,
        "inner_folds": 4,
        "minimum_folds": 2,
        "length_subgroups": [
            "within_single_view",
            "head_tail_overflow",
            "middle_omitted",
        ],
        "validation_status": "not_allowed_until_training_gate_passes",
        "test_status": "locked_not_opened",
    }
    if (
        raw.get("artifact_kind")
        != "formal-cleaning-qwen-english-head-tail-plan"
        or raw.get("status") != "frozen_not_fit"
        or raw.get("random_seed") != 20260728
        or raw.get("failure_behavior")
        != "retain_sparse_and_stop_issue45_search"
        or dict(data) != expected_data
        or dict(source) != expected_source
        or dict(acceptance) != expected_acceptance
        or dict(encoder) != expected_encoder
        or dict(execution) != expected_execution
        or dict(evaluation) != expected_evaluation
    ):
        raise QwenHeadTailConfigError("qwen_head_tail_plan_invalid")
    for mapping, fields in (
        (data, tuple(field for field in _DATA_FIELDS if field.endswith("sha256"))),
        (
            source,
            (
                "qwen_base_plan_sha256",
                "head_plan_sha256",
                "sparse_package_manifest_sha256",
                "sparse_paired_oof_sha256",
                "layer2_package_manifest_sha256",
            ),
        ),
        (acceptance, ("policy_sha256",)),
    ):
        for field in fields:
            _require_hex(mapping.get(field), 64, "qwen_head_tail_identity_invalid")
    plan_sha256 = _canonical_sha256(raw)
    return QwenHeadTailPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=int(raw["random_seed"]),
        reference_csv_sha256=str(data["reference_csv_sha256"]),
        train_manifest_sha256=str(data["train_manifest_sha256"]),
        validation_manifest_sha256=str(data["validation_manifest_sha256"]),
        test_manifest_sha256=str(data["test_manifest_sha256"]),
        split_anchor_model_id=str(data["split_anchor_model_id"]),
        qwen_base_plan_sha256=str(source["qwen_base_plan_sha256"]),
        head_plan_sha256=str(source["head_plan_sha256"]),
        sparse_run_id=str(source["sparse_run_id"]),
        sparse_model_id=str(source["sparse_model_id"]),
        sparse_package_manifest_sha256=str(
            source["sparse_package_manifest_sha256"]
        ),
        sparse_paired_oof_sha256=str(source["sparse_paired_oof_sha256"]),
        layer2_run_id=str(source["layer2_run_id"]),
        layer2_model_id=str(source["layer2_model_id"]),
        layer2_package_manifest_sha256=str(
            source["layer2_package_manifest_sha256"]
        ),
        acceptance_policy_sha256=str(acceptance["policy_sha256"]),
        instruction=str(encoder["instruction"]),
        prompt_template=str(encoder["prompt_template"]),
        max_length=int(encoder["max_length"]),
        projection=str(encoder["projection"]),
        aggregation=str(encoder["aggregation"]),
        device=str(execution["device"]),
        batch_size=int(execution["batch_size"]),
        parameter_dtype=str(execution["parameter_dtype"]),
        output_dtype=str(execution["output_dtype"]),
        outer_folds=int(evaluation["outer_folds"]),
        inner_folds=int(evaluation["inner_folds"]),
        minimum_folds=int(evaluation["minimum_folds"]),
    )
