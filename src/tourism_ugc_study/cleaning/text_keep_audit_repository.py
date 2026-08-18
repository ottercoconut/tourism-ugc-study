"""文本保留集人工审计的 SQLite 仓储。

仓储只从一个显式、已封存的候选帖子决定构建读取全部 ``keep`` 人口，调用
纯领域抽样器冻结至少 300 条的分平台等概率样本（人口不足则全查）。任务导出
不包含正文；人工双轴观察以追加式、整批事务导入。评估始终从真实观察重算，
先写 building 父对象和逐条证据链接，再由 schema v24 复核后封存。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from .schema import connect_derived, migrate_derived
from .text_keep_audit import (
    TextKeepAuditPlan,
    TextKeepAuditSlice,
    TextKeepAuditStratum,
    TextKeepObservation,
    TextKeepPopulationItem,
    TextKeepSampleMember,
    build_text_keep_audit_sample,
    evaluate_text_keep_audit,
)


AuditMode = Literal["formal", "smoke"]


class TextKeepAuditRepositoryError(RuntimeError):
    """审计人口、人工证据或评估契约失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("text keep audit repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class TextKeepAuditRoundResult:
    """一个已封存审计轮的身份、模式、人口和样本 manifest。"""

    audit_round_id: str
    audit_mode: AuditMode
    population_count: int
    sample_count: int
    population_manifest_sha256: str
    sample_manifest_sha256: str


@dataclass(frozen=True)
class TextKeepAuditTask:
    """可直接导出给研究者的无正文任务行。"""

    audit_round_id: str
    source_post_id: int
    source_version: int
    platform_key: str
    stratum_rank: int
    guide_version: str


@dataclass(frozen=True)
class TextKeepAuditAnnotationInput:
    """研究者填写的一条旅游相关性观察。

    ``annotator_hash`` 必须是研究者身份的外部 SHA-256，不接受姓名或临时占位
    字符串。``reason_codes`` 只保存代码，不得放入原文、路径或自由文本备注。
    """

    source_post_id: int
    source_version: int
    annotator_hash: str
    guide_version: str
    tourism_label: str
    reason_codes: tuple[str, ...]
    annotated_at_utc: str


@dataclass(frozen=True)
class TextKeepAuditImportResult:
    """一次原子导入实际关联的审计轮和标注身份。"""

    audit_round_id: str
    annotation_ids: tuple[str, ...]


@dataclass(frozen=True)
class TextKeepAuditEvaluationResult:
    """一个已封存审计评估的总体门禁、平台切片与证据 manifest。

    ``platform_slices`` 完整包含冻结人口中的每个平台，包括未抽中成员的平台；
    它们只作分层描述，不单独改变总体 ``evaluation_status``。
    """

    audit_evaluation_id: str
    audit_round_id: str
    completed_count: int
    event_count: int
    event_point_estimate: float
    one_sided_upper: float
    evaluation_status: Literal["passed", "failed"]
    evidence_manifest_sha256: str
    platform_slices: tuple[TextKeepAuditSlice, ...]


def _utcnow() -> str:
    """返回秒级 UTC 创建时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(value: object) -> str:
    """以规范 JSON 生成稳定 SHA-256。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: str) -> bool:
    """判断值是否为小写十六进制 SHA-256。"""

    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _raise(reason_code: str) -> None:
    """抛出仅含稳定机器码的仓储异常。"""

    raise TextKeepAuditRepositoryError(reason_code)


def _candidate_population(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    candidate_decision_build_id: str,
    audit_mode: AuditMode,
) -> tuple[str, tuple[TextKeepPopulationItem, ...]]:
    """读取显式候选构建的全部 keep 人口并核对模式与模型谱系。"""

    build = connection.execute(
        """
        SELECT decision_build_id, guide_version
        FROM post_decision_builds
        WHERE decision_build_id = ? AND run_id = ?
          AND build_kind = 'candidate' AND seal_status = 'finalized'
        """,
        (candidate_decision_build_id, run_id),
    ).fetchone()
    if build is None:
        _raise("finalized_candidate_decision_build_required")
    expected_model_status = "completed" if audit_mode == "formal" else "smoke"
    mismatched_model = connection.execute(
        """
        SELECT 1
        FROM post_decisions AS d
        JOIN text_model_runs AS m ON m.model_run_id = d.model_run_id
        WHERE d.decision_build_id = ? AND d.model_run_id IS NOT NULL
          AND (m.status != ? OR m.seal_status != 'finalized')
        LIMIT 1
        """,
        (candidate_decision_build_id, expected_model_status),
    ).fetchone()
    if mismatched_model is not None:
        _raise("text_keep_audit_mode_mismatch")
    population = tuple(
        TextKeepPopulationItem(
            int(row["source_post_id"]),
            int(row["source_version"]),
            str(row["platform_key"]),
        )
        for row in connection.execute(
            """
            SELECT d.source_post_id, d.source_version, p.platform_key
            FROM post_decisions AS d
            JOIN source_post_inventory AS p ON p.source_post_id = d.source_post_id
            WHERE d.decision_build_id = ? AND d.decision_action = 'keep'
            ORDER BY d.source_post_id, d.source_version
            """,
            (candidate_decision_build_id,),
        )
    )
    return str(build["guide_version"]), population


def _round_result(row: sqlite3.Row) -> TextKeepAuditRoundResult:
    """把已查询父行转换为公开结果。"""

    return TextKeepAuditRoundResult(
        str(row["audit_round_id"]),
        str(row["audit_mode"]),  # type: ignore[arg-type]
        int(row["population_count"]),
        int(row["sample_count"]),
        str(row["population_manifest_sha256"]),
        str(row["sample_manifest_sha256"]),
    )


def create_text_keep_audit_round(
    derived_db: str | Path,
    *,
    run_id: str,
    candidate_decision_build_id: str,
    audit_mode: AuditMode,
    round_number: int,
    seed: int,
    target_sample_size: int = 300,
) -> TextKeepAuditRoundResult:
    """冻结候选 keep 全人口和不可换 seed 的审计样本。

    相同候选、轮号和完整请求会复验后幂等返回。任何既有轮使用过同一人口
    manifest 都拒绝重新抽样；人口变化时也排除同运行旧轮成员，而当前 schema
    尚不能合并历史观察，因此存在交叠会稳定失败，绝不把剩余样本冒充新一轮
    完整审计。人口少于 300 条时全查，否则样本至少 300 条。
    """

    if (
        not run_id
        or not candidate_decision_build_id
        or audit_mode not in {"formal", "smoke"}
        or not isinstance(round_number, int)
        or round_number <= 0
        or not isinstance(seed, int)
        or not isinstance(target_sample_size, int)
        or target_sample_size < 300
    ):
        _raise("text_keep_audit_request_invalid")
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            with connection:
                existing = connection.execute(
                    """
                    SELECT * FROM text_keep_audit_rounds
                    WHERE candidate_decision_build_id = ? AND round_number = ?
                    """,
                    (candidate_decision_build_id, round_number),
                ).fetchone()
                guide_version, population = _candidate_population(
                    connection,
                    run_id=run_id,
                    candidate_decision_build_id=candidate_decision_build_id,
                    audit_mode=audit_mode,
                )
                if not population:
                    _raise("text_keep_audit_population_empty")
                previous_rows = tuple(
                    connection.execute(
                        """
                        SELECT audit_round_id, population_manifest_sha256
                        FROM text_keep_audit_rounds
                        WHERE run_id = ?
                          AND NOT (candidate_decision_build_id = ? AND round_number = ?)
                        ORDER BY audit_round_id
                        """,
                        (run_id, candidate_decision_build_id, round_number),
                    )
                )
                previous_member_keys = tuple(
                    f"{row['source_post_id']}:{row['source_version']}"
                    for row in connection.execute(
                        """
                        SELECT DISTINCT p.source_post_id, p.source_version
                        FROM text_keep_audit_population_members AS p
                        JOIN text_keep_audit_rounds AS r
                          ON r.audit_round_id = p.audit_round_id
                        WHERE r.run_id = ?
                          AND NOT (r.candidate_decision_build_id = ? AND r.round_number = ?)
                        ORDER BY p.source_post_id, p.source_version
                        """,
                        (run_id, candidate_decision_build_id, round_number),
                    )
                )
                plan = build_text_keep_audit_sample(
                    population,
                    seed=seed,
                    target_sample_size=target_sample_size,
                    previous_population_manifest_sha256s=(
                        str(row["population_manifest_sha256"]) for row in previous_rows
                    ),
                    previous_member_keys=previous_member_keys,
                )
                if plan.historical_excluded_count:
                    _raise("text_keep_audit_historical_evidence_combination_required")
                if existing is not None:
                    if (
                        existing["run_id"] != run_id
                        or existing["audit_mode"] != audit_mode
                        or int(existing["random_seed"]) != seed
                        or existing["population_manifest_sha256"]
                        != plan.population_manifest_sha256
                        or existing["sample_manifest_sha256"] != plan.sample_manifest_sha256
                        or existing["seal_status"] != "finalized"
                    ):
                        _raise("stored_text_keep_audit_round_mismatch")
                    return _round_result(existing)
                audit_round_id = _sha256(
                    [
                        "text-keep-audit-round-v1",
                        run_id,
                        candidate_decision_build_id,
                        audit_mode,
                        round_number,
                        seed,
                        plan.population_manifest_sha256,
                        plan.sample_manifest_sha256,
                    ]
                )[:32]
                sampling_method = (
                    "census"
                    if plan.interval_method == "census"
                    else "platform_stratified_equal_probability"
                )
                estimator = (
                    "census" if plan.interval_method == "census" else "wilson_one_sided_95"
                )
                now = _utcnow()
                connection.execute(
                    """
                    INSERT INTO text_keep_audit_rounds(
                      audit_round_id, run_id, candidate_decision_build_id,
                      audit_mode, round_number, random_seed, sampling_method,
                      estimator, population_count, population_manifest_sha256,
                      sample_count, sample_manifest_sha256, seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        audit_round_id,
                        run_id,
                        candidate_decision_build_id,
                        audit_mode,
                        round_number,
                        seed,
                        sampling_method,
                        estimator,
                        plan.source_population_count,
                        plan.population_manifest_sha256,
                        len(plan.members),
                        plan.sample_manifest_sha256,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO text_keep_audit_population_members(
                      audit_round_id, source_post_id, source_version, platform_key
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_round_id,
                            item.source_post_id,
                            item.source_version,
                            item.platform_key,
                        )
                        for item in population
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO text_keep_audit_members(
                      audit_round_id, source_post_id, source_version, platform_key,
                      stratum_rank, inclusion_probability, sampling_weight
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_round_id,
                            member.source_post_id,
                            member.source_version,
                            member.platform_key,
                            member.platform_rank,
                            member.inclusion_probability,
                            member.sampling_weight,
                        )
                        for member in plan.members
                    ],
                )
                connection.execute(
                    """
                    UPDATE text_keep_audit_rounds SET seal_status = 'finalized'
                    WHERE audit_round_id = ? AND seal_status = 'building'
                    """,
                    (audit_round_id,),
                )
                row = connection.execute(
                    "SELECT * FROM text_keep_audit_rounds WHERE audit_round_id = ?",
                    (audit_round_id,),
                ).fetchone()
                assert row is not None
                return _round_result(row)
    except TextKeepAuditRepositoryError:
        raise
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise TextKeepAuditRepositoryError("text_keep_audit_contract_rejected") from exc


def export_text_keep_audit_tasks(
    derived_db: str | Path,
    *,
    audit_round_id: str,
) -> tuple[TextKeepAuditTask, ...]:
    """导出已封存样本的无正文任务元组。

    返回值只含运行内帖子版本身份、平台、平台内次序和手册版本；研究环境可
    在受控范围内用这些身份关联正文，但仓储本身绝不把正文写入任务文件。
    """

    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            rows = tuple(
                connection.execute(
                    """
                    SELECT r.audit_round_id, m.source_post_id, m.source_version,
                           m.platform_key, m.stratum_rank, b.guide_version
                    FROM text_keep_audit_rounds AS r
                    JOIN text_keep_audit_members AS m
                      ON m.audit_round_id = r.audit_round_id
                    JOIN post_decision_builds AS b
                      ON b.decision_build_id = r.candidate_decision_build_id
                    WHERE r.audit_round_id = ? AND r.seal_status = 'finalized'
                    ORDER BY m.platform_key, m.stratum_rank,
                             m.source_post_id, m.source_version
                    """,
                    (audit_round_id,),
                )
            )
            if not rows:
                _raise("sealed_text_keep_audit_round_not_found")
            return tuple(
                TextKeepAuditTask(
                    str(row["audit_round_id"]),
                    int(row["source_post_id"]),
                    int(row["source_version"]),
                    str(row["platform_key"]),
                    int(row["stratum_rank"]),
                    str(row["guide_version"]),
                )
                for row in rows
            )
    except TextKeepAuditRepositoryError:
        raise
    except sqlite3.Error as exc:
        raise TextKeepAuditRepositoryError("text_keep_audit_export_rejected") from exc


def import_text_keep_audit_annotations(
    derived_db: str | Path,
    *,
    audit_round_id: str,
    rows: Sequence[TextKeepAuditAnnotationInput],
) -> TextKeepAuditImportResult:
    """原子追加一批真实双轴人工观察。

    每行必须属于已封存样本、使用候选构建手册版本并含真实 64 位研究者哈希。
    同一批内身份不得重复。完全相同的既有行幂等复用；同成员内容不一致时整批
    回滚。函数不接受调用方填写通过状态、事件率或 Wilson 上限。
    """

    if not audit_round_id or not rows:
        _raise("text_keep_audit_annotation_batch_empty")
    identities = [(row.source_post_id, row.source_version) for row in rows]
    if len(identities) != len(set(identities)):
        _raise("text_keep_audit_annotation_identity_duplicate")
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            with connection:
                parent = connection.execute(
                    """
                    SELECT b.guide_version
                    FROM text_keep_audit_rounds AS r
                    JOIN post_decision_builds AS b
                      ON b.decision_build_id = r.candidate_decision_build_id
                    WHERE r.audit_round_id = ? AND r.seal_status = 'finalized'
                    """,
                    (audit_round_id,),
                ).fetchone()
                if parent is None:
                    _raise("sealed_text_keep_audit_round_not_found")
                annotation_ids: list[str] = []
                now = _utcnow()
                for row in rows:
                    if (
                        row.source_post_id <= 0
                        or row.source_version <= 0
                        or not _is_sha256(row.annotator_hash)
                        or row.guide_version != parent["guide_version"]
                        or not row.annotated_at_utc
                        or not row.reason_codes
                        or any(not code or code.strip() != code for code in row.reason_codes)
                    ):
                        _raise("text_keep_audit_annotation_contract_invalid")
                    # 复用纯领域评估器的标签校验；单成员 census 计划使非法
                    # 枚举在写库前以稳定仓储错误失败。
                    observation = TextKeepObservation(
                        row.source_post_id,
                        row.source_version,
                        row.tourism_label,
                    )
                    member = connection.execute(
                        """
                        SELECT platform_key FROM text_keep_audit_members
                        WHERE audit_round_id = ? AND source_post_id = ?
                          AND source_version = ?
                        """,
                        (audit_round_id, row.source_post_id, row.source_version),
                    ).fetchone()
                    if member is None:
                        _raise("text_keep_audit_annotation_outside_sample")
                    # evaluate 的内部标签验证没有公开函数；构造最小合法计划并
                    # 运行一次即可共享唯一标签语义，结果值本身不持久化。
                    mini_plan = TextKeepAuditPlan(
                        seed=0,
                        target_sample_size=300,
                        source_population_count=1,
                        audit_population_count=1,
                        historical_excluded_count=0,
                        population_manifest_sha256="0" * 64,
                        sample_manifest_sha256="0" * 64,
                        interval_method="census",
                        strata=(TextKeepAuditStratum(str(member["platform_key"]), 1, 1),),
                        members=(
                            TextKeepSampleMember(
                                row.source_post_id,
                                row.source_version,
                                str(member["platform_key"]),
                                1,
                                1,
                                1,
                                1,
                                1.0,
                                1.0,
                            ),
                        ),
                    )
                    evaluate_text_keep_audit(mini_plan, (observation,))
                    row_payload = {
                        "annotated_at_utc": row.annotated_at_utc,
                        "annotator_hash": row.annotator_hash,
                        "audit_round_id": audit_round_id,
                        "guide_version": row.guide_version,
                        "reason_codes": sorted(set(row.reason_codes)),
                        "source_post_id": row.source_post_id,
                        "source_version": row.source_version,
                        "tourism_label": row.tourism_label,
                    }
                    row_sha = _sha256(row_payload)
                    annotation_id = _sha256(["text-keep-audit-annotation-v1", row_sha])[:32]
                    existing = connection.execute(
                        """
                        SELECT audit_annotation_id, row_sha256
                        FROM text_keep_audit_annotations
                        WHERE audit_round_id = ? AND source_post_id = ?
                          AND source_version = ?
                        """,
                        (audit_round_id, row.source_post_id, row.source_version),
                    ).fetchone()
                    if existing is not None:
                        if existing["row_sha256"] != row_sha:
                            _raise("stored_text_keep_audit_annotation_mismatch")
                        annotation_ids.append(str(existing["audit_annotation_id"]))
                        continue
                    connection.execute(
                        """
                        INSERT INTO text_keep_audit_annotations(
                          audit_annotation_id, audit_round_id, source_post_id,
                          source_version, annotator_hash, guide_version,
                          tourism_label, reason_codes_json,
                          row_sha256, annotated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            annotation_id,
                            audit_round_id,
                            row.source_post_id,
                            row.source_version,
                            row.annotator_hash,
                            row.guide_version,
                            row.tourism_label,
                            json.dumps(
                                sorted(set(row.reason_codes)),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            row_sha,
                            row.annotated_at_utc,
                        ),
                    )
                    annotation_ids.append(annotation_id)
                return TextKeepAuditImportResult(
                    audit_round_id,
                    tuple(annotation_ids),
                )
    except TextKeepAuditRepositoryError:
        raise
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise TextKeepAuditRepositoryError("text_keep_audit_annotation_contract_rejected") from exc


def _load_plan(
    connection: sqlite3.Connection,
    audit_round_id: str,
) -> tuple[sqlite3.Row, TextKeepAuditPlan]:
    """从已封存父行和成员重建纯领域评估所需计划。"""

    parent = connection.execute(
        "SELECT * FROM text_keep_audit_rounds WHERE audit_round_id = ?",
        (audit_round_id,),
    ).fetchone()
    if parent is None or parent["seal_status"] != "finalized":
        _raise("sealed_text_keep_audit_round_not_found")
    population_counts = {
        str(row["platform_key"]): int(row["population_count"])
        for row in connection.execute(
            """
            SELECT platform_key, COUNT(*) AS population_count
            FROM text_keep_audit_population_members WHERE audit_round_id = ?
            GROUP BY platform_key ORDER BY platform_key
            """,
            (audit_round_id,),
        )
    }
    member_rows = tuple(
        connection.execute(
            """
            SELECT * FROM text_keep_audit_members WHERE audit_round_id = ?
            ORDER BY platform_key, stratum_rank, source_post_id, source_version
            """,
            (audit_round_id,),
        )
    )
    sample_counts: dict[str, int] = {}
    for row in member_rows:
        key = str(row["platform_key"])
        sample_counts[key] = sample_counts.get(key, 0) + 1
    members = tuple(
        TextKeepSampleMember(
            int(row["source_post_id"]),
            int(row["source_version"]),
            str(row["platform_key"]),
            stable_rank,
            int(row["stratum_rank"]),
            population_counts[str(row["platform_key"])],
            sample_counts[str(row["platform_key"])],
            float(row["inclusion_probability"]),
            float(row["sampling_weight"]),
        )
        for stable_rank, row in enumerate(member_rows, start=1)
    )
    strata = tuple(
        TextKeepAuditStratum(platform, count, sample_counts.get(platform, 0))
        for platform, count in sorted(population_counts.items())
    )
    interval_method = (
        "census" if parent["estimator"] == "census" else "wilson_one_sided_95"
    )
    plan = TextKeepAuditPlan(
        seed=int(parent["random_seed"]),
        target_sample_size=max(300, int(parent["sample_count"])),
        source_population_count=int(parent["population_count"]),
        audit_population_count=int(parent["population_count"]),
        historical_excluded_count=0,
        population_manifest_sha256=str(parent["population_manifest_sha256"]),
        sample_manifest_sha256=str(parent["sample_manifest_sha256"]),
        interval_method=interval_method,
        strata=strata,
        members=members,
    )
    return parent, plan


def _stored_platform_slices(
    connection: sqlite3.Connection,
    *,
    audit_evaluation_id: str,
) -> tuple[TextKeepAuditSlice, ...]:
    """按平台键读取不可变切片并转换为纯领域结果类型。"""

    return tuple(
        TextKeepAuditSlice(
            platform_key=str(row["platform_key"]),
            population_count=int(row["population_count"]),
            sample_count=int(row["sample_count"]),
            completed_count=int(row["completed_count"]),
            event_count=int(row["event_count"]),
            point_estimate=(
                float(row["event_point_estimate"])
                if row["event_point_estimate"] is not None
                else None
            ),
        )
        for row in connection.execute(
            """
            SELECT platform_key, population_count, sample_count,
                   completed_count, event_count, event_point_estimate
            FROM text_keep_audit_platform_evaluations
            WHERE audit_evaluation_id = ? ORDER BY platform_key
            """,
            (audit_evaluation_id,),
        )
    )


def evaluate_and_seal_text_keep_audit(
    derived_db: str | Path,
    *,
    audit_round_id: str,
) -> TextKeepAuditEvaluationResult:
    """从已导入真实观察重算门禁、链接全部证据并封存评估。

    不完整样本不写 evaluation；研究者无需也不能提交通过状态。完整样本调用
    纯领域评估器执行 3% 点估计与单侧 95% Wilson 5% 上限，census 则以上限
    等于真实点估计。相同观察集合幂等复验，改变既有观察被追加式约束拒绝。
    """

    if not audit_round_id:
        _raise("text_keep_audit_evaluation_request_invalid")
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            with connection:
                parent, plan = _load_plan(connection, audit_round_id)
                annotation_rows = tuple(
                    connection.execute(
                        """
                        SELECT * FROM text_keep_audit_annotations
                        WHERE audit_round_id = ?
                        ORDER BY source_post_id, source_version
                        """,
                        (audit_round_id,),
                    )
                )
                observations = tuple(
                    TextKeepObservation(
                        int(row["source_post_id"]),
                        int(row["source_version"]),
                        str(row["tourism_label"]),
                    )
                    for row in annotation_rows
                )
                evaluation = evaluate_text_keep_audit(plan, observations)
                if evaluation.evaluation_status == "incomplete":
                    _raise("text_keep_audit_annotations_incomplete")
                if evaluation.ht_point_estimate is None or evaluation.one_sided_upper is None:
                    _raise("text_keep_audit_evaluation_not_computable")
                evidence_manifest = _sha256(
                    [
                        [row["audit_annotation_id"], row["row_sha256"]]
                        for row in annotation_rows
                    ]
                )
                existing = connection.execute(
                    "SELECT * FROM text_keep_audit_evaluations WHERE audit_round_id = ?",
                    (audit_round_id,),
                ).fetchone()
                if existing is not None:
                    stored_slices = _stored_platform_slices(
                        connection,
                        audit_evaluation_id=str(existing["audit_evaluation_id"]),
                    )
                    if (
                        existing["seal_status"] != "finalized"
                        or existing["evidence_manifest_sha256"] != evidence_manifest
                        or int(existing["completed_count"]) != evaluation.completed_count
                        or int(existing["event_count"]) != evaluation.event_count
                        or existing["evaluation_status"] != evaluation.evaluation_status
                        or stored_slices != evaluation.platform_slices
                    ):
                        _raise("stored_text_keep_audit_evaluation_mismatch")
                    return TextKeepAuditEvaluationResult(
                        str(existing["audit_evaluation_id"]),
                        audit_round_id,
                        int(existing["completed_count"]),
                        int(existing["event_count"]),
                        float(existing["event_point_estimate"]),
                        float(existing["one_sided_upper"]),
                        str(existing["evaluation_status"]),  # type: ignore[arg-type]
                        str(existing["evidence_manifest_sha256"]),
                        stored_slices,
                    )
                evaluation_id = _sha256(
                    [
                        "text-keep-audit-evaluation-v1",
                        audit_round_id,
                        evidence_manifest,
                    ]
                )[:32]
                reason_code = (
                    "text_keep_audit_passed"
                    if evaluation.evaluation_status == "passed"
                    else "+".join(evaluation.failure_reason_codes)
                )
                connection.execute(
                    """
                    INSERT INTO text_keep_audit_evaluations(
                      audit_evaluation_id, audit_round_id, completed_count,
                      event_count, event_point_estimate, one_sided_upper,
                      evaluation_status, reason_code, evidence_manifest_sha256,
                      seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        evaluation_id,
                        audit_round_id,
                        evaluation.completed_count,
                        evaluation.event_count,
                        evaluation.ht_point_estimate,
                        evaluation.one_sided_upper,
                        evaluation.evaluation_status,
                        reason_code,
                        evidence_manifest,
                        _utcnow(),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO text_keep_audit_evaluation_evidence_links(
                      audit_evaluation_id, audit_annotation_id
                    ) VALUES (?, ?)
                    """,
                    [
                        (evaluation_id, str(row["audit_annotation_id"]))
                        for row in annotation_rows
                    ],
                )
                # 平台切片在父评估仍为 building 时一次写齐；schema 封存 trigger
                # 会从冻结人口、样本和真实 annotation links 独立重算每项值。
                connection.executemany(
                    """
                    INSERT INTO text_keep_audit_platform_evaluations(
                      audit_evaluation_id, platform_key, population_count,
                      sample_count, completed_count, event_count,
                      event_point_estimate
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            evaluation_id,
                            item.platform_key,
                            item.population_count,
                            item.sample_count,
                            item.completed_count,
                            item.event_count,
                            item.point_estimate,
                        )
                        for item in evaluation.platform_slices
                    ],
                )
                connection.execute(
                    """
                    UPDATE text_keep_audit_evaluations SET seal_status = 'finalized'
                    WHERE audit_evaluation_id = ? AND seal_status = 'building'
                    """,
                    (evaluation_id,),
                )
                return TextKeepAuditEvaluationResult(
                    evaluation_id,
                    audit_round_id,
                    evaluation.completed_count,
                    evaluation.event_count,
                    evaluation.ht_point_estimate,
                    evaluation.one_sided_upper,
                    evaluation.evaluation_status,  # type: ignore[arg-type]
                    evidence_manifest,
                    evaluation.platform_slices,
                )
    except TextKeepAuditRepositoryError:
        raise
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise TextKeepAuditRepositoryError("text_keep_audit_evaluation_contract_rejected") from exc
