from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_research_dataset.py"
SPEC = importlib.util.spec_from_file_location("build_research_dataset", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_normalize_text_removes_format_and_collapses_whitespace() -> None:
    assert MODULE.normalize_text("  Ａ\u200bB\n\t C  ") == "AB C"


def test_content_normalization_preserves_paragraph_boundaries() -> None:
    value = "  第一段  \r\n\r\n\r\n  第二段\t内容  "
    assert MODULE.normalize_text(value, preserve_newlines=True) == "第一段\n\n第二段 内容"


def test_normalize_canonical_url_removes_tracking_but_keeps_access_token() -> None:
    value = "https://EXAMPLE.com/post/1/?utm_source=x&xsec_token=keep#fragment"
    normalized, changed, valid = MODULE.normalize_canonical_url(value)
    assert normalized == "https://example.com/post/1?xsec_token=keep"
    assert changed is True
    assert valid is True


def test_parse_timestamp_converts_to_shanghai() -> None:
    normalized, parsed, valid = MODULE.parse_timestamp("2026-07-15T00:00:00Z")
    assert normalized == "2026-07-15T08:00:00+08:00"
    assert parsed == dt.datetime.fromisoformat("2026-07-15T08:00:00+08:00")
    assert valid is True


def test_douyin_image_note_is_not_classified_as_video() -> None:
    assert MODULE.is_video_record("douyin", {"aweme_type": "68", "note_download_url": ["a"]}) is False
    assert MODULE.is_video_record("douyin", {"aweme_type": "0", "video_download_url": "https://v"}) is True


def test_negative_counts_become_missing_but_real_zero_is_retained() -> None:
    assert MODULE.nonnegative_int(-1) is None
    assert MODULE.nonnegative_int(0) == 0
