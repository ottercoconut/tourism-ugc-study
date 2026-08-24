"""Qwen 缓存向量分类头 nested OOF 与配对测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tourism_ugc_study.models.text.model_acceptance import PairedOofObservation
from tourism_ugc_study.models.text.qwen_head_challenger import (
    QwenHeadChallengerError,
    QwenHeadMember,
    fit_qwen_head_challenger_nested,
)
from tourism_ugc_study.models.text.qwen_head_challenger_artifacts import (
    QwenHeadChallengerArtifactError,
    pair_qwen_head_with_sparse_comparator,
)
from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    load_qwen_head_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]


def _members() -> tuple[QwenHeadMember, ...]:
    """构造每个 leakage component 内标签一致的平衡成员。"""

    return tuple(
        QwenHeadMember(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            tourism_label="unrelated" if (index // 2) % 2 else "related",
        )
        for index in range(60)
    )


def _embeddings(members: tuple[QwenHeadMember, ...]) -> np.ndarray:
    """生成前缀含稳定类别信号的归一化2560维测试向量。"""

    rng = np.random.default_rng(20260728)
    matrix = rng.normal(0.0, 0.02, size=(len(members), 2560)).astype(np.float32)
    for index, member in enumerate(members):
        matrix[index, :8] += (
            0.8 if member.tourism_label == "unrelated" else -0.8
        )
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def _small_plan():
    """缩小候选与折数以保持单元测试快速。"""

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
    return replace(
        plan,
        candidates=(anchor, compact),
        outer_folds=3,
        inner_folds=2,
        minimum_folds=2,
    )


def test_nested_head_search_produces_complete_oof_and_frozen_model() -> None:
    members = _members()
    embeddings = _embeddings(members)

    result = fit_qwen_head_challenger_nested(
        members, embeddings, plan=_small_plan()
    )

    assert result.outer_fold_count == 3
    assert result.final_inner_fold_count == 2
    assert len(result.oof_probabilities) == len(members)
    assert len({item.member_key for item in result.oof_probabilities}) == len(
        members
    )
    assert len(result.full_training_candidate_scores) == 2
    assert result.training_metrics["log_loss"] < 0.5
    probabilities = result.selected_model.predict_p_unrelated(embeddings[:4])
    assert probabilities.shape == (4,)
    assert np.isfinite(probabilities).all()


def test_nested_search_rejects_non_normalized_embedding() -> None:
    members = _members()
    embeddings = _embeddings(members)
    embeddings[0] *= 0.5

    with pytest.raises(QwenHeadChallengerError) as error:
        fit_qwen_head_challenger_nested(
            members, embeddings, plan=_small_plan()
        )

    assert error.value.reason_code == "qwen_head_embedding_matrix_not_normalized"


def test_pairing_uses_sparse_candidate_and_rejects_lineage_drift() -> None:
    members = _members()
    result = fit_qwen_head_challenger_nested(
        members, _embeddings(members), plan=_small_plan()
    )
    sparse = [
        PairedOofObservation(
            member_key=item.member_key,
            component_id=item.component_id,
            tourism_label=item.tourism_label,
            baseline_p_unrelated=0.5,
            candidate_p_unrelated=0.25,
        )
        for item in result.oof_probabilities
    ]

    paired = pair_qwen_head_with_sparse_comparator(
        result.oof_probabilities, sparse
    )

    assert all(item.baseline_p_unrelated == 0.25 for item in paired)
    sparse[0] = replace(sparse[0], component_id="drift")
    with pytest.raises(QwenHeadChallengerArtifactError) as error:
        pair_qwen_head_with_sparse_comparator(
            result.oof_probabilities, sparse
        )
    assert error.value.reason_code == "qwen_head_comparator_lineage_mismatch"
