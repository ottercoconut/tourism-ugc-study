"""图片技术噪声唯一人工轴、CSV 安全与一致性语义测试。"""

from __future__ import annotations

import pytest

from tourism_ugc_study.cleaning.image_review_annotation import (
    AnnotationPair,
    calculate_image_agreement,
    split_codes,
    validate_safe_csv_cell,
)


def test_csv_formula_prefixes_are_rejected() -> None:
    for value in ("=CMD()", "+1", "-2", " @SUM(A1:A2)"):
        with pytest.raises(ValueError):
            validate_safe_csv_cell(value)
    assert validate_safe_csv_cell("valid_content") == "valid_content"


def test_reason_and_flag_codes_are_safe_and_stable() -> None:
    assert split_codes("tiny;qr; tiny") == ("qr", "tiny")
    assert split_codes("") == ()
    with pytest.raises(ValueError):
        split_codes("自由文本")


def test_incomplete_plan_cannot_claim_agreement() -> None:
    report = calculate_image_agreement(
        [AnnotationPair("fp-1", "valid_content", "valid_content")],
        planned_pair_count=2,
        minimum_raw_agreement=0.8,
    )
    assert report.evaluation_status == "incomplete"
    assert report.raw_agreement is None
    assert report.cohen_kappa is None


def test_single_category_kappa_is_explicitly_undefined_but_can_pass() -> None:
    report = calculate_image_agreement(
        [
            AnnotationPair("fp-1", "valid_content", "valid_content"),
            AnnotationPair("fp-2", "valid_content", "valid_content"),
        ],
        planned_pair_count=2,
        minimum_raw_agreement=0.8,
    )
    assert report.raw_agreement == 1.0
    assert report.cohen_kappa is None
    assert report.kappa_status == "undefined_single_category"
    assert report.evaluation_status == "passed"


def test_low_raw_agreement_requires_supplement_even_when_kappa_is_estimable() -> None:
    report = calculate_image_agreement(
        [
            AnnotationPair("fp-1", "valid_content", "site_ui"),
            AnnotationPair("fp-2", "site_ui", "valid_content"),
        ],
        planned_pair_count=2,
        minimum_raw_agreement=0.8,
    )
    assert report.raw_agreement == 0.0
    assert report.cohen_kappa == -1.0
    assert report.evaluation_status == "supplement_required"
