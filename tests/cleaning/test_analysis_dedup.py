"""分析去重纯领域构建的单元测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tourism_ugc_study.cleaning.analysis_dedup import (
    AnalysisDedupMember,
    ConfirmedNearDuplicateRelation,
    build_analysis_dedup,
)


def _member(
    post_id: int,
    exact_key: str,
    *,
    human: tuple[str, str, str] | None = None,
) -> AnalysisDedupMember:
    """以单字符测试键扩展为合法 SHA-256，构造去重成员。"""

    human = human or (None, None, None)  # type: ignore[assignment]
    return AnalysisDedupMember(
        source_post_id=post_id,
        source_version=1,
        exact_canonical_sha256=exact_key * 64,
        human_structure_label=human[0],  # type: ignore[arg-type]
        human_tourism_label=human[1],  # type: ignore[arg-type]
        human_evidence_id=human[2],
    )


def test_exact_and_confirmed_near_relations_rebuild_components() -> None:
    """精确同一与人工确认边可传递合并，但每个成员只出现一次。"""

    members = [
        _member(1, "a"),
        _member(2, "a"),
        _member(3, "b"),
        _member(4, "c"),
    ]
    relation = ConfirmedNearDuplicateRelation(
        left=(2, 1),
        right=(3, 1),
        evidence_id="near-adjudication-1",
    )

    build = build_analysis_dedup(
        members,
        [relation],
        rule_version="analysis-dedup-v1",
    )
    multi = next(cluster for cluster in build.clusters if len(cluster.members) == 3)

    assert len(build.clusters) == 2
    assert multi.members == ((1, 1), (2, 1), (3, 1))
    assert multi.representative == (1, 1)
    assert multi.relation_evidence_ids == ("near-adjudication-1",)
    assert sum(len(cluster.members) for cluster in build.clusters) == 4


@pytest.mark.parametrize(
    ("kind", "adjudication", "message"),
    [
        ("candidate_pair", "duplicate", "candidate or component"),
        ("candidate_component", "duplicate", "candidate or component"),
        ("training_leakage_component", "duplicate", "candidate or component"),
        ("human_adjudication", "candidate", "explicitly adjudicated duplicate"),
        ("human_adjudication", "not_duplicate", "explicitly adjudicated duplicate"),
    ],
)
def test_candidate_and_unconfirmed_relation_leakage_is_rejected(
    kind: str,
    adjudication: str,
    message: str,
) -> None:
    """候选 pair/component 和泄漏分量无法伪装成分析重复真值。"""

    relation = ConfirmedNearDuplicateRelation(
        left=(1, 1),
        right=(2, 1),
        evidence_id="unsafe-edge",
        evidence_kind=kind,
        adjudication=adjudication,
    )

    with pytest.raises(ValueError, match=message):
        build_analysis_dedup(
            [_member(1, "a"), _member(2, "b")],
            [relation],
            rule_version="analysis-dedup-v1",
        )


def test_each_cluster_has_one_stable_representative_and_manifest() -> None:
    """输入顺序和边方向不影响代表项、簇或 manifest。"""

    members = [_member(4, "d"), _member(2, "b"), _member(3, "c")]
    relations = [
        ConfirmedNearDuplicateRelation((4, 1), (3, 1), "edge-2"),
        ConfirmedNearDuplicateRelation((3, 1), (2, 1), "edge-1"),
    ]

    forward = build_analysis_dedup(
        members,
        relations,
        rule_version="analysis-dedup-v1",
    )
    reversed_build = build_analysis_dedup(
        list(reversed(members)),
        [
            replace(relation, left=relation.right, right=relation.left)
            for relation in reversed(relations)
        ],
        rule_version="analysis-dedup-v1",
    )

    assert forward == reversed_build
    assert len(forward.clusters) == 1
    assert forward.clusters[0].representative == (2, 1)
    assert forward.clusters[0].representative_strategy == "lowest_source_identity_v1"
    assert forward.manifest_sha256 == reversed_build.manifest_sha256


def test_human_label_conflict_marks_whole_cluster_without_propagation() -> None:
    """簇内双轴冲突使相关成员复核，但原始成员标签保持各自证据。"""

    related = _member(
        1,
        "a",
        human=("usable", "related", "human-related"),
    )
    unrelated = _member(
        2,
        "a",
        human=("usable", "unrelated", "human-unrelated"),
    )
    unlabeled = _member(3, "a")

    build = build_analysis_dedup(
        [related, unrelated, unlabeled],
        [],
        rule_version="analysis-dedup-v1",
    )
    cluster = build.clusters[0]

    assert cluster.human_label_conflict is True
    assert cluster.review_members == ((1, 1), (2, 1), (3, 1))
    assert build.review_members == cluster.review_members
    # 输入对象不可变，且输出没有任何“传播后标签”字段。
    assert related.human_tourism_label == "related"
    assert unrelated.human_tourism_label == "unrelated"
    assert unlabeled.human_tourism_label is None
    assert not hasattr(cluster, "propagated_label")


def test_same_human_labels_and_singletons_do_not_create_false_conflicts() -> None:
    """相同人工判断与无证据单例不应被误报为冲突。"""

    build = build_analysis_dedup(
        [
            _member(1, "a", human=("usable", "related", "human-1")),
            _member(2, "a", human=("usable", "related", "human-2")),
            _member(3, "b"),
        ],
        [],
        rule_version="analysis-dedup-v1",
    )

    assert all(not cluster.human_label_conflict for cluster in build.clusters)
    assert build.review_members == ()
    assert sorted(len(cluster.members) for cluster in build.clusters) == [1, 2]


def test_near_relation_must_reference_known_distinct_members() -> None:
    """未知身份和自环均被拒绝，防止数量守恒遭到破坏。"""

    members = [_member(1, "a"), _member(2, "b")]
    with pytest.raises(ValueError, match="unknown member"):
        build_analysis_dedup(
            members,
            [ConfirmedNearDuplicateRelation((1, 1), (9, 1), "edge-unknown")],
            rule_version="analysis-dedup-v1",
        )
    with pytest.raises(ValueError, match="self-loop"):
        build_analysis_dedup(
            members,
            [ConfirmedNearDuplicateRelation((1, 1), (1, 1), "edge-self")],
            rule_version="analysis-dedup-v1",
        )
