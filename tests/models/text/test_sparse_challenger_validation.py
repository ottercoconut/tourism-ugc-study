"""唯一 challenger 无拟合验证方向性复核测试。"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace

import numpy as np
import pytest

import tourism_ugc_study.models.text.sparse_challenger_validation as validation_module
from tourism_ugc_study.models.text.sparse_challenger_validation import (
    ChallengerValidationDocument,
    SparseChallengerValidationError,
    evaluate_sparse_challenger_validation,
)


class _FrozenPredictor:
    """测试专用、只提供概率预测且记录调用次数的冻结对象。"""

    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = np.asarray(probabilities, dtype=float)
        self.predict_calls = 0

    def predict_p_unrelated(self, texts):
        self.predict_calls += 1
        assert all(isinstance(text, str) and text for text in texts)
        return self.probabilities


def _documents() -> tuple[ChallengerValidationDocument, ...]:
    """构造20条平衡验证证据，baseline 含一个 UGC 误排。"""

    values: list[ChallengerValidationDocument] = []
    for index in range(20):
        unrelated = index >= 10
        baseline_probability = 0.80 if unrelated else 0.20
        if index == 0:
            baseline_probability = 0.60
        values.append(
            ChallengerValidationDocument(
                member_key=f"member-{index:02d}",
                component_id=f"component-{index:02d}",
                normalized_model_text=f"冻结验证文本{index}",
                tourism_label="unrelated" if unrelated else "related",
                baseline_p_unrelated=baseline_probability,
            )
        )
    return tuple(values)


def test_directional_validation_only_calls_prediction_once() -> None:
    candidate = [0.10] * 10 + [0.90] * 10
    predictor = _FrozenPredictor(candidate)

    result = evaluate_sparse_challenger_validation(
        _documents(), frozen_candidate=predictor
    )

    assert predictor.predict_calls == 1
    assert result.validation_count == 20
    assert result.direction_status == "directionally_consistent"
    assert all(result.direction_checks.values())
    assert result.baseline_metrics["confusion"]["related_as_unrelated"] == 1
    assert result.candidate_metrics["confusion"]["related_as_unrelated"] == 0
    assert result.candidate_minus_baseline["log_loss"] < 0.0
    assert len(result.observations) == 20


def test_directional_validation_reports_reversal_without_model_selection() -> None:
    candidate = [0.70] * 10 + [0.90] * 10
    result = evaluate_sparse_challenger_validation(
        _documents(), frozen_candidate=_FrozenPredictor(candidate)
    )

    assert result.direction_status == "mixed_or_reversed"
    assert result.direction_checks["related_safety_not_worse"] is False
    assert result.candidate_minus_baseline["related_to_unrelated_rate"] > 0.0


def test_directional_validation_rejects_duplicate_member() -> None:
    documents = list(_documents())
    documents[-1] = replace(documents[-1], member_key=documents[0].member_key)

    with pytest.raises(SparseChallengerValidationError) as error:
        evaluate_sparse_challenger_validation(
            documents,
            frozen_candidate=_FrozenPredictor([0.5] * len(documents)),
        )

    assert (
        error.value.reason_code
        == "sparse_challenger_validation_document_invalid"
    )


def test_validation_module_contains_no_fit_call() -> None:
    """以语法树证明验证计算没有隐藏的 estimator.fit 调用。"""

    tree = ast.parse(inspect.getsource(validation_module))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "fit" not in called_attributes
    assert "fit_transform" not in called_attributes

