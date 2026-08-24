"""固定三候选的分组OOF、校准隔离与纯预测测试。"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from sklearn.svm import LinearSVC

from tourism_ugc_study.models.text.model_retraining_config import (
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_snapshot import (
    RetrainingDocument,
)
from tourism_ugc_study.models.text.model_retraining_training import (
    train_fixed_retraining_candidates,
)


@pytest.fixture(scope="module")
def trained_result():
    """用完整成员规模和小维合成向量训练一次固定候选。"""

    base = load_model_retraining_plan("configs/cleaning-model-retraining.yaml")
    plan = replace(base, qwen_embedding_dimension=4)
    documents: list[RetrainingDocument] = []
    matrix = np.zeros((1300, 4), dtype=np.float32)
    for index in range(1300):
        label = "related" if index < 585 else "unrelated"
        class_value = -1.0 if label == "related" else 1.0
        text = (
            f"游客到青岛旅行体验 海边散步 {index % 17}"
            if label == "related"
            else f"商家广告促销 招商优惠 本地资讯 {index % 19}"
        )
        origin = (
            "final_reference"
            if index < 700
            else "wave_a" if index < 940 else "wave_b"
        )
        documents.append(
            RetrainingDocument(
                member_key=f"member-{index:04d}",
                source_post_id=index + 1,
                source_version=1,
                component_id=f"component-{index // 2:04d}",
                normalized_model_text=text,
                normalized_sha256=f"{index:064x}",
                tourism_label=label,
                evidence_origin=origin,
                inclusion_probability=(None if index < 700 else 0.5),
                analysis_weight=(None if index < 700 else 2.0),
                historical_test_consumed=index < 148,
            )
        )
        matrix[index] = [class_value, (index % 7) / 10, 0.2, 0.1]
    matrix /= np.linalg.norm(matrix, axis=1)[:, None]
    return documents, matrix, train_fixed_retraining_candidates(
        documents, matrix, plan=plan
    )


def test_components_never_cross_outer_folds(trained_result) -> None:
    """同一finalized leakage component只能分配到一个外折。"""

    documents, _, result = trained_result
    component_folds: dict[str, set[int]] = {}
    for document in documents:
        component_folds.setdefault(document.component_id, set()).add(
            result.fold_assignments[document.member_key]
        )
    assert all(len(values) == 1 for values in component_folds.values())
    assert result.component_cross_fold_violation_count == 0
    assert result.outer_fold_count == 5


def test_every_oof_calibrator_excludes_members_own_fold(trained_result) -> None:
    """每条概率的校准训练端必须显式排除其外层留出折。"""

    _, _, result = trained_result
    assert len(result.oof_observations) == 3900
    assert all(
        row.calibration_excluded_fold_index == row.fold_index
        for row in result.oof_observations
    )
    assert {
        row.candidate_name for row in result.oof_observations
    } == {"qwen_linear_svc", "sparse_linear_svc", "logit_fusion"}


def test_historical_test_is_training_only_not_reopened(trained_result) -> None:
    """旧测试成员可产生训练OOF，但运行不得宣称重新测试。"""

    _, _, result = trained_result
    for name in ("qwen_linear_svc", "sparse_linear_svc", "logit_fusion"):
        rows = [
            row
            for row in result.oof_observations
            if row.candidate_name == name and row.historical_test_consumed
        ]
        assert len(rows) == 148
    assert result.historical_test_reopened is False


def test_frozen_candidates_predict_without_fit(trained_result, monkeypatch) -> None:
    """最终候选纯预测路径不能再次调用LinearSVC.fit。"""

    documents, matrix, result = trained_result

    def forbidden_fit(*args, **kwargs):
        del args, kwargs
        raise AssertionError("predict path called fit")

    monkeypatch.setattr(LinearSVC, "fit", forbidden_fit)
    texts = [item.normalized_model_text for item in documents[:8]]
    for candidate in result.candidates:
        probabilities = candidate.predict_p_unrelated(texts, matrix[:8])
        assert probabilities.shape == (8,)
        assert np.all((0.0 <= probabilities) & (probabilities <= 1.0))
