"""Qwen＋sparse 融合不可变运行包与可读输出测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

import tourism_ugc_study.models.text.qwen_sparse_fusion_artifacts as artifacts
from tourism_ugc_study.models.text.model_acceptance import (
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.qwen_head_challenger import QwenHeadMember
from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    load_qwen_head_challenger_plan,
)
from tourism_ugc_study.models.text.qwen_sparse_fusion_artifacts import (
    QwenSparseFusionEvidence,
    render_qwen_sparse_fusion_result,
    train_qwen_sparse_fusion_package,
)
from tourism_ugc_study.models.text.qwen_sparse_fusion_config import (
    load_qwen_sparse_fusion_plan,
)
from tourism_ugc_study.models.text.sparse_challenger import ChallengerDocument
from tourism_ugc_study.models.text.sparse_challenger_config import (
    load_sparse_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]


def _evidence() -> QwenSparseFusionEvidence:
    """构造 sparse 较弱、Qwen 向量可分的同序训练证据。"""

    documents = tuple(
        ChallengerDocument(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            normalized_model_text=f"中性合成文本 编号 {index}",
            tourism_label="unrelated" if (index // 2) % 2 else "related",
        )
        for index in range(60)
    )
    members = tuple(
        QwenHeadMember(
            member_key=item.member_key,
            component_id=item.component_id,
            tourism_label=item.tourism_label,
        )
        for item in documents
    )
    rng = np.random.default_rng(20260728)
    matrix = rng.normal(0.0, 0.02, size=(len(documents), 2560)).astype(np.float32)
    for index, item in enumerate(documents):
        matrix[index, :8] += (
            0.8 if item.tourism_label == "unrelated" else -0.8
        )
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    return QwenSparseFusionEvidence(
        documents=documents,
        members=members,
        embeddings=matrix,
        reference_manifest_sha256="a" * 64,
        split_manifest_sha256="b" * 64,
        split_anchor_package_manifest_sha256="c" * 64,
    )


def test_fusion_package_is_immutable_reusable_and_locked(
    tmp_path: Path, monkeypatch
) -> None:
    fusion_plan = replace(
        load_qwen_sparse_fusion_plan(
            ROOT / "configs/cleaning-qwen-sparse-fusion.yaml"
        ),
        outer_folds=3,
        inner_folds=2,
        minimum_folds=2,
    )
    head_plan = load_qwen_head_challenger_plan(
        ROOT / "configs/cleaning-qwen-head-challenger.yaml"
    )
    anchor = next(
        item
        for item in head_plan.candidates
        if item.candidate_id == head_plan.safety_anchor_candidate_id
    )
    compact = next(
        item
        for item in head_plan.candidates
        if item.family == "logistic_regression"
        and item.embedding_dimension == 256
        and item.C == 1.0
        and item.class_weight is None
    )
    head_plan = replace(
        head_plan,
        candidates=(anchor, compact),
        outer_folds=3,
        inner_folds=2,
        minimum_folds=2,
    )
    policy = replace(
        load_model_acceptance_policy(
            ROOT / "configs/cleaning-qwen-fusion-model-acceptance.yaml"
        ),
        bootstrap_repetitions=100,
    )
    sparse_plan = load_sparse_challenger_plan(
        ROOT / "configs/cleaning-text-challenger.yaml"
    )
    sparse_spec = next(
        item
        for item in sparse_plan.candidates
        if item.candidate_id == fusion_plan.sparse_candidate_id
    )
    evidence = _evidence()
    monkeypatch.setattr(
        artifacts, "load_qwen_sparse_fusion_plan", lambda _path: fusion_plan
    )
    monkeypatch.setattr(
        artifacts, "load_qwen_head_challenger_plan", lambda _path: head_plan
    )
    monkeypatch.setattr(
        artifacts, "load_model_acceptance_policy", lambda _path: policy
    )
    monkeypatch.setattr(
        artifacts, "_validate_head_failure_package", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        artifacts, "_load_sparse_spec", lambda *_args, **_kwargs: sparse_spec
    )
    monkeypatch.setattr(
        artifacts,
        "load_qwen_sparse_fusion_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    artifact_root = tmp_path / "artifacts"
    arguments = (
        "reference.csv",
        "reference.manifest.json",
        "derived.sqlite",
        "split-anchor",
        ROOT / "configs/cleaning-text-challenger.yaml",
        "sparse-package",
        "qwen-source-package",
        "head-package",
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml",
        ROOT / "configs/cleaning-qwen-head-challenger.yaml",
        ROOT / "configs/cleaning-qwen-sparse-fusion.yaml",
        ROOT / "configs/cleaning-qwen-fusion-model-acceptance.yaml",
        artifact_root,
    )

    first = train_qwen_sparse_fusion_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
    )
    second = train_qwen_sparse_fusion_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
        expected_existing_manifest_sha256=first.package_manifest_sha256,
    )

    assert first.reused is False
    assert second.reused is True
    assert first.run_id == second.run_id
    assert first.test_status == "locked_not_opened"
    assert first.threshold_status == "UNSET"
    assert first.audit_status == "UNSET"
    package = artifact_root / first.run_id
    assert (package / "model.joblib").is_file()
    assert (package / "paired-oof.json").is_file()
    assert not (package / "train-embeddings.npz").exists()
    rendered = render_qwen_sparse_fusion_result(first)
    assert "无泄漏融合" in rendered
    assert "同外层 paired nested group OOF" in rendered
    assert "锁定测试：locked_not_opened" in rendered
