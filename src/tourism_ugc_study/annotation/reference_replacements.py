"""平台无关的确定性候补队列与补充成员调度。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from .reference_candidates import compute_duplicate_candidates
from .reference_contract import (
    ALLOWED_LABELS,
    FINAL_PROBABILITY_COUNT,
    FINAL_TARGETED_COUNT,
    DuplicateDecision,
    PairIdentity,
    ReferenceDatasetError,
    ReferencePost,
    SourceIdentity,
    canonical_sha256,
)
from .reference_candidates import CandidateComputation
from .reference_human_evidence import DuplicateLabelConflict


@dataclass(frozen=True)
class ReplacementQueueItem:
    """冻结全局候补队列中的一条无标签记录。

    Attributes:
        post: 不携带候补旅游标签和样本框的候选帖子。
        queue_rank: 从1开始的全局确定性顺序。
        order_key: 固定种子和源身份生成的 SHA-256 排序键。
    """

    post: ReferencePost
    queue_rank: int
    order_key: str


@dataclass(frozen=True)
class ReplacementQueue:
    """一次性冻结、与平台和候补标签无关的候补队列。

    Attributes:
        items: 全局排序后的候补记录。
        random_seed: 冻结随机种子。
        excluded_identity_count: 因已是既有成员或确认重复分量成员而排除的数量。
        queue_sha256: 队列身份、秩和排序键摘要。
    """

    items: tuple[ReplacementQueueItem, ...]
    random_seed: int
    excluded_identity_count: int
    queue_sha256: str


@dataclass(frozen=True)
class ReplacementDisposition:
    """候补记录在当前调度轮次中的去敏结果。

    Attributes:
        identity: 候补源身份。
        queue_rank: 冻结全局秩。
        status: ``selected``、``structure_unusable``、``confirmed_duplicate``、
            ``duplicate_review_required``、``label_required`` 或 ``not_needed``。
        sample_frame: 选中时分配的样本框，否则为 ``None``。
    """

    identity: SourceIdentity
    queue_rank: int
    status: str
    sample_frame: str | None


@dataclass(frozen=True)
class ReplacementSelection:
    """按冻结队列补足样本框后的成员与状态。

    Attributes:
        final_posts: 既有代表与已选补充成员。
        dispositions: 已检查候补的逐条去敏状态。
        probability_estimation_status: ``valid`` 或
            ``unavailable_after_replacement``。
        probability_estimation_reason_code: 失效原因；没有概率框补样时为 ``None``。
        probability_replacement_count: 概率框实际补充数量。
        targeted_replacement_count: 定向框实际补充数量。
        confirmed_duplicate_pairs: 本轮精确或人工确认的全部重复边。
        duplicate_decision_sha256: 本轮精确边与人工决定的稳定摘要。
        reviewed_max_queue_rank: 已完成联合重复复核的冻结前缀末秩。
        component_by_identity: 联合输入中每条记录的稳定重复分量身份。
    """

    final_posts: tuple[ReferencePost, ...]
    dispositions: tuple[ReplacementDisposition, ...]
    probability_estimation_status: str
    probability_estimation_reason_code: str | None
    probability_replacement_count: int
    targeted_replacement_count: int
    confirmed_duplicate_pairs: tuple[PairIdentity, ...]
    duplicate_decision_sha256: str
    reviewed_max_queue_rank: int
    component_by_identity: Mapping[SourceIdentity, str]


@dataclass(frozen=True)
class ReplacementAnnotationPlan:
    """完成候补重复检查后、读取旅游标签前的任务计划。

    Attributes:
        tasks: 按冻结队列秩排序的新无标签候补任务。
        carried_labels: 从前轮不可变 artifact 携带的已终止标签证据。
        required_count: 扣除仍可计数的既有二元补充标签后的剩余缺口。
        reserve_count: 为结构或标签失败预留的额外任务数。
        task_sha256: 任务身份、文本哈希和队列秩摘要。
        duplicate_decision_sha256: 本轮精确边与人工决定的稳定摘要。
    """

    tasks: tuple[ReplacementQueueItem, ...]
    carried_labels: tuple[tuple[ReplacementQueueItem, str], ...]
    required_count: int
    reserve_count: int
    task_sha256: str
    duplicate_decision_sha256: str
    reviewed_max_queue_rank: int


@dataclass(frozen=True)
class _ReplacementDuplicatePlan:
    """候补前缀与既有代表的联合重复分量计算结果。"""

    confirmed_pairs: tuple[PairIdentity, ...]
    unresolved_pairs: tuple[PairIdentity, ...]
    blocked_identities: frozenset[SourceIdentity]
    representative_identities: frozenset[SourceIdentity]
    component_by_identity: Mapping[SourceIdentity, str]
    decision_sha256: str


def build_replacement_queue(
    population: Sequence[ReferencePost],
    *,
    blocked_identities: Sequence[SourceIdentity],
    random_seed: int,
) -> ReplacementQueue:
    """不读取候补标签地冻结全局候补队列。

    排序键只包含固定随机种子和稳定源身份。平台不在 ``ReferencePost`` 中，
    因而无法进入配额、排序或接受规则。输入中即使意外携带候补标签，也会在
    队列投影中主动清空，保证先抽取、后标注。

    Args:
        population: 同一冻结候选人口中的全部可寻址成员。
        blocked_identities: 既有700条身份及已确认重复分量中不得补入的身份。
        random_seed: 正整数冻结种子。

    Returns:
        身份唯一且全局排序的无标签候补队列。

    Raises:
        ReferenceDatasetError: 种子非法或候选人口身份不唯一。
    """

    if isinstance(random_seed, bool) or not isinstance(random_seed, int) or random_seed <= 0:
        raise ReferenceDatasetError("replacement_random_seed_invalid")
    identities = [post.identity for post in population]
    if len(identities) != len(set(identities)):
        raise ReferenceDatasetError("replacement_population_identity_not_unique")
    blocked = set(blocked_identities)
    eligible: list[tuple[str, ReferencePost]] = []
    for post in population:
        if post.identity in blocked:
            continue
        order_key = canonical_sha256([random_seed, post.identity.as_list()])
        eligible.append(
            (
                order_key,
                replace(
                    post,
                    tourism_label=None,
                    sample_frame=None,
                    inclusion_probability=None,
                    analysis_weight=None,
                    evidence_origin="supplemental_annotation",
                ),
            )
        )
    eligible.sort(key=lambda item: (item[0], item[1].identity))
    items = tuple(
        ReplacementQueueItem(post=post, queue_rank=index, order_key=order_key)
        for index, (order_key, post) in enumerate(eligible, start=1)
    )
    payload = [
        {
            "identity": item.post.identity.as_list(),
            "queue_rank": item.queue_rank,
            "order_key": item.order_key,
        }
        for item in items
    ]
    return ReplacementQueue(
        items=items,
        random_seed=random_seed,
        excluded_identity_count=len(population) - len(items),
        queue_sha256=canonical_sha256(payload),
    )


def compute_replacement_duplicate_candidates(
    accepted: Sequence[ReferencePost],
    queue: ReplacementQueue,
    *,
    max_queue_rank: int,
    lineage_posts: Sequence[ReferencePost] | None = None,
) -> CandidateComputation:
    """生成候补前缀与初始全部谱系/同轮候补之间的完整重复候选。

    Args:
        accepted: 去重后的既有代表，用于校验谱系锚点完整性。
        queue: 标签读取前冻结的全局候补队列。
        max_queue_rank: 本轮纳入全对比较的最大冻结秩。
        lineage_posts: 初始700条的全部谱系成员；包含被排除的重复成员。未提供
            时仅适用于没有初始排除成员的直接领域调用。

    Returns:
        仅保留至少一端为本轮候补的候选；全对计数仍记录联合输入上实际完成的
        所有无序比较，不做平台或阻塞预筛。

    Raises:
        ReferenceDatasetError: 秩不是非负整数或联合输入无法完成比较。
    """

    if (
        isinstance(max_queue_rank, bool)
        or not isinstance(max_queue_rank, int)
        or max_queue_rank < 0
    ):
        raise ReferenceDatasetError("replacement_review_max_rank_invalid")
    if max_queue_rank > len(queue.items):
        raise ReferenceDatasetError("replacement_review_max_rank_exceeds_queue")
    batch = tuple(
        item
        for item in queue.items
        if item.queue_rank <= max_queue_rank and item.post.structure_usable
    )
    anchors = tuple(accepted if lineage_posts is None else lineage_posts)
    anchor_identities = {post.identity for post in anchors}
    if len(anchor_identities) != len(anchors):
        raise ReferenceDatasetError("replacement_lineage_identity_not_unique")
    if not {post.identity for post in accepted} <= anchor_identities:
        raise ReferenceDatasetError("replacement_accepted_not_in_lineage")
    computation = compute_duplicate_candidates(
        [*anchors, *(item.post for item in batch)]
    )
    queued_identities = {item.post.identity for item in batch}
    relevant = tuple(
        candidate
        for candidate in computation.candidates
        if candidate.pair.left in queued_identities
        or candidate.pair.right in queued_identities
    )
    candidate_hash = canonical_sha256(
        [
            {
                "pair": candidate.pair.as_list(),
                "similarity": format(candidate.similarity, ".17g"),
                "exact": candidate.exact_normalized_hash_match,
                "reason_code": candidate.candidate_reason_code,
            }
            for candidate in relevant
        ]
    )
    return CandidateComputation(
        candidates=relevant,
        examined_pair_count=computation.examined_pair_count,
        expected_pair_count=computation.expected_pair_count,
        input_member_sha256=computation.input_member_sha256,
        candidate_sha256=candidate_hash,
        algorithm_identity=computation.algorithm_identity,
    )


def _build_replacement_duplicate_plan(
    accepted: Sequence[ReferencePost],
    batch: Sequence[ReplacementQueueItem],
    decisions: Sequence[DuplicateDecision],
    *,
    lineage_posts: Sequence[ReferencePost] | None = None,
    initial_component_by_identity: Mapping[SourceIdentity, str] | None = None,
) -> _ReplacementDuplicatePlan:
    """以联合全对结果构建候补稳定分量并验证人工决定。

    Args:
        accepted: 已去重的既有代表。
        batch: 本轮冻结候补前缀。
        decisions: 本轮 finalized 人工重复决定。
        lineage_posts: 初始700条的全部谱系成员，含被排除成员。
        initial_component_by_identity: 初始确认重复分量映射，用于把候补连接到
            任一历史成员时传播到仍保留的代表。

    Returns:
        可供预标注计划与最终调度共同消费的稳定分量结果。

    Raises:
        ReferenceDatasetError: 身份、决定、精确边或既有代表约束不满足。
    """

    usable_batch = tuple(item for item in batch if item.post.structure_usable)
    lineage = tuple(accepted if lineage_posts is None else lineage_posts)
    lineage_identities = {post.identity for post in lineage}
    if len(lineage_identities) != len(lineage):
        raise ReferenceDatasetError("replacement_lineage_identity_not_unique")
    accepted_identities = {post.identity for post in accepted}
    if not accepted_identities <= lineage_identities:
        raise ReferenceDatasetError("replacement_accepted_not_in_lineage")
    if initial_component_by_identity is None:
        initial_components = {
            identity: canonical_sha256([identity.as_list()])[:32]
            for identity in lineage_identities
        }
    else:
        initial_components = dict(initial_component_by_identity)
        if set(initial_components) != lineage_identities or any(
            not isinstance(component_id, str) or not component_id
            for component_id in initial_components.values()
        ):
            raise ReferenceDatasetError("replacement_initial_component_lineage_invalid")
    posts = [*lineage, *(item.post for item in usable_batch)]
    computation = compute_duplicate_candidates(posts)
    identities = {post.identity for post in posts}
    if len(identities) != len(posts):
        raise ReferenceDatasetError("replacement_comparison_identity_not_unique")
    decision_by_pair = {decision.pair: decision for decision in decisions}
    if len(decision_by_pair) != len(decisions):
        raise ReferenceDatasetError("replacement_duplicate_decision_not_unique")
    queued_identities = {item.post.identity for item in usable_batch}
    relevant_candidates = tuple(
        candidate
        for candidate in computation.candidates
        if candidate.pair.left in queued_identities
        or candidate.pair.right in queued_identities
    )
    candidate_by_pair = {candidate.pair: candidate for candidate in relevant_candidates}
    for decision in decisions:
        if decision.pair.left not in identities or decision.pair.right not in identities:
            raise ReferenceDatasetError("replacement_duplicate_decision_member_not_found")
        if (
            decision.pair.left not in queued_identities
            and decision.pair.right not in queued_identities
        ):
            raise ReferenceDatasetError("replacement_duplicate_decision_not_a_candidate")
        if (
            decision.pair not in candidate_by_pair
            and decision.evidence_origin != "manual_low_similarity"
        ):
            raise ReferenceDatasetError("replacement_duplicate_decision_not_a_candidate")
    exact_pairs = {
        candidate.pair
        for candidate in relevant_candidates
        if candidate.exact_normalized_hash_match
    }
    if any(
        decision_by_pair.get(pair) is not None
        and decision_by_pair[pair].decision == "not_duplicate"
        for pair in exact_pairs
    ):
        raise ReferenceDatasetError("exact_duplicate_cannot_be_rejected")
    unresolved = tuple(
        sorted(
            candidate.pair
            for candidate in relevant_candidates
            if not candidate.exact_normalized_hash_match
            and candidate.pair not in decision_by_pair
        )
    )
    confirmed = set(exact_pairs)
    confirmed.update(
        decision.pair for decision in decisions if decision.decision == "duplicate"
    )

    parent = {identity: identity for identity in identities}

    def find(identity: SourceIdentity) -> SourceIdentity:
        root = parent[identity]
        if root != identity:
            parent[identity] = find(root)
        return parent[identity]

    def union(first: SourceIdentity, second: SourceIdentity) -> None:
        first_root, second_root = find(first), find(second)
        root, child = sorted((first_root, second_root))
        parent[child] = root

    initial_members_by_component: dict[str, list[SourceIdentity]] = defaultdict(list)
    for identity, component_id in initial_components.items():
        initial_members_by_component[component_id].append(identity)
    for members in initial_members_by_component.values():
        first, *others = sorted(members)
        for other in others:
            union(first, other)
    for pair in sorted(confirmed):
        union(pair.left, pair.right)
    grouped: dict[SourceIdentity, list[SourceIdentity]] = defaultdict(list)
    for identity in sorted(identities):
        grouped[find(identity)].append(identity)
    component_by_identity: dict[SourceIdentity, str] = {}
    for members in grouped.values():
        component_id = canonical_sha256(
            [identity.as_list() for identity in sorted(members)]
        )[:32]
        for identity in members:
            component_by_identity[identity] = component_id

    batch_by_identity = {item.post.identity: item for item in usable_batch}
    blocked: set[SourceIdentity] = set()
    representatives: set[SourceIdentity] = set()
    for members in grouped.values():
        accepted_members = accepted_identities.intersection(members)
        queued_members = [
            batch_by_identity[identity]
            for identity in members
            if identity in batch_by_identity
        ]
        if len(accepted_members) > 1:
            raise ReferenceDatasetError(
                "replacement_confirmed_duplicate_between_accepted"
            )
        if accepted_members:
            # 候补阶段只补足初始去重后的缺口。既有代表不会被候补重新抽样；
            # 与既有代表确认重复的候补按契约不得进入最终700条。
            blocked.update(item.post.identity for item in queued_members)
            continue
        usable = [item for item in queued_members if item.post.structure_usable]
        if not usable:
            continue
        representative = min(
            usable,
            key=lambda item: (-item.post.completeness, item.post.identity),
        )
        representatives.add(representative.post.identity)
        blocked.update(
            item.post.identity
            for item in queued_members
            if item.post.identity != representative.post.identity
        )
    decision_payload = [
        {
            "pair": pair.as_list(),
            "decision": "duplicate",
            "origin": (
                "exact_normalized_hash"
                if pair in exact_pairs
                else decision_by_pair[pair].evidence_origin
            ),
            "evidence_id": (
                None if pair in exact_pairs else decision_by_pair[pair].evidence_id
            ),
        }
        for pair in sorted(confirmed)
    ] + [
        {
            "pair": decision.pair.as_list(),
            "decision": "not_duplicate",
            "origin": decision.evidence_origin,
            "evidence_id": decision.evidence_id,
        }
        for decision in sorted(decisions, key=lambda item: item.pair)
        if decision.decision == "not_duplicate"
    ]
    return _ReplacementDuplicatePlan(
        confirmed_pairs=tuple(sorted(confirmed)),
        unresolved_pairs=unresolved,
        blocked_identities=frozenset(blocked),
        representative_identities=frozenset(representatives),
        component_by_identity=component_by_identity,
        decision_sha256=canonical_sha256(decision_payload),
    )


def prepare_replacement_annotation_plan(
    accepted: Sequence[ReferencePost],
    queue: ReplacementQueue,
    *,
    duplicate_decisions: Sequence[DuplicateDecision],
    max_queue_rank: int,
    reserve_count: int = 0,
    prior_labels: Mapping[SourceIdentity, str] | None = None,
    label_resolutions: Mapping[str, str] | None = None,
    lineage_posts: Sequence[ReferencePost] | None = None,
    initial_component_by_identity: Mapping[SourceIdentity, str] | None = None,
) -> ReplacementAnnotationPlan:
    """在读取候补标签前完成结构门、重复确认和稳定代表选择。

    Args:
        accepted: 初始去重后保留的既有代表。
        queue: 已冻结全局候补队列。
        duplicate_decisions: 本轮所有非精确候选的人工最终决定。
        max_queue_rank: 本轮检查的候补前缀末秩。
        reserve_count: 超出当前缺口的非负备用标注任务数。
        prior_labels: 前轮 finalized 补充标签；可含 ``uncertain``，只用于累计
            谱系、跳过已终止任务和计算仍可计数的二元标签数。
        label_resolutions: 当前候补重复分量标签冲突的人工最终确认。
        lineage_posts: 初始700条全部谱系成员，含被排除重复成员。
        initial_component_by_identity: 初始确认重复分量映射。

    Returns:
        不含旅游标签、已排除确认重复成员的补充标注任务。

    Raises:
        ReferenceDatasetError: 近重复候选尚未全部确认、决定不属于本轮、既有计数
            超标、reserve非法或安全候补不足。
    """

    if isinstance(reserve_count, bool) or not isinstance(reserve_count, int) or reserve_count < 0:
        raise ReferenceDatasetError("replacement_reserve_count_invalid")
    if (
        isinstance(max_queue_rank, bool)
        or not isinstance(max_queue_rank, int)
        or max_queue_rank < 0
    ):
        raise ReferenceDatasetError("replacement_review_max_rank_invalid")
    if max_queue_rank > len(queue.items):
        raise ReferenceDatasetError("replacement_review_max_rank_exceeds_queue")
    probability_count = sum(post.sample_frame == "probability" for post in accepted)
    targeted_count = sum(post.sample_frame == "targeted" for post in accepted)
    if probability_count > FINAL_PROBABILITY_COUNT or targeted_count > FINAL_TARGETED_COUNT:
        raise ReferenceDatasetError("replacement_existing_frame_count_exceeds_target")
    required = (FINAL_PROBABILITY_COUNT - probability_count) + (
        FINAL_TARGETED_COUNT - targeted_count
    )
    batch = tuple(item for item in queue.items if item.queue_rank <= max_queue_rank)
    duplicate_plan = _build_replacement_duplicate_plan(
        accepted,
        batch,
        duplicate_decisions,
        lineage_posts=lineage_posts,
        initial_component_by_identity=initial_component_by_identity,
    )
    if duplicate_plan.unresolved_pairs:
        raise ReferenceDatasetError("replacement_duplicate_candidates_require_final_review")
    representatives = sorted(
        (
            item
            for item in batch
            if item.post.identity in duplicate_plan.representative_identities
        ),
        key=lambda item: item.queue_rank,
    )
    prior = dict(prior_labels or {})
    if set(prior) - {item.post.identity for item in batch}:
        raise ReferenceDatasetError("prior_supplemental_label_not_in_reviewed_prefix")
    if set(prior.values()) - (set(ALLOWED_LABELS) | {"uncertain"}):
        raise ReferenceDatasetError("prior_supplemental_label_invalid")
    resolutions = dict(label_resolutions or {})
    if set(resolutions.values()) - set(ALLOWED_LABELS):
        raise ReferenceDatasetError("replacement_label_resolution_invalid")
    labels_by_component: dict[str, set[str]] = defaultdict(set)
    for post in accepted:
        if post.tourism_label in ALLOWED_LABELS:
            labels_by_component[
                duplicate_plan.component_by_identity[post.identity]
            ].add(post.tourism_label)
    for identity, label in prior.items():
        component_id = duplicate_plan.component_by_identity[identity]
        if label in ALLOWED_LABELS:
            labels_by_component[component_id].add(label)
    conflict_components = {
        component_id
        for component_id, component_labels in labels_by_component.items()
        if len(component_labels) > 1
    }
    if conflict_components - set(resolutions):
        raise ReferenceDatasetError("replacement_component_label_conflict")
    if set(resolutions) - conflict_components:
        raise ReferenceDatasetError("replacement_label_resolution_not_required")
    effective_prior = dict(prior)
    representative_by_component = {
        duplicate_plan.component_by_identity[identity]: identity
        for identity in duplicate_plan.representative_identities
    }
    for component_id, component_label in resolutions.items():
        representative_identity = representative_by_component.get(component_id)
        if representative_identity is not None:
            effective_prior[representative_identity] = component_label
    representative_identities = {item.post.identity for item in representatives}
    usable_binary_prior = sum(
        identity in representative_identities and label in ALLOWED_LABELS
        for identity, label in effective_prior.items()
    )
    remaining_required = max(0, required - usable_binary_prior)
    new_representatives = [
        item for item in representatives if item.post.identity not in effective_prior
    ]
    target = remaining_required + reserve_count
    if len(new_representatives) < target:
        raise ReferenceDatasetError("replacement_annotation_pool_exhausted")
    tasks = tuple(new_representatives[:target])
    by_identity = {item.post.identity: item for item in batch}
    carried = tuple(
        (by_identity[identity], label)
        for identity, label in sorted(
            effective_prior.items(), key=lambda item: by_identity[item[0]].queue_rank
        )
    )
    task_hash = canonical_sha256(
        [
            {
                "identity": item.post.identity.as_list(),
                "task_id": item.post.task_id,
                "queue_rank": item.queue_rank,
                "normalized_sha256": item.post.normalized_sha256,
            }
            for item in [*(item for item, _label in carried), *tasks]
        ]
    )
    return ReplacementAnnotationPlan(
        tasks,
        carried,
        remaining_required,
        reserve_count,
        task_hash,
        duplicate_plan.decision_sha256,
        max_queue_rank,
    )


def find_replacement_label_conflicts(
    accepted: Sequence[ReferencePost],
    queue: ReplacementQueue,
    *,
    duplicate_decisions: Sequence[DuplicateDecision],
    max_queue_rank: int,
    prior_labels: Mapping[SourceIdentity, str],
    lineage_posts: Sequence[ReferencePost] | None = None,
    initial_component_by_identity: Mapping[SourceIdentity, str] | None = None,
) -> tuple[DuplicateLabelConflict, ...]:
    """查找扩展候补前缀后形成的二元标签冲突分量。

    Args:
        accepted: 初始去重后的既有代表。
        queue: 读取标签前冻结的全局候补队列。
        duplicate_decisions: 当前前缀 finalized 重复决定。
        max_queue_rank: 已完成联合复核的前缀末秩。
        prior_labels: 前轮 finalized 补充标签。
        lineage_posts: 初始700条全部谱系成员，含被排除重复成员。
        initial_component_by_identity: 初始确认重复分量映射。

    Returns:
        按分量身份排序、必须人工确认的候补标签冲突。

    Raises:
        ReferenceDatasetError: 重复候选未关闭或前轮标签不属于当前前缀。
    """

    batch = tuple(item for item in queue.items if item.queue_rank <= max_queue_rank)
    duplicate_plan = _build_replacement_duplicate_plan(
        accepted,
        batch,
        duplicate_decisions,
        lineage_posts=lineage_posts,
        initial_component_by_identity=initial_component_by_identity,
    )
    if duplicate_plan.unresolved_pairs:
        raise ReferenceDatasetError("replacement_duplicate_candidates_require_final_review")
    by_identity = {item.post.identity: item.post for item in batch}
    if set(prior_labels) - set(by_identity):
        raise ReferenceDatasetError("prior_supplemental_label_not_in_reviewed_prefix")
    grouped: dict[str, list[ReferencePost]] = defaultdict(list)
    labels_by_component: dict[str, set[str]] = defaultdict(set)
    for post in [*accepted, *(item.post for item in batch if item.post.structure_usable)]:
        component_id = duplicate_plan.component_by_identity[post.identity]
        grouped[component_id].append(post)
    for post in accepted:
        if post.tourism_label not in ALLOWED_LABELS:
            continue
        component_id = duplicate_plan.component_by_identity[post.identity]
        labels_by_component[component_id].add(post.tourism_label)
    for identity, label in prior_labels.items():
        component_id = duplicate_plan.component_by_identity[identity]
        grouped[component_id] = [
            replace(post, tourism_label=label)
            if post.identity == identity
            else post
            for post in grouped[component_id]
        ]
        if label not in ALLOWED_LABELS:
            continue
        labels_by_component[component_id].add(label)
    return tuple(
        DuplicateLabelConflict(
            component_id,
            tuple(sorted(grouped[component_id], key=lambda item: item.identity)),
        )
        for component_id in sorted(grouped)
        if len(labels_by_component[component_id]) > 1
    )


def _next_frame(probability_gap: int, targeted_gap: int) -> str:
    """按剩余缺口比例确定下一条候补的样本框。

    Args:
        probability_gap: 尚缺概率框数量。
        targeted_gap: 尚缺定向框数量。

    Returns:
        缺口相对目标更大的框；比例相等时稳定选择概率框。
    """

    probability_ratio = probability_gap / FINAL_PROBABILITY_COUNT
    targeted_ratio = targeted_gap / FINAL_TARGETED_COUNT
    return "probability" if probability_ratio >= targeted_ratio else "targeted"


def select_replacements(
    accepted: Sequence[ReferencePost],
    queue: ReplacementQueue,
    *,
    labels: Mapping[SourceIdentity, str],
    duplicate_decisions: Sequence[DuplicateDecision] = (),
    max_queue_rank: int | None = None,
    label_resolutions: Mapping[str, str] | None = None,
    lineage_posts: Sequence[ReferencePost] | None = None,
    initial_component_by_identity: Mapping[SourceIdentity, str] | None = None,
) -> ReplacementSelection:
    """按冻结队列补足500/200，并跳过所有不能计数的记录。

    调度先对当前候补批次与初始700条全部谱系成员、同轮候补记录执行完整相同的
    字符 TF-IDF 检查，再读取独立传入的最终标签映射。结构不可用、确认重复、待人工
    重复判断或标签未最终确定者均不计入700条。概率框补样不会继承旧记录权重；
    当前实现无法从响应与去重过程证明联合纳入概率，因此显式标记总体估计不可用。

    Args:
        accepted: 去重后的既有代表，必须已带最终标签和样本框。
        queue: 读取标签前冻结的全局候补队列。
        labels: 独立完成的候补最终标签映射。
        duplicate_decisions: 本轮候补比较产生的人工最终重复决定。
        max_queue_rank: 本轮实际进入比较和标注流程的最大冻结秩；``None`` 表示
            检查完整队列。
        label_resolutions: 当前候补重复分量二元标签冲突的人工最终确认。
        lineage_posts: 初始700条全部谱系成员，含被排除重复成员。
        initial_component_by_identity: 初始确认重复分量映射。

    Returns:
        恰好500/200时的最终帖子和候补状态。

    Raises:
        ReferenceDatasetError: 既有计数已超标、候补标签非法、候补耗尽，或最终
            身份与计数不满足契约。
    """

    accepted_posts = list(accepted)
    if any(post.tourism_label not in ALLOWED_LABELS for post in accepted_posts):
        raise ReferenceDatasetError("replacement_accepted_label_not_final")
    probability_count = sum(post.sample_frame == "probability" for post in accepted_posts)
    targeted_count = sum(post.sample_frame == "targeted" for post in accepted_posts)
    if probability_count > FINAL_PROBABILITY_COUNT or targeted_count > FINAL_TARGETED_COUNT:
        raise ReferenceDatasetError("replacement_existing_frame_count_exceeds_target")
    if max_queue_rank is not None:
        if (
            isinstance(max_queue_rank, bool)
            or not isinstance(max_queue_rank, int)
            or max_queue_rank < 0
        ):
            raise ReferenceDatasetError("replacement_review_max_rank_invalid")
        if max_queue_rank > len(queue.items):
            raise ReferenceDatasetError("replacement_review_max_rank_exceeds_queue")
    batch = tuple(
        item
        for item in queue.items
        if max_queue_rank is None or item.queue_rank <= max_queue_rank
    )
    duplicate_plan = _build_replacement_duplicate_plan(
        accepted_posts,
        batch,
        duplicate_decisions,
        lineage_posts=lineage_posts,
        initial_component_by_identity=initial_component_by_identity,
    )
    effective_labels = dict(labels)
    resolutions = dict(label_resolutions or {})
    if set(resolutions.values()) - set(ALLOWED_LABELS):
        raise ReferenceDatasetError("replacement_label_resolution_invalid")
    labels_by_component: dict[str, set[str]] = defaultdict(set)
    for post in accepted_posts:
        labels_by_component[
            duplicate_plan.component_by_identity[post.identity]
        ].add(post.tourism_label)
    for identity, label in effective_labels.items():
        if identity not in duplicate_plan.component_by_identity:
            raise ReferenceDatasetError("replacement_label_not_in_reviewed_prefix")
        if label in ALLOWED_LABELS:
            labels_by_component[
                duplicate_plan.component_by_identity[identity]
            ].add(label)
    conflict_components = {
        component_id
        for component_id, component_labels in labels_by_component.items()
        if len(component_labels) > 1
    }
    if conflict_components - set(resolutions):
        raise ReferenceDatasetError("replacement_component_label_conflict")
    if set(resolutions) - conflict_components:
        raise ReferenceDatasetError("replacement_label_resolution_not_required")
    accepted_posts = [
        replace(
            post,
            tourism_label=resolutions.get(
                duplicate_plan.component_by_identity[post.identity],
                post.tourism_label,
            ),
        )
        for post in accepted_posts
    ]
    for identity in duplicate_plan.representative_identities:
        component_id = duplicate_plan.component_by_identity[identity]
        component_label = resolutions.get(component_id)
        if component_label is not None:
            effective_labels[identity] = component_label
    probability_gap = FINAL_PROBABILITY_COUNT - probability_count
    targeted_gap = FINAL_TARGETED_COUNT - targeted_count
    dispositions: list[ReplacementDisposition] = []
    probability_replacements = 0
    targeted_replacements = 0
    for item in batch:
        identity = item.post.identity
        if not item.post.structure_usable:
            dispositions.append(
                ReplacementDisposition(identity, item.queue_rank, "structure_unusable", None)
            )
            continue
        if identity in duplicate_plan.blocked_identities:
            dispositions.append(
                ReplacementDisposition(identity, item.queue_rank, "confirmed_duplicate", None)
            )
            continue
        touching_unresolved = any(
            identity in (pair.left, pair.right)
            for pair in duplicate_plan.unresolved_pairs
        )
        if touching_unresolved:
            dispositions.append(
                ReplacementDisposition(
                    identity, item.queue_rank, "duplicate_review_required", None
                )
            )
            continue
        if probability_gap == 0 and targeted_gap == 0:
            dispositions.append(
                ReplacementDisposition(identity, item.queue_rank, "not_needed", None)
            )
            continue
        label = effective_labels.get(identity)
        if label is None:
            dispositions.append(
                ReplacementDisposition(identity, item.queue_rank, "label_required", None)
            )
            continue
        if label == "uncertain":
            dispositions.append(
                ReplacementDisposition(identity, item.queue_rank, "label_not_final", None)
            )
            continue
        if label not in ALLOWED_LABELS:
            raise ReferenceDatasetError("replacement_label_value_invalid")
        frame = _next_frame(probability_gap, targeted_gap)
        if frame == "probability" and probability_gap == 0:
            frame = "targeted"
        elif frame == "targeted" and targeted_gap == 0:
            frame = "probability"
        if frame == "probability":
            probability_gap -= 1
            probability_replacements += 1
        else:
            targeted_gap -= 1
            targeted_replacements += 1
        accepted_posts.append(
            replace(
                item.post,
                tourism_label=label,
                sample_frame=frame,
                selection_reason_code=f"dedup_replacement_{frame}",
                selection_rank=item.queue_rank,
                inclusion_probability=None,
                analysis_weight=None,
                evidence_origin="supplemental_annotation",
            )
        )
        dispositions.append(ReplacementDisposition(identity, item.queue_rank, "selected", frame))
    if probability_gap or targeted_gap:
        raise ReferenceDatasetError("replacement_queue_exhausted")
    final_identities = [post.identity for post in accepted_posts]
    if len(final_identities) != len(set(final_identities)):
        raise ReferenceDatasetError("replacement_final_identity_not_unique")
    final_probability = sum(post.sample_frame == "probability" for post in accepted_posts)
    final_targeted = sum(post.sample_frame == "targeted" for post in accepted_posts)
    if (final_probability, final_targeted) != (
        FINAL_PROBABILITY_COUNT,
        FINAL_TARGETED_COUNT,
    ):
        raise ReferenceDatasetError("replacement_final_frame_count_invalid")
    probability_status = (
        "unavailable_after_replacement" if probability_replacements else "valid"
    )
    probability_reason = (
        "replacement_joint_inclusion_probability_not_proven"
        if probability_replacements
        else None
    )
    return ReplacementSelection(
        final_posts=tuple(sorted(accepted_posts, key=lambda item: item.identity)),
        dispositions=tuple(dispositions),
        probability_estimation_status=probability_status,
        probability_estimation_reason_code=probability_reason,
        probability_replacement_count=probability_replacements,
        targeted_replacement_count=targeted_replacements,
        confirmed_duplicate_pairs=duplicate_plan.confirmed_pairs,
        duplicate_decision_sha256=duplicate_plan.decision_sha256,
        reviewed_max_queue_rank=(
            len(queue.items) if max_queue_rank is None else max_queue_rank
        ),
        component_by_identity=duplicate_plan.component_by_identity,
    )
