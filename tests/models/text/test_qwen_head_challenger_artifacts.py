"""Qwen 分类头 challenger 不可变运行包与可读输出测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

import tourism_ugc_study.models.text.qwen_head_challenger_artifacts as artifacts
from tourism_ugc_study.models.text.model_acceptance import (
    PairedOofObservation,
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_head_challenger import QwenHeadMember
from tourism_ugc_study.models.text.qwen_head_challenger_artifacts import (
    CachedQwenTrainingEvidence,
    render_qwen_head_challenger_result,
    train_qwen_head_challenger_package,
)
from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    load_qwen_head_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]


def _evidence() -> CachedQwenTrainingEvidence:
    """生成不含正文的确定性缓存向量证据。"""

    members = tuple(
        QwenHeadMember(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            tourism_label="unrelated" if (index // 2) % 2 else "related",
        )
        for index in range(60)
    )
    rng = np.random.default_rng(20260728)
    matrix = rng.normal(0.0, 0.02, size=(len(members), 2560)).astype(np.float32)
    for index, member in enumerate(members):
        matrix[index, :8] += (
            0.8 if member.tourism_label == "unrelated" else -0.8
        )
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    return CachedQwenTrainingEvidence(
        members=members,
        embeddings=matrix,
        source_manifest_sha256="a" * 64,
        source_embeddings_sha256="b" * 64,
        source_training_oof_sha256="c" * 64,
    )


def test_package_is_immutable_reusable_and_keeps_all_gates_locked(
    tmp_path: Path, monkeypatch
) -> None:
    plan = load_qwen_head_challenger_plan(
        ROOT / "configs/cleaning-qwen-head-challenger.yaml"
    )
    anchor = next(
        item
        for item in plan.candidates
        if item.candidate_id == plan.safety_anchor_candidate_id
    )
    compact = next(
        item
        for item in plan.candidates
        if item.family == "logistic_regression"
        and item.embedding_dimension == 256
        and item.C == 1.0
        and item.class_weight is None
    )
    plan = replace(
        plan,
        candidates=(anchor, compact),
        outer_folds=3,
        inner_folds=2,
        minimum_folds=2,
    )
    base_plan = load_qwen_embedding_plan(
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
    )
    policy = replace(
        load_model_acceptance_policy(
            ROOT / "configs/cleaning-qwen-model-acceptance.yaml"
        ),
        bootstrap_repetitions=100,
    )
    evidence = _evidence()
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
        for item in evidence.members
    )
    monkeypatch.setattr(
        artifacts, "load_qwen_head_challenger_plan", lambda _path: plan
    )
    monkeypatch.setattr(
        artifacts, "load_qwen_embedding_plan", lambda _path: base_plan
    )
    monkeypatch.setattr(
        artifacts, "load_model_acceptance_policy", lambda _path: policy
    )
    monkeypatch.setattr(
        artifacts,
        "load_cached_qwen_training_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    monkeypatch.setattr(
        artifacts,
        "load_sparse_comparator_oof",
        lambda *_args, **_kwargs: comparator,
    )
    artifact_root = tmp_path / "artifacts"
    arguments = (
        "source-package",
        "comparator-package",
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml",
        ROOT / "configs/cleaning-qwen-head-challenger.yaml",
        ROOT / "configs/cleaning-qwen-model-acceptance.yaml",
        artifact_root,
    )

    first = train_qwen_head_challenger_package(
        *arguments, code_version="d" * 40
    )
    second = train_qwen_head_challenger_package(
        *arguments,
        code_version="d" * 40,
        expected_existing_manifest_sha256=first.package_manifest_sha256,
    )

    assert first.reused is False
    assert second.reused is True
    assert first.run_id == second.run_id
    assert first.candidate_count == 2
    assert first.acceptance_status == "passed"
    assert first.validation_status == "pending_directional_check"
    assert first.test_status == "locked_not_opened"
    assert first.threshold_status == "UNSET"
    assert first.audit_status == "UNSET"
    package = artifact_root / first.run_id
    assert (package / "model.joblib").is_file()
    assert (package / "paired-oof.json").is_file()
    assert not (package / "train-embeddings.npz").exists()
    rendered = render_qwen_head_challenger_result(first)
    assert "分类头 challenger" in rendered
    assert "锁定测试：locked_not_opened" in rendered
    assert "路由阈值：UNSET" in rendered
