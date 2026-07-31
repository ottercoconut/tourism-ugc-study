from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.image_candidates import (
    CandidateImage,
    build_image_candidate_plan,
    phash_hamming_distance,
    url_has_role_hint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _candidate(index: int, *, author: str | None, phash: str = "0" * 16) -> CandidateImage:
    return CandidateImage(
        fingerprint_id=f"fp-{index:02d}",
        row_identity_sha256=f"{index:x}" * 64,
        source_image_id=index,
        source_post_id=index,
        author_identity_sha256=author,
        file_sha256="a" * 64,
        phash_hex=phash,
        byte_size=4096,
        width_px=800,
        height_px=600,
        is_fully_transparent=False,
        url_role_hint=False,
    )


def test_phash_distance_only_builds_candidates_not_labels() -> None:
    config = load_config(CONFIG_PATH).image
    first = _candidate(1, author="1" * 64, phash="0000000000000000")
    second = replace(
        _candidate(2, author="2" * 64, phash="0000000000000003"),
        file_sha256="b" * 64,
    )
    plan = build_image_candidate_plan([first, second], config)

    assert phash_hamming_distance(first.phash_hex, second.phash_hex) == 2
    assert len(plan.near_pairs) == 1
    assert not hasattr(plan.near_pairs[0], "final_label")


def test_high_reuse_requires_three_known_authors_and_does_not_merge_empty_author() -> None:
    config = load_config(CONFIG_PATH).image
    records = [
        _candidate(index, author=(f"{index:x}" * 64 if index <= 2 else None))
        for index in range(1, 11)
    ]
    without_three = build_image_candidate_plan(records, config)
    assert "high_reuse" not in {signal.signal_code for signal in without_three.signals}

    records[2] = replace(records[2], author_identity_sha256="c" * 64)
    with_three = build_image_candidate_plan(records, config)
    high_reuse = [signal for signal in with_three.signals if signal.signal_code == "high_reuse"]
    assert len(high_reuse) == 10
    assert high_reuse[0].evidence == {"post_count": 10, "known_author_count": 3}


def test_technical_flags_remain_signals() -> None:
    config = load_config(CONFIG_PATH).image
    record = replace(
        _candidate(1, author="a" * 64),
        byte_size=100,
        width_px=16,
        height_px=512,
        is_fully_transparent=True,
        url_role_hint=True,
    )
    plan = build_image_candidate_plan([record], config)

    assert {signal.signal_code for signal in plan.signals} == {
        "url_role_hint",
        "tiny_dimensions",
        "tiny_file",
        "extreme_aspect_ratio",
        "fully_transparent",
    }


def test_url_role_hint_covers_versioned_technical_asset_terms() -> None:
    """URL 只生成布尔候选，覆盖科研方案冻结的常见技术资产词。"""

    for term in (
        "avatar",
        "head",
        "profile",
        "logo",
        "icon",
        "sprite",
        "background",
        "default",
        "placeholder",
        "error",
        "loading",
        "bg",
        "qr",
    ):
        assert url_has_role_hint(f"https://invalid/assets/{term}/image.png")
    assert not url_has_role_hint("https://invalid/content/route-plan.png")
