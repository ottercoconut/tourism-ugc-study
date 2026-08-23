"""正式 baseline 开发误差聚合与人工类型绑定测试。"""

from __future__ import annotations

import pytest

from tourism_ugc_study.models.text.baseline_error_analysis import (
    BaselineErrorAnalysisError,
    _split_analysis,
    apply_error_type_coding,
    render_baseline_development_error_summary,
)


def _rows():
    """构造同时覆盖两类与两个错误方向的开发概率行。"""

    return (
        {
            "tourism_label": "related",
            "p_unrelated": 0.1,
            "predicted_label": "related",
            "is_error": False,
            "text_length": 100,
            "length_band": "0000-0299",
            "probability_band": "0.10-0.25",
        },
        {
            "tourism_label": "related",
            "p_unrelated": 0.8,
            "predicted_label": "unrelated",
            "is_error": True,
            "text_length": 500,
            "length_band": "0300-0599",
            "probability_band": "0.75-0.90",
        },
        {
            "tourism_label": "unrelated",
            "p_unrelated": 0.2,
            "predicted_label": "related",
            "is_error": True,
            "text_length": 800,
            "length_band": "0600-1199",
            "probability_band": "0.10-0.25",
        },
        {
            "tourism_label": "unrelated",
            "p_unrelated": 0.9,
            "predicted_label": "unrelated",
            "is_error": False,
            "text_length": 2500,
            "length_band": "2400+",
            "probability_band": "0.90-1.00",
        },
    )


def _analysis():
    """构造只含去敏 review key 的开发分析摘要。"""

    split = _split_analysis(_rows())
    return {
        "model_id": "model-1",
        "splits": {"train_oof": split, "validation": split},
        "error_records": [
            {
                "review_key": "key-1",
                "split": "train_oof",
                "actual_label": "related",
                "predicted_label": "unrelated",
            },
            {
                "review_key": "key-2",
                "split": "validation",
                "actual_label": "unrelated",
                "predicted_label": "related",
            },
        ],
    }


def test_split_analysis_reports_both_error_directions_and_length_bands() -> None:
    result = _split_analysis(_rows())

    assert result["accuracy"] == 0.5
    assert result["correct_count"] == 2
    assert result["confusion"] == {
        "related_as_related": 1,
        "related_as_unrelated": 1,
        "unrelated_as_related": 1,
        "unrelated_as_unrelated": 1,
    }
    assert result["text_length"]["bands"]["0300-0599"]["error_rate"] == 1.0
    assert result["text_length"]["bands"]["1200-2399"]["count"] == 0


def test_complete_error_coding_is_bound_and_aggregated() -> None:
    coding = {
        "artifact_kind": "formal-cleaning-baseline-development-error-type-coding",
        "status": "finalized",
        "model_id": "model-1",
        "codebook": {"boundary": "标签边界"},
        "records": [
            {"review_key": "key-1", "error_type": "boundary"},
            {"review_key": "key-2", "error_type": "boundary"},
        ],
    }

    result = apply_error_type_coding(_analysis(), coding)
    report = render_baseline_development_error_summary(result)

    assert result["error_type_coding"]["overall_counts"] == {"boundary": 2}
    assert result["error_records"][0]["error_type"] == "boundary"
    assert "标签边界：2 条" in report


def test_error_coding_must_cover_every_development_error() -> None:
    coding = {
        "artifact_kind": "formal-cleaning-baseline-development-error-type-coding",
        "status": "finalized",
        "model_id": "model-1",
        "codebook": {"boundary": "标签边界"},
        "records": [{"review_key": "key-1", "error_type": "boundary"}],
    }

    with pytest.raises(BaselineErrorAnalysisError) as error:
        apply_error_type_coding(_analysis(), coding)

    assert error.value.reason_code == "baseline_analysis_error_coding_members_mismatch"
