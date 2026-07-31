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
from .image_evaluation_integrity import (
    ImageEvaluationIntegrityError,
    recompute_keep_audit_evaluation,
    validate_keep_audit_round_integrity,
    validate_stored_keep_audit_evaluation,
)
from .image_keep_audit import (
    AuditPopulationItem,
    AuditSampleMember,
    build_keep_audit_sample,
)
from .image_review_annotation import TECHNICAL_NOISE_LABELS, split_codes, validate_safe_csv_cell
from .image_review_gate import ImageReviewGateError, validate_formal_image_review_gate
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
    """审计抽样或证据违反冻结契约时抛出的去敏领域异常。

    ``reason_code`` 不含路径、图片、逐条标签或 SQLite 原文；异常表示当前事务
    未封存，调用方可据机器码补标、修订决定或终止，而不能跳过质量门。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("image keep audit repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class KeepAuditRoundResult:
    """已封存两层样本的身份、计数、区间方法和 manifest。

    运行/决定 ID 与 ``round_number`` 绑定轮次谱系；``population_count`` 是完整
    保留人口，primary/supplement 计数是实际任务量。两个 manifest 分别冻结层
    内成员、次序、概率和权重；``interval_method`` 决定总体上限算法。
    """

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
    """审计人工标签追加导入的去敏回执。

    ``imported_count`` 是本次文件内合法成员数，``evidence_manifest_sha256``
    绑定源文件和追加式 annotation ID。回执不含逐图标签或文件路径；相同输入
    幂等复用既有行，冲突输入不返回结果。
    """

    audit_round_id: str
    imported_count: int
    evidence_manifest_sha256: str


@dataclass(frozen=True)
class KeepAuditEvaluationResult:
    """一轮保留集审计的最终或未完成评估回执。

    评估/轮次 ID 固定谱系；两层事件数分开报告。未完成时点估计和单侧上限
    为空，完成时 ``evaluation_status`` 为 passed/failed，``reason_code`` 是稳定
    质量结论。补充层事件不进入总体估计但仍计数并阻断通过。
    """

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
    平台追加至 ``min(30,Np)``。第二轮起必须等待上一轮完整评估为 failed，并
    使用决定 manifest 已变化的新 decision build；旧成员按同一 candidate build
    全部排除。成功返回可幂等复用的封存回执；上一轮未评估、已通过、决定未
    修正、人口耗尽、正式复核门未通过或数据库谱系冲突时抛出
    :class:`ImageKeepAuditRepositoryError`，不留下部分成员。
    """

    if not 1 <= round_number <= config.image_review.audit_max_rounds:
        raise ImageKeepAuditRepositoryError("image_keep_audit_round_limit")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        candidate_build_id, population = _population(connection, decision_build_id)
        try:
            validate_formal_image_review_gate(
                connection,
                candidate_build_id=candidate_build_id,
                config=config,
            )
        except ImageReviewGateError as exc:
            raise ImageKeepAuditRepositoryError(exc.reason_code) from exc
        stored = connection.execute(
            """
            SELECT * FROM image_keep_audit_rounds
            WHERE decision_build_id = ? AND round_number = ?
            """,
            (decision_build_id, round_number),
        ).fetchone()
        if stored is not None:
            try:
                validate_keep_audit_round_integrity(
                    connection, audit_round_id=str(stored["audit_round_id"]),
                    config=config,
                )
            except ImageEvaluationIntegrityError as exc:
                raise ImageKeepAuditRepositoryError(exc.reason_code) from exc
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
        prior_rounds = connection.execute(
            """
            SELECT r.audit_round_id, r.round_number, r.decision_build_id,
                   d.decision_manifest_sha256
            FROM image_keep_audit_rounds r
            JOIN image_decision_builds d
              ON d.decision_build_id = r.decision_build_id
            WHERE d.candidate_build_id = ?
            ORDER BY r.round_number
            """,
            (candidate_build_id,),
        ).fetchall()
        existing_round_count = len(prior_rounds)
        if (
            existing_round_count >= config.image_review.audit_max_rounds
            or round_number != existing_round_count + 1
        ):
            raise ImageKeepAuditRepositoryError("image_keep_audit_round_sequence_invalid")
        if prior_rounds:
            previous = prior_rounds[-1]
            evaluation_rows = connection.execute(
                """
                SELECT * FROM image_keep_audit_evaluations
                WHERE audit_round_id = ? AND seal_status = 'finalized'
                ORDER BY created_at_utc DESC, audit_evaluation_id DESC
                """,
                (previous["audit_round_id"],),
            ).fetchall()
            trusted_evaluation = None
            for row in evaluation_rows:
                try:
                    validate_stored_keep_audit_evaluation(
                        connection, row, config=config
                    )
                except ImageEvaluationIntegrityError:
                    continue
                trusted_evaluation = row
                break
            if trusted_evaluation is None:
                raise ImageKeepAuditRepositoryError(
                    "previous_image_keep_audit_not_evaluated"
                )
            if trusted_evaluation["evaluation_status"] != "failed":
                raise ImageKeepAuditRepositoryError(
                    "previous_image_keep_audit_not_failed"
                )
            current_manifest = connection.execute(
                """
                SELECT decision_manifest_sha256 FROM image_decision_builds
                WHERE decision_build_id = ?
                """,
                (decision_build_id,),
            ).fetchone()[0]
            if (
                decision_build_id == previous["decision_build_id"]
                or current_manifest == previous["decision_manifest_sha256"]
            ):
                raise ImageKeepAuditRepositoryError(
                    "revised_image_decision_build_required"
                )
        previous_ids = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT m.fingerprint_id FROM image_keep_audit_rounds r
                JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
                JOIN image_keep_audit_members m ON m.audit_round_id = r.audit_round_id
                WHERE d.candidate_build_id = ?
                  AND r.seal_status = 'finalized'
                  AND r.integrity_status = 'finalized'
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
                "image-keep-audit-v2",
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
            "SELECT * FROM image_keep_audit_rounds WHERE audit_round_id = ?",
            (audit_round_id,),
        ).fetchone()
        if existing is not None:
            try:
                validate_keep_audit_round_integrity(
                    connection, audit_round_id=audit_round_id, config=config
                )
            except ImageEvaluationIntegrityError as exc:
                raise ImageKeepAuditRepositoryError(exc.reason_code) from exc
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
                      seal_status, created_at_utc, integrity_status,
                      sampling_algorithm_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?,
                              'building', 'image-keep-audit-sampling-v1')
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
                connection.executemany(
                    """
                    INSERT INTO image_keep_audit_population_members(
                      audit_round_id, fingerprint_id, platform_key,
                      population_rank
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (audit_round_id, item.fingerprint_id, item.platform_key, rank)
                        for rank, item in enumerate(population, start=1)
                    ],
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
                    """
                    UPDATE image_keep_audit_rounds
                    SET seal_status = 'finalized', integrity_status = 'finalized'
                    WHERE audit_round_id = ?
                    """,
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
    """导出审计标签空表，包含抽样层但不含路径、URL、权重或既有决定。

    输入必须指向已封存轮次；成功返回按主样本、补充层稳定排序的数据行数。
    父轮缺失或输出不可写时抛出领域异常，输出路径不会写入派生库或 JSON 回执。
    """

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        parent = connection.execute(
            """
            SELECT r.seal_status, r.integrity_status, d.guide_version
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
        ):
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
    64 位小写十六进制。成功返回导入计数和证据 manifest；整份文件任一行的
    表头、公式、成员、层、手册、时间、标签或理由非法时抛出领域异常且不写
    任何行。已有同身份不同内容被视为冲突，不能覆盖。
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
            SELECT r.seal_status, r.integrity_status, d.guide_version
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
        ):
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
    区间，但任一技术噪声或 uncertain 同样失败。未完成标注只返回 incomplete
    回执，不写评估表；补齐后才以完整 evidence manifest 追加不可变结论。成功
    返回完整或 incomplete 回执；父轮人口/抽样身份不可信、样本/观察不一致或
    数据库约束失败时抛出领域异常。只有完整 pass/fail 才持久化最终评估行。
    """

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        try:
            facts = recompute_keep_audit_evaluation(
                connection, audit_round_id=audit_round_id, config=config
            )
        except ImageEvaluationIntegrityError as exc:
            raise ImageKeepAuditRepositoryError(exc.reason_code) from exc
        report = facts.report
        evidence_manifest = facts.evidence_manifest_sha256
        evaluation_id = facts.audit_evaluation_id
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
            "SELECT * FROM image_keep_audit_evaluations WHERE audit_evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if existing is not None:
            try:
                validate_stored_keep_audit_evaluation(
                    connection, existing, config=config
                )
            except ImageEvaluationIntegrityError as exc:
                raise ImageKeepAuditRepositoryError(exc.reason_code) from exc
            return result
        # schema v20 每轮只允许一个可信 evaluation；不完整时只返回状态而不
        # 冻结最终行，防止缺标结论占据唯一槽位。完整 pass/fail 才链接全部
        # 原始 annotation 并追加不可变评估。
        if report.evaluation_status == "incomplete":
            return result
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_keep_audit_evaluations(
                      audit_evaluation_id, audit_round_id, completed_count,
                      primary_event_count, supplement_event_count,
                      primary_point_estimate, one_sided_upper, evaluation_status,
                      reason_code, evidence_manifest_sha256, seal_status,
                      created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
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
                connection.executemany(
                    """
                    INSERT INTO image_keep_audit_evaluation_annotations(
                      audit_evaluation_id, audit_annotation_id, audit_round_id,
                      fingerprint_id, sampling_layer
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            evaluation_id,
                            item.audit_annotation_id,
                            item.audit_round_id,
                            item.fingerprint_id,
                            item.sampling_layer,
                        )
                        for item in facts.annotations
                    ],
                )
                connection.execute(
                    """
                    UPDATE image_keep_audit_evaluations
                    SET seal_status = 'finalized' WHERE audit_evaluation_id = ?
                    """,
                    (evaluation_id,),
                )
        except sqlite3.IntegrityError as exc:
            raise ImageKeepAuditRepositoryError(
                "image_keep_audit_evaluation_integrity_error"
            ) from exc
    return result
