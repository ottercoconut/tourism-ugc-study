"""Qwen3-Embedding 本地语义 baseline 的冻结配置解析。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class QwenEmbeddingConfigError(RuntimeError):
    """语义 baseline 配置违反冻结契约时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、成员身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: 供 CLI、测试和运行 manifest 使用的失败码。
        """

        super().__init__("qwen embedding baseline configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenEncoderSpec:
    """冻结的本地 Qwen 编码器身份与文本投影。

    Attributes:
        repository: 上游 Hugging Face 仓库身份。
        revision: 下载时必须使用的完整提交 SHA。
        weights_sha256: 全部 safetensors 分片及其摘要映射的规范 SHA-256。
        weight_files: safetensors 分片文件名及各自 SHA-256。
        license: 上游许可证标识。
        embedding_dimension: 完整向量维数。
        max_length: 单通道全文最大 token 数。
        pooling: 固定为末 token pooling。
        normalize_embeddings: 是否进行 L2 归一化。
        instruction: 加到每条文本前的同一任务说明，不包含平台或答案。
    """

    repository: str
    revision: str
    weights_sha256: str
    weight_files: tuple[tuple[str, str], ...]
    snapshot_sha256: str
    snapshot_files: tuple[tuple[str, str], ...]
    license: str
    embedding_dimension: int
    max_length: int
    pooling: str
    normalize_embeddings: bool
    instruction: str


@dataclass(frozen=True)
class QwenExecutionSpec:
    """冻结的本地编码执行环境参数。"""

    device: str
    batch_size: int
    parameter_dtype: str
    output_dtype: str


@dataclass(frozen=True)
class QwenClassifierSpec:
    """冻结语义向量之上的唯一线性概率头。"""

    family: str
    C: float
    class_weight: str
    solver: str
    max_iter: int
    probability: str


@dataclass(frozen=True)
class QwenEmbeddingPlan:
    """内容寻址的 Qwen 语义 baseline 预登记计划。

    该计划只允许一个冻结编码器和一个线性分类头，不包含搜索空间、平台、
    路由阈值或测试读取入口。
    """

    plan_id: str
    plan_sha256: str
    random_seed: int
    reference_csv_sha256: str
    train_manifest_sha256: str
    validation_manifest_sha256: str
    test_manifest_sha256: str
    split_anchor_model_id: str
    comparator_run_id: str
    comparator_model_id: str
    comparator_package_manifest_sha256: str
    comparator_paired_oof_sha256: str
    comparator_validation_id: str
    comparator_validation_manifest_sha256: str
    acceptance_policy_sha256: str
    encoder: QwenEncoderSpec
    execution: QwenExecutionSpec
    classifier: QwenClassifierSpec
    outer_folds: int
    minimum_folds: int
    diagnostic_cutoff: float
    confidence_band_low: float
    confidence_band_high: float
    risk_coverage_confidence_grid: tuple[float, ...]


_ROOT_FIELDS = frozenset(
    {
        "artifact_kind",
        "status",
        "random_seed",
        "data",
        "comparator",
        "acceptance",
        "encoder",
        "execution",
        "classifier",
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
_COMPARATOR_FIELDS = frozenset(
    {
        "run_id",
        "model_id",
        "package_manifest_sha256",
        "paired_oof_sha256",
        "validation_id",
        "validation_manifest_sha256",
        "validation_status",
    }
)
_ACCEPTANCE_FIELDS = frozenset({"policy_sha256", "decision_rule"})
_ENCODER_FIELDS = frozenset(
    {
        "repository",
        "revision",
        "weights_sha256",
        "weight_files",
        "snapshot_sha256",
        "snapshot_files",
        "license",
        "embedding_dimension",
        "max_length",
        "pooling",
        "normalize_embeddings",
        "instruction",
        "local_files_only",
        "fine_tuning",
        "title_body_channels",
        "platform_used",
    }
)
_EXECUTION_FIELDS = frozenset(
    {"device", "batch_size", "parameter_dtype", "output_dtype"}
)
_CLASSIFIER_FIELDS = frozenset(
    {"family", "C", "class_weight", "solver", "max_iter", "probability"}
)
_EVALUATION_FIELDS = frozenset(
    {
        "scope",
        "outer_folds",
        "minimum_folds",
        "diagnostic_cutoff",
        "diagnostic_cutoff_is_routing_threshold",
        "confidence_band_low",
        "confidence_band_high",
        "confidence_band_is_routing_threshold",
        "risk_coverage_confidence_grid",
        "validation_role",
        "test_status",
    }
)


def _exact_mapping(
    value: Any,
    fields: frozenset[str],
    reason_code: str,
) -> Mapping[str, Any]:
    """要求节点为具有精确字段集合的映射。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise QwenEmbeddingConfigError(reason_code)
    return value


def _require_hex(value: Any, length: int, reason_code: str) -> str:
    """要求固定长度的小写十六进制身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QwenEmbeddingConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算排序、紧凑 JSON 的 SHA-256。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_frozen_values(raw: Mapping[str, Any]) -> None:
    """拒绝任何未预登记的模型、文本、评估或部署漂移。"""

    data = _exact_mapping(
        raw["data"], _DATA_FIELDS, "qwen_embedding_data_binding_invalid"
    )
    comparator = _exact_mapping(
        raw["comparator"],
        _COMPARATOR_FIELDS,
        "qwen_embedding_comparator_invalid",
    )
    acceptance = _exact_mapping(
        raw["acceptance"],
        _ACCEPTANCE_FIELDS,
        "qwen_embedding_acceptance_invalid",
    )
    encoder = _exact_mapping(
        raw["encoder"], _ENCODER_FIELDS, "qwen_embedding_encoder_invalid"
    )
    execution = _exact_mapping(
        raw["execution"], _EXECUTION_FIELDS, "qwen_embedding_execution_invalid"
    )
    classifier = _exact_mapping(
        raw["classifier"],
        _CLASSIFIER_FIELDS,
        "qwen_embedding_classifier_invalid",
    )
    evaluation = _exact_mapping(
        raw["evaluation"],
        _EVALUATION_FIELDS,
        "qwen_embedding_evaluation_invalid",
    )
    expected_encoder = {
        "repository": "Qwen/Qwen3-Embedding-4B",
        "revision": "5cf2132abc99cad020ac570b19d031efec650f2b",
        "weights_sha256": "e49e59781ff5f117a16cbf9e37655202ce529729ace5aaf3b68fa88fe57906b9",
        "weight_files": {
            "model-00001-of-00002.safetensors": "e70bfe3c970523fb7ef4eddffed2254ce3f1e7150c3de2af4342de129dd756f8",
            "model-00002-of-00002.safetensors": "ed1b87c8e9eb7e535a1a155e4fd00d9f4dba80e58a6db48a4c9f82cede7079c1",
        },
        "snapshot_sha256": "cce6e0f7cd81e6c7cf31a67708362e6e9762b6c343d9805506c08ee283d0bac9",
        "snapshot_files": {
            ".gitattributes": "34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930",
            "1_Pooling/config.json": "0f0ed3380602b252fced3fab6d07c76752c32ffca818ccc61afaf17ae8edd96f",
            "README.md": "3c8dafc1e6529491fa8918cd96c62b90229b317fd83f2794cea20cda100d6122",
            "config.json": "78d2861cbbfd80eee05839200c5a3b7ed64c789f6c1cab4fbb84cc4eae33eaf5",
            "config_sentence_transformers.json": "10667c72ddb772627bf1780cb7f86af8e2ae0032b8c243c731172064105c6961",
            "generation_config.json": "28396d421a2108acce96383f6a7de78008f7f1b17f807958f3c14c51dbfb65fb",
            "merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
            "model-00001-of-00002.safetensors": "e70bfe3c970523fb7ef4eddffed2254ce3f1e7150c3de2af4342de129dd756f8",
            "model-00002-of-00002.safetensors": "ed1b87c8e9eb7e535a1a155e4fd00d9f4dba80e58a6db48a4c9f82cede7079c1",
            "model.safetensors.index.json": "9d130c7f24fa1f9a2a7e19fad42c7d6d2d6fea31b180bdf3e8aac1924c26c39a",
            "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
            "tokenizer.json": "83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d",
            "tokenizer_config.json": "2f58f4bbd7bbce15d683f525954ef3a92cd82f5e06415a9c513859bf8ab72436",
            "vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
        },
        "license": "Apache-2.0",
        "embedding_dimension": 2560,
        "max_length": 2048,
        "pooling": "last_token",
        "normalize_embeddings": True,
        "instruction": "判断以下社交媒体帖子是否属于游客在青岛的旅游体验 UGC，而不是纯广告或非游客内容",
        "local_files_only": True,
        "fine_tuning": False,
        "title_body_channels": False,
        "platform_used": False,
    }
    expected_classifier = {
        "family": "logistic_regression",
        "C": 1.0,
        "class_weight": "balanced",
        "solver": "liblinear",
        "max_iter": 2000,
        "probability": "native_predict_proba",
    }
    expected_execution = {
        "device": "mps",
        "batch_size": 1,
        "parameter_dtype": "bfloat16",
        "output_dtype": "float32",
    }
    expected_evaluation = {
        "scope": "train_group_oof_fixed_candidate",
        "outer_folds": 5,
        "minimum_folds": 2,
        "diagnostic_cutoff": 0.50,
        "diagnostic_cutoff_is_routing_threshold": False,
        "confidence_band_low": 0.10,
        "confidence_band_high": 0.90,
        "confidence_band_is_routing_threshold": False,
        "risk_coverage_confidence_grid": [
            0.50,
            0.60,
            0.70,
            0.80,
            0.90,
            0.95,
            0.975,
            0.99,
        ],
        "validation_role": "unique_candidate_directional_check_only",
        "test_status": "locked_not_opened",
    }
    if (
        raw.get("artifact_kind")
        != "formal-cleaning-qwen-embedding-baseline-plan"
        or raw.get("status") != "frozen_not_fit"
        or raw.get("random_seed") != 20260728
        or raw.get("failure_behavior") != "retain_sparse_comparator"
        or data.get("split_anchor_model_id")
        != "9cd30922aabf7fb2e2ba42e5a0396cfd"
        or comparator.get("run_id") != "ce19406cd132e55b2eb00531f5cc4cd3"
        or comparator.get("model_id") != "1b68baa8bef99d6b9d75b7bf3226cfb4"
        or comparator.get("validation_id")
        != "6d105b334df0234350a4b5c32358f7a9"
        or comparator.get("validation_status") != "directionally_consistent"
        or acceptance.get("decision_rule") != "ugc_safety_first"
        or dict(encoder) != expected_encoder
        or dict(execution) != expected_execution
        or dict(classifier) != expected_classifier
        or dict(evaluation) != expected_evaluation
    ):
        raise QwenEmbeddingConfigError("qwen_embedding_plan_invalid")
    for field in (
        "reference_csv_sha256",
        "train_manifest_sha256",
        "validation_manifest_sha256",
        "test_manifest_sha256",
    ):
        _require_hex(data.get(field), 64, "qwen_embedding_data_binding_invalid")
    for field in (
        "package_manifest_sha256",
        "paired_oof_sha256",
        "validation_manifest_sha256",
    ):
        _require_hex(
            comparator.get(field), 64, "qwen_embedding_comparator_invalid"
        )
    _require_hex(
        acceptance.get("policy_sha256"),
        64,
        "qwen_embedding_acceptance_invalid",
    )


def load_qwen_embedding_plan(path: str | Path) -> QwenEmbeddingPlan:
    """加载并严格校验唯一 Qwen 语义 baseline 计划。

    Args:
        path: 预登记 YAML 路径。

    Returns:
        内容寻址且没有候选搜索空间的冻结计划。

    Raises:
        QwenEmbeddingConfigError: 文件不可读、字段未知或任一值漂移。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise QwenEmbeddingConfigError("qwen_embedding_config_unreadable") from exc
    raw = _exact_mapping(
        loaded, _ROOT_FIELDS, "qwen_embedding_config_invalid"
    )
    _validate_frozen_values(raw)
    data = raw["data"]
    comparator = raw["comparator"]
    encoder = raw["encoder"]
    execution = raw["execution"]
    classifier = raw["classifier"]
    evaluation = raw["evaluation"]
    plan_sha256 = _canonical_sha256(raw)
    return QwenEmbeddingPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=int(raw["random_seed"]),
        reference_csv_sha256=str(data["reference_csv_sha256"]),
        train_manifest_sha256=str(data["train_manifest_sha256"]),
        validation_manifest_sha256=str(data["validation_manifest_sha256"]),
        test_manifest_sha256=str(data["test_manifest_sha256"]),
        split_anchor_model_id=str(data["split_anchor_model_id"]),
        comparator_run_id=str(comparator["run_id"]),
        comparator_model_id=str(comparator["model_id"]),
        comparator_package_manifest_sha256=str(
            comparator["package_manifest_sha256"]
        ),
        comparator_paired_oof_sha256=str(comparator["paired_oof_sha256"]),
        comparator_validation_id=str(comparator["validation_id"]),
        comparator_validation_manifest_sha256=str(
            comparator["validation_manifest_sha256"]
        ),
        acceptance_policy_sha256=str(raw["acceptance"]["policy_sha256"]),
        encoder=QwenEncoderSpec(
            repository=str(encoder["repository"]),
            revision=str(encoder["revision"]),
            weights_sha256=str(encoder["weights_sha256"]),
            weight_files=tuple(sorted(dict(encoder["weight_files"]).items())),
            snapshot_sha256=str(encoder["snapshot_sha256"]),
            snapshot_files=tuple(sorted(dict(encoder["snapshot_files"]).items())),
            license=str(encoder["license"]),
            embedding_dimension=int(encoder["embedding_dimension"]),
            max_length=int(encoder["max_length"]),
            pooling=str(encoder["pooling"]),
            normalize_embeddings=bool(encoder["normalize_embeddings"]),
            instruction=str(encoder["instruction"]),
        ),
        execution=QwenExecutionSpec(
            device=str(execution["device"]),
            batch_size=int(execution["batch_size"]),
            parameter_dtype=str(execution["parameter_dtype"]),
            output_dtype=str(execution["output_dtype"]),
        ),
        classifier=QwenClassifierSpec(
            family=str(classifier["family"]),
            C=float(classifier["C"]),
            class_weight=str(classifier["class_weight"]),
            solver=str(classifier["solver"]),
            max_iter=int(classifier["max_iter"]),
            probability=str(classifier["probability"]),
        ),
        outer_folds=int(evaluation["outer_folds"]),
        minimum_folds=int(evaluation["minimum_folds"]),
        diagnostic_cutoff=float(evaluation["diagnostic_cutoff"]),
        confidence_band_low=float(evaluation["confidence_band_low"]),
        confidence_band_high=float(evaluation["confidence_band_high"]),
        risk_coverage_confidence_grid=tuple(
            float(value) for value in evaluation["risk_coverage_confidence_grid"]
        ),
    )
