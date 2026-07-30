"""与 SQLite 无关的精确重复簇和近似重复候选计算。"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer

from .text_config import NearDuplicateRules


@dataclass(frozen=True, order=True)
class TextDocument:
    """进入重复分析的可用文本及其冻结来源身份。"""

    source_post_id: int
    source_version: int
    platform_key: str
    normalized_title: str
    normalized_body: str
    exact_canonical_sha256: str

    @property
    def near_text(self) -> str:
        """不含固定边界标记的近似比较文本，避免模板字符主导相似度。"""

        return f"{self.normalized_title}\n{self.normalized_body}".strip()


@dataclass(frozen=True)
class ExactCluster:
    """精确规范哈希相同的一组记录；单例也保留以实现完整追踪。"""

    cluster_id: str
    exact_canonical_sha256: str
    representative: TextDocument
    members: tuple[TextDocument, ...]
    is_cross_platform: bool


@dataclass(frozen=True)
class NearCandidatePair:
    """达到候选阈值的一对精确簇代表，不等同于确认重复。"""

    left_cluster_id: str
    right_cluster_id: str
    left_source_post_id: int
    right_source_post_id: int
    similarity_ppm: int
    length_ratio_ppm: int
    shared_block_key_count: int
    is_cross_platform: bool


@dataclass(frozen=True)
class NearCandidateComponent:
    """候选边形成的工作流连通分量，不传播任何清洗标签。"""

    component_id: str
    cluster_ids: tuple[str, ...]
    members: tuple[tuple[str, TextDocument], ...]
    is_cross_platform: bool


@dataclass(frozen=True)
class DuplicatePlan:
    """一次纯计算所得的精确簇、候选对、候选分量和输出哈希。"""

    exact_clusters: tuple[ExactCluster, ...]
    near_pairs: tuple[NearCandidatePair, ...]
    near_components: tuple[NearCandidateComponent, ...]
    library_versions: Mapping[str, str]
    output_sha256: str


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _char_ngrams(text: str, lower: int, upper: int) -> set[str]:
    """生成用于阻塞的字符 n-gram 集合；不跨过标题正文的显式换行。"""

    grams: set[str] = set()
    for field in text.split("\n"):
        for width in range(lower, upper + 1):
            if len(field) < width:
                continue
            grams.update(field[index : index + width] for index in range(len(field) - width + 1))
    return grams


def _build_exact_clusters(documents: Sequence[TextDocument]) -> tuple[ExactCluster, ...]:
    grouped: dict[str, list[TextDocument]] = defaultdict(list)
    for document in sorted(documents):
        grouped[document.exact_canonical_sha256].append(document)
    clusters: list[ExactCluster] = []
    for exact_sha256, members_list in sorted(grouped.items()):
        members = tuple(sorted(members_list))
        cluster_id = _sha256({"exact_canonical_sha256": exact_sha256})[:32]
        clusters.append(
            ExactCluster(
                cluster_id=cluster_id,
                exact_canonical_sha256=exact_sha256,
                representative=members[0],
                members=members,
                is_cross_platform=len({member.platform_key for member in members}) > 1,
            )
        )
    return tuple(sorted(clusters, key=lambda cluster: cluster.cluster_id))


def _blocked_pairs(
    clusters: Sequence[ExactCluster],
    rules: NearDuplicateRules,
) -> dict[tuple[int, int], tuple[int, int]]:
    """用稀有共享 n-gram 形成确定性候选阻塞，返回长度比和共享键数。"""

    representatives = [cluster.representative for cluster in clusters]
    grams_by_document = [
        _char_ngrams(document.near_text, *rules.ngram_range) for document in representatives
    ]
    frequencies = Counter(gram for grams in grams_by_document for gram in grams)
    document_count = len(representatives)
    ratio_cap = math.floor(document_count * rules.blocking_df_ratio_ppm / 1_000_000)
    df_cap = max(2, min(rules.blocking_df_cap, ratio_cap))
    selected_by_document: list[tuple[str, ...]] = []
    for grams in grams_by_document:
        eligible = [gram for gram in grams if 2 <= frequencies[gram] <= df_cap]
        eligible.sort(key=lambda gram: (frequencies[gram], -len(gram), gram))
        selected_by_document.append(tuple(eligible[: rules.blocking_keys_per_document]))

    inverted: dict[str, list[int]] = defaultdict(list)
    for index, keys in enumerate(selected_by_document):
        for key in keys:
            inverted[key].append(index)
    shared_counts: Counter[tuple[int, int]] = Counter()
    for key in sorted(inverted):
        for pair in itertools.combinations(sorted(inverted[key]), 2):
            shared_counts[pair] += 1

    blocked: dict[tuple[int, int], tuple[int, int]] = {}
    for pair, shared_count in sorted(shared_counts.items()):
        left_length = len(representatives[pair[0]].near_text)
        right_length = len(representatives[pair[1]].near_text)
        maximum = max(left_length, right_length)
        length_ratio_ppm = 1_000_000 if maximum == 0 else min(left_length, right_length) * 1_000_000 // maximum
        if length_ratio_ppm >= rules.length_ratio_min_ppm:
            blocked[pair] = (length_ratio_ppm, shared_count)
    return blocked


def _near_pairs(
    clusters: Sequence[ExactCluster],
    rules: NearDuplicateRules,
) -> tuple[NearCandidatePair, ...]:
    representatives = [cluster.representative for cluster in clusters]
    if len(representatives) < 2:
        return ()
    blocked = _blocked_pairs(clusters, rules)
    if not blocked:
        return ()
    # 用整数文档上限消除小语料下 max_df 比例低于 min_df 的歧义。
    max_df_documents = max(rules.min_df, math.floor(len(representatives) * rules.max_df_ratio))
    vectorizer = TfidfVectorizer(
        analyzer=rules.analyzer,
        ngram_range=rules.ngram_range,
        min_df=rules.min_df,
        max_df=max_df_documents,
        sublinear_tf=rules.sublinear_tf,
        smooth_idf=rules.smooth_idf,
        lowercase=False,
        norm="l2",
        dtype=np.float64,
    )
    try:
        matrix = vectorizer.fit_transform(document.near_text for document in representatives)
    except ValueError:
        # 全部文本过短或特征被频率规则排空时，没有可复核候选，而不是任务失败。
        return ()
    candidates: list[NearCandidatePair] = []
    for (left_index, right_index), (length_ratio_ppm, shared_count) in sorted(blocked.items()):
        similarity = float(matrix[left_index].multiply(matrix[right_index]).sum())
        similarity_ppm = min(1_000_000, max(0, int(math.floor(similarity * 1_000_000 + 0.5))))
        if similarity_ppm < rules.candidate_threshold_ppm:
            continue
        left_cluster = clusters[left_index]
        right_cluster = clusters[right_index]
        if left_cluster.cluster_id > right_cluster.cluster_id:
            left_cluster, right_cluster = right_cluster, left_cluster
        candidates.append(
            NearCandidatePair(
                left_cluster_id=left_cluster.cluster_id,
                right_cluster_id=right_cluster.cluster_id,
                left_source_post_id=left_cluster.representative.source_post_id,
                right_source_post_id=right_cluster.representative.source_post_id,
                similarity_ppm=similarity_ppm,
                length_ratio_ppm=length_ratio_ppm,
                shared_block_key_count=shared_count,
                is_cross_platform=(
                    left_cluster.representative.platform_key
                    != right_cluster.representative.platform_key
                ),
            )
        )
    return tuple(sorted(candidates, key=lambda pair: (pair.left_cluster_id, pair.right_cluster_id)))


class _DisjointSet:
    """按字典序稳定选根的最小并查集，仅服务候选连通分量。"""

    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        root, child = sorted((left_root, right_root))
        self.parent[child] = root


def _near_components(
    clusters: Sequence[ExactCluster],
    pairs: Sequence[NearCandidatePair],
) -> tuple[NearCandidateComponent, ...]:
    by_id = {cluster.cluster_id: cluster for cluster in clusters}
    disjoint = _DisjointSet(by_id)
    for pair in pairs:
        disjoint.union(pair.left_cluster_id, pair.right_cluster_id)
    grouped: dict[str, list[str]] = defaultdict(list)
    for cluster_id in sorted(by_id):
        grouped[disjoint.find(cluster_id)].append(cluster_id)
    components: list[NearCandidateComponent] = []
    for cluster_ids_list in grouped.values():
        cluster_ids = tuple(sorted(cluster_ids_list))
        members = tuple(
            (cluster_id, member)
            for cluster_id in cluster_ids
            for member in by_id[cluster_id].members
        )
        component_id = _sha256({"cluster_ids": cluster_ids})[:32]
        components.append(
            NearCandidateComponent(
                component_id=component_id,
                cluster_ids=cluster_ids,
                members=members,
                is_cross_platform=len({member.platform_key for _, member in members}) > 1,
            )
        )
    return tuple(sorted(components, key=lambda component: component.component_id))


def build_duplicate_plan(
    documents: Sequence[TextDocument],
    rules: NearDuplicateRules,
) -> DuplicatePlan:
    """对完整显式语料构建可复现候选；输入不得含结构不可用记录。"""

    identities = [(item.source_post_id, item.source_version) for item in documents]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate document identities")
    if any(not item.exact_canonical_sha256 for item in documents):
        raise ValueError("exact canonical sha256 is required")
    clusters = _build_exact_clusters(documents)
    pairs = _near_pairs(clusters, rules)
    components = _near_components(clusters, pairs)
    versions = {"numpy": np.__version__, "scikit-learn": sklearn.__version__}
    output = {
        "components": [
            {
                "component_id": component.component_id,
                "cluster_ids": component.cluster_ids,
                "members": [
                    [cluster_id, member.source_post_id, member.source_version]
                    for cluster_id, member in component.members
                ],
            }
            for component in components
        ],
        "exact_clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "exact_canonical_sha256": cluster.exact_canonical_sha256,
                "members": [
                    [member.source_post_id, member.source_version, member.platform_key]
                    for member in cluster.members
                ],
            }
            for cluster in clusters
        ],
        "near_pairs": [pair.__dict__ for pair in pairs],
        "rules": rules.__dict__,
        "versions": versions,
    }
    return DuplicatePlan(clusters, pairs, components, versions, _sha256(output))
