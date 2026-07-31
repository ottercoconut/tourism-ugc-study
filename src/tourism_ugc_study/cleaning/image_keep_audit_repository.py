"""图片保留集两层审计的抽样、标注与评估仓储。

本模块把已封存图片决定投影为关系级保留人口，稳定冻结 primary 主样本和
platform_supplement 补充层。CSV 与标准输出边界不含路径、URL 或图片内容；
原图和正式采集库始终只读且本模块不会打开它们。
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CleaningConfig
from .image_keep_audit import (
    AuditObservation,
    AuditPopulationItem,
    AuditSampleMember,
    AuditSamplePlan,
    build_keep_audit_sample,
    evaluate_keep_audit,
)
from .image_review_annotation import TECHNICAL_NOISE_LABELS, split_codes, validate_safe_csv_cell
from .schema import connect_derived, migrate_derived


IMAGE_AUDIT_COLUMNS = (
    "audit_round_id",
    "fingerprint_id",
    "sampling_layer",
    "technical_noise_label",
    "reason_codes",
    "annotator_hash",
    "guide_version",
    "annotated_at_utc",
)


class ImageKeepAuditRepositoryError(RuntimeError):
    """审计抽样或证据违反冻结契约时抛出的去敏领域异常。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("image keep audit repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class KeepAuditRoundResult:
    """已封存两层样本的身份、计数、区间方法和 manifest。"""

    audit_round_id: str
    decision_build_id: str
    round_number: int
    population_count: int
    primary_count: int
    supplement_count: int
    interval_method: str
    primary_manifest_sha256: str
    supplement_manifest_sha256: str


@dataclass(frozen=True)
class KeepAuditImportResult:
    """审计人工标签追加导入的去敏回执。"""

    audit_round_id: str
    imported_count: int
    evidence_manifest_sha256: str


@dataclass(frozen=True)
class KeepAuditEvaluationResult:
    """一轮保留集审计的最终或未完成评估回执。"""

    audit_evaluation_id: str
    audit_round_id: str
    evaluation_status: str
    primary_event_count: int
    supplement_event_count: int
    primary_point_estimate: float | None
    one_sided_upper: float | None
    reason_code: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _population(
    connection: sqlite3.Connection,
    decision_build_id: str,
) -> tuple[str, tuple[AuditPopulationItem, ...]]:
    """展开代表动作到 SHA 精确簇关系，并要求有标签的重复簇已传播。"""

    parent = connection.execute(
        """
        SELECT candidate_build_id, seal_status FROM image_decision_builds
        WHERE decision_build_id = ?
        """,
        (decision_build_id,),
    ).fetchone()
    if parent is None or parent["seal_status"] != "finalized":
        raise ImageKeepAuditRepositoryError("finalized_image_decision_build_required")
    candidate_build_id = str(parent["candidate_build_id"])
    missing_propagation = connection.execute(
        """
        SELECT 1 FROM image_decisions d
        JOIN image_exact_clusters c ON c.build_id = ?
          AND c.representative_fingerprint_id = d.fingerprint_id
        WHERE d.decision_build_id = ? AND c.member_count > 1
          AND d.decision_action = 'exclude'
          AND d.provenance IN ('double_agreement', 'adjudication')
          AND d.technical_noise_label IN (
            'site_background', 'site_ui', 'placeholder_or_error',
            'tracking_or_qr_only'
          )
          AND NOT EXISTS (
            SELECT 1 FROM image_sha_propagation_runs p
            WHERE p.decision_build_id = d.decision_build_id
              AND p.candidate_build_id = c.build_id
              AND p.exact_cluster_id = c.cluster_id
              AND p.seal_status = 'finalized'
          ) LIMIT 1
        """,
        (candidate_build_id, decision_build_id),
    ).fetchone()
    if missing_propagation is not None:
        raise ImageKeepAuditRepositoryError("exact_sha_propagation_required_before_audit")
    rows = connection.execute(
        """
        SELECT m.fingerprint_id, p.platform_key
        FROM image_decisions d
        JOIN image_exact_clusters c ON c.build_id = ?
          AND c.representative_fingerprint_id = d.fingerprint_id
        JOIN image_exact_cluster_members m ON m.build_id = c.build_id
          AND m.cluster_id = c.cluster_id
        JOIN image_candidate_build_members bm ON bm.build_id = m.build_id
          AND bm.fingerprint_id = m.fingerprint_id
        JOIN source_post_inventory p ON p.source_post_id = bm.source_post_id
        WHERE d.decision_build_id = ? AND d.decision_action IN ('keep', 'review')
        ORDER BY m.fingerprint_id
        """,
        (candidate_build_id, decision_build_id),
    ).fetchall()
    population = tuple(
        AuditPopulationItem(str(row["fingerprint_id"]), str(row["platform_key"]))
        for row in rows
    )
    if not population:
        raise ImageKeepAuditRepositoryError("image_keep_population_empty")
    return candidate_build_id, population


def _sample_manifest(members: tuple[AuditSampleMember, ...]) -> str:
    """覆盖层、平台、稳定次序、纳入概率和权重的抽样 manifest。"""

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


def create_keep_audit_round(
    derived_db: str | Path,
    *,
    decision_build_id: str,
    round_number: int,
    config: CleaningConfig,
) -> KeepAuditRoundResult:
    """创建至多三轮、跨同一候选构建不重叠的两层审计样本。

    primary 从全部可用保留关系等概率取至多 200；随后只对主样本不足 30 的
    平台追加至 ``min(30,Np)``。旧轮成员按同一 candidate build 全部排除，即使
    修正决定生成了新的 decision build 也不复抽；人口耗尽时明确失败。
    """

    if not 1 <= round_number <= config.image_review.audit_max_rounds:
        raise ImageKeepAuditRepositoryError("image_keep_audit_round_limit")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        candidate_build_id, population = _population(connection, decision_build_id)
        stored = connection.execute(
            """
            SELECT * FROM image_keep_audit_rounds
            WHERE decision_build_id = ? AND round_number = ?
            """,
            (decision_build_id, round_number),
        ).fetchone()
        if stored is not None:
            if stored["seal_status"] != "finalized":
                raise ImageKeepAuditRepositoryError("image_keep_audit_round_conflict")
            return KeepAuditRoundResult(
                str(stored["audit_round_id"]),
                decision_build_id,
                round_number,
                int(stored["population_count"]),
                int(stored["primary_count"]),
                int(stored["supplement_count"]),
                str(stored["interval_method"]),
                str(stored["primary_manifest_sha256"]),
                str(stored["supplement_manifest_sha256"]),
            )
        existing_round_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM image_keep_audit_rounds r
                JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
                WHERE d.candidate_build_id = ?
                """,
                (candidate_build_id,),
            ).fetchone()[0]
        )
        if (
            existing_round_count >= config.image_review.audit_max_rounds
            or round_number != existing_round_count + 1
        ):
            raise ImageKeepAuditRepositoryError("image_keep_audit_round_sequence_invalid")
        previous_ids = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT m.fingerprint_id FROM image_keep_audit_rounds r
                JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
                JOIN image_keep_audit_members m ON m.audit_round_id = r.audit_round_id
                WHERE d.candidate_build_id = ?
                """,
                (candidate_build_id,),
            )
        }
        try:
            plan = build_keep_audit_sample(
                population,
                seed=config.random_seed + round_number - 1,
                primary_size=config.image_review.audit_primary_size,
                platform_supplement_min=config.image_review.audit_platform_supplement_min,
                excluded_fingerprint_ids=previous_ids,
            )
        except ValueError as exc:
            raise ImageKeepAuditRepositoryError("image_keep_audit_population_exhausted") from exc
        population_manifest = _canonical_sha256(
            [[item.fingerprint_id, item.platform_key] for item in population]
        )
        primary_manifest = _sample_manifest(plan.primary_members)
        supplement_manifest = _sample_manifest(plan.supplement_members)
        audit_round_id = _canonical_sha256(
            [
                "image-keep-audit-v1",
                decision_build_id,
                round_number,
                population_manifest,
                primary_manifest,
                supplement_manifest,
            ]
        )[:32]
        result = KeepAuditRoundResult(
            audit_round_id,
            decision_build_id,
            round_number,
            plan.population_count,
            len(plan.primary_members),
            len(plan.supplement_members),
            plan.interval_method,
            primary_manifest,
            supplement_manifest,
        )
        existing = connection.execute(
            "SELECT seal_status FROM image_keep_audit_rounds WHERE audit_round_id = ?",
            (audit_round_id,),
        ).fetchone()
        if existing is not None:
            if existing["seal_status"] != "finalized":
                raise ImageKeepAuditRepositoryError("image_keep_audit_round_conflict")
            return result
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_keep_audit_rounds(
                      audit_round_id, decision_build_id, round_number, random_seed,
                      population_count, population_manifest_sha256, primary_count,
                      supplement_count, primary_manifest_sha256,
                      supplement_manifest_sha256, interval_method,
                      seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        audit_round_id,
                        decision_build_id,
                        round_number,
                        config.random_seed + round_number - 1,
                        plan.population_count,
                        population_manifest,
                        len(plan.primary_members),
                        len(plan.supplement_members),
                        primary_manifest,
                        supplement_manifest,
                        plan.interval_method,
                        _utcnow(),
                    ),
                )
                for member in (*plan.primary_members, *plan.supplement_members):
                    connection.execute(
                        """
                        INSERT INTO image_keep_audit_members(
                          audit_round_id, fingerprint_id, platform_key,
                          sampling_layer, stable_rank, inclusion_probability,
                          sampling_weight
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            audit_round_id,
                            member.fingerprint_id,
                            member.platform_key,
                            member.sampling_layer,
                            member.stable_rank,
                            member.inclusion_probability,
                            member.sampling_weight,
                        ),
                    )
                connection.execute(
                    "UPDATE image_keep_audit_rounds SET seal_status = 'finalized' WHERE audit_round_id = ?",
                    (audit_round_id,),
                )
        except sqlite3.IntegrityError as exc:
            raise ImageKeepAuditRepositoryError("image_keep_audit_integrity_error") from exc
    return result


def export_keep_audit_tasks(
    derived_db: str | Path,
    *,
    audit_round_id: str,
    output_path: str | Path,
) -> int:
    """导出审计标签空表，包含抽样层但不含路径、URL、权重或既有决定。"""

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        parent = connection.execute(
            """
            SELECT r.seal_status, d.guide_version FROM image_keep_audit_rounds r
            JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
            WHERE r.audit_round_id = ?
            """,
            (audit_round_id,),
        ).fetchone()
        if parent is None or parent["seal_status"] != "finalized":
            raise ImageKeepAuditRepositoryError("finalized_image_keep_audit_required")
        members = connection.execute(
            """
            SELECT fingerprint_id, sampling_layer, stable_rank
            FROM image_keep_audit_members WHERE audit_round_id = ?
            ORDER BY CASE sampling_layer WHEN 'primary' THEN 0 ELSE 1 END, stable_rank
            """,
            (audit_round_id,),
        ).fetchall()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=IMAGE_AUDIT_COLUMNS)
            writer.writeheader()
            for member in members:
                writer.writerow(
                    {
                        "audit_round_id": audit_round_id,
                        "fingerprint_id": member["fingerprint_id"],
                        "sampling_layer": member["sampling_layer"],
                        "technical_noise_label": "",
                        "reason_codes": "",
                        "annotator_hash": "",
                        "guide_version": parent["guide_version"],
                        "annotated_at_utc": "",
                    }
                )
    except OSError as exc:
        raise ImageKeepAuditRepositoryError("image_keep_audit_csv_unwritable") from exc
    return len(members)


def import_keep_audit_annotations(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
) -> KeepAuditImportResult:
    """严格校验并追加审计标签；相同对象只能有一条原始记录。

    所有单元格先经过公式注入检查，标签只接受唯一技术噪声轴，匿名标注者为
    64 位小写十六进制。整份文件任一行非法时不写入任何行。
    """

    path = Path(csv_path)
    try:
        source_bytes = path.read_bytes()
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != IMAGE_AUDIT_COLUMNS:
                raise ImageKeepAuditRepositoryError("image_keep_audit_columns_invalid")
            rows = [dict(row) for row in reader]
    except (OSError, csv.Error) as exc:
        raise ImageKeepAuditRepositoryError("image_keep_audit_csv_unreadable") from exc
    if not rows:
        raise ImageKeepAuditRepositoryError("image_keep_audit_csv_empty")
    try:
        for row in rows:
            for value in row.values():
                validate_safe_csv_cell(value)
    except ValueError as exc:
        raise ImageKeepAuditRepositoryError("image_keep_audit_formula_rejected") from exc
    round_ids = {row["audit_round_id"] for row in rows}
    if len(round_ids) != 1:
        raise ImageKeepAuditRepositoryError("image_keep_audit_mixed_rounds")
    audit_round_id = next(iter(round_ids))
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        parent = connection.execute(
            """
            SELECT r.seal_status, d.guide_version FROM image_keep_audit_rounds r
            JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
            WHERE r.audit_round_id = ?
            """,
            (audit_round_id,),
        ).fetchone()
        if parent is None or parent["seal_status"] != "finalized":
            raise ImageKeepAuditRepositoryError("finalized_image_keep_audit_required")
        prepared: list[tuple[object, ...]] = []
        for row in rows:
            if row["technical_noise_label"] not in TECHNICAL_NOISE_LABELS:
                raise ImageKeepAuditRepositoryError("image_technical_noise_label_invalid")
            annotator = row["annotator_hash"]
            if len(annotator) != 64 or any(c not in "0123456789abcdef" for c in annotator):
                raise ImageKeepAuditRepositoryError("image_annotator_hash_invalid")
            if row["guide_version"] != parent["guide_version"] or not row["annotated_at_utc"]:
                raise ImageKeepAuditRepositoryError("image_keep_audit_guide_or_time_invalid")
            try:
                reasons = split_codes(row["reason_codes"])
            except ValueError as exc:
                raise ImageKeepAuditRepositoryError("image_keep_audit_codes_invalid") from exc
            member = connection.execute(
                """
                SELECT sampling_layer FROM image_keep_audit_members
                WHERE audit_round_id = ? AND fingerprint_id = ?
                """,
                (audit_round_id, row["fingerprint_id"]),
            ).fetchone()
            if member is None or member["sampling_layer"] != row["sampling_layer"]:
                raise ImageKeepAuditRepositoryError("image_keep_audit_member_mismatch")
            row_sha = _canonical_sha256(row)
            annotation_id = _canonical_sha256(
                ["image-keep-audit-annotation-v1", audit_round_id, row["fingerprint_id"], row_sha]
            )[:32]
            prepared.append(
                (
                    annotation_id,
                    audit_round_id,
                    row["fingerprint_id"],
                    annotator,
                    row["guide_version"],
                    row["technical_noise_label"],
                    json.dumps(reasons, separators=(",", ":")),
                    row["annotated_at_utc"],
                    row_sha,
                )
            )
        try:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO image_keep_audit_annotations(
                      audit_annotation_id, audit_round_id, fingerprint_id,
                      annotator_hash, guide_version, technical_noise_label,
                      reason_codes_json, annotated_at_utc, row_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    prepared,
                )
        except sqlite3.IntegrityError as exc:
            # 完全相同文件重复导入时，逐行摘要已存在；验证计数后安全复用。
            existing = connection.execute(
                """
                SELECT COUNT(*) FROM image_keep_audit_annotations
                WHERE audit_round_id = ? AND row_sha256 IN (%s)
                """ % ",".join("?" for _ in prepared),
                (audit_round_id, *(row[-1] for row in prepared)),
            ).fetchone()[0]
            if int(existing) != len(prepared):
                raise ImageKeepAuditRepositoryError("image_keep_audit_annotation_conflict") from exc
    evidence_manifest = _canonical_sha256([source_sha, [row[0] for row in prepared]])
    return KeepAuditImportResult(audit_round_id, len(prepared), evidence_manifest)


def evaluate_keep_audit_round(
    derived_db: str | Path,
    *,
    audit_round_id: str,
    config: CleaningConfig,
) -> KeepAuditEvaluationResult:
    """计算并追加一轮两层审计评估，最多三轮且未通过不得验收。

    主样本非加权事件率和单侧 95% Wilson 只使用 primary；平台补充不进入总体
    区间，但任一技术噪声或 uncertain 同样失败。未完成标注保存 incomplete，
    后续完整证据以不同 evidence manifest 追加新评估，不覆盖旧结论。
    """

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        parent = connection.execute(
            "SELECT * FROM image_keep_audit_rounds WHERE audit_round_id = ?",
            (audit_round_id,),
        ).fetchone()
        if parent is None or parent["seal_status"] != "finalized":
            raise ImageKeepAuditRepositoryError("finalized_image_keep_audit_required")
        members = connection.execute(
            """
            SELECT fingerprint_id, platform_key, sampling_layer, stable_rank,
                   inclusion_probability, sampling_weight
            FROM image_keep_audit_members WHERE audit_round_id = ?
            ORDER BY CASE sampling_layer WHEN 'primary' THEN 0 ELSE 1 END, stable_rank
            """,
            (audit_round_id,),
        ).fetchall()
        annotations = connection.execute(
            """
            SELECT audit_annotation_id, fingerprint_id, technical_noise_label
            FROM image_keep_audit_annotations WHERE audit_round_id = ?
            ORDER BY fingerprint_id
            """,
            (audit_round_id,),
        ).fetchall()
        primary = tuple(
            AuditSampleMember(
                str(row["fingerprint_id"]), str(row["platform_key"]),
                str(row["sampling_layer"]), int(row["stable_rank"]),
                float(row["inclusion_probability"]), float(row["sampling_weight"]),
            )
            for row in members if row["sampling_layer"] == "primary"
        )
        supplement = tuple(
            AuditSampleMember(
                str(row["fingerprint_id"]), str(row["platform_key"]),
                str(row["sampling_layer"]), int(row["stable_rank"]),
                float(row["inclusion_probability"]), float(row["sampling_weight"]),
            )
            for row in members if row["sampling_layer"] == "platform_supplement"
        )
        plan = AuditSamplePlan(int(parent["population_count"]), primary, supplement, str(parent["interval_method"]))
        layer_by_id = {item.fingerprint_id: item.sampling_layer for item in (*primary, *supplement)}
        observations = [
            AuditObservation(
                str(row["fingerprint_id"]),
                layer_by_id[str(row["fingerprint_id"])],
                str(row["technical_noise_label"]),
            )
            for row in annotations
        ]
        report = evaluate_keep_audit(
            plan,
            observations,
            residual_noise_rate_max=config.image_review.residual_noise_rate_max,
            confidence_level=config.image_review.confidence_level,
        )
        evidence_manifest = _canonical_sha256(
            [[row["audit_annotation_id"], row["fingerprint_id"]] for row in annotations]
        )
        evaluation_id = _canonical_sha256(
            ["image-keep-audit-evaluation-v1", audit_round_id, evidence_manifest]
        )[:32]
        result = KeepAuditEvaluationResult(
            evaluation_id,
            audit_round_id,
            report.evaluation_status,
            report.primary_event_count,
            report.supplement_event_count,
            report.primary_point_estimate,
            report.one_sided_upper,
            report.reason_code,
        )
        existing = connection.execute(
            "SELECT 1 FROM image_keep_audit_evaluations WHERE audit_evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if existing is not None:
            return result
        # schema v16 每轮只允许一个 evaluation；不完整时只返回状态而不冻结最终行，
        # 防止缺标结论占据唯一槽位。完整 pass/fail 才追加不可变评估。
        if report.evaluation_status == "incomplete":
            return result
        with connection:
            connection.execute(
                """
                INSERT INTO image_keep_audit_evaluations(
                  audit_evaluation_id, audit_round_id, completed_count,
                  primary_event_count, supplement_event_count,
                  primary_point_estimate, one_sided_upper, evaluation_status,
                  reason_code, evidence_manifest_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    audit_round_id,
                    report.completed_count,
                    report.primary_event_count,
                    report.supplement_event_count,
                    report.primary_point_estimate,
                    report.one_sided_upper,
                    report.evaluation_status,
                    report.reason_code,
                    evidence_manifest,
                    _utcnow(),
                ),
            )
    return result
