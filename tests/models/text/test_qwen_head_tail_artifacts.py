"""英文 head-tail 长度分组和不可变运行包测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

import tourism_ugc_study.models.text.qwen_head_tail_artifacts as artifacts
from tourism_ugc_study.models.text.model_acceptance import (
    PairedOofObservation,
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.qwen_embedding_artifacts import (
    QwenEmbeddingEvidenceBundle,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    QwenExecutionReceipt,
    QwenHeadTailEncodingDiagnostics,
    QwenHeadTailEncodingResult,
    QwenModelSnapshot,
)
from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    load_qwen_head_challenger_plan,
)
from tourism_ugc_study.models.text.qwen_head_tail_artifacts import (
    length_subgroup_diagnostics,
    render_qwen_head_tail_result,
    train_qwen_head_tail_package,
)
from tourism_ugc_study.models.text.qwen_head_tail_config import (
    load_qwen_head_tail_plan,
)
from tourism_ugc_study.models.text.sparse_challenger import ChallengerDocument


ROOT = Path(__file__).resolve().parents[3]


def _documents() -> tuple[ChallengerDocument, ...]:
    """构造同组标签一致的平衡训练成员。"""

    return tuple(
        ChallengerDocument(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            normalized_model_text=f"合成文本 {index}",
            tourism_label="unrelated" if (index // 2) % 2 else "related",
        )
        for index in range(60)
    )


class _FakeEncoder:
    """不加载公开权重的确定性英文双视图测试编码器。"""

    def __init__(self, receipt: QwenExecutionReceipt) -> None:
        self.execution_receipt = receipt

    def encode_head_tail_with_diagnostics(self, texts, *, show_progress=False):
        documents = _documents()
        rng = np.random.default_rng(20260728)
        matrix = rng.normal(0.0, 0.02, size=(len(texts), 2560)).astype(np.float32)
        for index, item in enumerate(documents):
            matrix[index, :8] += (
                0.8 if item.tourism_label == "unrelated" else -0.8
            )
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        over = np.asarray([index % 5 == 0 for index in range(len(texts))])
        middle = np.asarray([index % 10 == 0 for index in range(len(texts))])
        return QwenHeadTailEncodingResult(
            embeddings=matrix,
            original_over_limit_mask=over,
            middle_omitted_mask=middle,
            diagnostics=QwenHeadTailEncodingDiagnostics(
                count=len(texts),
                original_over_limit_count=int(np.sum(over)),
                original_over_limit_rate=float(np.mean(over)),
                encoded_view_count=len(texts) + int(np.sum(over)),
                two_view_count=int(np.sum(over)),
                middle_omitted_count=int(np.sum(middle)),
                middle_omitted_rate=float(np.mean(middle)),
                middle_omitted_token_count_total=100,
                original_token_count_max=5000,
                original_token_count_p95=2200.0,
                content_window_token_budget=2000,
                boundary_adjusted_view_count=3,
                retained_content_token_count_total=10000,
                max_length=2048,
                encoded_view_over_limit_count=0,
            ),
        )


def test_length_subgroups_are_aggregate_and_keep_routing_unset() -> None:
    labels = ["related", "unrelated"] * 10
    probabilities = [0.1, 0.9] * 10
    over = np.asarray([index >= 10 for index in range(20)])
    middle = np.asarray([index >= 16 for index in range(20)])

    result = length_subgroup_diagnostics(
        labels,
        probabilities,
        over_limit_mask=over,
        middle_omitted_mask=middle,
    )

    assert result["within_single_view"]["count"] == 10
    assert result["head_tail_overflow"]["count"] == 10
    assert result["middle_omitted"]["count"] == 4
    assert result["subgroups_are_population_estimates"] is False
    assert result["diagnostic_cutoff_is_routing_threshold"] is False


def test_head_tail_package_is_immutable_reusable_and_locked(
    tmp_path: Path, monkeypatch
) -> None:
    projection = load_qwen_head_tail_plan(
        ROOT / "configs/cleaning-qwen-english-head-tail.yaml"
    )
    base = load_qwen_embedding_plan(
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
    )
    head = load_qwen_head_challenger_plan(
        ROOT / "configs/cleaning-qwen-head-challenger.yaml"
    )
    anchor = next(
        item
        for item in head.candidates
        if item.candidate_id == head.safety_anchor_candidate_id
    )
    compact = next(
        item
        for item in head.candidates
        if item.family == "logistic_regression"
        and item.embedding_dimension == 256
        and item.C == 1.0
        and item.class_weight is None
    )
    head = replace(
        head,
        candidates=(anchor, compact),
        outer_folds=3,
        inner_folds=2,
        minimum_folds=2,
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
                0.65 if item.tourism_label == "unrelated" else 0.35
            ),
        )
        for item in documents
    )
    snapshot = QwenModelSnapshot(
        repository=base.encoder.repository,
        revision=base.encoder.revision,
        weights_sha256=base.encoder.weights_sha256,
        snapshot_sha256=base.encoder.snapshot_sha256,
        embedding_dimension=2560,
        max_length=2048,
        reused=True,
    )
    receipt = QwenExecutionReceipt(
        device="mps",
        batch_size=1,
        parameter_dtype="bfloat16",
        output_dtype="float32",
        python_version="3.13.5",
        operating_system="Darwin",
        operating_system_release="25.5.0",
        macos_version="26.5.2",
        machine="arm64",
        hardware_model="Apple M5",
    )
    monkeypatch.setattr(artifacts, "load_qwen_head_tail_plan", lambda _path: projection)
    monkeypatch.setattr(artifacts, "load_qwen_embedding_plan", lambda _path: base)
    monkeypatch.setattr(artifacts, "load_qwen_head_challenger_plan", lambda _path: head)
    monkeypatch.setattr(artifacts, "load_model_acceptance_policy", lambda _path: policy)
    monkeypatch.setattr(
        artifacts, "_validate_layer2_failure_package", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        artifacts, "validate_qwen_model_directory", lambda *_args, **_kwargs: snapshot
    )
    monkeypatch.setattr(
        artifacts,
        "LocalQwenHeadTailEncoder",
        lambda *_args, **_kwargs: _FakeEncoder(receipt),
    )
    monkeypatch.setattr(
        artifacts, "load_qwen_embedding_evidence", lambda *_args, **_kwargs: evidence
    )
    monkeypatch.setattr(
        artifacts, "load_sparse_comparator_oof", lambda *_args, **_kwargs: comparator
    )
    artifact_root = tmp_path / "artifacts"
    arguments = (
        "reference.csv",
        "reference.manifest.json",
        "derived.sqlite",
        "split-anchor",
        ROOT / "configs/cleaning-text-challenger.yaml",
        "sparse-package",
        "layer2-package",
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml",
        ROOT / "configs/cleaning-qwen-head-challenger.yaml",
        ROOT / "configs/cleaning-qwen-english-head-tail.yaml",
        ROOT / "configs/cleaning-qwen-model-acceptance.yaml",
        "model-dir",
        artifact_root,
    )

    first = train_qwen_head_tail_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
        show_progress=False,
    )
    second = train_qwen_head_tail_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
        expected_existing_manifest_sha256=first.package_manifest_sha256,
        show_progress=False,
    )

    assert first.reused is False
    assert second.reused is True
    assert first.run_id == second.run_id
    assert first.test_status == "locked_not_opened"
    assert first.threshold_status == "UNSET"
    assert first.audit_status == "UNSET"
    assert first.encoding_diagnostics["encoded_view_over_limit_count"] == 0
    assert first.encoding_diagnostics["boundary_adjusted_view_count"] == 3
    package = artifact_root / first.run_id
    assert (package / "train-embeddings.npz").is_file()
    assert (package / "paired-oof.json").is_file()
    rendered = render_qwen_head_tail_result(first)
    assert "英文 head-tail" in rendered
    assert "不声称覆盖" in rendered
    assert "锁定测试：locked_not_opened" in rendered
