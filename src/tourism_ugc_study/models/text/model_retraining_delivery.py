"""研究者冻结阈值后的概率重分流、人工中间层和最终动作计算。"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model_retraining_delivery_config import ModelRetrainingDeliveryPlan
from .model_retraining_inference import ScoredRoutingMember
from .model_retraining_snapshot import RetrainingDocument


class ModelRetrainingDeliveryError(RuntimeError):
    """交付计算输入、身份覆盖或路由分区非法时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、成员、标签或概率的稳定失败码。"""

        super().__init__("formal model retraining delivery calculation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class DeliveryDecision:
    """一条候选记录的最终派生动作与可追溯证据来源。"""

    source_post_id: int
    source_version: int
    component_id: str
    final_action: str
    decision_source: str
    policy_id: str | None
    p_unrelated: float | None

    @property
    def identity(self) -> tuple[int, int]:
        """返回稳定帖子身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class ManualReviewTaskMember:
    """中间层人工表公开行与私有映射所需的稳定绑定。"""

    task_id: str
    sample_run_id: str
    member: ScoredRoutingMember


def route_probability(
    p_unrelated: float, *, T_keep: float, T_exclude: float
) -> str:
    """把一个有限概率映射为互斥且穷尽的三段动作。

    Args:
        p_unrelated: 模型给出的“不相关”校准概率。
        T_keep: 自动保留端的闭区间上界。
        T_exclude: 自动排除端的闭区间下界。

    Returns:
        ``auto_keep``、``manual_review``或``auto_exclude``。

    Raises:
        ModelRetrainingDeliveryError: 概率或阈值不是有限合法值。
    """

    values = (p_unrelated, T_keep, T_exclude)
    if (
        any(isinstance(value, bool) or not math.isfinite(float(value)) for value in values)
        or not 0.0 <= float(p_unrelated) <= 1.0
        or not 0.0 <= float(T_keep) < float(T_exclude) <= 1.0
    ):
        raise ModelRetrainingDeliveryError(
            "model_retraining_delivery_probability_or_threshold_invalid"
        )
    if p_unrelated <= T_keep:
        return "auto_keep"
    if p_unrelated >= T_exclude:
        return "auto_exclude"
    return "manual_review"


def reroute_scored_members(
    records: Sequence[ScoredRoutingMember],
    *,
    T_keep: float,
    T_exclude: float,
) -> tuple[ScoredRoutingMember, ...]:
    """只复用既有概率重算动作，不调用编码、预测或拟合。

    Args:
        records: 已封存的全人口纯预测记录。
        T_keep: 新自动保留阈值。
        T_exclude: 新自动排除阈值。

    Returns:
        身份、正文、哈希和概率不变，仅动作更新的有序记录。

    Raises:
        ModelRetrainingDeliveryError: 身份重复、正文哈希不符或分区非法。
    """

    rerouted: list[ScoredRoutingMember] = []
    seen: set[tuple[int, int]] = set()
    for record in records:
        if (
            record.identity in seen
            or hashlib.sha256(
                record.normalized_model_text.encode("utf-8")
            ).hexdigest()
            != record.normalized_sha256
        ):
            raise ModelRetrainingDeliveryError(
                "model_retraining_delivery_scored_records_invalid"
            )
        seen.add(record.identity)
        rerouted.append(
            ScoredRoutingMember(
                member_key=record.member_key,
                source_post_id=record.source_post_id,
                source_version=record.source_version,
                component_id=record.component_id,
                normalized_model_text=record.normalized_model_text,
                normalized_sha256=record.normalized_sha256,
                p_unrelated=record.p_unrelated,
                provisional_action=route_probability(
                    record.p_unrelated,
                    T_keep=T_keep,
                    T_exclude=T_exclude,
                ),
            )
        )
    return tuple(rerouted)


def build_manual_review_members(
    records: Sequence[ScoredRoutingMember],
    *,
    excluded_human_member_keys: set[str],
    sample_run_id: str,
) -> tuple[ManualReviewTaskMember, ...]:
    """提取尚无人工作答的中间层并生成确定性任务身份。

    Args:
        records: 按最终阈值重分流的人口记录。
        excluded_human_member_keys: 已有审计人工标签、不得重复派发的成员键。
        sample_run_id: 本次人工中间层任务包身份。

    Returns:
        按帖子身份排序的一行一条任务成员。

    Raises:
        ModelRetrainingDeliveryError: 任务身份、排除集合或记录重复。
    """

    if len(sample_run_id) != 32 or any(
        character not in "0123456789abcdef" for character in sample_run_id
    ):
        raise ModelRetrainingDeliveryError(
            "model_retraining_delivery_manual_run_id_invalid"
        )
    member_keys = {record.member_key for record in records}
    if len(member_keys) != len(records) or not excluded_human_member_keys.issubset(
        member_keys
    ):
        raise ModelRetrainingDeliveryError(
            "model_retraining_delivery_manual_members_invalid"
        )
    selected = sorted(
        (
            record
            for record in records
            if record.provisional_action == "manual_review"
            and record.member_key not in excluded_human_member_keys
        ),
        key=lambda item: item.identity,
    )
    tasks = tuple(
        ManualReviewTaskMember(
            task_id=hashlib.sha256(
                f"{sample_run_id}|task|{record.member_key}".encode("utf-8")
            ).hexdigest()[:24],
            sample_run_id=sample_run_id,
            member=record,
        )
        for record in selected
    )
    if len({item.task_id for item in tasks}) != len(tasks):
        raise ModelRetrainingDeliveryError(
            "model_retraining_delivery_manual_task_id_collision"
        )
    return tasks


def build_delivery_decisions(
    training_documents: Sequence[RetrainingDocument],
    rerouted_records: Sequence[ScoredRoutingMember],
    audit_labels: Sequence[Mapping[str, Any]],
    *,
    policy_id: str,
    delivery_plan: ModelRetrainingDeliveryPlan,
) -> tuple[DeliveryDecision, ...]:
    """以人工标签优先规则合并1,300+300人工证据与两个自动尾部。

    Args:
        training_documents: 已进入训练的1,300条人工标签。
        rerouted_records: 按0.31/0.96重分流的12,558条既有概率。
        audit_labels: 最后300条盲审完成标签；无论其新动作如何均人工覆盖。
        policy_id: 新研究者冻结策略身份。
        delivery_plan: 预期计数和自动动作均已冻结的交付计划。

    Returns:
        覆盖完整候选人口、按帖子身份排序的最终派生决定。

    Raises:
        ModelRetrainingDeliveryError: 身份交叠、人工标签或交付计数不一致。
    """

    audit_by_identity: dict[tuple[int, int], Mapping[str, Any]] = {}
    for item in audit_labels:
        try:
            identity = (int(item["source_post_id"]), int(item["source_version"]))
            label = str(item["tourism_label"])
            member_key = str(item["member_key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelRetrainingDeliveryError(
                "model_retraining_delivery_audit_labels_invalid"
            ) from exc
        if (
            identity in audit_by_identity
            or label not in {"related", "unrelated", "uncertain"}
            or not member_key
        ):
            raise ModelRetrainingDeliveryError(
                "model_retraining_delivery_audit_labels_invalid"
            )
        audit_by_identity[identity] = item
    decisions: list[DeliveryDecision] = []
    training_identities: set[tuple[int, int]] = set()
    for document in training_documents:
        if (
            document.identity in training_identities
            or document.tourism_label not in {"related", "unrelated"}
        ):
            raise ModelRetrainingDeliveryError(
                "model_retraining_delivery_training_labels_invalid"
            )
        training_identities.add(document.identity)
        decisions.append(
            DeliveryDecision(
                source_post_id=document.source_post_id,
                source_version=document.source_version,
                component_id=document.component_id,
                final_action=(
                    "keep" if document.tourism_label == "related" else "exclude"
                ),
                decision_source="human_training_label",
                policy_id=None,
                p_unrelated=None,
            )
        )
    scored_identities: set[tuple[int, int]] = set()
    for record in rerouted_records:
        if record.identity in scored_identities or record.identity in training_identities:
            raise ModelRetrainingDeliveryError(
                "model_retraining_delivery_population_identity_invalid"
            )
        scored_identities.add(record.identity)
        audited = audit_by_identity.get(record.identity)
        if audited is not None:
            label = str(audited["tourism_label"])
            action = (
                "keep"
                if label == "related"
                else "exclude"
                if label == "unrelated"
                else "manual_review"
            )
            source = "human_audit_label"
        elif record.provisional_action == "auto_keep":
            action, source = "keep", "model_released_auto_keep"
        elif record.provisional_action == "auto_exclude":
            action, source = "exclude", "model_released_auto_exclude"
        elif record.provisional_action == "manual_review":
            action, source = "manual_review", "model_middle_band"
        else:
            raise ModelRetrainingDeliveryError(
                "model_retraining_delivery_routing_action_invalid"
            )
        decisions.append(
            DeliveryDecision(
                source_post_id=record.source_post_id,
                source_version=record.source_version,
                component_id=record.component_id,
                final_action=action,
                decision_source=source,
                policy_id=policy_id,
                p_unrelated=record.p_unrelated,
            )
        )
    if set(audit_by_identity) - scored_identities:
        raise ModelRetrainingDeliveryError(
            "model_retraining_delivery_audit_members_invalid"
        )
    decisions.sort(key=lambda item: item.identity)
    action_counts = dict(Counter(item.final_action for item in decisions))
    source_counts = dict(Counter(item.decision_source for item in decisions))
    if (
        len(decisions) != delivery_plan.expected_total_decision_count
        or len({item.identity for item in decisions}) != len(decisions)
        or action_counts != dict(delivery_plan.expected_action_counts)
        or source_counts != dict(delivery_plan.expected_source_counts)
    ):
        raise ModelRetrainingDeliveryError(
            "model_retraining_delivery_final_counts_mismatch"
        )
    return tuple(decisions)
