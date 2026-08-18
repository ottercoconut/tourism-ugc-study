"""分析视图确认重复关系的 SQLite 仓储。

仓储只消费一个显式、已封存的最终帖子决定构建和同运行的文本候选构建。
规范文本完全同一关系来自候选构建的 exact cluster；近重复关系只接受调用方
逐 ID 选中的人工 ``duplicate`` 仲裁。候选 pair/component 和 leakage component
不在查询或 API 中出现，因而不能泄漏为分析去重真值。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .analysis_dedup import (
    AnalysisDedupBuild,
    AnalysisDedupMember,
    ConfirmedNearDuplicateRelation,
    build_analysis_dedup,
)
from .schema import connect_derived, migrate_derived


class AnalysisDedupRepositoryError(RuntimeError):
    """分析去重输入或封存谱系失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("analysis dedup repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class AnalysisDedupBuildResult:
    """一个已封存分析去重构建的身份、守恒计数和 manifest。"""

    dedup_build_id: str
    expected_eligible_count: int
    edge_count: int
    cluster_count: int
    member_count: int
    representative_count: int
    input_manifest_sha256: str
    member_manifest_sha256: str


@dataclass(frozen=True)
class _AllowedEdge:
    """一条将写入 schema v24 的可验证 exact 或人工 duplicate 边。"""

    left: tuple[int, int]
    right: tuple[int, int]
    relation_kind: str
    exact_cluster_id: str | None
    adjudication_id: str | None
    reason_code: str


def _utcnow() -> str:
    """返回秒级 UTC 创建时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(value: object) -> str:
    """以规范 JSON 计算排序稳定的 SHA-256。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _raise(reason_code: str) -> None:
    """集中抛出不泄露 SQLite 内容的稳定异常。"""

    raise AnalysisDedupRepositoryError(reason_code)


def _lineage(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    post_decision_build_id: str,
    candidate_build_id: str,
) -> None:
    """验证最终决定与文本候选构建同运行、同快照且均已封存。"""

    row = connection.execute(
        """
        SELECT 1
        FROM post_decision_builds AS p
        JOIN text_candidate_builds AS c ON c.build_id = ?
        WHERE p.decision_build_id = ? AND p.run_id = ?
          AND p.build_kind = 'final' AND p.seal_status = 'finalized'
          AND c.run_id = p.run_id AND c.source_snapshot_id = p.source_snapshot_id
          AND c.status = 'finalized'
        """,
        (candidate_build_id, post_decision_build_id, run_id),
    ).fetchone()
    if row is None:
        _raise("analysis_dedup_lineage_invalid")


def _final_population_members(
    connection: sqlite3.Connection,
    *,
    post_decision_build_id: str,
    candidate_build_id: str,
) -> tuple[
    tuple[AnalysisDedupMember, ...],
    dict[str, tuple[tuple[int, int], ...]],
    frozenset[tuple[int, int]],
]:
    """加载完整 final population 的 exact 关系、人工决定与 keep 身份。

    冲突检查必须先看到 exclude/review 成员，不能先用 ``decision_action``
    过滤。没有 exact cluster 的非 keep 成员无法参与被选重复关系，可安全留在
    关系图外；每个 keep 版本仍必须属于显式候选构建的 exact cluster。
    """

    rows = tuple(
        connection.execute(
            """
            SELECT d.source_post_id, d.source_version, d.decision_action,
                   d.provenance,
                   c.cluster_id, c.exact_canonical_sha256,
                   a.tourism_label AS human_tourism_label,
                   a.adjudication_id AS human_evidence_id
            FROM post_decisions AS d
            LEFT JOIN text_exact_cluster_members AS m
              ON m.build_id = ?
             AND m.source_post_id = d.source_post_id
             AND m.source_version = d.source_version
            LEFT JOIN text_exact_clusters AS c
              ON c.build_id = m.build_id AND c.cluster_id = m.cluster_id
            LEFT JOIN post_decision_evidence_links AS l
              ON l.decision_id = d.decision_id
             AND l.evidence_kind = 'human_adjudication'
             AND d.provenance = 'human_adjudication'
            LEFT JOIN text_post_adjudications AS a
              ON a.adjudication_id = l.evidence_id
            WHERE d.decision_build_id = ?
            ORDER BY d.source_post_id, d.source_version, a.adjudication_id
            """,
            (candidate_build_id, post_decision_build_id),
        )
    )
    grouped: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in rows:
        identity = int(row["source_post_id"]), int(row["source_version"])
        grouped.setdefault(identity, []).append(row)
    members: list[AnalysisDedupMember] = []
    exact_members: dict[str, list[tuple[int, int]]] = {}
    keep_identities: set[tuple[int, int]] = set()
    for identity, identity_rows in sorted(grouped.items()):
        first = identity_rows[0]
        if first["decision_action"] == "keep":
            keep_identities.add(identity)
        if first["cluster_id"] is None or first["exact_canonical_sha256"] is None:
            if first["decision_action"] == "keep":
                _raise("analysis_dedup_member_outside_candidate_build")
            continue
        human_rows = [row for row in identity_rows if row["human_evidence_id"] is not None]
        if len(human_rows) > 1:
            _raise("analysis_dedup_human_evidence_not_unique")
        human = human_rows[0] if human_rows else None
        members.append(
            AnalysisDedupMember(
                identity[0],
                identity[1],
                str(first["exact_canonical_sha256"]),
                (
                    str(human["human_tourism_label"])  # type: ignore[arg-type]
                    if human is not None
                    else None
                ),
                str(human["human_evidence_id"]) if human is not None else None,
            )
        )
        exact_members.setdefault(str(first["cluster_id"]), []).append(identity)
    return (
        tuple(members),
        {
            cluster_id: tuple(sorted(identities))
            for cluster_id, identities in exact_members.items()
        },
        frozenset(keep_identities),
    )


def _human_relations(
    connection: sqlite3.Connection,
    *,
    candidate_build_id: str,
    adjudication_ids: Sequence[str],
    exact_members: dict[str, tuple[tuple[int, int], ...]],
    skip_outside_population: bool = False,
) -> tuple[ConfirmedNearDuplicateRelation, ...]:
    """验证显式 duplicate 仲裁并选择两侧的稳定 eligible 连接端点。"""

    if not adjudication_ids:
        return ()
    if any(not value for value in adjudication_ids) or len(adjudication_ids) != len(
        set(adjudication_ids)
    ):
        _raise("analysis_dedup_adjudication_identity_invalid")
    placeholders = ",".join("?" for _ in adjudication_ids)
    rows = tuple(
        connection.execute(
            f"""
            SELECT adjudication_id, build_id, left_cluster_id, right_cluster_id,
                   decision
            FROM text_near_duplicate_adjudications
            WHERE adjudication_id IN ({placeholders})
            ORDER BY adjudication_id
            """,
            tuple(adjudication_ids),
        )
    )
    if len(rows) != len(adjudication_ids):
        _raise("analysis_dedup_adjudication_not_found")
    relations: list[ConfirmedNearDuplicateRelation] = []
    endpoint_pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for row in rows:
        if row["build_id"] != candidate_build_id or row["decision"] != "duplicate":
            _raise("analysis_dedup_adjudication_not_confirmed_duplicate")
        left_members = exact_members.get(str(row["left_cluster_id"]), ())
        right_members = exact_members.get(str(row["right_cluster_id"]), ())
        if not left_members or not right_members:
            if skip_outside_population:
                continue
            _raise("analysis_dedup_adjudication_outside_eligible_population")
        left, right = sorted((left_members[0], right_members[0]))
        if (left, right) in endpoint_pairs:
            _raise("analysis_dedup_duplicate_relation_repeated")
        endpoint_pairs.add((left, right))
        relations.append(
            ConfirmedNearDuplicateRelation(
                left,
                right,
                str(row["adjudication_id"]),
            )
        )
    return tuple(relations)


def _allowed_edges(
    exact_members: dict[str, tuple[tuple[int, int], ...]],
    relations: Sequence[ConfirmedNearDuplicateRelation],
) -> tuple[_AllowedEdge, ...]:
    """把 exact 簇压成星形边，并追加逐条人工 duplicate 边。"""

    edges: list[_AllowedEdge] = []
    for cluster_id, identities in sorted(exact_members.items()):
        if not identities:
            continue
        for identity in identities[1:]:
            left, right = sorted((identities[0], identity))
            edges.append(
                _AllowedEdge(
                    left,
                    right,
                    "exact",
                    cluster_id,
                    None,
                    "exact_canonical_sha256_equal",
                )
            )
    edges.extend(
        _AllowedEdge(
            relation.left,
            relation.right,
            "human_duplicate",
            None,
            relation.evidence_id,
            "human_adjudicated_duplicate",
        )
        for relation in relations
    )
    identities = [(edge.left, edge.right) for edge in edges]
    if len(identities) != len(set(identities)):
        _raise("analysis_dedup_edge_identity_conflict")
    return tuple(
        sorted(
            edges,
            key=lambda edge: (
                edge.left,
                edge.right,
                edge.relation_kind,
                edge.adjudication_id or "",
            ),
        )
    )


def _input_manifest(
    *,
    run_id: str,
    post_decision_build_id: str,
    candidate_build_id: str,
    dedup_version: str,
    members: Sequence[AnalysisDedupMember],
    adjudication_ids: Sequence[str],
) -> str:
    """计算显式构建请求及全部成员证据的稳定 manifest。"""

    return _sha256(
        {
            "candidate_build_id": candidate_build_id,
            "dedup_version": dedup_version,
            "duplicate_adjudication_ids": sorted(adjudication_ids),
            "members": [
                {
                    "exact_canonical_sha256": member.exact_canonical_sha256,
                    "human_evidence_id": member.human_evidence_id,
                    "human_tourism_label": member.human_tourism_label,
                    "source_post_id": member.source_post_id,
                    "source_version": member.source_version,
                }
                for member in sorted(members)
            ],
            "post_decision_build_id": post_decision_build_id,
            "run_id": run_id,
            "schema": "analysis-dedup-request-v1",
        }
    )


def _stored_result(
    connection: sqlite3.Connection,
    *,
    input_manifest: str,
    pure_build: AnalysisDedupBuild,
    edge_count: int,
) -> AnalysisDedupBuildResult | None:
    """复验相同完整请求的封存构建，不按 latest 或部分字段复用。"""

    row = connection.execute(
        "SELECT * FROM text_dedup_builds WHERE input_manifest_sha256 = ?",
        (input_manifest,),
    ).fetchone()
    if row is None:
        return None
    if (
        row["seal_status"] != "finalized"
        or row["member_manifest_sha256"] != pure_build.manifest_sha256
        or int(row["edge_count"]) != edge_count
        or int(row["cluster_count"]) != len(pure_build.clusters)
        or int(row["member_count"])
        != sum(len(cluster.members) for cluster in pure_build.clusters)
    ):
        _raise("stored_analysis_dedup_build_mismatch")
    representatives = connection.execute(
        """
        SELECT COUNT(*) FROM text_dedup_members
        WHERE dedup_build_id = ? AND is_representative = 1
        """,
        (row["dedup_build_id"],),
    ).fetchone()[0]
    if int(representatives) != len(pure_build.clusters):
        _raise("stored_analysis_dedup_representative_mismatch")
    return AnalysisDedupBuildResult(
        str(row["dedup_build_id"]),
        int(row["expected_eligible_count"]),
        int(row["edge_count"]),
        int(row["cluster_count"]),
        int(row["member_count"]),
        int(row["representative_count"]),
        str(row["input_manifest_sha256"]),
        str(row["member_manifest_sha256"]),
    )


def build_analysis_dedup_snapshot(
    derived_db: str | Path,
    *,
    run_id: str,
    post_decision_build_id: str,
    candidate_build_id: str,
    dedup_version: str,
    duplicate_adjudication_ids: Sequence[str] = (),
) -> AnalysisDedupBuildResult:
    """构建并封存最终 keep 人口的分析去重簇。

    所有最终 keep 成员恰好进入一个簇，每簇按纯领域规则选择唯一稳定代表。
    人工标签若在确认重复闭包内冲突，本次构建在写入前拒绝，要求先重建帖子
    决定；仓储绝不沿重复边传播标签。相同完整请求复验后幂等返回。
    """

    if not run_id or not post_decision_build_id or not candidate_build_id or not dedup_version:
        _raise("analysis_dedup_request_invalid")
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            with connection:
                _lineage(
                    connection,
                    run_id=run_id,
                    post_decision_build_id=post_decision_build_id,
                    candidate_build_id=candidate_build_id,
                )
                population_members, population_exact_members, keep_identities = (
                    _final_population_members(
                        connection,
                        post_decision_build_id=post_decision_build_id,
                        candidate_build_id=candidate_build_id,
                    )
                )
                population_relations = _human_relations(
                    connection,
                    candidate_build_id=candidate_build_id,
                    adjudication_ids=duplicate_adjudication_ids,
                    exact_members=population_exact_members,
                )
                # 先以完整 final population 重建被选 exact/人工 duplicate 闭包。
                # excluded/review 成员若与 keep 的人工双轴决定冲突，必须重建帖子
                # 决定；不能先过滤后让冲突从分析视图中消失。
                population_check = build_analysis_dedup(
                    population_members,
                    population_relations,
                    rule_version=dedup_version,
                )
                if population_check.review_members:
                    _raise(
                        "analysis_dedup_human_conflict_requires_post_decision_rebuild"
                    )
                members = tuple(
                    member
                    for member in population_members
                    if member.identity in keep_identities
                )
                exact_members = {
                    cluster_id: tuple(
                        identity
                        for identity in identities
                        if identity in keep_identities
                    )
                    for cluster_id, identities in population_exact_members.items()
                    if any(identity in keep_identities for identity in identities)
                }
                relations = _human_relations(
                    connection,
                    candidate_build_id=candidate_build_id,
                    adjudication_ids=duplicate_adjudication_ids,
                    exact_members=exact_members,
                    skip_outside_population=True,
                )
                pure_build = build_analysis_dedup(
                    members,
                    relations,
                    rule_version=dedup_version,
                )
                if pure_build.review_members:
                    _raise("analysis_dedup_human_conflict_requires_post_decision_rebuild")
                edges = _allowed_edges(exact_members, relations)
                input_manifest = _input_manifest(
                    run_id=run_id,
                    post_decision_build_id=post_decision_build_id,
                    candidate_build_id=candidate_build_id,
                    dedup_version=dedup_version,
                    members=members,
                    adjudication_ids=duplicate_adjudication_ids,
                )
                stored = _stored_result(
                    connection,
                    input_manifest=input_manifest,
                    pure_build=pure_build,
                    edge_count=len(edges),
                )
                if stored is not None:
                    return stored
                dedup_build_id = _sha256(
                    ["analysis-dedup-build-v1", input_manifest, pure_build.manifest_sha256]
                )[:32]
                member_count = sum(len(cluster.members) for cluster in pure_build.clusters)
                now = _utcnow()
                connection.execute(
                    """
                    INSERT INTO text_dedup_builds(
                      dedup_build_id, run_id, post_decision_build_id,
                      candidate_build_id, dedup_version, selection_strategy,
                      expected_eligible_count, edge_count, cluster_count,
                      member_count, representative_count, input_manifest_sha256,
                      member_manifest_sha256, seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, 'lowest_source_identity_v1', ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        dedup_build_id,
                        run_id,
                        post_decision_build_id,
                        candidate_build_id,
                        dedup_version,
                        len(members),
                        len(edges),
                        len(pure_build.clusters),
                        member_count,
                        len(pure_build.clusters),
                        input_manifest,
                        pure_build.manifest_sha256,
                        now,
                    ),
                )
                for edge in edges:
                    edge_payload = {
                        "adjudication_id": edge.adjudication_id,
                        "exact_cluster_id": edge.exact_cluster_id,
                        "left": edge.left,
                        "relation_kind": edge.relation_kind,
                        "right": edge.right,
                    }
                    connection.execute(
                        """
                        INSERT INTO text_dedup_edges(
                          edge_id, dedup_build_id, left_source_post_id,
                          left_source_version, right_source_post_id,
                          right_source_version, relation_kind, exact_cluster_id,
                          adjudication_id, reason_code, edge_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _sha256(["analysis-dedup-edge-v1", dedup_build_id, edge_payload])[:32],
                            dedup_build_id,
                            edge.left[0],
                            edge.left[1],
                            edge.right[0],
                            edge.right[1],
                            edge.relation_kind,
                            edge.exact_cluster_id,
                            edge.adjudication_id,
                            edge.reason_code,
                            _sha256(edge_payload),
                        ),
                    )
                for cluster in pure_build.clusters:
                    cluster_manifest = _sha256(
                        [
                            {
                                "is_representative": identity == cluster.representative,
                                "source_post_id": identity[0],
                                "source_version": identity[1],
                            }
                            for identity in cluster.members
                        ]
                    )
                    connection.execute(
                        """
                        INSERT INTO text_dedup_clusters(
                          dedup_build_id, cluster_id,
                          representative_source_post_id,
                          representative_source_version, member_count,
                          representative_reason_code, member_manifest_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dedup_build_id,
                            cluster.cluster_id,
                            cluster.representative[0],
                            cluster.representative[1],
                            len(cluster.members),
                            "lowest_source_identity_v1",
                            cluster_manifest,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO text_dedup_members(
                          dedup_build_id, cluster_id, source_post_id,
                          source_version, is_representative, selection_reason_code
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                dedup_build_id,
                                cluster.cluster_id,
                                identity[0],
                                identity[1],
                                int(identity == cluster.representative),
                                (
                                    "selected_lowest_source_identity"
                                    if identity == cluster.representative
                                    else "confirmed_duplicate_non_representative"
                                ),
                            )
                            for identity in cluster.members
                        ],
                    )
                connection.execute(
                    """
                    UPDATE text_dedup_builds SET seal_status = 'finalized'
                    WHERE dedup_build_id = ? AND seal_status = 'building'
                    """,
                    (dedup_build_id,),
                )
                return AnalysisDedupBuildResult(
                    dedup_build_id,
                    len(members),
                    len(edges),
                    len(pure_build.clusters),
                    member_count,
                    len(pure_build.clusters),
                    input_manifest,
                    pure_build.manifest_sha256,
                )
    except AnalysisDedupRepositoryError:
        raise
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise AnalysisDedupRepositoryError("analysis_dedup_contract_rejected") from exc
