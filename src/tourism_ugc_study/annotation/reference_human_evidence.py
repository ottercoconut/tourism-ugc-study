"""人工重复决定、标签冲突确认与稳定重复分量处理。"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .reference_contract import (
    ALLOWED_LABELS,
    DuplicateCandidate,
    DuplicateDecision,
    PairIdentity,
    ReferenceDatasetError,
    ReferencePost,
    SourceIdentity,
    canonical_sha256,
)


DUPLICATE_DECISION_FIELDS: tuple[str, ...] = (
    "evidence_id",
    "left_source_post_id",
    "left_source_version",
    "right_source_post_id",
    "right_source_version",
    "similarity",
    "exact_normalized_hash_match",
    "candidate_reason_code",
    "left_normalized_model_text",
    "right_normalized_model_text",
    "decision",
    "review_status",
    "evidence_origin",
)
LABEL_RESOLUTION_FIELDS: tuple[str, ...] = (
    "component_id",
    "member_source_identities_json",
    "member_labels_json",
    "member_normalized_model_texts_json",
    "tourism_label",
    "review_status",
    "evidence_id",
)


@dataclass(frozen=True)
class DuplicateComponentPlan:
    """最终确认重复边形成的稳定连通分量计划。

    Attributes:
        component_by_identity: 每个输入身份对应的稳定分量身份。
        members_by_component: 每个分量按源身份排序的成员。
        representatives: 每个分量唯一保留的建模代表。
        excluded: 因确认重复而不进入最终参考集的成员。
        confirmed_edges: 实际形成连通关系的精确或人工确认边。
        decision_sha256: 人工决定与精确边的稳定摘要。
        member_sha256: 全部成员到分量的稳定摘要。
    """

    component_by_identity: Mapping[SourceIdentity, str]
    members_by_component: Mapping[str, tuple[ReferencePost, ...]]
    representatives: tuple[ReferencePost, ...]
    excluded: tuple[ReferencePost, ...]
    confirmed_edges: tuple[PairIdentity, ...]
    decision_sha256: str
    member_sha256: str


@dataclass(frozen=True)
class DuplicateLabelConflict:
    """需要人工最终确认旅游标签的重复分量。

    Attributes:
        component_id: 由全部成员稳定身份计算的分量 ID。
        members: 按稳定源身份排序的冲突成员。
    """

    component_id: str
    members: tuple[ReferencePost, ...]


@dataclass(frozen=True)
class _ComponentState:
    """代表选择前已经验证的重复分量内部状态。"""

    ordered_posts: tuple[ReferencePost, ...]
    candidate_by_pair: Mapping[PairIdentity, DuplicateCandidate]
    decision_by_pair: Mapping[PairIdentity, DuplicateDecision]
    exact_edges: frozenset[PairIdentity]
    confirmed_edges: frozenset[PairIdentity]
    component_by_identity: Mapping[SourceIdentity, str]
    members_by_component: Mapping[str, tuple[ReferencePost, ...]]


class _DisjointSet:
    """以稳定帖子身份为节点的确定性并查集。"""

    def __init__(self, identities: Iterable[SourceIdentity]) -> None:
        self._parent = {identity: identity for identity in identities}

    def find(self, identity: SourceIdentity) -> SourceIdentity:
        """查找并压缩身份所在分量的稳定根。

        Args:
            identity: 已存在于并查集的帖子身份。

        Returns:
            当前分量的根身份。
        """

        parent = self._parent[identity]
        if parent != identity:
            self._parent[identity] = self.find(parent)
        return self._parent[identity]

    def union(self, first: SourceIdentity, second: SourceIdentity) -> None:
        """合并两个分量并始终选择较小根，消除输入顺序影响。

        Args:
            first: 第一个端点。
            second: 第二个端点。
        """

        first_root = self.find(first)
        second_root = self.find(second)
        root, child = sorted((first_root, second_root))
        self._parent[child] = root


def _validate_complete_csv_rows(
    rows: Sequence[Mapping[str | None, str | None]],
    fields: Sequence[str],
    reason_code: str,
) -> None:
    """拒绝短行、长行或缺失单元格，避免解析器泄漏底层异常。

    Args:
        rows: ``csv.DictReader`` 产生的原始行。
        fields: 已冻结的完整字段顺序。
        reason_code: 行结构不完整时使用的稳定失败码。

    Raises:
        ReferenceDatasetError: 任一行缺字段、含额外字段或单元格为 ``None``。
    """

    expected = set(fields)
    if any(
        set(row) != expected or any(value is None for value in row.values())
        for row in rows
    ):
        raise ReferenceDatasetError(reason_code)


def _positive_int(value: str | None, reason_code: str) -> int:
    """解析 CSV 中的规范正整数。

    Args:
        value: 原始 CSV 值。
        reason_code: 解析失败时使用的稳定失败码。

    Returns:
        不含前导零的正整数。

    Raises:
        ReferenceDatasetError: 值不是规范正整数。
    """

    if value is None:
        raise ReferenceDatasetError(reason_code)
    stripped = value.strip()
    try:
        parsed = int(stripped)
    except ValueError as exc:
        raise ReferenceDatasetError(reason_code) from exc
    if parsed <= 0 or str(parsed) != stripped:
        raise ReferenceDatasetError(reason_code)
    return parsed


def load_duplicate_decisions(path: str | Path) -> tuple[DuplicateDecision, ...]:
    """读取只含人工最终结论的重复决定 CSV。

    低于0.80的人工显式重复边通过 ``evidence_origin`` 等于
    ``manual_low_similarity`` 补入。任何未最终确认行都会失败关闭，避免把草稿
    或空白决定解释为不重复。

    Args:
        path: 冻结人工决定 CSV。

    Returns:
        按无向帖子对排序的唯一最终决定。

    Raises:
        ReferenceDatasetError: 字段、状态、值、身份或唯一性不满足契约。
    """

    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != DUPLICATE_DECISION_FIELDS:
                raise ReferenceDatasetError("duplicate_decision_field_contract_mismatch")
            raw_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("duplicate_decision_csv_unreadable") from exc
    _validate_complete_csv_rows(
        raw_rows,
        DUPLICATE_DECISION_FIELDS,
        "duplicate_decision_row_structure_invalid",
    )
    decisions: list[DuplicateDecision] = []
    seen_pairs: set[PairIdentity] = set()
    seen_evidence: set[str] = set()
    for row in raw_rows:
        if row["review_status"].strip() != "finalized":
            raise ReferenceDatasetError("duplicate_decision_not_finalized")
        decision = row["decision"].strip()
        if decision not in {"duplicate", "not_duplicate"}:
            raise ReferenceDatasetError("duplicate_decision_value_invalid")
        evidence_origin = row["evidence_origin"].strip()
        if evidence_origin not in {"threshold_candidate", "manual_low_similarity"}:
            raise ReferenceDatasetError("duplicate_decision_origin_invalid")
        evidence_id = row["evidence_id"].strip()
        if not evidence_id or evidence_id in seen_evidence:
            raise ReferenceDatasetError("duplicate_decision_evidence_identity_invalid")
        pair = PairIdentity.of(
            SourceIdentity(
                _positive_int(
                    row["left_source_post_id"], "duplicate_decision_source_identity_invalid"
                ),
                _positive_int(
                    row["left_source_version"], "duplicate_decision_source_identity_invalid"
                ),
            ),
            SourceIdentity(
                _positive_int(
                    row["right_source_post_id"], "duplicate_decision_source_identity_invalid"
                ),
                _positive_int(
                    row["right_source_version"], "duplicate_decision_source_identity_invalid"
                ),
            ),
        )
        if pair in seen_pairs:
            raise ReferenceDatasetError("duplicate_decision_pair_not_unique")
        seen_pairs.add(pair)
        seen_evidence.add(evidence_id)
        decisions.append(DuplicateDecision(evidence_id, pair, decision, evidence_origin))
    return tuple(sorted(decisions, key=lambda item: item.pair))


def load_label_resolutions(path: str | Path | None) -> dict[str, str]:
    """读取重复分量旅游标签冲突的人工最终确认。

    Args:
        path: 标签冲突确认 CSV；没有冲突证据时可为 ``None``。

    Returns:
        分量身份到 ``related`` 或 ``unrelated`` 的唯一映射。

    Raises:
        ReferenceDatasetError: 文件字段、状态、标签或身份不满足契约。
    """

    if path is None:
        return {}
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != LABEL_RESOLUTION_FIELDS:
                raise ReferenceDatasetError("duplicate_label_resolution_field_mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("duplicate_label_resolution_unreadable") from exc
    _validate_complete_csv_rows(
        rows,
        LABEL_RESOLUTION_FIELDS,
        "duplicate_label_resolution_row_structure_invalid",
    )
    resolutions: dict[str, str] = {}
    evidence_ids: set[str] = set()
    for row in rows:
        component_id = row["component_id"].strip()
        label = row["tourism_label"].strip()
        evidence_id = row["evidence_id"].strip()
        if row["review_status"].strip() != "finalized":
            raise ReferenceDatasetError("duplicate_label_resolution_not_finalized")
        if not component_id or component_id in resolutions or not evidence_id:
            raise ReferenceDatasetError("duplicate_label_resolution_identity_invalid")
        if evidence_id in evidence_ids:
            raise ReferenceDatasetError("duplicate_label_resolution_evidence_duplicate")
        if label not in ALLOWED_LABELS:
            raise ReferenceDatasetError("duplicate_label_resolution_value_invalid")
        resolutions[component_id] = label
        evidence_ids.add(evidence_id)
    return resolutions


def unresolved_candidate_pairs(
    candidates: Sequence[DuplicateCandidate],
    decisions: Sequence[DuplicateDecision],
) -> tuple[PairIdentity, ...]:
    """列出尚无人工最终决定的非精确阈值候选。

    Args:
        candidates: 全对计算产生的候选边。
        decisions: 已解析的人工最终决定。

    Returns:
        排序后的未确认候选；精确哈希边因自动成组而不在结果中。
    """

    decided = {decision.pair for decision in decisions}
    return tuple(
        sorted(
            candidate.pair
            for candidate in candidates
            if not candidate.exact_normalized_hash_match and candidate.pair not in decided
        )
    )


def _representative(members: Sequence[ReferencePost]) -> ReferencePost:
    """按冻结、平台无关且标签无关的规则选择建模代表。

    全部分量成员按规范化文本完整度降序、稳定帖子身份升序决胜。
    样本框、旅游标签和平台均不参与选择；500/200缺口由后续候补调度恢复。

    Args:
        members: 同一确认重复分量的非空成员。

    Returns:
        唯一稳定代表。
    """

    return min(members, key=lambda item: (-item.completeness, item.identity))


def _build_component_state(
    posts: Sequence[ReferencePost],
    candidates: Sequence[DuplicateCandidate],
    decisions: Sequence[DuplicateDecision],
    *,
    require_all_candidates_finalized: bool,
) -> _ComponentState:
    """验证重复边并构建不涉及标签选择的稳定分量。

    Args:
        posts: 待分组帖子。
        candidates: 完整全对候选。
        decisions: finalized 人工决定。
        require_all_candidates_finalized: 是否拒绝任何未决非精确候选。

    Returns:
        可供冲突任务生成和最终代表选择共同使用的内部状态。

    Raises:
        ReferenceDatasetError: 身份、候选、决定或精确边约束不满足。
    """

    ordered_posts = tuple(sorted(posts, key=lambda item: item.identity))
    by_identity = {post.identity: post for post in ordered_posts}
    if len(by_identity) != len(ordered_posts):
        raise ReferenceDatasetError("duplicate_component_input_identity_not_unique")
    unresolved = unresolved_candidate_pairs(candidates, decisions)
    if require_all_candidates_finalized and unresolved:
        raise ReferenceDatasetError("duplicate_candidates_require_final_review")
    candidate_by_pair = {candidate.pair: candidate for candidate in candidates}
    decision_by_pair = {decision.pair: decision for decision in decisions}
    if len(decision_by_pair) != len(decisions):
        raise ReferenceDatasetError("duplicate_decision_pair_not_unique")
    identities = set(by_identity)
    for pair in (*candidate_by_pair, *decision_by_pair):
        if pair.left not in identities or pair.right not in identities:
            raise ReferenceDatasetError("duplicate_edge_member_not_found")
    exact_edges = frozenset(
        candidate.pair for candidate in candidates if candidate.exact_normalized_hash_match
    )
    for pair in exact_edges:
        decision = decision_by_pair.get(pair)
        if decision is not None and decision.decision == "not_duplicate":
            raise ReferenceDatasetError("exact_duplicate_cannot_be_rejected")
    for decision in decisions:
        if (
            decision.pair not in candidate_by_pair
            and decision.evidence_origin != "manual_low_similarity"
        ):
            raise ReferenceDatasetError("duplicate_decision_not_a_candidate")
    confirmed_edges = frozenset(
        set(exact_edges).union(
            decision.pair for decision in decisions if decision.decision == "duplicate"
        )
    )
    disjoint = _DisjointSet(identities)
    for pair in sorted(confirmed_edges):
        disjoint.union(pair.left, pair.right)
    grouped: dict[SourceIdentity, list[ReferencePost]] = defaultdict(list)
    for post in ordered_posts:
        grouped[disjoint.find(post.identity)].append(post)
    members_by_component: dict[str, tuple[ReferencePost, ...]] = {}
    component_by_identity: dict[SourceIdentity, str] = {}
    for members in grouped.values():
        stable_members = tuple(sorted(members, key=lambda item: item.identity))
        component_id = canonical_sha256(
            [member.identity.as_list() for member in stable_members]
        )[:32]
        members_by_component[component_id] = stable_members
        for member in stable_members:
            component_by_identity[member.identity] = component_id
    return _ComponentState(
        ordered_posts,
        candidate_by_pair,
        decision_by_pair,
        exact_edges,
        confirmed_edges,
        component_by_identity,
        members_by_component,
    )


def find_duplicate_label_conflicts(
    posts: Sequence[ReferencePost],
    candidates: Sequence[DuplicateCandidate],
    decisions: Sequence[DuplicateDecision],
) -> tuple[DuplicateLabelConflict, ...]:
    """列出确认重复分量中的旅游标签冲突，不自动选择标签。

    Args:
        posts: 现有完成参考集成员。
        candidates: 完整全对候选。
        decisions: 所有非精确候选的 finalized 人工决定。

    Returns:
        按稳定分量 ID 排序的冲突任务。

    Raises:
        ReferenceDatasetError: 重复决定尚未关闭或边证据不合法。
    """

    state = _build_component_state(
        posts,
        candidates,
        decisions,
        require_all_candidates_finalized=True,
    )
    conflicts = []
    for component_id, members in state.members_by_component.items():
        labels = {member.tourism_label for member in members}
        if not labels <= ALLOWED_LABELS:
            raise ReferenceDatasetError("duplicate_component_label_not_final")
        if len(labels) > 1:
            conflicts.append(DuplicateLabelConflict(component_id, members))
    return tuple(sorted(conflicts, key=lambda item: item.component_id))


def build_duplicate_component_plan(
    posts: Sequence[ReferencePost],
    candidates: Sequence[DuplicateCandidate],
    decisions: Sequence[DuplicateDecision],
    *,
    label_resolutions: Mapping[str, str] | None = None,
    require_all_candidates_finalized: bool = True,
) -> DuplicateComponentPlan:
    """以精确边和人工确认边构建稳定连通分量并选择代表。

    Args:
        posts: 现有完成参考集成员。
        candidates: 完整全对计算输出的候选边。
        decisions: 人工最终重复决定，可含低于阈值的显式补入边。
        label_resolutions: 对冲突分量的人工最终标签确认。
        require_all_candidates_finalized: 最终化时必须为 ``True``；测试或预览可设
            为 ``False``，未确认候选不会形成排除边。

    Returns:
        包含全部单例和重复分量、代表、排除成员及哈希的计划。

    Raises:
        ReferenceDatasetError: 身份缺失、精确边被否认、候选未确认、标签冲突
            未解决或存在多余标签确认。
    """

    state = _build_component_state(
        posts,
        candidates,
        decisions,
        require_all_candidates_finalized=require_all_candidates_finalized,
    )
    ordered_posts = state.ordered_posts
    decision_by_pair = state.decision_by_pair
    exact_edges = state.exact_edges
    confirmed_edges = state.confirmed_edges
    members_by_component = state.members_by_component
    component_by_identity = state.component_by_identity
    resolutions = dict(label_resolutions or {})
    representatives: list[ReferencePost] = []
    excluded: list[ReferencePost] = []
    used_resolutions: set[str] = set()
    for component_id in sorted(members_by_component):
        members = members_by_component[component_id]
        labels = {member.tourism_label for member in members}
        if not labels <= ALLOWED_LABELS:
            raise ReferenceDatasetError("duplicate_component_label_not_final")
        chosen = _representative(members)
        if len(labels) > 1:
            resolution = resolutions.get(component_id)
            if resolution not in ALLOWED_LABELS:
                raise ReferenceDatasetError("duplicate_component_label_conflict")
            chosen = replace(chosen, tourism_label=resolution)
            used_resolutions.add(component_id)
        representatives.append(chosen)
        excluded.extend(member for member in members if member.identity != chosen.identity)
    if set(resolutions) != used_resolutions:
        raise ReferenceDatasetError("duplicate_label_resolution_without_conflict")
    decision_payload = [
        {
            "pair": pair.as_list(),
            "decision": "duplicate",
            "origin": (
                "exact_normalized_hash"
                if pair in exact_edges
                else decision_by_pair[pair].evidence_origin
            ),
            "evidence_id": (
                None if pair in exact_edges else decision_by_pair[pair].evidence_id
            ),
        }
        for pair in sorted(confirmed_edges)
    ] + [
        {
            "pair": decision.pair.as_list(),
            "decision": "not_duplicate",
            "origin": decision.evidence_origin,
            "evidence_id": decision.evidence_id,
        }
        for decision in sorted(decisions, key=lambda item: item.pair)
        if decision.decision == "not_duplicate"
    ] + [
        {
            "component_id": component_id,
            "tourism_label": resolutions[component_id],
            "decision": "label_resolution",
        }
        for component_id in sorted(used_resolutions)
    ]
    member_payload = [
        {
            "identity": post.identity.as_list(),
            "component_id": component_by_identity[post.identity],
            "is_representative": any(
                selected.identity == post.identity for selected in representatives
            ),
        }
        for post in ordered_posts
    ]
    return DuplicateComponentPlan(
        component_by_identity=component_by_identity,
        members_by_component=members_by_component,
        representatives=tuple(sorted(representatives, key=lambda item: item.identity)),
        excluded=tuple(sorted(excluded, key=lambda item: item.identity)),
        confirmed_edges=tuple(sorted(confirmed_edges)),
        decision_sha256=canonical_sha256(decision_payload),
        member_sha256=canonical_sha256(member_payload),
    )
