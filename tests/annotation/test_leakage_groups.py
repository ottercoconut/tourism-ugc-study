from __future__ import annotations

from tourism_ugc_study.annotation.leakage_groups import (
    ConfirmedNearRelation,
    LeakagePost,
    build_leakage_plan,
)


def test_missing_authors_stay_independent_while_confirmed_edges_merge() -> None:
    posts = (
        LeakagePost(1, 1, "missing", False, "exact-1"),
        LeakagePost(2, 1, "missing", False, "exact-2"),
        LeakagePost(3, 1, "author-a", True, "exact-3"),
        LeakagePost(4, 1, "author-a", True, "exact-4"),
        LeakagePost(5, 1, "author-b", True, "exact-5"),
        LeakagePost(6, 1, "author-c", True, "exact-5"),
        LeakagePost(7, 1, "author-d", True, "exact-7"),
    )
    plan = build_leakage_plan(
        posts,
        (ConfirmedNearRelation("human-gold", "exact-5", "exact-7"),),
    )
    by_post = {member.source_post_id: member for member in plan.members}

    assert by_post[1].component_id != by_post[2].component_id
    assert by_post[3].component_id == by_post[4].component_id
    assert by_post[5].component_id == by_post[6].component_id == by_post[7].component_id
    assert by_post[3].author_edge_used is True
    assert by_post[5].exact_edge_used is True
    assert by_post[7].confirmed_near_edge_used is True


def test_candidate_components_are_not_an_input_to_leakage_plan() -> None:
    """没有人工确认关系时，两个不同精确簇保持分离。"""

    posts = (
        LeakagePost(1, 1, "a", True, "exact-a"),
        LeakagePost(2, 1, "b", True, "exact-b"),
    )

    plan = build_leakage_plan(posts, ())

    assert len({member.component_id for member in plan.members}) == 2
