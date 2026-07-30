from __future__ import annotations

from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.config import ConfigurationError
from tourism_ugc_study.cleaning.text_config import load_text_config
from tourism_ugc_study.cleaning.text_normalize import normalize_post_text


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEXT_CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-text-normalization-v1.yaml"


def test_text_config_has_reproducible_version_lock() -> None:
    config = load_text_config(TEXT_CONFIG_PATH)

    assert config.version == "text-normalization-v1"
    assert config.version_lock == f"text-normalization-v1+sha256:{config.sha256}"
    assert config.near_duplicate.ngram_range == (3, 5)
    assert config.near_duplicate.final_threshold is None


def test_text_config_rejects_wrong_frozen_lock() -> None:
    with pytest.raises(ConfigurationError, match="version lock mismatch"):
        load_text_config(TEXT_CONFIG_PATH, expected_version_lock="wrong")


def test_normalization_golden_case_and_hashes_are_stable() -> None:
    config = load_text_config(TEXT_CONFIG_PATH)

    result = normalize_post_text(
        "  Ｑｉｎｇｄａｏ\u200b攻略\r\n第二行  ",
        "看 https://EXAMPLE.com/a?x=1，@小王 #青岛旅行# 👨‍👩‍👧‍👦\t出发",
        source_status="success",
        config=config,
    )

    assert result.model_text == (
        "[TITLE]\nQingdao攻略\n第二行\n[BODY]\n"
        "看 [URL],[MENTION] [TOPIC]青岛旅行[/TOPIC] [EMOJI] 出发"
    )
    assert result.normalized_sha256 == "e5ee00197f997879213f938a543422117d31db2fbe4a4c163c13cee7849a89fc"
    assert result.exact_canonical_sha256 == "03ad34da9f4db2654b3fa1ede157187cfe98ad0c341ef4a9796814cf418a2cb8"
    assert result.structure_status == "usable"
    assert result.evidence["replacement_counts"] == {
        "control": 1,
        "emoji": 1,
        "mention": 1,
        "topic": 1,
        "url": 1,
    }


def test_body_only_post_is_usable_and_keeps_explicit_empty_title() -> None:
    result = normalize_post_text(
        None,
        "栈桥\u00a0夜景很好！",
        source_status="success",
        config=load_text_config(TEXT_CONFIG_PATH),
    )

    assert result.model_text == "[TITLE]\n\n[BODY]\n栈桥 夜景很好!"
    assert result.structure_status == "usable"
    assert result.structure_reason_code == "structure_usable"
    assert result.exact_canonical_sha256 is not None


@pytest.mark.parametrize(
    ("title", "body", "source_status", "expected_status", "expected_reason"),
    [
        (None, "", "success", "invalid", "empty_title_and_body"),
        (None, "请先登录", "success", "invalid", "login_wall"),
        (None, "正常正文", "failed", "invalid", "invalid_source_status"),
        (None, "https://example.com", "success", "uncertain", "replacement_tokens_only"),
        (None, "青岛", "partial", "uncertain", "uncertain_source_status"),
        (None, "登录青岛后继续旅行", "success", "usable", "structure_usable"),
        (None, "好", "success", "usable", "structure_usable"),
    ],
)
def test_structure_status_is_independent_from_relevance_and_text_length(
    title: object,
    body: object,
    source_status: object,
    expected_status: str,
    expected_reason: str,
) -> None:
    result = normalize_post_text(
        title,
        body,
        source_status=source_status,
        config=load_text_config(TEXT_CONFIG_PATH),
    )

    assert result.structure_status == expected_status
    assert result.structure_reason_code == expected_reason
    assert result.exact_canonical_sha256 is None if expected_status != "usable" else True


def test_semantic_replacements_do_not_collapse_exact_duplicate_key() -> None:
    config = load_text_config(TEXT_CONFIG_PATH)
    first = normalize_post_text(None, "看 https://a.example @甲", config=config)
    second = normalize_post_text(None, "看 https://b.example @乙", config=config)

    assert first.model_text == second.model_text
    assert first.exact_canonical_sha256 != second.exact_canonical_sha256


def test_topic_text_remains_substantive_and_url_mention_boundaries_are_preserved() -> None:
    config = load_text_config(TEXT_CONFIG_PATH)
    topic = normalize_post_text(None, "#青岛旅行#", config=config)
    boundaries = normalize_post_text(
        None,
        "访问 https://x.example:8443/a:b:，联系 @foo.",
        config=config,
    )

    assert topic.normalized_body == "[TOPIC]青岛旅行[/TOPIC]"
    assert topic.structure_status == "usable"
    assert boundaries.normalized_body == "访问 [URL]:,联系 [MENTION]."
