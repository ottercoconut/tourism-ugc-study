"""Qwen 本地模型快照与编码输出契约测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingPlan,
    QwenEncoderSpec,
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    QwenEmbeddingRuntimeError,
    validate_qwen_model_directory,
)


ROOT = Path(__file__).resolve().parents[3]


def _fake_snapshot(tmp_path: Path) -> tuple[Path, str]:
    """创建不含真实权重的小型合法目录。"""

    directory = tmp_path / "model"
    directory.mkdir()
    weights = b"fixed-test-weights"
    (directory / "model.safetensors").write_bytes(weights)
    (directory / "config.json").write_text(
        json.dumps({"model_type": "qwen3", "hidden_size": 1024}),
        encoding="utf-8",
    )
    for filename in ("modules.json", "tokenizer.json", "tokenizer_config.json"):
        (directory / filename).write_text("{}\n", encoding="utf-8")
    return directory, hashlib.sha256(weights).hexdigest()


def _plan_with_weight_hash(weight_hash: str) -> QwenEmbeddingPlan:
    """仅替换测试快照摘要，保持其余预登记值。"""

    plan = load_qwen_embedding_plan(
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
    )
    encoder = QwenEncoderSpec(
        repository=plan.encoder.repository,
        revision=plan.encoder.revision,
        weights_filename=plan.encoder.weights_filename,
        weights_sha256=weight_hash,
        license=plan.encoder.license,
        embedding_dimension=plan.encoder.embedding_dimension,
        max_length=plan.encoder.max_length,
        pooling=plan.encoder.pooling,
        normalize_embeddings=plan.encoder.normalize_embeddings,
        instruction=plan.encoder.instruction,
    )
    return QwenEmbeddingPlan(**{**plan.__dict__, "encoder": encoder})


def test_model_directory_binds_config_and_weight_hash(tmp_path: Path) -> None:
    directory, weight_hash = _fake_snapshot(tmp_path)
    plan = _plan_with_weight_hash(weight_hash)

    snapshot = validate_qwen_model_directory(directory, plan=plan)

    assert snapshot.weights_sha256 == weight_hash
    assert snapshot.embedding_dimension == 1024
    assert snapshot.reused is True


def test_model_directory_rejects_tampered_weight(tmp_path: Path) -> None:
    directory, weight_hash = _fake_snapshot(tmp_path)
    plan = _plan_with_weight_hash(weight_hash)
    (directory / "model.safetensors").write_bytes(b"tampered")

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_weights_hash_mismatch"


def test_model_directory_rejects_wrong_architecture(tmp_path: Path) -> None:
    directory, weight_hash = _fake_snapshot(tmp_path)
    plan = _plan_with_weight_hash(weight_hash)
    (directory / "config.json").write_text(
        json.dumps({"model_type": "bert", "hidden_size": 1024}),
        encoding="utf-8",
    )

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_model_config_invalid"
