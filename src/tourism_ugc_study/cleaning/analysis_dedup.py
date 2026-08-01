"""分析视图使用的确认重复关系与稳定代表项计算。

近重复候选、候选连通分量及训练泄漏分量只用于组织复核或隔离数据，均不
是分析去重真值。本模块只接收规范文本完全同一的哈希，以及明确由人工仲裁
为 ``duplicate`` 的近重复边，再从这些关系重新计算连通分量。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence


PostIdentity = tuple[int, int]
HumanStructureLabel = Literal["usable", "invalid", "uncertain"]
HumanTourismLabel = Literal[
    "related", "unrelated", "uncertain", "not_applicable"
]


@dataclass(frozen=True, order=True)
class AnalysisDedupMember:
    """去重构建中的帖子身份、精确哈希和可选人工双轴决定。

    人工标签仅用于发现簇内冲突，绝不会沿重复边传播。没有人工决定的成员
    应将三个 ``human_*`` 字段全部设为 ``None``。
    """

    source_post_id: int
    source_version: int
    exact_canonical_sha256: str
    human_structure_label: HumanStructureLabel | None = None
    human_tourism_label: HumanTourismLabel | None = None
    human_evidence_id: str | None = None

    @property
    def identity(self) -> PostIdentity:
        """返回不含文本和平台标识的稳定帖子版本身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class ConfirmedNearDuplicateRelation:
    """一条显式人工确认的近重复边。

    ``evidence_kind`` 和 ``adjudication`` 故意保留在领域输入中，让候选 pair、
    component 或未确认边无法伪装成已确认关系进入构建。
    """

    left: PostIdentity
    right: PostIdentity
    evidence_id: str
    evidence_kind: str = "human_adjudication"
    adjudication: str = "duplicate"


@dataclass(frozen=True)
class AnalysisDedupCluster:
    """由确认关系重建的单个簇及其唯一代表和冲突状态。"""

    cluster_id: str
    members: tuple[PostIdentity, ...]
    representative: PostIdentity
    representative_strategy: str
    representative_reason: str
    relation_evidence_ids: tuple[str, ...]
    human_label_conflict: bool
    review_members: tuple[PostIdentity, ...]


@dataclass(frozen=True)
class AnalysisDedupBuild:
    """一次纯计算得到的完整分析去重构建。"""

    rule_version: str
    clusters: tuple[AnalysisDedupCluster, ...]
    review_members: tuple[PostIdentity, ...]
    manifest_sha256: str


def _sha256(payload: object) -> str:
    """计算排序稳定、与 Python 对象地址无关的 SHA-256。"""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _DisjointSet:
    """以最小身份为根的确定性并查集，只服务确认关系闭包。"""

    def __init__(self, identities: Iterable[PostIdentity]) -> None:
        self._parents = {identity: identity for identity in identities}

    def find(self, identity: PostIdentity) -> PostIdentity:
        """查找并压缩路径；未知身份由调用方在合并前拒绝。"""

        parent = self._parents[identity]
        if parent != identity:
            self._parents[identity] = self.find(parent)
        return self._parents[identity]

    def union(self, left: PostIdentity, right: PostIdentity) -> None:
        """合并两组并稳定选择字典序更小的根。"""

        left_root = self.find(left)
        right_root = self.find(right)
        root, child = sorted((left_root, right_root))
        self._parents[child] = root


def _validate_sha256(value: str) -> None:
    """验证规范文本哈希，避免空哈希把无关记录合为一簇。"""

    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("exact canonical sha256 must be lowercase hexadecimal")


def _validate_member(member: AnalysisDedupMember) -> None:
    """验证成员身份和可选人工双轴证据的完整性。"""

    if member.source_post_id <= 0 or member.source_version <= 0:
        raise ValueError("positive source post identity is required")
    _validate_sha256(member.exact_canonical_sha256)
    human_fields = (
        member.human_structure_label,
        member.human_tourism_label,
        member.human_evidence_id,
    )
    if all(value is None for value in human_fields):
        return
    if any(value is None for value in human_fields):
        raise ValueError("human decision fields must be provided together")
    if member.human_structure_label not in {"usable", "invalid", "uncertain"}:
        raise ValueError("unsupported human structure label")
    if member.human_tourism_label not in {
        "related",
        "unrelated",
        "uncertain",
        "not_applicable",
    }:
        raise ValueError("unsupported human tourism label")
    if not member.human_evidence_id:
        raise ValueError("human evidence id is required")
    if (member.human_structure_label == "invalid") != (
        member.human_tourism_label == "not_applicable"
    ):
        raise ValueError("tourism applicability conflicts with structure label")


def _validate_relations(
    relations: Sequence[ConfirmedNearDuplicateRelation],
    identities: set[PostIdentity],
) -> tuple[ConfirmedNearDuplicateRelation, ...]:
    """拒绝候选泄漏、未知成员、自环和重复证据身份。"""

    evidence_ids: set[str] = set()
    normalized: list[ConfirmedNearDuplicateRelation] = []
    for relation in relations:
        if relation.evidence_kind != "human_adjudication":
            raise ValueError("candidate or component evidence cannot enter analysis dedup")
        if relation.adjudication != "duplicate":
            raise ValueError("near duplicate relation must be explicitly adjudicated duplicate")
        if not relation.evidence_id or relation.evidence_id in evidence_ids:
            raise ValueError("near duplicate evidence ids must be non-empty and unique")
        if relation.left not in identities or relation.right not in identities:
            raise ValueError("near duplicate relation references an unknown member")
        if relation.left == relation.right:
            raise ValueError("near duplicate relation cannot be a self-loop")
        evidence_ids.add(relation.evidence_id)
        left, right = sorted((relation.left, relation.right))
        normalized.append(
            ConfirmedNearDuplicateRelation(
                left=left,
                right=right,
                evidence_id=relation.evidence_id,
                evidence_kind=relation.evidence_kind,
                adjudication=relation.adjudication,
            )
        )
    return tuple(
        sorted(normalized, key=lambda item: (item.left, item.right, item.evidence_id))
    )


def build_analysis_dedup(
    members: Sequence[AnalysisDedupMember],
    confirmed_near_relations: Sequence[ConfirmedNearDuplicateRelation],
    *,
    rule_version: str,
) -> AnalysisDedupBuild:
    """从精确哈希和人工确认边构建分析去重簇。

    所有输入成员都进入且只进入一个簇，单例也保留。每簇按帖子身份升序
    选择唯一代表；这一策略只决定分析视图显示谁，不修改帖子清洗决定。
    如果簇内存在两个不同的显式人工双轴决定，则全簇成员列入复核，函数
    不会用代表项覆盖其他成员的标签。

    输入不合法、关系不是显式人工 ``duplicate`` 或包含候选/component 时
    抛出 ``ValueError``，从接口边界阻止候选关系泄漏为分析真值。
    """

    if not rule_version:
        raise ValueError("analysis dedup rule version is required")
    for member in members:
        _validate_member(member)
    by_identity = {member.identity: member for member in members}
    if len(by_identity) != len(members):
        raise ValueError("duplicate analysis dedup member identity")
    relations = _validate_relations(
        confirmed_near_relations,
        set(by_identity),
    )

    disjoint = _DisjointSet(by_identity)
    by_exact_sha: dict[str, list[PostIdentity]] = defaultdict(list)
    for member in sorted(members):
        by_exact_sha[member.exact_canonical_sha256].append(member.identity)
    for exact_members in by_exact_sha.values():
        # 以首项为中心连接即可表达完全同一关系，避免生成 O(n²) 的隐式边。
        for identity in exact_members[1:]:
            disjoint.union(exact_members[0], identity)
    for relation in relations:
        disjoint.union(relation.left, relation.right)

    grouped: dict[PostIdentity, list[PostIdentity]] = defaultdict(list)
    for identity in sorted(by_identity):
        grouped[disjoint.find(identity)].append(identity)
    relation_ids_by_root: dict[PostIdentity, list[str]] = defaultdict(list)
    for relation in relations:
        relation_ids_by_root[disjoint.find(relation.left)].append(relation.evidence_id)

    clusters: list[AnalysisDedupCluster] = []
    all_review_members: set[PostIdentity] = set()
    for identities_list in grouped.values():
        identities = tuple(sorted(identities_list))
        representative = identities[0]
        human_labels = {
            (
                by_identity[identity].human_structure_label,
                by_identity[identity].human_tourism_label,
            )
            for identity in identities
            if by_identity[identity].human_evidence_id is not None
        }
        conflict = len(human_labels) > 1
        review_members = identities if conflict else ()
        all_review_members.update(review_members)
        relation_evidence_ids = tuple(
            sorted(relation_ids_by_root[disjoint.find(representative)])
        )
        cluster_payload = {
            "members": identities,
            "relation_evidence_ids": relation_evidence_ids,
            "rule_version": rule_version,
        }
        clusters.append(
            AnalysisDedupCluster(
                cluster_id=_sha256(cluster_payload)[:32],
                members=identities,
                representative=representative,
                representative_strategy="lowest_source_identity_v1",
                representative_reason=(
                    "按 source_post_id、source_version 升序选择稳定代表项"
                ),
                relation_evidence_ids=relation_evidence_ids,
                human_label_conflict=conflict,
                review_members=review_members,
            )
        )
    ordered_clusters = tuple(sorted(clusters, key=lambda item: item.cluster_id))
    ordered_review_members = tuple(sorted(all_review_members))
    manifest_payload = {
        "clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "human_label_conflict": cluster.human_label_conflict,
                "members": cluster.members,
                "relation_evidence_ids": cluster.relation_evidence_ids,
                "representative": cluster.representative,
                "representative_reason": cluster.representative_reason,
                "representative_strategy": cluster.representative_strategy,
                "review_members": cluster.review_members,
            }
            for cluster in ordered_clusters
        ],
        "members": [
            {
                "exact_canonical_sha256": member.exact_canonical_sha256,
                "human_evidence_id": member.human_evidence_id,
                "human_structure_label": member.human_structure_label,
                "human_tourism_label": member.human_tourism_label,
                "identity": member.identity,
            }
            for member in sorted(members)
        ],
        "relations": [
            {
                "adjudication": relation.adjudication,
                "evidence_id": relation.evidence_id,
                "evidence_kind": relation.evidence_kind,
                "left": relation.left,
                "right": relation.right,
            }
            for relation in relations
        ],
        "rule_version": rule_version,
    }
    return AnalysisDedupBuild(
        rule_version=rule_version,
        clusters=ordered_clusters,
        review_members=ordered_review_members,
        manifest_sha256=_sha256(manifest_payload),
    )
