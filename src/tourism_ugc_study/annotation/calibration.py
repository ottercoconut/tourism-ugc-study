"""共同校准主表的校验、规范化与问题队列提取。

本模块把编码员使用的单一、可读主表转换为研究存储层的逐原子字段长表。
编码员填写编码值和规则要求的证据原文，确实拿不准时再加疑问标记与简短备注；
JSON、文本 offset、问题编号与版本字段由本模块生成。模块不决定类目、修订
编码簿或裁决分歧，研究负责人仍须复核问题队列并另行填写修订决策表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping


CALIBRATION_TEMPLATE_VERSION = "calibration-coding-v2.0"
LABEL_TEMPLATE_VERSION = "labels-v3.0"

CALIBRATION_INPUT_FIELDS = (
    "annotation_id",
    "item_id",
    "post_id",
    "unit_type",
    "unit_id",
    "platform",
    "raw_text",
    "dimension_code",
    "field_name",
    "field_label_zh",
    "valid_values",
    "is_subjective",
    "label_value",
    "review_flag",
    "reason_code",
    "alternative_values",
    "review_note",
    "evidence_quote",
    "evidence_start_if_repeated",
    "other_issue_type",
    "other_issue_note",
    "annotator_id",
    "annotated_at",
    "codebook_version",
    "template_schema_version",
)

LABEL_OUTPUT_FIELDS = (
    "annotation_id",
    "item_id",
    "unit_type",
    "unit_id",
    "annotator_id",
    "dimension_code",
    "field_name",
    "label_value",
    "review_flag",
    "review_notes_json",
    "evidence_spans_json",
    "template_schema_version",
    "codebook_version",
    "annotated_at",
)

ISSUE_OUTPUT_FIELDS = (
    "issue_id",
    "post_id",
    "seg_id",
    "img_id",
    "issue_type",
    "affected_fields_json",
    "evidence_spans_json",
    "issue_note",
    "coder",
    "codebook_version",
    "status",
)

VALID_UNIT_TYPES = frozenset({"POST", "TEXT_SEGMENT", "IMAGE"})
VALID_REASON_CODES = frozenset(
    {"BOUNDARY", "CONTEXT", "CONFLICT", "EVIDENCE_MISSING", "OTHER"}
)
VALID_ISSUE_TYPES = frozenset(
    {
        "FRAMEWORK_GAP",
        "BOUNDARY_CASE",
        "COUNTEREXAMPLE",
        "CONTEXT_DEPENDENCE",
        "UNITIZATION_PROBLEM",
    }
)
_REASON_TO_ISSUE_TYPE = {
    "BOUNDARY": "BOUNDARY_CASE",
    "CONTEXT": "CONTEXT_DEPENDENCE",
    "CONFLICT": "COUNTEREXAMPLE",
    # 这里只做负责人问题队列的初步分流，不替代研究负责人的正式分类。
    "EVIDENCE_MISSING": "CONTEXT_DEPENDENCE",
    "OTHER": "FRAMEWORK_GAP",
}


class CalibrationValidationError(ValueError):
    """主表记录不满足共同校准契约时抛出，错误包含输入行号。"""


@dataclass(frozen=True)
class CalibrationConversion:
    """一次确定性转换的标签记录与负责人问题队列。"""

    labels: tuple[dict[str, str], ...]
    issues: tuple[dict[str, str], ...]


def _text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field, "")
    return "" if value is None else str(value)


def _required(row: Mapping[str, object], field: str, row_number: int) -> str:
    value = _text(row, field).strip()
    if not value:
        raise CalibrationValidationError(f"第{row_number}行缺少{field}")
    return value


def _parse_subjective(row: Mapping[str, object], row_number: int) -> bool:
    value = _required(row, "is_subjective", row_number)
    if value not in {"0", "1"}:
        raise CalibrationValidationError(
            f"第{row_number}行is_subjective必须为0或1"
        )
    return value == "1"


def _parse_review_flag(row: Mapping[str, object], row_number: int) -> bool:
    """读取编码员的可选疑问标记，并统一转换为布尔值。

    人工表推荐填写直观的问号；CSV直接录入时也兼容``1``。空白表示编码员
    已完成判断且没有主动提出疑问，不代表标签值为0。
    """

    value = _text(row, "review_flag").strip().upper()
    if not value:
        return False
    if value not in {"?", "1"}:
        raise CalibrationValidationError(
            f"第{row_number}行review_flag只能留空或填写?"
        )
    return True


def _parse_alternatives(value: str) -> list[str | int]:
    """把编码员易填的竖线分隔值转换为结构化数组。"""

    alternatives: list[str | int] = []
    for item in value.split("|"):
        stripped = item.strip()
        if not stripped:
            continue
        alternatives.append(int(stripped) if stripped.isdigit() else stripped)
    return alternatives


def _evidence_spans(
    row: Mapping[str, object], row_number: int, field_name: str
) -> list[dict[str, object]]:
    """从证据原文确定唯一Unicode字符offset；重复时要求人工给起点。"""

    quote = _text(row, "evidence_quote")
    if not quote:
        return []
    raw_text = _text(row, "raw_text")
    if not raw_text.strip():
        raise CalibrationValidationError(f"第{row_number}行缺少raw_text")
    explicit_start = _text(row, "evidence_start_if_repeated").strip()
    if explicit_start:
        try:
            start = int(explicit_start)
        except ValueError as error:
            raise CalibrationValidationError(
                f"第{row_number}行evidence_start_if_repeated必须为非负整数"
            ) from error
        if start < 0:
            raise CalibrationValidationError(
                f"第{row_number}行evidence_start_if_repeated必须为非负整数"
            )
    else:
        start = raw_text.find(quote)
        if start < 0:
            raise CalibrationValidationError(
                f"第{row_number}行evidence_quote不在raw_text中"
            )
        if raw_text.find(quote, start + 1) >= 0:
            raise CalibrationValidationError(
                f"第{row_number}行证据原文重复出现，须填写evidence_start_if_repeated"
            )
    end = start + len(quote)
    if raw_text[start:end] != quote:
        raise CalibrationValidationError(
            f"第{row_number}行证据起点与evidence_quote不匹配"
        )
    return [{"fields": [field_name], "start": start, "end": end, "quote": quote}]


def _issue_row(
    *,
    issue_id: str,
    row: Mapping[str, object],
    issue_type: str,
    issue_note: str,
    evidence_json: str,
    field_name: str,
    unit_type: str,
) -> dict[str, str]:
    unit_id = _text(row, "unit_id").strip()
    return {
        "issue_id": issue_id,
        "post_id": _text(row, "post_id").strip(),
        "seg_id": unit_id if unit_type == "TEXT_SEGMENT" else "",
        "img_id": unit_id if unit_type == "IMAGE" else "",
        "issue_type": issue_type,
        "affected_fields_json": json.dumps(
            [field_name] if field_name else [], ensure_ascii=False, separators=(",", ":")
        ),
        "evidence_spans_json": evidence_json,
        "issue_note": issue_note,
        "coder": _text(row, "annotator_id").strip(),
        "codebook_version": _text(row, "codebook_version").strip(),
        "status": "OPEN",
    }


def convert_calibration_rows(
    rows: Iterable[Mapping[str, object]], *, expected_codebook_version: str
) -> CalibrationConversion:
    """校验编码员主表，生成规范标签和负责人问题队列。

    完全空行会被忽略。结构性``NA``和客观字段不允许填写疑问标记；其他主观
    判断可在确实拿不准时填写问号。``UNRESOLVED``必须带疑问标记。凡带标记
    的判断均须填写原因代码和一句说明，替代值可留空并自动保存为空数组。
    转换不读取或修改原始数据库。
    """

    labels: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    seen_annotation_ids: set[str] = set()

    for row_number, row in enumerate(rows, start=2):
        if not any(_text(row, field).strip() for field in CALIBRATION_INPUT_FIELDS):
            continue
        annotation_id = _required(row, "annotation_id", row_number)
        if annotation_id in seen_annotation_ids:
            raise CalibrationValidationError(
                f"第{row_number}行annotation_id重复：{annotation_id}"
            )
        seen_annotation_ids.add(annotation_id)
        unit_type = _required(row, "unit_type", row_number)
        if unit_type not in VALID_UNIT_TYPES:
            raise CalibrationValidationError(
                f"第{row_number}行unit_type不在允许值中"
            )
        field_name = _required(row, "field_name", row_number)
        label_value = _required(row, "label_value", row_number)
        valid_values = tuple(
            value.strip()
            for value in _required(row, "valid_values", row_number).split("|")
            if value.strip()
        )
        if label_value not in valid_values:
            raise CalibrationValidationError(
                f"第{row_number}行label_value={label_value}不在valid_values中"
            )
        is_subjective = _parse_subjective(row, row_number)
        review_flag = _parse_review_flag(row, row_number)
        if label_value == "NA" or not is_subjective:
            if review_flag:
                raise CalibrationValidationError(
                    f"第{row_number}行客观字段或NA不得填写review_flag"
                )
        if label_value == "UNRESOLVED" and not review_flag:
            raise CalibrationValidationError(
                f"第{row_number}行UNRESOLVED必须填写review_flag"
            )

        needs_note = review_flag
        reason_code = _text(row, "reason_code").strip()
        review_note = _text(row, "review_note").strip()
        if needs_note:
            if reason_code not in VALID_REASON_CODES:
                raise CalibrationValidationError(
                    f"第{row_number}行疑问记录必须填写合法reason_code"
                )
            if not review_note:
                raise CalibrationValidationError(
                    f"第{row_number}行疑问记录必须填写一句说明"
                )
        elif reason_code or review_note or _text(row, "alternative_values").strip():
            raise CalibrationValidationError(
                f"第{row_number}行仅疑问记录可填写原因、替代值和一句说明"
            )
        alternatives = _parse_alternatives(_text(row, "alternative_values"))
        review_note_payload = (
            {
                "reason_code": reason_code,
                "alternative_values": alternatives,
                "note": review_note,
            }
            if needs_note
            else None
        )

        spans = _evidence_spans(row, row_number, field_name)
        evidence_json = (
            json.dumps(spans, ensure_ascii=False, separators=(",", ":"))
            if spans
            else ""
        )
        codebook_version = _required(row, "codebook_version", row_number)
        if codebook_version != expected_codebook_version:
            raise CalibrationValidationError(
                f"第{row_number}行codebook_version={codebook_version}，"
                f"预期{expected_codebook_version}"
            )
        schema_version = _required(row, "template_schema_version", row_number)
        if schema_version != CALIBRATION_TEMPLATE_VERSION:
            raise CalibrationValidationError(
                f"第{row_number}行template_schema_version={schema_version}，"
                f"预期{CALIBRATION_TEMPLATE_VERSION}"
            )

        labels.append(
            {
                "annotation_id": annotation_id,
                "item_id": _required(row, "item_id", row_number),
                "unit_type": unit_type,
                "unit_id": _required(row, "unit_id", row_number),
                "annotator_id": _required(row, "annotator_id", row_number),
                "dimension_code": _required(row, "dimension_code", row_number),
                "field_name": field_name,
                "label_value": label_value,
                "review_flag": "1" if review_flag else "",
                "review_notes_json": (
                    json.dumps(
                        review_note_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if review_note_payload is not None
                    else ""
                ),
                "evidence_spans_json": evidence_json,
                "template_schema_version": LABEL_TEMPLATE_VERSION,
                "codebook_version": codebook_version,
                "annotated_at": _required(row, "annotated_at", row_number),
            }
        )

        other_issue_type = _text(row, "other_issue_type").strip()
        other_issue_note = _text(row, "other_issue_note").strip()
        if other_issue_type and not other_issue_note:
            raise CalibrationValidationError(
                f"第{row_number}行选择其他问题类型后必须填写问题说明"
            )
        if other_issue_note and other_issue_type not in VALID_ISSUE_TYPES:
            raise CalibrationValidationError(
                f"第{row_number}行填写其他问题时必须选择合法other_issue_type"
            )
        if label_value == "UNRESOLVED":
            unresolved_type = _REASON_TO_ISSUE_TYPE[reason_code]
            issues.append(
                _issue_row(
                    issue_id=f"ISSUE-{annotation_id}-U",
                    row=row,
                    issue_type=unresolved_type,
                    issue_note=review_note,
                    evidence_json=evidence_json,
                    field_name=field_name,
                    unit_type=unit_type,
                )
            )
        if other_issue_note:
            issues.append(
                _issue_row(
                    issue_id=f"ISSUE-{annotation_id}-O",
                    row=row,
                    issue_type=other_issue_type,
                    issue_note=other_issue_note,
                    evidence_json=evidence_json,
                    field_name=field_name,
                    unit_type=unit_type,
                )
            )

    return CalibrationConversion(tuple(labels), tuple(issues))
