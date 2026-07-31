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
    AuditPopulationItem,
    AuditSampleMember,
    AuditSamplePlan,
    build_keep_audit_sample,
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
    """一致性评估链接的一条原始标注身份，不携带标签正文。

    ``annotation_id`` 必须引用不可变原始标注；``review_run_id`` 与评估父运行
    同源；``fingerprint_id`` 必须属于唯一冻结双标计划；``assignment_slot`` 仅为
    1/2。对象只描述应写入链接表的证据身份，不证明对应数据库行存在；调用方
    必须先通过重算函数，谱系缺失、跨运行、跨手册或槽位非法时函数整体失败。
    """

    annotation_id: str
    review_run_id: str
    fingerprint_id: str
    assignment_slot: int


@dataclass(frozen=True)
class AgreementEvidenceFacts:
    """由双标计划和原始标注重算的一致性事实及封存身份。

    ``evaluation_id`` 绑定 v2 算法、运行、两份 manifest 和统计结果；
    ``plan_manifest_sha256`` 冻结计划顺序，``annotation_manifest_sha256`` 冻结
    当前原始标注集合；``report`` 是纯函数重算结果；``annotations`` 是必须逐条
    持久化的链接。计划可暂时未完成，因此 report 可为 incomplete，但 manifest
    与已有标注仍须完整同源；任何身份或统计不变量失败时不返回部分对象。
    """

    evaluation_id: str
    plan_manifest_sha256: str
    annotation_manifest_sha256: str
    report: ImageAgreement
    annotations: tuple[AgreementAnnotationEvidence, ...]


@dataclass(frozen=True)
class KeepAuditAnnotationEvidence:
    """保留集审计评估链接的一条原始标注身份和冻结抽样层。

    ``audit_annotation_id`` 引用追加式原始审计标注；``audit_round_id`` 必须是
    已可信封存的同一轮；``fingerprint_id`` 必须属于该轮确定性样本；
    ``sampling_layer`` 必须与冻结成员的 primary/platform_supplement 一致。对象
    不允许补充层冒充主样本，也不代表评估已完成；任一链接越界时重算失败。
    """

    audit_annotation_id: str
    audit_round_id: str
    fingerprint_id: str
    sampling_layer: str


@dataclass(frozen=True)
class KeepAuditEvidenceFacts:
    """由审计计划和原始标注重算的验收事实及封存身份。

    ``audit_evaluation_id`` 绑定 v2 评估算法、可信轮和证据 manifest；
    ``evidence_manifest_sha256`` 覆盖按身份排序的原始标注；``report`` 分层记录
    完成数、事件数、点估计、上限和状态；``annotations`` 是封存所需链接。
    incomplete 只供调用方提示补标，不应持久化；只有完整 pass/failed 才能写入
    building 父行并链接全部证据，轮身份或观察不变量失败时无返回值。
    """

    audit_evaluation_id: str
    evidence_manifest_sha256: str
    report: AuditEvaluation
    annotations: tuple[KeepAuditAnnotationEvidence, ...]


@dataclass(frozen=True)
class KeepAuditPopulationEvidence:
    """一条可信审计人口快照证据及其稳定身份顺序。

    ``fingerprint_id`` 是决定后精确 SHA 簇展开得到的 content 图片关系身份；
    ``platform_key`` 来自同一冻结 build member 对应帖子；``population_rank`` 按
    fingerprint 字典序从 1 连续编号。对象不表达是否被抽中，也不得从父表自报
    数量构造；实际人口、平台或顺序不一致时轮完整性验证失败。
    """

    fingerprint_id: str
    platform_key: str
    population_rank: int


@dataclass(frozen=True)
class KeepAuditRoundEvidenceFacts:
    """由决定人口和冻结算法完整重建的一轮抽样事实。

    ID/决定/轮次/手册与 seed 构成父身份；三份 manifest 分别覆盖完整人口、
    primary 和平台补充；``population`` 是全量快照，``plan`` 是按当前轮 seed、
    200 主样本和每平台 30 补充规则重建的唯一结果。primary 永远非空；只有
    primary 完整覆盖人口、supplement 为空且概率/权重均为 1 时才是 census。
    旧轮不可信、人口漂移、成员/概率/权重/manifest 或抽样次序不符时不返回。
    """

    audit_round_id: str
    decision_build_id: str
    round_number: int
    guide_version: str
    random_seed: int
    population_manifest_sha256: str
    primary_manifest_sha256: str
    supplement_manifest_sha256: str
    population: tuple[KeepAuditPopulationEvidence, ...]
    plan: AuditSamplePlan


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


def _audit_sample_manifest(members: tuple[AuditSampleMember, ...]) -> str:
    """按抽样协议覆盖层、平台、次序、概率和权重。"""

    return _canonical_sha256(
        [
            [
                item.fingerprint_id,
                item.platform_key,
                item.sampling_layer,
                item.stable_rank,
                round(item.inclusion_probability, 15),
                round(item.sampling_weight, 15),
            ]
            for item in members
        ]
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


def validate_keep_audit_round_integrity(
    connection: sqlite3.Connection,
    *,
    audit_round_id: str,
    config: CleaningConfig,
) -> KeepAuditRoundEvidenceFacts:
    """从决定后的真实保留人口重建并验证审计轮全部抽样身份。

    真实人口按协议从决定代表展开到精确 SHA 簇中的 content 图片关系，并从
    同一 build member 读取平台。函数核对 v21 人口快照、跨可信旧轮排除、seed、
    两层确定性成员、概率/权重、census/Wilson 条件、三份 manifest 和轮 ID。
    输入轮必须 ``seal_status/integrity_status`` 均 finalized；任何自报字段或成员
    与重建结果不符都抛出 :class:`ImageEvaluationIntegrityError`，不读取标注、
    不写库，也不尝试修复旧轮。
    """

    parent = connection.execute(
        """
        SELECT r.*, d.candidate_build_id, d.guide_version,
               d.decision_manifest_sha256
        FROM image_keep_audit_rounds r
        JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
        WHERE r.audit_round_id = ?
        """,
        (audit_round_id,),
    ).fetchone()
    if (
        parent is None
        or parent["seal_status"] != "finalized"
        or parent["integrity_status"] != "finalized"
        or parent["sampling_algorithm_version"]
        != "image-keep-audit-sampling-v1"
        or parent["guide_version"] != config.image_label_guide_version
    ):
        raise ImageEvaluationIntegrityError("image_keep_audit_round_untrusted")
    round_number = int(parent["round_number"])
    expected_seed = config.random_seed + round_number - 1
    if int(parent["random_seed"]) != expected_seed:
        raise ImageEvaluationIntegrityError("image_keep_audit_seed_mismatch")

    actual_rows = connection.execute(
        """
        SELECT fingerprint_id, platform_key
        FROM image_keep_audit_actual_population
        WHERE decision_build_id = ? ORDER BY fingerprint_id
        """,
        (parent["decision_build_id"],),
    ).fetchall()
    if not actual_rows:
        raise ImageEvaluationIntegrityError("image_keep_audit_population_empty")
    actual_population = tuple(
        AuditPopulationItem(str(row["fingerprint_id"]), str(row["platform_key"]))
        for row in actual_rows
    )
    population_evidence = tuple(
        KeepAuditPopulationEvidence(item.fingerprint_id, item.platform_key, rank)
        for rank, item in enumerate(actual_population, start=1)
    )
    stored_population = connection.execute(
        """
        SELECT fingerprint_id, platform_key, population_rank
        FROM image_keep_audit_population_members
        WHERE audit_round_id = ? ORDER BY population_rank
        """,
        (audit_round_id,),
    ).fetchall()
    observed_population = tuple(
        KeepAuditPopulationEvidence(
            str(row["fingerprint_id"]),
            str(row["platform_key"]),
            int(row["population_rank"]),
        )
        for row in stored_population
    )
    population_manifest = _canonical_sha256(
        [[item.fingerprint_id, item.platform_key] for item in actual_population]
    )
    if (
        observed_population != population_evidence
        or int(parent["population_count"]) != len(actual_population)
        or parent["population_manifest_sha256"] != population_manifest
    ):
        raise ImageEvaluationIntegrityError("image_keep_audit_population_mismatch")

    excluded_ids = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT m.fingerprint_id FROM image_keep_audit_rounds prior
            JOIN image_decision_builds pb
              ON pb.decision_build_id = prior.decision_build_id
            JOIN image_keep_audit_members m
              ON m.audit_round_id = prior.audit_round_id
            WHERE pb.candidate_build_id = ?
              AND prior.round_number < ?
              AND prior.seal_status = 'finalized'
              AND prior.integrity_status = 'finalized'
            """,
            (parent["candidate_build_id"], round_number),
        )
    }
    try:
        plan = build_keep_audit_sample(
            actual_population,
            seed=expected_seed,
            primary_size=config.image_review.audit_primary_size,
            platform_supplement_min=config.image_review.audit_platform_supplement_min,
            excluded_fingerprint_ids=excluded_ids,
        )
    except ValueError as exc:
        raise ImageEvaluationIntegrityError("image_keep_audit_sample_invalid") from exc
    if not plan.primary_members:
        raise ImageEvaluationIntegrityError("image_keep_audit_primary_empty")
    primary_manifest = _audit_sample_manifest(plan.primary_members)
    supplement_manifest = _audit_sample_manifest(plan.supplement_members)
    expected_id = _canonical_sha256(
        [
            "image-keep-audit-v2",
            str(parent["decision_build_id"]),
            round_number,
            population_manifest,
            primary_manifest,
            supplement_manifest,
        ]
    )[:32]
    expected_members = (*plan.primary_members, *plan.supplement_members)
    stored_members = connection.execute(
        """
        SELECT fingerprint_id, platform_key, sampling_layer, stable_rank,
               inclusion_probability, sampling_weight
        FROM image_keep_audit_members WHERE audit_round_id = ?
        ORDER BY CASE sampling_layer WHEN 'primary' THEN 0 ELSE 1 END,
                 stable_rank
        """,
        (audit_round_id,),
    ).fetchall()
    if len(stored_members) != len(expected_members):
        raise ImageEvaluationIntegrityError("image_keep_audit_selection_mismatch")
    for row, expected in zip(stored_members, expected_members, strict=True):
        if (
            str(row["fingerprint_id"]) != expected.fingerprint_id
            or str(row["platform_key"]) != expected.platform_key
            or str(row["sampling_layer"]) != expected.sampling_layer
            or int(row["stable_rank"]) != expected.stable_rank
            or not _same_float(
                row["inclusion_probability"], expected.inclusion_probability,
                tolerance=1.0e-12,
            )
            or not _same_float(
                row["sampling_weight"], expected.sampling_weight
            )
        ):
            raise ImageEvaluationIntegrityError("image_keep_audit_selection_mismatch")
    if (
        str(parent["audit_round_id"]) != expected_id
        or int(parent["primary_count"]) != len(plan.primary_members)
        or int(parent["supplement_count"]) != len(plan.supplement_members)
        or str(parent["primary_manifest_sha256"]) != primary_manifest
        or str(parent["supplement_manifest_sha256"]) != supplement_manifest
        or str(parent["interval_method"]) != plan.interval_method
    ):
        raise ImageEvaluationIntegrityError("image_keep_audit_round_identity_mismatch")
    return KeepAuditRoundEvidenceFacts(
        expected_id,
        str(parent["decision_build_id"]),
        round_number,
        str(parent["guide_version"]),
        expected_seed,
        population_manifest,
        primary_manifest,
        supplement_manifest,
        population_evidence,
        plan,
    )


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

    round_facts = validate_keep_audit_round_integrity(
        connection, audit_round_id=audit_round_id, config=config
    )
    plan = round_facts.plan
    primary = plan.primary_members
    supplement = plan.supplement_members
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
        if (
            identity not in layer_by_id
            or row["guide_version"] != round_facts.guide_version
        ):
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
