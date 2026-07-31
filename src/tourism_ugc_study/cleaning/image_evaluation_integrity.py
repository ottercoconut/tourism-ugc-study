"""从原始图片标注重算一致性与保留集审计的可信派生事实。

一致性评估和保留集审计评估都是可重建的派生结果，不能反过来证明自身
可信。本模块只读取已经冻结的计划、成员和原始人工标注，调用纯统计模块重算
全部计数、比例、状态和 manifest。仓储写入和正式门读取共用这些函数，避免
任一路径仅凭一条可直接插入的 ``passed``/``failed`` 记录放行。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Mapping

from .config import CleaningConfig
from .image_keep_audit import (
    AuditEvaluation,
    AuditObservation,
    AuditSampleMember,
    AuditSamplePlan,
    evaluate_keep_audit,
)
from .image_review_annotation import AnnotationPair, ImageAgreement, calculate_image_agreement


class ImageEvaluationIntegrityError(RuntimeError):
    """原始证据不完整或已存派生事实与重算结果不一致。

    ``reason_code`` 是去敏机器码，不包含图片身份、标签或 SQL 原文。调用方应
    把错误转换为所在仓储/门禁的领域异常，且不得据此修补或覆盖原始标注。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("image evaluation integrity validation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class AgreementAnnotationEvidence:
    """一致性评估链接的一条原始标注身份，不携带标签正文。"""

    annotation_id: str
    review_run_id: str
    fingerprint_id: str
    assignment_slot: int


@dataclass(frozen=True)
class AgreementEvidenceFacts:
    """由双标计划和原始标注重算的一致性事实及封存身份。"""

    evaluation_id: str
    plan_manifest_sha256: str
    annotation_manifest_sha256: str
    report: ImageAgreement
    annotations: tuple[AgreementAnnotationEvidence, ...]


@dataclass(frozen=True)
class KeepAuditAnnotationEvidence:
    """保留集审计评估链接的一条原始标注身份和冻结抽样层。"""

    audit_annotation_id: str
    audit_round_id: str
    fingerprint_id: str
    sampling_layer: str


@dataclass(frozen=True)
class KeepAuditEvidenceFacts:
    """由审计计划和原始标注重算的验收事实及封存身份。"""

    audit_evaluation_id: str
    evidence_manifest_sha256: str
    report: AuditEvaluation
    annotations: tuple[KeepAuditAnnotationEvidence, ...]


def _canonical_sha256(value: object) -> str:
    """以排序紧凑 JSON 计算稳定 SHA-256，不纳入创建时间。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _same_float(left: object, right: float | None, *, tolerance: float = 1.0e-9) -> bool:
    """按 SQLite 触发器相同容差比较可空浮点，拒绝 NaN 和无穷值。"""

    if left is None or right is None:
        return left is None and right is None
    try:
        observed = float(left)
    except (TypeError, ValueError):
        return False
    return math.isfinite(observed) and math.isclose(
        observed, right, rel_tol=0.0, abs_tol=tolerance
    )


def recompute_image_agreement(
    connection: sqlite3.Connection,
    *,
    review_run_id: str,
    config: CleaningConfig,
) -> AgreementEvidenceFacts:
    """从唯一冻结双标计划和真实 slot 1/2 标注重算一致性评估。

    运行必须已封存、手册与当前配置一致，且只存在一个 boundary 或 supplement
    计划。函数允许尚未完成的计划，以便生成 ``incomplete`` 追加证据；已完成
    pair 的两个槽位必须由不同匿名研究者提供。任何谱系歧义均抛出
    :class:`ImageEvaluationIntegrityError`，函数不写数据库。
    """

    run = connection.execute(
        """
        SELECT review_run_id, guide_version, seal_status
        FROM image_review_runs WHERE review_run_id = ?
        """,
        (review_run_id,),
    ).fetchone()
    if (
        run is None
        or run["seal_status"] != "finalized"
        or run["guide_version"] != config.image_label_guide_version
    ):
        raise ImageEvaluationIntegrityError("image_agreement_review_run_untrusted")
    plans = connection.execute(
        """
        SELECT plan_id, member_count, member_manifest_sha256, guide_version
        FROM image_double_label_plans
        WHERE review_run_id = ? AND seal_status = 'finalized'
          AND plan_kind IN ('boundary', 'boundary_supplement')
        ORDER BY plan_id
        """,
        (review_run_id,),
    ).fetchall()
    if len(plans) != 1 or plans[0]["guide_version"] != run["guide_version"]:
        raise ImageEvaluationIntegrityError("image_agreement_plan_untrusted")
    plan = plans[0]
    member_rows = connection.execute(
        """
        SELECT pm.fingerprint_id, pm.stable_rank
        FROM image_double_label_plan_members pm
        WHERE pm.plan_id = ? ORDER BY pm.stable_rank
        """,
        (plan["plan_id"],),
    ).fetchall()
    ranked_ids = [str(row["fingerprint_id"]) for row in member_rows]
    planned_ids = sorted(ranked_ids)
    # 计划 manifest 的科研身份包含冻结顺序，不能用评估查询的排序重写。统计
    # 计算另用排序后的身份列表，以保持 annotation manifest 与既有协议一致。
    plan_manifest = str(plan["member_manifest_sha256"])
    if (
        not planned_ids
        or len(planned_ids) != int(plan["member_count"])
        or _canonical_sha256(ranked_ids) != plan_manifest
    ):
        raise ImageEvaluationIntegrityError("image_agreement_plan_manifest_mismatch")

    placeholders = ",".join("?" for _ in planned_ids)
    annotation_rows = connection.execute(
        f"""
        SELECT annotation_id, review_run_id, fingerprint_id, assignment_slot,
               technical_noise_label, annotator_hash, guide_version
        FROM image_review_annotations
        WHERE review_run_id = ? AND fingerprint_id IN ({placeholders})
        ORDER BY fingerprint_id, assignment_slot
        """,
        (review_run_id, *planned_ids),
    ).fetchall()
    slots_by_id: dict[str, dict[int, sqlite3.Row]] = {}
    evidence: list[AgreementAnnotationEvidence] = []
    for row in annotation_rows:
        if row["guide_version"] != run["guide_version"]:
            raise ImageEvaluationIntegrityError("image_agreement_annotation_guide_mismatch")
        identity = str(row["fingerprint_id"])
        slot = int(row["assignment_slot"])
        slots_by_id.setdefault(identity, {})[slot] = row
        evidence.append(
            AgreementAnnotationEvidence(
                str(row["annotation_id"]), review_run_id, identity, slot
            )
        )
    pairs: list[AnnotationPair] = []
    for identity in planned_ids:
        slots = slots_by_id.get(identity, {})
        if 1 not in slots or 2 not in slots:
            continue
        if slots[1]["annotator_hash"] == slots[2]["annotator_hash"]:
            raise ImageEvaluationIntegrityError("image_agreement_annotators_not_independent")
        pairs.append(
            AnnotationPair(
                identity,
                str(slots[1]["technical_noise_label"]),
                str(slots[2]["technical_noise_label"]),
            )
        )
    report = calculate_image_agreement(
        pairs,
        planned_pair_count=len(planned_ids),
        minimum_raw_agreement=config.image_review.minimum_raw_agreement,
    )
    annotation_manifest = _canonical_sha256(
        [
            [item.annotation_id, item.fingerprint_id, item.assignment_slot]
            for item in evidence
        ]
    )
    evaluation_id = _canonical_sha256(
        [
            "image-agreement-v2",
            review_run_id,
            plan_manifest,
            annotation_manifest,
            report.complete_pair_count,
            report.raw_agreement,
            report.cohen_kappa,
        ]
    )[:32]
    return AgreementEvidenceFacts(
        evaluation_id,
        plan_manifest,
        annotation_manifest,
        report,
        tuple(evidence),
    )


def validate_stored_image_agreement(
    connection: sqlite3.Connection,
    row: Mapping[str, object] | sqlite3.Row,
    *,
    config: CleaningConfig,
) -> AgreementEvidenceFacts:
    """重算并逐字段验证已存一致性评估，仅接受 v20 finalized 证据。"""

    if row["seal_status"] != "finalized":
        raise ImageEvaluationIntegrityError("image_agreement_evaluation_untrusted")
    facts = recompute_image_agreement(
        connection, review_run_id=str(row["review_run_id"]), config=config
    )
    report = facts.report
    expected_scalars = {
        "evaluation_id": facts.evaluation_id,
        "plan_manifest_sha256": facts.plan_manifest_sha256,
        "annotation_manifest_sha256": facts.annotation_manifest_sha256,
        "planned_pair_count": report.planned_pair_count,
        "complete_pair_count": report.complete_pair_count,
        "agreement_count": report.agreement_count,
        "kappa_status": report.kappa_status,
        "evaluation_status": report.evaluation_status,
    }
    if any(row[key] != value for key, value in expected_scalars.items()):
        raise ImageEvaluationIntegrityError("image_agreement_evaluation_mismatch")
    if not _same_float(row["raw_agreement"], report.raw_agreement) or not _same_float(
        row["cohen_kappa"], report.cohen_kappa
    ):
        raise ImageEvaluationIntegrityError("image_agreement_evaluation_mismatch")
    try:
        disagreements = tuple(tuple(item) for item in json.loads(str(row["label_disagreements_json"])))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ImageEvaluationIntegrityError("image_agreement_evaluation_mismatch") from exc
    if disagreements != report.label_disagreements:
        raise ImageEvaluationIntegrityError("image_agreement_evaluation_mismatch")
    return facts


def recompute_keep_audit_evaluation(
    connection: sqlite3.Connection,
    *,
    audit_round_id: str,
    config: CleaningConfig,
) -> KeepAuditEvidenceFacts:
    """从冻结两层成员和原始审计标注重算一轮保留集验收。

    轮次、决定和手册必须同源；成员的抽样层、次序、概率与权重原样交给纯统计
    函数。函数可返回 incomplete 供调用方提示补标，但只有完整结果才可被仓储
    封存。未知成员、层错位或手册漂移均作为证据完整性错误处理。
    """

    parent = connection.execute(
        """
        SELECT r.population_count, r.interval_method, r.seal_status,
               d.guide_version
        FROM image_keep_audit_rounds r
        JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
        WHERE r.audit_round_id = ?
        """,
        (audit_round_id,),
    ).fetchone()
    if (
        parent is None
        or parent["seal_status"] != "finalized"
        or parent["guide_version"] != config.image_label_guide_version
    ):
        raise ImageEvaluationIntegrityError("image_keep_audit_round_untrusted")
    members = connection.execute(
        """
        SELECT fingerprint_id, platform_key, sampling_layer, stable_rank,
               inclusion_probability, sampling_weight
        FROM image_keep_audit_members WHERE audit_round_id = ?
        ORDER BY CASE sampling_layer WHEN 'primary' THEN 0 ELSE 1 END, stable_rank
        """,
        (audit_round_id,),
    ).fetchall()
    if not members:
        raise ImageEvaluationIntegrityError("image_keep_audit_members_missing")

    def member(row: sqlite3.Row) -> AuditSampleMember:
        return AuditSampleMember(
            str(row["fingerprint_id"]),
            str(row["platform_key"]),
            str(row["sampling_layer"]),
            int(row["stable_rank"]),
            float(row["inclusion_probability"]),
            float(row["sampling_weight"]),
        )

    primary = tuple(member(row) for row in members if row["sampling_layer"] == "primary")
    supplement = tuple(
        member(row) for row in members if row["sampling_layer"] == "platform_supplement"
    )
    plan = AuditSamplePlan(
        int(parent["population_count"]),
        primary,
        supplement,
        str(parent["interval_method"]),
    )
    layer_by_id = {
        item.fingerprint_id: item.sampling_layer for item in (*primary, *supplement)
    }
    annotation_rows = connection.execute(
        """
        SELECT audit_annotation_id, audit_round_id, fingerprint_id,
               technical_noise_label, guide_version
        FROM image_keep_audit_annotations
        WHERE audit_round_id = ? ORDER BY fingerprint_id
        """,
        (audit_round_id,),
    ).fetchall()
    evidence: list[KeepAuditAnnotationEvidence] = []
    observations: list[AuditObservation] = []
    for row in annotation_rows:
        identity = str(row["fingerprint_id"])
        if identity not in layer_by_id or row["guide_version"] != parent["guide_version"]:
            raise ImageEvaluationIntegrityError("image_keep_audit_annotation_lineage_mismatch")
        layer = layer_by_id[identity]
        evidence.append(
            KeepAuditAnnotationEvidence(
                str(row["audit_annotation_id"]), audit_round_id, identity, layer
            )
        )
        observations.append(
            AuditObservation(identity, layer, str(row["technical_noise_label"]))
        )
    try:
        report = evaluate_keep_audit(
            plan,
            observations,
            residual_noise_rate_max=config.image_review.residual_noise_rate_max,
            confidence_level=config.image_review.confidence_level,
        )
    except ValueError as exc:
        raise ImageEvaluationIntegrityError("image_keep_audit_observations_invalid") from exc
    evidence_manifest = _canonical_sha256(
        [[item.audit_annotation_id, item.fingerprint_id] for item in evidence]
    )
    evaluation_id = _canonical_sha256(
        ["image-keep-audit-evaluation-v2", audit_round_id, evidence_manifest]
    )[:32]
    return KeepAuditEvidenceFacts(
        evaluation_id, evidence_manifest, report, tuple(evidence)
    )


def validate_stored_keep_audit_evaluation(
    connection: sqlite3.Connection,
    row: Mapping[str, object] | sqlite3.Row,
    *,
    config: CleaningConfig,
) -> KeepAuditEvidenceFacts:
    """重算并逐字段验证已存审计评估，仅接受 v20 finalized 证据。"""

    if row["seal_status"] != "finalized":
        raise ImageEvaluationIntegrityError("image_keep_audit_evaluation_untrusted")
    facts = recompute_keep_audit_evaluation(
        connection, audit_round_id=str(row["audit_round_id"]), config=config
    )
    report = facts.report
    expected_scalars = {
        "audit_evaluation_id": facts.audit_evaluation_id,
        "evidence_manifest_sha256": facts.evidence_manifest_sha256,
        "completed_count": report.completed_count,
        "primary_event_count": report.primary_event_count,
        "supplement_event_count": report.supplement_event_count,
        "evaluation_status": report.evaluation_status,
        "reason_code": report.reason_code,
    }
    if any(row[key] != value for key, value in expected_scalars.items()):
        raise ImageEvaluationIntegrityError("image_keep_audit_evaluation_mismatch")
    if not _same_float(
        row["primary_point_estimate"], report.primary_point_estimate
    ) or not _same_float(row["one_sided_upper"], report.one_sided_upper):
        raise ImageEvaluationIntegrityError("image_keep_audit_evaluation_mismatch")
    return facts
