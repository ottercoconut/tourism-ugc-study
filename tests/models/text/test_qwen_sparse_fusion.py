"""Qwen＋sparse 同外层 nested OOF 融合测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tourism_ugc_study.models.text.qwen_head_challenger import QwenHeadMember
from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    load_qwen_head_challenger_plan,
)
from tourism_ugc_study.models.text.qwen_sparse_fusion import (
    QwenSparseFusionError,
    blend_logit_probabilities,
    fit_qwen_sparse_fusion_nested,
)
from tourism_ugc_study.models.text.qwen_sparse_fusion_config import (
    load_qwen_sparse_fusion_plan,
)
from tourism_ugc_study.models.text.sparse_challenger import ChallengerDocument
from tourism_ugc_study.models.text.sparse_challenger_config import (
    load_sparse_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]


def _inputs():
    """构造文本、成员和含互补类别信号的归一化 embedding。"""

    documents = tuple(
        ChallengerDocument(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            normalized_model_text=(
                f"游客体验 崂山 海边 旅行 记录 {index}"
                if (index // 2) % 2 == 0
                else f"婚纱摄影 商家 优惠 预约 广告 {index}"
            ),
            tourism_label="related" if (index // 2) % 2 == 0 else "unrelated",
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
    return documents, members, matrix


def _plans():
    """缩小折数与 Qwen 候选以保持单元测试快速。"""

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
    fusion = replace(
        load_qwen_sparse_fusion_plan(
            ROOT / "configs/cleaning-qwen-sparse-fusion.yaml"
        ),
        outer_folds=3,
        inner_folds=2,
        minimum_folds=2,
    )
    sparse_plan = load_sparse_challenger_plan(
        ROOT / "configs/cleaning-text-challenger.yaml"
    )
    sparse_spec = next(
        item
        for item in sparse_plan.candidates
        if item.candidate_id == fusion.sparse_candidate_id
    )
    return head, fusion, sparse_spec


def test_logit_blend_preserves_both_audit_anchors() -> None:
    sparse = np.asarray([0.1, 0.4, 0.8])
    qwen = np.asarray([0.2, 0.7, 0.9])

    sparse_anchor = blend_logit_probabilities(
        sparse, qwen, qwen_weight=0.0, probability_clip=1e-6
    )
    qwen_anchor = blend_logit_probabilities(
        sparse, qwen, qwen_weight=1.0, probability_clip=1e-6
    )

    assert np.allclose(sparse_anchor, sparse)
    assert np.allclose(qwen_anchor, qwen)


def test_nested_fusion_produces_same_outer_fold_paired_oof() -> None:
    documents, members, embeddings = _inputs()
    head_plan, fusion_plan, sparse_spec = _plans()

    result = fit_qwen_sparse_fusion_nested(
        documents,
        members,
        embeddings,
        sparse_spec=sparse_spec,
        head_plan=head_plan,
        fusion_plan=fusion_plan,
    )

    assert result.outer_fold_count == 3
    assert result.final_inner_fold_count == 2
    assert len(result.paired_outer_oof) == len(documents)
    assert len({item.member_key for item in result.paired_outer_oof}) == len(
        documents
    )
    assert len(result.full_training_weight_scores) == 5
    assert result.selected_weight.qwen_weight in {0.0, 0.25, 0.5, 0.75, 1.0}
    assert all(item.inner_fold_count == 2 for item in result.outer_selections)
    probabilities = result.selected_model.predict_p_unrelated(
        [item.normalized_model_text for item in documents[:4]], embeddings[:4]
    )
    assert probabilities.shape == (4,)


def test_fusion_rejects_member_text_lineage_drift() -> None:
    documents, members, embeddings = _inputs()
    head_plan, fusion_plan, sparse_spec = _plans()
    drifted = list(members)
    drifted[0] = replace(drifted[0], component_id="drift")

    with pytest.raises(QwenSparseFusionError) as error:
        fit_qwen_sparse_fusion_nested(
            documents,
            drifted,
            embeddings,
            sparse_spec=sparse_spec,
            head_plan=head_plan,
            fusion_plan=fusion_plan,
        )

    assert error.value.reason_code == "qwen_sparse_fusion_lineage_mismatch"
