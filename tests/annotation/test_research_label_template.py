"""研究内容标签模板与最高标准版本的对齐检查。"""

from __future__ import annotations

import csv
import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LABEL_TEMPLATE = REPOSITORY_ROOT / "data/annotations/templates/labels.csv"
ANNOTATION_README = REPOSITORY_ROOT / "data/annotations/README.md"
CANONICAL_CODEBOOK = REPOSITORY_ROOT / "docs/data-dictionary/编码表.md"

EXPECTED_LABEL_FIELDS = (
    "annotation_id",
    "item_id",
    "unit_type",
    "unit_id",
    "annotator_id",
    "dimension_code",
    "field_name",
    "label_value",
    "confidence",
    "confidence_notes_json",
    "evidence_spans_json",
    "template_schema_version",
    "codebook_version",
    "annotated_at",
)


def _required_version(pattern: str, text: str, source: Path) -> str:
    """读取显式版本；缺失时拒绝通过，避免模板静默脱离编码表。"""

    match = re.search(pattern, text, flags=re.MULTILINE)
    assert match is not None, f"missing version marker in {source}"
    return match.group("version")


def test_research_label_template_uses_v3_6_atomic_field_contract() -> None:
    """空白模板必须保持逐观察单位、逐原子字段的稳定列顺序。"""

    with LABEL_TEMPLATE.open(encoding="utf-8", newline="") as stream:
        rows = tuple(csv.reader(stream))

    assert len(rows) == 1
    assert tuple(rows[0]) == EXPECTED_LABEL_FIELDS


def test_research_label_template_alignment_version_matches_canonical() -> None:
    """编码表升版后，标注说明必须在同一变更中确认模板对齐状态。"""

    canonical_text = CANONICAL_CODEBOOK.read_text(encoding="utf-8")
    annotation_text = ANNOTATION_README.read_text(encoding="utf-8")
    canonical_version = _required_version(
        r"^# 编码表 v(?P<version>\d+\.\d+\.\d+)$",
        canonical_text,
        CANONICAL_CODEBOOK,
    )
    template_version = _required_version(
        r"^> \*\*当前对齐编码表\*\*：`v(?P<version>\d+\.\d+\.\d+)`$",
        annotation_text,
        ANNOTATION_README,
    )

    assert template_version == canonical_version
