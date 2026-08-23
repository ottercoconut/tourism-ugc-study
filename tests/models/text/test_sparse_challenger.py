"""稀疏文本 challenger 的 NB 比率与嵌套分组比较测试。"""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import numpy as np
import pytest
from scipy import sparse

from tourism_ugc_study.models.text.sparse_challenger import (
    ChallengerDocument,
    NBLogCountRatioTransformer,
    SparseChallengerError,
    fit_sparse_challenger_nested,
)
from tourism_ugc_study.models.text.sparse_challenger_config import (
    load_sparse_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]


def _documents() -> tuple[ChallengerDocument, ...]:
    """构造平台不可见、分量唯一的平衡训练样本。"""

    values: list[ChallengerDocument] = []
    for index in range(60):
        unrelated = index % 2 == 1
        values.append(
            ChallengerDocument(
                member_key=f"member-{index:03d}",
                component_id=f"component-{index:03d}",
                normalized_model_text=(
                    f"[TITLE]\n招聘推广{index}\n[BODY]\n商家套餐房产广告联系方式{index}"
                    if unrelated
                    else f"[TITLE]\n青岛旅行{index}\n[BODY]\n游客海边景点美食路线体验{index}"
                ),
                tourism_label="unrelated" if unrelated else "related",
            )
        )
    return tuple(values)


def _small_plan():
    """从冻结矩阵各取一个模型，降低单元测试拟合成本。"""

    plan = load_sparse_challenger_plan(
        ROOT / "configs/cleaning-text-challenger.yaml"
    )
    candidates = tuple(
        next(candidate for candidate in plan.candidates if candidate.family == family)
        for family in (
            "tfidf_linear_svc",
            "nbsvm",
            "tfidf_logistic_regression",
        )
    )
    return replace(plan, outer_folds=3, inner_folds=2, candidates=candidates)


def test_nb_log_count_ratio_is_fit_only_from_supplied_rows() -> None:
    X = sparse.csr_matrix(
        [
            [1.0, 0.0, 1.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 1.0],
            [0.0, 0.0, 1.0],
        ]
    )
    labels = np.asarray(["unrelated", "unrelated", "related", "related"])
    transformer = NBLogCountRatioTransformer(alpha=1.0).fit(X, labels)
    expected_unrelated = np.asarray([3.0, 2.0, 2.0]) / 7.0
    expected_related = np.asarray([1.0, 2.0, 3.0]) / 6.0

    assert np.allclose(
        transformer.log_count_ratio_,
        np.log(expected_unrelated / expected_related),
    )
    transformed = transformer.transform(X)
    assert sparse.issparse(transformed)
    assert np.allclose(
        transformed.toarray(), X.toarray() * transformer.log_count_ratio_
    )


def test_nested_comparison_produces_complete_paired_outer_oof() -> None:
    documents = _documents()
    result = fit_sparse_challenger_nested(documents, plan=_small_plan())

    assert len(result.paired_outer_oof) == len(documents)
    assert len({row.member_key for row in result.paired_outer_oof}) == len(documents)
    assert result.outer_fold_count == 3
    assert len(result.outer_selections) == 3
    assert len(result.full_training_candidate_scores) == 3
    assert result.selected_spec.family in {
        "baseline_anchor",
        "tfidf_linear_svc",
        "nbsvm",
        "tfidf_logistic_regression",
    }
    assert all(
        row.selected_related_as_unrelated_rate
        <= row.baseline_related_as_unrelated_rate
        for row in result.outer_selections
    )
    assert all(
        0.0 <= row.baseline_p_unrelated <= 1.0
        and 0.0 <= row.candidate_p_unrelated <= 1.0
        for row in result.paired_outer_oof
    )


def test_challenger_contract_has_no_platform_or_split_field() -> None:
    names = {field.name for field in fields(ChallengerDocument)}

    assert "platform_key" not in names
    assert "split_name" not in names


def test_nested_comparison_rejects_duplicate_member_key() -> None:
    documents = list(_documents())
    documents[-1] = replace(documents[-1], member_key=documents[0].member_key)

    with pytest.raises(SparseChallengerError) as error:
        fit_sparse_challenger_nested(documents, plan=_small_plan())

    assert error.value.reason_code == "sparse_challenger_document_invalid"

