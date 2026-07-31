"""图片技术噪声人工证据到决定动作的纯解析规则。

本模块不访问 SQLite、图片或网络。它只把同一 SHA 代表的原始槽位与可选仲裁
解析为 keep/review/exclude；默认保留绝不伪称人工 ``valid_content``，而拟排除
必须由双标同标签或合法第三人仲裁支持。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .image_review_annotation import EXCLUSION_LABELS, TECHNICAL_NOISE_LABELS


@dataclass(frozen=True)
class DecisionEvidence:
    """一个人工证据的最小去敏投影。

    ``evidence_kind`` 仅接受 ``annotation/adjudication``；annotation 必须携带
    slot 1 或 2，仲裁 slot 为空。ID 在同一代表内唯一，标签限定为清洗唯一轴。
    """

    evidence_id: str
    evidence_kind: str
    technical_noise_label: str
    assignment_slot: int | None


@dataclass(frozen=True)
class ResolvedImageDecision:
    """代表图片的最终动作、标签与证据来源。

    ``technical_noise_label`` 为空只用于没有候选/人工证据的默认保留。``review``
    表示 uncertain 仲裁结论；不会因时间压力转为排除。
    """

    technical_noise_label: str | None
    decision_action: str
    provenance: str
    evidence_id: str | None


def _action_for_label(label: str) -> str:
    """把人工技术噪声标签映射为动作，uncertain 始终保持复核。"""

    if label == "valid_content":
        return "keep"
    if label == "uncertain":
        return "review"
    if label in EXCLUSION_LABELS:
        return "exclude"
    raise ValueError("unknown technical noise label")


def resolve_image_decision(
    evidence: Sequence[DecisionEvidence],
    *,
    is_candidate: bool,
) -> ResolvedImageDecision:
    """按仲裁、双标、单人有效内容的顺序解析一个代表决定。

    非候选且没有证据时返回无标签的 ``default_keep_no_candidate``。候选必须至少
    完成 slot 1；单人只能确认 ``valid_content``。任一拟排除或 uncertain 必须
    有独立 slot 2，分歧或任一 uncertain 还必须有第三人仲裁。多个仲裁或完整
    双标结论冲突时抛出 ``ValueError``，调用方不得自行挑选有利证据。
    """

    identifiers = [item.evidence_id for item in evidence]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("decision evidence ids must be unique")
    for item in evidence:
        if item.technical_noise_label not in TECHNICAL_NOISE_LABELS:
            raise ValueError("unknown technical noise label")
        if item.evidence_kind not in {"annotation", "adjudication"}:
            raise ValueError("unknown decision evidence kind")
        if item.evidence_kind == "annotation" and item.assignment_slot not in (1, 2):
            raise ValueError("annotation evidence requires slot 1 or 2")
        if item.evidence_kind == "adjudication" and item.assignment_slot is not None:
            raise ValueError("adjudication evidence must not have a slot")

    adjudications = [item for item in evidence if item.evidence_kind == "adjudication"]
    if adjudications:
        labels = {item.technical_noise_label for item in adjudications}
        if len(labels) != 1:
            raise ValueError("conflicting image adjudications")
        chosen = min(adjudications, key=lambda item: item.evidence_id)
        return ResolvedImageDecision(
            chosen.technical_noise_label,
            _action_for_label(chosen.technical_noise_label),
            "adjudication",
            chosen.evidence_id,
        )

    annotations = [item for item in evidence if item.evidence_kind == "annotation"]
    by_slot: dict[int, DecisionEvidence] = {}
    for item in annotations:
        assert item.assignment_slot is not None
        if item.assignment_slot in by_slot:
            raise ValueError("multiple annotations for one assignment slot")
        by_slot[item.assignment_slot] = item
    if not by_slot:
        if is_candidate:
            raise ValueError("candidate representative requires human review")
        return ResolvedImageDecision(None, "keep", "default_keep_no_candidate", None)
    if 1 not in by_slot:
        raise ValueError("slot 1 annotation is required")
    first = by_slot[1]
    if 2 not in by_slot:
        if first.technical_noise_label != "valid_content":
            raise ValueError("proposed exclusion or uncertain requires slot 2")
        return ResolvedImageDecision(
            "valid_content", "keep", "single_valid_content", first.evidence_id
        )
    second = by_slot[2]
    if first.technical_noise_label != second.technical_noise_label:
        raise ValueError("annotation disagreement requires adjudication")
    if first.technical_noise_label == "uncertain":
        raise ValueError("uncertain annotation requires adjudication")
    evidence_id = "+".join(sorted((first.evidence_id, second.evidence_id)))
    return ResolvedImageDecision(
        first.technical_noise_label,
        _action_for_label(first.technical_noise_label),
        "double_agreement",
        evidence_id,
    )

