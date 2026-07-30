"""由作者、精确重复和人工确认近重复构建训练泄漏分量。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived


class LeakageGroupError(RuntimeError):
    """泄漏输入不完整、含候选而非确认关系或身份冲突时抛出。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("leakage group construction failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class LeakagePost:
    """泄漏分组所需的最小去标识化帖子投影。"""

    source_post_id: int
    source_version: int
    author_sha256: str
    author_identity_present: bool
    exact_cluster_id: str


@dataclass(frozen=True)
class ConfirmedNearRelation:
    """经人工仲裁确认为重复的两个精确簇关系。"""

    adjudication_id: str
    left_cluster_id: str
    right_cluster_id: str


@dataclass(frozen=True)
class LeakageMember:
    """帖子所属稳定分量以及实际参与的边类型。"""

    component_id: str
    source_post_id: int
    source_version: int
    author_edge_used: bool
    exact_edge_used: bool
    confirmed_near_edge_used: bool


@dataclass(frozen=True)
class LeakagePlan:
    """可复现泄漏分组计划，不包含候选连通分量。"""

    members: tuple[LeakageMember, ...]
    adjudication_manifest_sha256: str
    output_sha256: str


@dataclass(frozen=True)
class LeakageBuildResult:
    """已持久化泄漏分组的身份与计数。"""

    leakage_build_id: str
    input_post_count: int
    component_count: int
    adjudication_manifest_sha256: str
    output_sha256: str


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _DisjointSet:
    """以帖子 `(id, version)` 为节点的稳定并查集。"""

    def __init__(self, values: Iterable[tuple[int, int]]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: tuple[int, int]) -> tuple[int, int]:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: tuple[int, int], right: tuple[int, int]) -> None:
        left_root, right_root = self.find(left), self.find(right)
        root, child = sorted((left_root, right_root))
        self.parent[child] = root


def build_leakage_plan(
    posts: Sequence[LeakagePost],
    confirmed_relations: Sequence[ConfirmedNearRelation],
) -> LeakagePlan:
    """合并三种确认边；作者缺失时绝不按空哈希把帖子合并。

    近似候选边和候选连通分量不在参数中，调用方只能传入具备人工仲裁 ID
    的 `ConfirmedNearRelation`。连通分量仅用于泄漏隔离，不自动成为分析去重真值。
    """

    identities = [(post.source_post_id, post.source_version) for post in posts]
    if len(identities) != len(set(identities)):
        raise LeakageGroupError("duplicate_leakage_post_identity")
    disjoint = _DisjointSet(identities)
    by_author: dict[str, list[tuple[int, int]]] = defaultdict(list)
    by_cluster: dict[str, list[tuple[int, int]]] = defaultdict(list)
    edge_flags: dict[tuple[int, int], set[str]] = defaultdict(set)
    for post in posts:
        identity = (post.source_post_id, post.source_version)
        by_cluster[post.exact_cluster_id].append(identity)
        if post.author_identity_present:
            by_author[post.author_sha256].append(identity)
    for members in by_author.values():
        if len(members) <= 1:
            continue
        for member in members:
            edge_flags[member].add("author")
            disjoint.union(members[0], member)
    for members in by_cluster.values():
        if len(members) <= 1:
            continue
        for member in members:
            edge_flags[member].add("exact")
            disjoint.union(members[0], member)
    for relation in confirmed_relations:
        left_members = by_cluster.get(relation.left_cluster_id)
        right_members = by_cluster.get(relation.right_cluster_id)
        if not left_members or not right_members:
            raise LeakageGroupError("confirmed_relation_cluster_not_found")
        for member in (*left_members, *right_members):
            edge_flags[member].add("confirmed_near")
        disjoint.union(left_members[0], right_members[0])

    grouped: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for identity in identities:
        grouped[disjoint.find(identity)].append(identity)
    component_by_identity: dict[tuple[int, int], str] = {}
    for component in grouped.values():
        ordered = sorted(component)
        component_id = _sha256(ordered)[:32]
        for identity in ordered:
            component_by_identity[identity] = component_id
    members = tuple(
        LeakageMember(
            component_id=component_by_identity[identity],
            source_post_id=identity[0],
            source_version=identity[1],
            author_edge_used="author" in edge_flags[identity],
            exact_edge_used="exact" in edge_flags[identity],
            confirmed_near_edge_used="confirmed_near" in edge_flags[identity],
        )
        for identity in sorted(identities)
    )
    adjudication_manifest = _sha256(
        [
            [item.adjudication_id, item.left_cluster_id, item.right_cluster_id]
            for item in sorted(confirmed_relations, key=lambda value: value.adjudication_id)
        ]
    )
    return LeakagePlan(
        members,
        adjudication_manifest,
        _sha256([member.__dict__ for member in members]),
    )


def create_leakage_build(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    duplicate_adjudication_ids: Sequence[str],
) -> LeakageBuildResult:
    """只消费显式列出的 `duplicate` 仲裁，创建不可变泄漏分组。"""

    if len(duplicate_adjudication_ids) != len(set(duplicate_adjudication_ids)):
        raise LeakageGroupError("duplicate_adjudication_id")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        build = connection.execute(
            """
            SELECT status, is_complete_corpus FROM text_candidate_builds WHERE build_id = ?
            """,
            (candidate_build_id,),
        ).fetchone()
        if build is None or build["status"] != "finalized" or not int(build["is_complete_corpus"]):
            raise LeakageGroupError("finalized_complete_candidate_build_required")
        posts = tuple(
            LeakagePost(
                source_post_id=int(row["source_post_id"]),
                source_version=int(row["source_version"]),
                author_sha256=str(row["author_sha256"]),
                author_identity_present=bool(row["author_identity_present"]),
                exact_cluster_id=str(row["exact_cluster_id"]),
            )
            for row in connection.execute(
                """
                SELECT c.source_post_id, c.source_version, c.exact_cluster_id,
                       v.author_sha256, v.author_identity_present
                FROM text_candidate_corpus_members AS c
                JOIN source_post_versions AS v
                  ON v.source_post_id = c.source_post_id
                 AND v.source_version = c.source_version
                WHERE c.build_id = ? AND c.structure_status = 'usable'
                ORDER BY c.source_post_id, c.source_version
                """,
                (candidate_build_id,),
            )
        )
        relations: list[ConfirmedNearRelation] = []
        if duplicate_adjudication_ids:
            placeholders = ",".join("?" for _ in duplicate_adjudication_ids)
            rows = connection.execute(
                f"""
                SELECT adjudication_id, build_id, left_cluster_id, right_cluster_id, decision
                FROM text_near_duplicate_adjudications
                WHERE adjudication_id IN ({placeholders})
                """,
                tuple(duplicate_adjudication_ids),
            ).fetchall()
            if len(rows) != len(duplicate_adjudication_ids):
                raise LeakageGroupError("duplicate_adjudication_not_found")
            for row in rows:
                if str(row["build_id"]) != candidate_build_id or row["decision"] != "duplicate":
                    raise LeakageGroupError("unconfirmed_duplicate_relation")
                relations.append(
                    ConfirmedNearRelation(
                        str(row["adjudication_id"]),
                        str(row["left_cluster_id"]),
                        str(row["right_cluster_id"]),
                    )
                )
        plan = build_leakage_plan(posts, relations)
        leakage_build_id = _sha256(
            [candidate_build_id, plan.adjudication_manifest_sha256, plan.output_sha256]
        )[:32]
        existing = connection.execute(
            "SELECT * FROM text_leakage_builds WHERE leakage_build_id = ?",
            (leakage_build_id,),
        ).fetchone()
        if existing is not None:
            return LeakageBuildResult(
                leakage_build_id,
                int(existing["input_post_count"]),
                int(existing["component_count"]),
                str(existing["adjudication_manifest_sha256"]),
                str(existing["output_sha256"]),
            )
        with connection:
            connection.execute(
                """
                INSERT INTO text_leakage_builds(
                    leakage_build_id, candidate_build_id,
                    adjudication_manifest_sha256, input_post_count,
                    component_count, output_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    leakage_build_id,
                    candidate_build_id,
                    plan.adjudication_manifest_sha256,
                    len(posts),
                    len({member.component_id for member in plan.members}),
                    plan.output_sha256,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            connection.executemany(
                """
                INSERT INTO text_leakage_members(
                    leakage_build_id, component_id, source_post_id, source_version,
                    author_edge_used, exact_edge_used, confirmed_near_edge_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        leakage_build_id,
                        member.component_id,
                        member.source_post_id,
                        member.source_version,
                        int(member.author_edge_used),
                        int(member.exact_edge_used),
                        int(member.confirmed_near_edge_used),
                    )
                    for member in plan.members
                ],
            )
        return LeakageBuildResult(
            leakage_build_id,
            len(posts),
            len({member.component_id for member in plan.members}),
            plan.adjudication_manifest_sha256,
            plan.output_sha256,
        )
