"""Qwen 本地模型快照与编码输出契约测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingPlan,
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    QwenEmbeddingRuntimeError,
    validate_qwen_model_directory,
)


ROOT = Path(__file__).resolve().parents[3]


def _fake_snapshot(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """创建不含真实权重的小型合法目录。"""

    directory = tmp_path / "model"
    directory.mkdir()
    plan = load_qwen_embedding_plan(
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
    )
    for filename, _digest in plan.encoder.snapshot_files:
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"test:{filename}".encode())
    (directory / "model.safetensors").write_bytes(b"fixed-test-weights")
    (directory / "config.json").write_text(
        json.dumps({"model_type": "qwen3", "hidden_size": 1024}),
        encoding="utf-8",
    )
    (directory / "modules.json").write_text(
        json.dumps(
            [
                {"type": "sentence_transformers.models.Transformer"},
                {"type": "sentence_transformers.models.Pooling"},
                {"type": "sentence_transformers.models.Normalize"},
            ]
        ),
        encoding="utf-8",
    )
    (directory / "1_Pooling/config.json").write_text(
        json.dumps(
            {
                "pooling_mode_lasttoken": True,
                "pooling_mode_cls_token": False,
                "pooling_mode_mean_tokens": False,
                "pooling_mode_max_tokens": False,
                "pooling_mode_mean_sqrt_len_tokens": False,
                "pooling_mode_weightedmean_tokens": False,
            }
        ),
        encoding="utf-8",
    )
    hashes = {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }
    return directory, hashes


def _plan_with_snapshot(hashes: dict[str, str]) -> QwenEmbeddingPlan:
    """仅替换测试快照摘要，保持其余预登记值。"""

    plan = load_qwen_embedding_plan(
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
    )
    snapshot_sha256 = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    encoder = replace(
        plan.encoder,
        weights_sha256=hashes["model.safetensors"],
        snapshot_sha256=snapshot_sha256,
        snapshot_files=tuple(sorted(hashes.items())),
    )
    return replace(plan, encoder=encoder)


def test_model_directory_binds_config_and_weight_hash(tmp_path: Path) -> None:
    directory, hashes = _fake_snapshot(tmp_path)
    plan = _plan_with_snapshot(hashes)

    snapshot = validate_qwen_model_directory(directory, plan=plan)

    assert snapshot.weights_sha256 == hashes["model.safetensors"]
    assert snapshot.embedding_dimension == 1024
    assert snapshot.reused is True


def test_model_directory_rejects_tampered_weight(tmp_path: Path) -> None:
    directory, hashes = _fake_snapshot(tmp_path)
    plan = _plan_with_snapshot(hashes)
    (directory / "model.safetensors").write_bytes(b"tampered")

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_model_snapshot_hash_mismatch"


def test_model_directory_rejects_wrong_architecture(tmp_path: Path) -> None:
    directory, hashes = _fake_snapshot(tmp_path)
    plan = _plan_with_snapshot(hashes)
    (directory / "config.json").write_text(
        json.dumps({"model_type": "bert", "hidden_size": 1024}),
        encoding="utf-8",
    )

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_model_snapshot_hash_mismatch"
