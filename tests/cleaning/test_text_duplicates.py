from __future__ import annotations

from pathlib import Path

from tourism_ugc_study.cleaning.text_config import load_text_config
from tourism_ugc_study.cleaning.text_duplicates import TextDocument, build_duplicate_plan


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES = load_text_config(
    PROJECT_ROOT / "configs" / "cleaning-text-normalization-v1.yaml"
).near_duplicate


def _document(
    source_post_id: int,
    platform: str,
    body: str,
    exact_sha256: str,
) -> TextDocument:
    return TextDocument(source_post_id, 1, platform, "", body, exact_sha256)


def test_exact_clusters_preserve_singletons_and_choose_stable_representatives() -> None:
    documents = [
        _document(3, "weibo", "青岛栈桥夜景非常漂亮", "b" * 64),
        _document(1, "xiaohongshu", "青岛栈桥夜景非常漂亮", "b" * 64),
        _document(2, "douyin", "崂山一日游路线推荐", "a" * 64),
    ]

    plan = build_duplicate_plan(documents, RULES)
    duplicate = next(cluster for cluster in plan.exact_clusters if len(cluster.members) == 2)

    assert len(plan.exact_clusters) == 2
    assert duplicate.representative.source_post_id == 1
    assert duplicate.is_cross_platform is True
    assert sum(len(cluster.members) for cluster in plan.exact_clusters) == 3


def test_near_candidates_are_deterministic_and_do_not_expand_exact_copies() -> None:
    documents = [
        _document(1, "xiaohongshu", "青岛三天两夜旅游路线推荐", "a" * 64),
        _document(2, "weibo", "青岛三天两夜旅游路线推荐", "a" * 64),
        _document(3, "douyin", "青岛三天两夜旅游路线攻略", "b" * 64),
        _document(4, "weibo", "完全不同的崂山天气记录", "c" * 64),
    ]

    first = build_duplicate_plan(documents, RULES)
    second = build_duplicate_plan(list(reversed(documents)), RULES)

    assert first.output_sha256 == second.output_sha256
    assert first.near_pairs == second.near_pairs
    assert len(first.near_pairs) == 1
    assert {first.near_pairs[0].left_source_post_id, first.near_pairs[0].right_source_post_id} == {1, 3}
    assert first.near_pairs[0].is_cross_platform is True
    assert all(pair.left_source_post_id != 2 for pair in first.near_pairs)
    assert sum(len(component.members) for component in first.near_components) == 4


def test_short_or_disjoint_corpus_produces_traceable_singleton_components() -> None:
    documents = [
        _document(1, "weibo", "青岛", "1" * 64),
        _document(2, "weibo", "崂山", "2" * 64),
    ]

    plan = build_duplicate_plan(documents, RULES)

    assert plan.near_pairs == ()
    assert len(plan.near_components) == 2
    assert sum(len(component.members) for component in plan.near_components) == 2
