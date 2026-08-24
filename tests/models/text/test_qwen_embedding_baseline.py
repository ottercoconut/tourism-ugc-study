"""Qwen 语义 baseline 的固定候选分组 OOF 测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tourism_ugc_study.models.text.model_acceptance import PairedOofObservation
from tourism_ugc_study.models.text.qwen_embedding_baseline import (
    QwenEmbeddingBaselineError,
    fit_qwen_embedding_baseline,
    pair_qwen_with_sparse_comparator,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.sparse_challenger import ChallengerDocument


ROOT = Path(__file__).resolve().parents[3]


def _documents() -> tuple[ChallengerDocument, ...]:
    """构造同作者组不跨折的平衡训练证据。"""

    return tuple(
        ChallengerDocument(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index // 2:03d}",
            normalized_model_text=f"合成文本{index}",
            tourism_label="unrelated" if index % 4 in {2, 3} else "related",
        )
        for index in range(80)
    )


def _embeddings(documents: tuple[ChallengerDocument, ...]) -> np.ndarray:
    """生成线性可分且逐行归一化的2560维测试向量。"""

    rng = np.random.default_rng(20260728)
    matrix = rng.normal(0.0, 0.01, size=(len(documents), 2560)).astype(np.float32)
    for index, document in enumerate(documents):
        matrix[index, 0] = 2.0 if document.tourism_label == "unrelated" else -2.0
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def test_fixed_candidate_produces_complete_group_oof() -> None:
    plan = replace(
        load_qwen_embedding_plan(
            ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
        ),
        outer_folds=4,
    )
    documents = _documents()

    result = fit_qwen_embedding_baseline(
        documents, _embeddings(documents), plan=plan
    )

    assert result.fold_count == 4
    assert len(result.oof_probabilities) == len(documents)
    assert len({row.member_key for row in result.oof_probabilities}) == len(documents)
    assert result.training_metrics["log_loss"] < 0.5
    assert result.confidence_band_diagnostics["is_routing_threshold"] is False
    predicted = result.model.predict_p_unrelated(_embeddings(documents)[:3])
    assert predicted.shape == (3,)


def test_embedding_matrix_requires_l2_normalization() -> None:
    plan = load_qwen_embedding_plan(
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
    )
    documents = _documents()
    matrix = _embeddings(documents)
    matrix[0] *= 0.5

    with pytest.raises(QwenEmbeddingBaselineError) as error:
        fit_qwen_embedding_baseline(documents, matrix, plan=plan)

    assert error.value.reason_code == "qwen_embedding_matrix_not_normalized"


def test_pairing_uses_sparse_candidate_probability_as_baseline() -> None:
    plan = replace(
        load_qwen_embedding_plan(
            ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
        ),
        outer_folds=4,
    )
    documents = _documents()
    result = fit_qwen_embedding_baseline(
        documents, _embeddings(documents), plan=plan
    )
    sparse = tuple(
        PairedOofObservation(
            member_key=row.member_key,
            component_id=row.component_id,
            tourism_label=row.tourism_label,
            baseline_p_unrelated=0.5,
            candidate_p_unrelated=0.25,
        )
        for row in result.oof_probabilities
    )

    paired = pair_qwen_with_sparse_comparator(result.oof_probabilities, sparse)

    assert all(row.baseline_p_unrelated == 0.25 for row in paired)
    assert [row.candidate_p_unrelated for row in paired] == [
        row.p_unrelated for row in result.oof_probabilities
    ]


def test_pairing_rejects_component_drift() -> None:
    plan = replace(
        load_qwen_embedding_plan(
            ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
        ),
        outer_folds=4,
    )
    documents = _documents()
    result = fit_qwen_embedding_baseline(
        documents, _embeddings(documents), plan=plan
    )
    sparse = [
        PairedOofObservation(
            member_key=row.member_key,
            component_id=row.component_id,
            tourism_label=row.tourism_label,
            baseline_p_unrelated=0.5,
            candidate_p_unrelated=0.5,
        )
        for row in result.oof_probabilities
    ]
    sparse[0] = replace(sparse[0], component_id="drift")

    with pytest.raises(QwenEmbeddingBaselineError) as error:
        pair_qwen_with_sparse_comparator(result.oof_probabilities, sparse)

    assert error.value.reason_code == "qwen_embedding_comparator_lineage_mismatch"
