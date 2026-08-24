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
    for index, (filename, _digest) in enumerate(plan.encoder.weight_files):
        (directory / filename).write_bytes(f"fixed-test-weights:{index}".encode())
    (directory / "config.json").write_text(
        json.dumps({"model_type": "qwen3", "hidden_size": 2560}),
        encoding="utf-8",
    )
    (directory / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "weight_map": {
                    "layer.0": plan.encoder.weight_files[0][0],
                    "layer.1": plan.encoder.weight_files[1][0],
                }
            }
        ),
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
    weight_files = {
        filename: hashes[filename]
        for filename, _digest in plan.encoder.weight_files
    }
    weights_sha256 = hashlib.sha256(
        json.dumps(
            weight_files, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    encoder = replace(
        plan.encoder,
        weights_sha256=weights_sha256,
        weight_files=tuple(sorted(weight_files.items())),
        snapshot_sha256=snapshot_sha256,
        snapshot_files=tuple(sorted(hashes.items())),
    )
    return replace(plan, encoder=encoder)


def test_model_directory_binds_config_and_weight_hash(tmp_path: Path) -> None:
    directory, hashes = _fake_snapshot(tmp_path)
    plan = _plan_with_snapshot(hashes)

    snapshot = validate_qwen_model_directory(directory, plan=plan)

    assert snapshot.weights_sha256 == plan.encoder.weights_sha256
    assert snapshot.embedding_dimension == 2560
    assert snapshot.reused is True


def test_model_directory_rejects_tampered_weight(tmp_path: Path) -> None:
    directory, hashes = _fake_snapshot(tmp_path)
    plan = _plan_with_snapshot(hashes)
    first_weight = plan.encoder.weight_files[0][0]
    (directory / first_weight).write_bytes(b"tampered")

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_model_snapshot_hash_mismatch"


def test_model_directory_rejects_wrong_architecture(tmp_path: Path) -> None:
    directory, hashes = _fake_snapshot(tmp_path)
    plan = _plan_with_snapshot(hashes)
    (directory / "config.json").write_text(
        json.dumps({"model_type": "bert", "hidden_size": 2560}),
        encoding="utf-8",
    )

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_model_snapshot_hash_mismatch"


def test_model_directory_rejects_weight_index_outside_frozen_shards(
    tmp_path: Path,
) -> None:
    """即使摘要同步更新，索引也不得引用预登记以外的权重文件。"""

    directory, _hashes = _fake_snapshot(tmp_path)
    (directory / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"layer.0": "unregistered.safetensors"}}),
        encoding="utf-8",
    )
    hashes = {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }
    plan = _plan_with_snapshot(hashes)

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_model_config_invalid"


def test_model_directory_rejects_non_mapping_config_with_stable_reason(
    tmp_path: Path,
) -> None:
    """摘要匹配但配置顶层非法时仍返回稳定失败码。"""

    directory, _hashes = _fake_snapshot(tmp_path)
    (directory / "config.json").write_text("[]\n", encoding="utf-8")
    hashes = {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }
    plan = _plan_with_snapshot(hashes)

    with pytest.raises(QwenEmbeddingRuntimeError) as error:
        validate_qwen_model_directory(directory, plan=plan)

    assert error.value.reason_code == "qwen_embedding_model_config_invalid"
