"""最简共同校准主表、规范化和问题提取测试。"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from tourism_ugc_study.annotation.calibration import (
    CALIBRATION_INPUT_FIELDS,
    ISSUE_OUTPUT_FIELDS,
    CalibrationValidationError,
    convert_calibration_rows,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPOSITORY_ROOT / "data/annotations/templates"
CONVERTER_SCRIPT = REPOSITORY_ROOT / "scripts/annotation_prepare_calibration.py"


def _header(path: Path) -> tuple[str, ...]:
    with path.open(encoding="utf-8", newline="") as stream:
        return tuple(next(csv.reader(stream)))


def _row(**overrides: str) -> dict[str, str]:
    row = {field: "" for field in CALIBRATION_INPUT_FIELDS}
    row.update(
        {
            "annotation_id": "ann-1",
            "item_id": "item-1",
            "post_id": "post-1",
            "unit_type": "TEXT_SEGMENT",
            "unit_id": "3",
            "platform": "xhs",
            "raw_text": "海风很舒服，海风也很温柔。",
            "dimension_code": "V3",
            "field_name": "at_has_eval",
            "field_label_zh": "评价性表达",
            "valid_values": "0|1|UNK|NA|UNRESOLVED",
            "is_subjective": "1",
            "label_value": "1",
            "confidence": "4",
            "evidence_quote": "很舒服",
            "annotator_id": "coder-a",
            "annotated_at": "2026-08-15T09:00:00+08:00",
            "codebook_version": "v3.6.1",
            "template_schema_version": "calibration-coding-v1.0",
        }
    )
    row.update(overrides)
    return row


def test_calibration_templates_have_frozen_headers() -> None:
    assert _header(TEMPLATE_DIR / "calibration-coding.csv") == CALIBRATION_INPUT_FIELDS
    assert _header(TEMPLATE_DIR / "calibration-issues.csv") == ISSUE_OUTPUT_FIELDS
    assert _header(TEMPLATE_DIR / "revision-decisions.csv") == (
        "decision_id",
        "issue_ids_json",
        "decision_type",
        "affected_fields_json",
        "decision_rationale",
        "recoding_scope",
        "effective_version",
        "decided_by_json",
        "decided_at",
    )


def test_conversion_computes_unicode_offset_and_normalizes_label() -> None:
    conversion = convert_calibration_rows(
        [_row()], expected_codebook_version="v3.6.1"
    )
    assert len(conversion.labels) == 1
    label = conversion.labels[0]
    assert label["template_schema_version"] == "labels-v2.0"
    assert json.loads(label["evidence_spans_json"]) == [
        {"fields": ["at_has_eval"], "start": 2, "end": 5, "quote": "很舒服"}
    ]
    assert conversion.issues == ()


def test_low_confidence_note_and_unresolved_issue_are_derived() -> None:
    conversion = convert_calibration_rows(
        [
            _row(
                label_value="UNRESOLVED",
                confidence="1",
                reason_code="BOUNDARY",
                alternative_values="ADM|SAT",
                low_confidence_note="同一片段同时包含惊叹与舒适评价。",
            )
        ],
        expected_codebook_version="v3.6.1",
    )
    note = json.loads(conversion.labels[0]["confidence_notes_json"])
    assert note["alternative_values"] == ["ADM", "SAT"]
    assert conversion.issues[0]["issue_type"] == "BOUNDARY_CASE"
    assert conversion.issues[0]["status"] == "OPEN"


def test_repeated_quote_requires_start_only_in_ambiguous_case() -> None:
    with pytest.raises(CalibrationValidationError, match="重复出现"):
        convert_calibration_rows(
            [_row(evidence_quote="海风")], expected_codebook_version="v3.6.1"
        )
    conversion = convert_calibration_rows(
        [_row(evidence_quote="海风", evidence_start_if_repeated="6")],
        expected_codebook_version="v3.6.1",
    )
    span = json.loads(conversion.labels[0]["evidence_spans_json"])[0]
    assert (span["start"], span["end"]) == (6, 8)


def test_offset_preserves_leading_whitespace_in_raw_text() -> None:
    conversion = convert_calibration_rows(
        [_row(raw_text=" 海风很舒服。", evidence_quote="很舒服")],
        expected_codebook_version="v3.6.1",
    )
    span = json.loads(conversion.labels[0]["evidence_spans_json"])[0]
    assert (span["start"], span["end"]) == (3, 6)


def test_low_confidence_requires_only_reason_and_short_note() -> None:
    with pytest.raises(CalibrationValidationError, match="一句说明"):
        convert_calibration_rows(
            [_row(confidence="2", reason_code="CONTEXT")],
            expected_codebook_version="v3.6.1",
        )


def test_label_value_must_match_prefilled_valid_values() -> None:
    with pytest.raises(CalibrationValidationError, match="不在valid_values中"):
        convert_calibration_rows(
            [_row(label_value="2")], expected_codebook_version="v3.6.1"
        )


def test_excel_exported_preamble_and_bilingual_header_are_readable(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location("calibration_cli", CONVERTER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "excel-export.csv"
    bilingual_header = [f"中文\n{field}" for field in CALIBRATION_INPUT_FIELDS]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["共同校准说明"])
        writer.writerow([])
        writer.writerow(bilingual_header + ["行检查\nrow_check"])
        writer.writerow([_row()[field] for field in CALIBRATION_INPUT_FIELDS] + ["完成"])
    rows = module._read_rows(path)
    assert rows[0]["field_name"] == "at_has_eval"
    assert rows[0]["row_check"] == "完成"
