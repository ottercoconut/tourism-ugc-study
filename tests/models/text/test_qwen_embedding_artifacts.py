"""Qwen 语义 baseline 不可变运行包与可读输出测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import tourism_ugc_study.models.text.qwen_embedding_artifacts as artifacts
from tourism_ugc_study.models.text.model_acceptance import (
    PairedOofObservation,
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.qwen_embedding_artifacts import (
    QwenEmbeddingArtifactError,
    QwenEmbeddingEvidenceBundle,
    load_frozen_qwen_embedding_classifier,
    render_qwen_embedding_result,
    train_qwen_embedding_package,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    QwenEncodingDiagnostics,
    QwenEncodingResult,
    QwenExecutionReceipt,
    QwenModelSnapshot,
)
from tourism_ugc_study.models.text.sparse_challenger import ChallengerDocument


ROOT = Path(__file__).resolve().parents[3]


def _documents() -> tuple[ChallengerDocument, ...]:
    """构造固定候选四折训练证据。"""

    return tuple(
        ChallengerDocument(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            normalized_model_text=f"合成文本{index}",
            tourism_label="unrelated" if index % 4 in {2, 3} else "related",
        )
        for index in range(80)
    )


class _FakeEncoder:
    """不加载公开模型的确定性测试编码器。"""

    def __init__(self, receipt: QwenExecutionReceipt) -> None:
        self.execution_receipt = receipt

    def encode_with_diagnostics(self, texts, *, show_progress=False):
        rng = np.random.default_rng(20260728)
        matrix = rng.normal(0.0, 0.01, size=(len(texts), 1024)).astype(np.float32)
        for index in range(len(texts)):
            matrix[index, 0] = 2.0 if index % 4 in {2, 3} else -2.0
        embeddings = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        return QwenEncodingResult(
            embeddings=embeddings,
            diagnostics=QwenEncodingDiagnostics(
                count=len(texts),
                truncated_count=0,
                truncated_rate=0.0,
                token_count_max=12,
                token_count_p95=12.0,
                max_length=2048,
            ),
        )


def _patch_inputs(monkeypatch: pytest.MonkeyPatch):
    """替换私有证据与权重读取，只测试持久化边界。"""

    plan = replace(
        load_qwen_embedding_plan(
            ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
        ),
        outer_folds=4,
    )
    policy = replace(
        load_model_acceptance_policy(
            ROOT / "configs/cleaning-qwen-model-acceptance.yaml"
        ),
        bootstrap_repetitions=100,
    )
    documents = _documents()
    evidence = QwenEmbeddingEvidenceBundle(
        documents=documents,
        reference_manifest_sha256="a" * 64,
        split_manifest_sha256="b" * 64,
        split_anchor_package_manifest_sha256="c" * 64,
        train_count=len(documents),
        validation_count=10,
        test_count=10,
    )
    comparator = tuple(
        PairedOofObservation(
            member_key=item.member_key,
            component_id=item.component_id,
            tourism_label=item.tourism_label,
            baseline_p_unrelated=0.5,
            candidate_p_unrelated=(
                0.70 if item.tourism_label == "unrelated" else 0.30
            ),
        )
        for item in documents
    )
    snapshot = QwenModelSnapshot(
        repository=plan.encoder.repository,
        revision=plan.encoder.revision,
        weights_sha256=plan.encoder.weights_sha256,
        snapshot_sha256=plan.encoder.snapshot_sha256,
        embedding_dimension=1024,
        max_length=2048,
        reused=True,
    )
    receipt = QwenExecutionReceipt(
        device="mps",
        batch_size=4,
        parameter_dtype="bfloat16",
        output_dtype="float32",
        python_version="3.13.5",
        operating_system="Darwin",
        machine="arm64",
        hardware_model="Apple M5",
    )
    monkeypatch.setattr(artifacts, "load_qwen_embedding_plan", lambda _path: plan)
    monkeypatch.setattr(
        artifacts, "load_model_acceptance_policy", lambda _path: policy
    )
    monkeypatch.setattr(
        artifacts, "validate_qwen_model_directory", lambda *_args, **_kwargs: snapshot
    )
    monkeypatch.setattr(
        artifacts, "load_qwen_embedding_evidence", lambda *_args, **_kwargs: evidence
    )
    monkeypatch.setattr(
        artifacts, "load_sparse_comparator_oof", lambda *_args, **_kwargs: comparator
    )
    monkeypatch.setattr(
        artifacts,
        "LocalQwenEmbeddingEncoder",
        lambda *_args, **_kwargs: _FakeEncoder(receipt),
    )
    return plan, snapshot, receipt


def _arguments(tmp_path: Path) -> tuple:
    """构造训练包入口的路径参数。"""

    return (
        "reference.csv",
        "reference.manifest.json",
        "derived.sqlite",
        "split-anchor",
        "sparse-plan.yaml",
        "comparator",
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml",
        ROOT / "configs/cleaning-qwen-model-acceptance.yaml",
        "model-dir",
        tmp_path / "artifacts",
    )


def test_package_is_immutable_reusable_and_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, snapshot, receipt = _patch_inputs(monkeypatch)
    arguments = _arguments(tmp_path)

    first = train_qwen_embedding_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
        show_progress=False,
    )
    second = train_qwen_embedding_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
        show_progress=False,
    )

    assert first.reused is False
    assert second.reused is True
    assert first.run_id == second.run_id
    assert first.acceptance_status == "passed"
    assert first.validation_status == "pending_directional_check"
    assert first.test_status == "locked_not_opened"
    assert first.threshold_status == "UNSET"
    assert first.audit_status == "UNSET"
    package = tmp_path / "artifacts" / first.run_id
    assert (package / "train-embeddings.npz").is_file()
    assert (package / "paired-oof.json").is_file()
    model = load_frozen_qwen_embedding_classifier(
        package,
        expected_manifest_sha256=first.package_manifest_sha256,
        expected_model_id=first.model_id,
        plan=plan,
        snapshot=snapshot,
        execution=receipt,
    )
    assert model.embedding_dimension == 1024
    human = render_qwen_embedding_result(first)
    assert "Qwen3-Embedding 语义 baseline" in human
    assert "不是路由阈值" in human
    assert "审计策略：UNSET" in human
    assert "自动清洗决定：未生成" in human


def test_existing_package_rejects_classifier_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_inputs(monkeypatch)
    arguments = _arguments(tmp_path)
    result = train_qwen_embedding_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
        show_progress=False,
    )
    path = tmp_path / "artifacts" / result.run_id / "classifier.joblib"
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(QwenEmbeddingArtifactError) as error:
        train_qwen_embedding_package(
            *arguments,
            config=object(),
            normalization_config=object(),
            code_version="d" * 40,
            show_progress=False,
        )

    assert (
        error.value.reason_code
        == "qwen_embedding_package_artifact_hash_mismatch"
    )
