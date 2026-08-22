"""文本抽样、盲审导出和追加式人工审核证据的持久化边界。"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from tourism_ugc_study.cleaning.config import CleaningConfig
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived

from .config import annotation_config
from .sampling import (
    SamplingPost,
    build_initial_sample_plan,
    build_periodic_sample_plan,
    freeze_periodic_source_id_window,
)


# 帖子盲审文件的公开列契约。静态模板与实际导出共用这一定义，避免文档模板
# 在字段增删或顺序调整后悄悄偏离导入流程。
POST_ANNOTATION_TASK_FIELDS: tuple[str, ...] = (
    "task_id",
    "sample_run_id",
    "source_post_id",
    "source_version",
    "platform_key",
    "normalized_model_text",
    "tourism_label",
)


class AnnotationRepositoryError(RuntimeError):
    """抽样或人工证据违反冻结/追加式契约时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("annotation repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class SamplingRunResult:
    """抽样运行的非敏感身份与计数。"""

    sample_run_id: str
    sample_kind: str
    population_count: int
    probability_count: int
    targeted_count: int
    periodic_round_number: int
    output_sha256: str


@dataclass(frozen=True)
class ImportResult:
    """一次追加式导入的身份、行数和幂等复用状态。"""

    import_id: str
    record_kind: str
    row_count: int
    reused: bool


@dataclass(frozen=True)
class ReusedLabelExportResult:
    """重抽样任务、既有标签复用和待标任务导出的可审计摘要。"""

    sample_run_id: str
    optimized_row_count: int
    reused_label_count: int
    pending_label_count: int
    optimized_sha256: str
    pending_sha256: str


@dataclass(frozen=True)
class FinalizedPostAnnotationExportResult:
    """把临时待标表合回正式样本后的唯一完成表摘要。"""

    sample_run_id: str
    row_count: int
    related_count: int
    unrelated_count: int
    uncertain_count: int
    output_sha256: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: str, field: str) -> datetime:
    """解析带时区的 ISO 时间；复核间隔不得依赖本地时区或导入顺序。"""

    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnnotationRepositoryError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise AnnotationRepositoryError(f"invalid_{field}")
    return parsed.astimezone(timezone.utc)


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blind_export_order(
    rows: Sequence[sqlite3.Row], *, scope_id: str
) -> tuple[sqlite3.Row, ...]:
    """生成与输入顺序无关的稳定作业顺序。"""

    def key(row: sqlite3.Row) -> str:
        return _sha256(
            [
                scope_id,
                int(row["source_post_id"]),
                int(row["source_version"]),
            ]
        )

    return tuple(sorted(rows, key=key))


def _require_hash(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise AnnotationRepositoryError(f"invalid_{field}")
    return normalized


def _validated_tourism_label(row: Mapping[str, str]) -> str:
    """校验唯一的人工清洗标签，并拒绝已删除或旧版字段。"""

    if "commercial_label" in row:
        raise AnnotationRepositoryError("commercial_label_not_in_cleaning_contract")
    if "structure_label" in row or "review_round" in row:
        raise AnnotationRepositoryError("obsolete_cleaning_fields_present")
    if "reason_codes" in row:
        raise AnnotationRepositoryError("reason_codes_not_in_cleaning_contract")
    if "annotator_hash" in row or "annotated_at_utc" in row:
        raise AnnotationRepositoryError("row_annotation_metadata_not_in_contract")
    if "tourism_label" not in row:
        raise AnnotationRepositoryError("tourism_label_missing")
    tourism = row["tourism_label"].strip()
    if tourism not in {"related", "unrelated", "uncertain"}:
        raise AnnotationRepositoryError("invalid_tourism_label")
    return tourism


def _candidate_build_row(connection: sqlite3.Connection, build_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM text_candidate_builds
        WHERE build_id = ? AND status = 'finalized'
        """,
        (build_id,),
    ).fetchone()
    if row is None:
        raise AnnotationRepositoryError("finalized_candidate_build_not_found")
    if not int(row["is_complete_corpus"]):
        raise AnnotationRepositoryError("partial_candidate_build_not_allowed")
    return row


def _sampling_population(
    connection: sqlite3.Connection,
    build_id: str,
) -> tuple[SamplingPost, ...]:
    """从完整候选构建读取结构可用语料，不访问正式源库。"""

    rows = connection.execute(
        """
        SELECT c.source_post_id, c.source_version, c.platform_key,
               c.exact_cluster_id, n.component_id,
               length(r.normalized_model_text) AS normalized_length
        FROM text_candidate_corpus_members AS c
        JOIN text_deterministic_results AS r ON r.task_id = c.task_id
        JOIN text_near_candidate_component_members AS n
          ON n.build_id = c.build_id
         AND n.source_post_id = c.source_post_id
         AND n.source_version = c.source_version
        WHERE c.build_id = ? AND c.structure_status = 'usable'
        ORDER BY c.source_post_id, c.source_version
        """,
        (build_id,),
    ).fetchall()
    pair_counts: dict[str, list[int]] = {}
    for pair in connection.execute(
        """
        SELECT left_cluster_id, right_cluster_id, is_cross_platform
        FROM text_near_candidate_pairs WHERE build_id = ?
        """,
        (build_id,),
    ):
        for cluster_id in (str(pair["left_cluster_id"]), str(pair["right_cluster_id"])):
            counts = pair_counts.setdefault(cluster_id, [0, 0])
            counts[0] += 1
            counts[1] += int(pair["is_cross_platform"])
    return tuple(
        SamplingPost(
            source_post_id=int(row["source_post_id"]),
            source_version=int(row["source_version"]),
            platform_key=str(row["platform_key"]),
            normalized_length=int(row["normalized_length"] or 0),
            near_candidate_count=pair_counts.get(str(row["exact_cluster_id"]), [0, 0])[0],
            cross_platform_near_count=pair_counts.get(
                str(row["exact_cluster_id"]), [0, 0]
            )[1],
            near_component_id=str(row["component_id"]),
        )
        for row in rows
    )


def _sampling_member_manifest(
    connection: sqlite3.Connection,
    sample_run_id: str,
) -> str:
    """从已写入子行重建抽样输出哈希，避免只相信父表声明。"""

    rows = connection.execute(
        """
        SELECT source_post_id, source_version, platform_key, sample_frame,
               selection_reason_code, selection_rank, inclusion_probability_ppm,
               analysis_weight
        FROM text_sample_members WHERE sample_run_id = ?
        ORDER BY CASE sample_frame
                   WHEN 'probability' THEN 1
                   WHEN 'targeted' THEN 2
                   ELSE 3 END,
                 selection_rank, source_post_id, source_version
        """,
        (sample_run_id,),
    ).fetchall()
    return _sha256(
        [
            {
                "source_post_id": int(row["source_post_id"]),
                "source_version": int(row["source_version"]),
                "platform_key": str(row["platform_key"]),
                "sample_frame": str(row["sample_frame"]),
                "selection_reason_code": str(row["selection_reason_code"]),
                "selection_rank": int(row["selection_rank"]),
                "inclusion_probability_ppm": (
                    int(row["inclusion_probability_ppm"])
                    if row["inclusion_probability_ppm"] is not None
                    else None
                ),
                "analysis_weight": (
                    float(row["analysis_weight"])
                    if row["analysis_weight"] is not None
                    else None
                ),
            }
            for row in rows
        ]
    )


def _seal_sampling_run(
    connection: sqlite3.Connection,
    sample_run_id: str,
    expected_manifest: str,
) -> None:
    """核对子行哈希后执行唯一允许的 building→finalized 转换。"""

    manifest = _sampling_member_manifest(connection, sample_run_id)
    if manifest != expected_manifest:
        raise AnnotationRepositoryError("sampling_member_manifest_mismatch")
    connection.execute(
        """
        UPDATE text_sampling_runs SET seal_status = 'finalized'
        WHERE sample_run_id = ? AND seal_status = 'building'
        """,
        (sample_run_id,),
    )


def _periodic_window_manifest(
    connection: sqlite3.Connection,
    sample_run_id: str,
) -> str:
    """按冻结窗口顺序重建 source_post_id 清单哈希。"""

    return _sha256(
        tuple(
            int(row[0])
            for row in connection.execute(
                """
                SELECT source_post_id FROM text_periodic_review_window_members
                WHERE sample_run_id = ? ORDER BY window_rank
                """,
                (sample_run_id,),
            )
        )
    )


def _seal_periodic_window(
    connection: sqlite3.Connection,
    sample_run_id: str,
    expected_manifest: str,
) -> None:
    """核对全部窗口成员后封存，防止提交后跨连接追加。"""

    if _periodic_window_manifest(connection, sample_run_id) != expected_manifest:
        raise AnnotationRepositoryError("periodic_window_manifest_mismatch")
    connection.execute(
        """
        UPDATE text_periodic_review_windows SET seal_status = 'finalized'
        WHERE sample_run_id = ? AND seal_status = 'building'
        """,
        (sample_run_id,),
    )


def _validate_periodic_window(
    connection: sqlite3.Connection,
    sample_run_id: str,
) -> None:
    """幂等复用周期轮次前复核父状态、成员计数与 manifest。"""

    row = connection.execute(
        "SELECT * FROM text_periodic_review_windows WHERE sample_run_id = ?",
        (sample_run_id,),
    ).fetchone()
    if row is None or row["seal_status"] != "finalized":
        raise AnnotationRepositoryError("periodic_window_not_finalized")
    counts = connection.execute(
        """
        SELECT COUNT(*) AS member_count,
               SUM(eligible_in_candidate_build) AS eligible_count
        FROM text_periodic_review_window_members WHERE sample_run_id = ?
        """,
        (sample_run_id,),
    ).fetchone()
    if (
        int(counts["member_count"]) != int(row["window_member_count"])
        or int(counts["eligible_count"] or 0) != int(row["eligible_member_count"])
        or _periodic_window_manifest(connection, sample_run_id)
        != row["member_manifest_sha256"]
    ):
        raise AnnotationRepositoryError("periodic_window_integrity_mismatch")


def _stored_sampling_result(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> SamplingRunResult:
    """复用前重新核对封存状态、成员哈希和触发器已校验的计数。"""

    if row["seal_status"] != "finalized":
        raise AnnotationRepositoryError("sampling_run_not_finalized")
    manifest = _sampling_member_manifest(connection, str(row["sample_run_id"]))
    if manifest != row["output_sha256"] or manifest != row["member_manifest_sha256"]:
        raise AnnotationRepositoryError("sampling_run_integrity_mismatch")
    return SamplingRunResult(
        sample_run_id=str(row["sample_run_id"]),
        sample_kind=str(row["sample_kind"]),
        population_count=int(row["population_count"]),
        probability_count=int(row["probability_count"]),
        targeted_count=int(row["targeted_count"]),
        periodic_round_number=int(row["periodic_round_number"]),
        output_sha256=str(row["output_sha256"]),
    )


def create_initial_sampling_run(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    config: CleaningConfig,
) -> SamplingRunResult:
    """基于完整规范化语料创建或幂等复用首轮抽样运行。"""

    rules = annotation_config(config)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        build = _candidate_build_row(connection, candidate_build_id)
        posts = _sampling_population(connection, candidate_build_id)
        plan = build_initial_sample_plan(posts, config=rules, random_seed=config.random_seed)
        sample_run_id = _sha256(
            [
                candidate_build_id,
                "initial",
                config.text_label_guide_version,
                config.algorithm_versions["annotation_sampling"],
                config.random_seed,
                plan.population_manifest_sha256,
                plan.output_sha256,
            ]
        )[:32]
        existing = connection.execute(
            "SELECT * FROM text_sampling_runs WHERE sample_run_id = ?", (sample_run_id,)
        ).fetchone()
        if existing is not None:
            return _stored_sampling_result(connection, existing)
        probability_count = sum(m.sample_frame == "probability" for m in plan.members)
        targeted_count = sum(m.sample_frame == "targeted" for m in plan.members)
        now = _utcnow()
        with connection:
            connection.execute(
                """
                INSERT INTO text_sampling_runs(
                    sample_run_id, run_id, source_snapshot_id, candidate_build_id,
                    sample_kind, guide_version, random_seed,
                    population_manifest_sha256, population_count,
                    probability_count, targeted_count,
                    periodic_round_number, output_sha256, created_at_utc
                    , seal_status, member_manifest_sha256
                ) VALUES (?, ?, ?, ?, 'initial', ?, ?, ?, ?, ?, ?, 0, ?, ?,
                          'building', ?)
                """,
                (
                    sample_run_id,
                    str(build["run_id"]),
                    str(build["source_snapshot_id"]),
                    candidate_build_id,
                    config.text_label_guide_version,
                    config.random_seed,
                    plan.population_manifest_sha256,
                    len(posts),
                    probability_count,
                    targeted_count,
                    plan.output_sha256,
                    now,
                    plan.output_sha256,
                ),
            )
            connection.executemany(
                """
                INSERT INTO text_sample_members(
                    sample_run_id, source_post_id, source_version, platform_key,
                    sample_frame, selection_reason_code, selection_rank,
                    inclusion_probability_ppm, analysis_weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sample_run_id,
                        member.source_post_id,
                        member.source_version,
                        member.platform_key,
                        member.sample_frame,
                        member.selection_reason_code,
                        member.selection_rank,
                        member.inclusion_probability_ppm,
                        member.analysis_weight,
                    )
                    for member in plan.members
                ],
            )
            _seal_sampling_run(connection, sample_run_id, plan.output_sha256)
        return SamplingRunResult(
            sample_run_id,
            "initial",
            len(posts),
            probability_count,
            targeted_count,
            0,
            plan.output_sha256,
        )


def create_periodic_sampling_run(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    baseline_sample_run_id: str,
    round_number: int,
    config: CleaningConfig,
) -> SamplingRunResult:
    """从冻结的第 N 个 true-new source_post_id 窗口创建概率复核轮次。

    新增量来自 inventory 的首次出现快照，不使用当前行数减基线行数，因此旧帖
    删失或失效不会抵消新增量。同一 post 的新 source_version 也不会重复计数。
    每个窗口冻结全部 2,000 个身份；实际样本从其中当前 usable 的对象取至多 100。
    """

    rules = annotation_config(config)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        build = _candidate_build_row(connection, candidate_build_id)
        baseline = connection.execute(
            "SELECT * FROM text_sampling_runs WHERE sample_run_id = ? AND sample_kind = 'initial'",
            (baseline_sample_run_id,),
        ).fetchone()
        if baseline is None:
            raise AnnotationRepositoryError("baseline_sampling_run_not_found")
        existing = connection.execute(
            """
            SELECT s.* FROM text_sampling_runs AS s
            JOIN text_periodic_review_windows AS w
              ON w.sample_run_id = s.sample_run_id
            WHERE w.baseline_sample_run_id = ? AND w.round_number = ?
              AND w.seal_status = 'finalized'
            """,
            (baseline_sample_run_id, round_number),
        ).fetchone()
        if existing is not None:
            _validate_periodic_window(connection, str(existing["sample_run_id"]))
            return _stored_sampling_result(connection, existing)
        expected_round = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM text_periodic_review_windows
                WHERE baseline_sample_run_id = ?
                """,
                (baseline_sample_run_id,),
            ).fetchone()[0]
        ) + 1
        if round_number != expected_round:
            raise AnnotationRepositoryError("periodic_round_out_of_sequence")

        lineage = connection.execute(
            """
            SELECT baseline_snapshot.rowid AS baseline_rowid,
                   current_snapshot.rowid AS current_rowid,
                   baseline_snapshot.source_identity_sha256 AS baseline_source,
                   current_snapshot.source_identity_sha256 AS current_source
            FROM source_snapshots AS baseline_snapshot
            JOIN source_snapshots AS current_snapshot
            WHERE baseline_snapshot.snapshot_id = ?
              AND current_snapshot.snapshot_id = ?
            """,
            (str(baseline["source_snapshot_id"]), str(build["source_snapshot_id"])),
        ).fetchone()
        if (
            lineage is None
            or lineage["baseline_source"] != lineage["current_source"]
            or int(lineage["current_rowid"]) < int(lineage["baseline_rowid"])
        ):
            raise AnnotationRepositoryError("periodic_snapshot_lineage_mismatch")
        first_seen_rows = connection.execute(
            """
            SELECT i.source_post_id, i.current_source_version
            FROM source_post_inventory AS i
            JOIN source_snapshots AS first_snapshot
              ON first_snapshot.snapshot_id = i.first_seen_snapshot_id
            WHERE first_snapshot.source_identity_sha256 = ?
              AND first_snapshot.rowid > ? AND first_snapshot.rowid <= ?
            ORDER BY first_snapshot.rowid, i.source_post_id
            """,
            (
                str(lineage["current_source"]),
                int(lineage["baseline_rowid"]),
                int(lineage["current_rowid"]),
            ),
        ).fetchall()
        window_ids = freeze_periodic_source_id_window(
            (int(row["source_post_id"]) for row in first_seen_rows),
            round_number=round_number,
            increment_posts=rules.periodic_increment_posts,
        )
        if len(window_ids) < rules.periodic_increment_posts:
            raise AnnotationRepositoryError("periodic_increment_not_reached")
        window_id_set = set(window_ids)
        version_by_id = {
            int(row["source_post_id"]): int(row["current_source_version"])
            for row in first_seen_rows
            if int(row["source_post_id"]) in window_id_set
        }
        usable_by_id = {
            item.source_post_id: item
            for item in _sampling_population(connection, candidate_build_id)
        }
        eligible_posts = tuple(
            usable_by_id[source_post_id]
            for source_post_id in window_ids
            if source_post_id in usable_by_id
        )
        plan = build_periodic_sample_plan(
            eligible_posts,
            already_sampled_ids=(),
            sample_size=rules.periodic_probability_size,
            random_seed=config.random_seed,
            round_number=round_number,
        )
        window_manifest = _sha256(window_ids)
        sample_run_id = _sha256(
            [candidate_build_id, baseline_sample_run_id, round_number,
             window_manifest, plan.output_sha256]
        )[:32]
        now = _utcnow()
        with connection:
            connection.execute(
                """
                INSERT INTO text_sampling_runs(
                    sample_run_id, run_id, source_snapshot_id, candidate_build_id,
                    baseline_sample_run_id, sample_kind, guide_version, random_seed,
                    population_manifest_sha256, population_count,
                    probability_count, targeted_count,
                    periodic_round_number, output_sha256, created_at_utc
                    , seal_status, member_manifest_sha256
                ) VALUES (?, ?, ?, ?, ?, 'periodic_review', ?, ?, ?, ?, ?, 0,
                          ?, ?, ?, 'building', ?)
                """,
                (
                    sample_run_id,
                    str(build["run_id"]),
                    str(build["source_snapshot_id"]),
                    candidate_build_id,
                    baseline_sample_run_id,
                    config.text_label_guide_version,
                    config.random_seed,
                    window_manifest,
                    len(window_ids),
                    len(plan.members),
                    round_number,
                    plan.output_sha256,
                    now,
                    plan.output_sha256,
                ),
            )
            connection.executemany(
                """
                INSERT INTO text_sample_members(
                    sample_run_id, source_post_id, source_version, platform_key,
                    sample_frame, selection_reason_code, selection_rank,
                    inclusion_probability_ppm, analysis_weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sample_run_id,
                        member.source_post_id,
                        member.source_version,
                        member.platform_key,
                        member.sample_frame,
                        member.selection_reason_code,
                        member.selection_rank,
                        member.inclusion_probability_ppm,
                        member.analysis_weight,
                    )
                    for member in plan.members
                ],
            )
            _seal_sampling_run(connection, sample_run_id, plan.output_sha256)
            connection.execute(
                """
                INSERT INTO text_periodic_review_windows(
                    sample_run_id, baseline_sample_run_id, candidate_build_id,
                    round_number, window_start_rank, window_end_rank,
                    new_post_count_at_freeze, window_member_count,
                    eligible_member_count, member_manifest_sha256, created_at_utc
                    , seal_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building')
                """,
                (
                    sample_run_id,
                    baseline_sample_run_id,
                    candidate_build_id,
                    round_number,
                    (round_number - 1) * rules.periodic_increment_posts + 1,
                    round_number * rules.periodic_increment_posts,
                    len(first_seen_rows),
                    len(window_ids),
                    len(eligible_posts),
                    window_manifest,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO text_periodic_review_window_members(
                    sample_run_id, source_post_id, source_version, window_rank,
                    eligible_in_candidate_build
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        sample_run_id,
                        source_post_id,
                        (
                            usable_by_id[source_post_id].source_version
                            if source_post_id in usable_by_id
                            else version_by_id[source_post_id]
                        ),
                        rank,
                        int(source_post_id in usable_by_id),
                    )
                    for rank, source_post_id in enumerate(window_ids, 1)
                ],
            )
            _seal_periodic_window(connection, sample_run_id, window_manifest)
        return SamplingRunResult(
            sample_run_id,
            "periodic_review",
            len(window_ids),
            len(plan.members),
            0,
            round_number,
            plan.output_sha256,
        )


def export_post_annotation_tasks(
    derived_db: str | Path,
    *,
    sample_run_id: str,
    output_path: str | Path,
) -> int:
    """导出冻结样本的单次旅游相关性审核任务。"""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        rows = connection.execute(
            """
            SELECT DISTINCT m.source_post_id, m.source_version, m.platform_key,
                   r.normalized_model_text
            FROM text_sample_members AS m
            JOIN text_sampling_runs AS s ON s.sample_run_id = m.sample_run_id
            JOIN text_candidate_corpus_members AS c
              ON c.build_id = s.candidate_build_id
             AND c.source_post_id = m.source_post_id
             AND c.source_version = m.source_version
            JOIN text_deterministic_results AS r ON r.task_id = c.task_id
            WHERE m.sample_run_id = ?
            ORDER BY m.source_post_id, m.source_version
            """,
            (sample_run_id,),
        ).fetchall()
    rows = _blind_export_order(rows, scope_id=sample_run_id)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=POST_ANNOTATION_TASK_FIELDS,
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "task_id": _sha256(
                        [sample_run_id, int(row["source_post_id"])]
                    )[:32],
                    "sample_run_id": sample_run_id,
                    "source_post_id": row["source_post_id"],
                    "source_version": row["source_version"],
                    "platform_key": row["platform_key"],
                    "normalized_model_text": row["normalized_model_text"],
                    "tourism_label": "",
                }
            )
    return len(rows)


def export_post_annotation_tasks_reusing_labels(
    derived_db: str | Path,
    *,
    sample_run_id: str,
    previous_completed_path: str | Path,
    output_path: str | Path,
    pending_output_path: str | Path,
) -> ReusedLabelExportResult:
    """导出新抽样任务，并仅按冻结帖子身份复用已经完成的人工标签。

    复用键固定为 ``(source_post_id, source_version)``。抽样运行在调用本函数前
    已经封存，因此本函数不会读取标签值来决定哪些帖子进入新样本。旧表中未被
    新样本选中的记录保持在原文件中；新表只复制身份命中的标签及其可选元数据。
    ``pending_output_path`` 仅包含仍需人工判断的行。

    失败语义：旧表身份重复、标签非法或必要字段缺失时拒绝生成混合结果；输出
    路径相同也会被拒绝，避免覆盖唯一的历史标注证据。
    """

    previous = Path(previous_completed_path).resolve()
    optimized = Path(output_path).resolve()
    pending = Path(pending_output_path).resolve()
    if len({previous, optimized, pending}) != 3:
        raise AnnotationRepositoryError("distinct_reuse_export_paths_required")

    previous_rows = _read_csv(previous)
    required = {
        "source_post_id",
        "source_version",
        "tourism_label",
    }
    if not previous_rows or not required.issubset(previous_rows[0]):
        raise AnnotationRepositoryError("reuse_source_fields_missing")
    by_identity: dict[tuple[int, int], dict[str, str]] = {}
    for row in previous_rows:
        try:
            identity = (int(row["source_post_id"]), int(row["source_version"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AnnotationRepositoryError("invalid_reuse_source_identity") from exc
        if identity in by_identity:
            raise AnnotationRepositoryError("duplicate_reuse_source_identity")
        label = row.get("tourism_label", "").strip()
        if label not in {"related", "unrelated", "uncertain"}:
            raise AnnotationRepositoryError("invalid_reuse_tourism_label")
        by_identity[identity] = row

    optimized.parent.mkdir(parents=True, exist_ok=True)
    export_post_annotation_tasks(
        derived_db,
        sample_run_id=sample_run_id,
        output_path=optimized,
    )
    exported_rows = _read_csv(optimized)
    reused_count = 0
    for row in exported_rows:
        identity = (int(row["source_post_id"]), int(row["source_version"]))
        evidence = by_identity.get(identity)
        if evidence is None:
            continue
        row["tourism_label"] = evidence["tourism_label"].strip()
        reused_count += 1

    def write_rows(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=POST_ANNOTATION_TASK_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    write_rows(optimized, exported_rows)
    pending_rows = [row for row in exported_rows if not row["tourism_label"]]
    write_rows(pending, pending_rows)
    return ReusedLabelExportResult(
        sample_run_id=sample_run_id,
        optimized_row_count=len(exported_rows),
        reused_label_count=reused_count,
        pending_label_count=len(pending_rows),
        optimized_sha256=_file_sha256(optimized),
        pending_sha256=_file_sha256(pending),
    )


def finalize_post_annotation_tasks(
    *,
    base_path: str | Path,
    pending_path: str | Path,
    output_path: str | Path,
) -> FinalizedPostAnnotationExportResult:
    """将已完成的临时待标表合并为唯一正式完成表。

    ``base_path`` 是当前抽样运行的完整任务表，允许部分标签为空；
    ``pending_path`` 必须恰好覆盖其全部空标签身份，且每行都已填写合法标签。
    合并仅按 ``(source_post_id, source_version)`` 进行；人工表不保存逐行编码者
    身份或事后补造的标注时间。

    失败语义：路径重合、表头不符、身份重复/越界、待标覆盖不完整、标签冲突或
    最终仍有空标签时拒绝写出。输入文件不会被修改或删除；调用方只能在输出校验
    成功后显式清理临时文件。
    """

    base = Path(base_path).resolve()
    pending = Path(pending_path).resolve()
    output = Path(output_path).resolve()
    if len({base, pending, output}) != 3:
        raise AnnotationRepositoryError("distinct_finalize_paths_required")

    base_rows = _read_csv(base)
    pending_rows = _read_csv(pending)
    if not base_rows or not pending_rows:
        raise AnnotationRepositoryError("finalize_rows_missing")
    if tuple(base_rows[0]) != POST_ANNOTATION_TASK_FIELDS:
        raise AnnotationRepositoryError("finalize_base_fields_mismatch")
    if tuple(pending_rows[0]) != POST_ANNOTATION_TASK_FIELDS:
        raise AnnotationRepositoryError("finalize_pending_fields_mismatch")

    def identity(row: Mapping[str, str]) -> tuple[int, int]:
        try:
            return int(row["source_post_id"]), int(row["source_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnnotationRepositoryError("invalid_finalize_identity") from exc

    base_by_identity: dict[tuple[int, int], dict[str, str]] = {}
    blank_identities: set[tuple[int, int]] = set()
    for row in base_rows:
        key = identity(row)
        if key in base_by_identity:
            raise AnnotationRepositoryError("duplicate_finalize_base_identity")
        label = row.get("tourism_label", "").strip()
        if label and label not in {"related", "unrelated", "uncertain"}:
            raise AnnotationRepositoryError("invalid_finalize_base_label")
        base_by_identity[key] = row
        if not label:
            blank_identities.add(key)

    pending_by_identity: dict[tuple[int, int], dict[str, str]] = {}
    for row in pending_rows:
        key = identity(row)
        if key in pending_by_identity:
            raise AnnotationRepositoryError("duplicate_finalize_pending_identity")
        label = row.get("tourism_label", "").strip()
        if label not in {"related", "unrelated", "uncertain"}:
            raise AnnotationRepositoryError("incomplete_finalize_pending_label")
        pending_by_identity[key] = row

    if set(pending_by_identity) != blank_identities:
        raise AnnotationRepositoryError("finalize_pending_identity_mismatch")

    for key, evidence in pending_by_identity.items():
        target = base_by_identity[key]
        target["tourism_label"] = evidence["tourism_label"].strip()

    labels = [row["tourism_label"].strip() for row in base_rows]
    if any(label not in {"related", "unrelated", "uncertain"} for label in labels):
        raise AnnotationRepositoryError("finalize_output_incomplete")
    sample_run_ids = {row.get("sample_run_id", "").strip() for row in base_rows}
    if len(sample_run_ids) != 1 or not next(iter(sample_run_ids)):
        raise AnnotationRepositoryError("finalize_sample_run_mismatch")

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=POST_ANNOTATION_TASK_FIELDS)
        writer.writeheader()
        writer.writerows(base_rows)
    return FinalizedPostAnnotationExportResult(
        sample_run_id=next(iter(sample_run_ids)),
        row_count=len(base_rows),
        related_count=labels.count("related"),
        unrelated_count=labels.count("unrelated"),
        uncertain_count=labels.count("uncertain"),
        output_sha256=_file_sha256(output),
    )


def export_near_duplicate_candidates(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    output_path: str | Path,
) -> int:
    """导出近似重复候选对供人工复核；候选身份不会被写成确认关系。"""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        _candidate_build_row(connection, candidate_build_id)
        rows = connection.execute(
            """
            SELECT p.left_cluster_id, p.right_cluster_id, p.left_source_post_id,
                   p.right_source_post_id, p.similarity_ppm, p.is_cross_platform,
                   l.normalized_model_text AS left_text,
                   r.normalized_model_text AS right_text
            FROM text_near_candidate_pairs AS p
            JOIN text_candidate_corpus_members AS lc
              ON lc.build_id = p.build_id AND lc.source_post_id = p.left_source_post_id
            JOIN text_deterministic_results AS l ON l.task_id = lc.task_id
            JOIN text_candidate_corpus_members AS rc
              ON rc.build_id = p.build_id AND rc.source_post_id = p.right_source_post_id
            JOIN text_deterministic_results AS r ON r.task_id = rc.task_id
            WHERE p.build_id = ?
            ORDER BY p.similarity_ppm DESC, p.left_cluster_id, p.right_cluster_id
            """,
            (candidate_build_id,),
        ).fetchall()
    fields = (
        "build_id", "left_cluster_id", "right_cluster_id", "left_source_post_id",
        "right_source_post_id", "similarity_ppm", "is_cross_platform",
        "left_text", "right_text", "decision", "reason_code",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**dict(row), "build_id": candidate_build_id})
    return len(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    except (OSError, csv.Error) as exc:
        raise AnnotationRepositoryError("annotation_file_unreadable") from exc


def _start_import(
    connection: sqlite3.Connection,
    *,
    record_kind: str,
    guide_version: str,
    source_sha256: str,
    row_count: int,
    imported_by_hash: str,
) -> tuple[str, bool]:
    import_id = _sha256([record_kind, source_sha256, guide_version])[:32]
    existing = connection.execute(
        "SELECT row_count FROM text_annotation_imports WHERE import_id = ?", (import_id,)
    ).fetchone()
    if existing is not None:
        if int(existing["row_count"]) != row_count:
            raise AnnotationRepositoryError("annotation_import_identity_conflict")
        return import_id, True
    connection.execute(
        """
        INSERT INTO text_annotation_imports(
            import_id, record_kind, guide_version, source_sha256, row_count,
            imported_by_hash, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_id,
            record_kind,
            guide_version,
            source_sha256,
            row_count,
            _require_hash(imported_by_hash, "importer_hash"),
            _utcnow(),
        ),
    )
    return import_id, False


def import_post_annotations(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    guide_version: str,
    imported_by_hash: str,
) -> ImportResult:
    """追加导入一次性旅游相关性审核；重复文件只作幂等复用。"""

    path = Path(csv_path)
    rows = _read_csv(path)
    source_hash = _file_sha256(path)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        with connection:
            import_id, reused = _start_import(
                connection,
                record_kind="post_annotation",
                guide_version=guide_version,
                source_sha256=source_hash,
                row_count=len(rows),
                imported_by_hash=imported_by_hash,
            )
            if reused:
                return ImportResult(import_id, "post_annotation", len(rows), True)
            for index, row in enumerate(rows, 1):
                if row.get("guide_version", guide_version) not in ("", guide_version):
                    raise AnnotationRepositoryError("annotation_guide_version_mismatch")
                tourism_label = _validated_tourism_label(row)
                sample_run_id = row.get("sample_run_id", "").strip() or None
                post_id, source_version = int(row["source_post_id"]), int(row["source_version"])
                if sample_run_id is not None:
                    member = connection.execute(
                        """
                        SELECT 1 FROM text_sample_members
                        WHERE sample_run_id = ? AND source_post_id = ?
                          AND source_version = ?
                        LIMIT 1
                        """,
                        (sample_run_id, post_id, source_version),
                    ).fetchone()
                    if member is None:
                        raise AnnotationRepositoryError("annotation_post_not_in_sample")
                annotation_id = row.get("annotation_id", "").strip() or _sha256(
                    [import_id, index, post_id, source_version]
                )[:32]
                connection.execute(
                    """
                    INSERT INTO text_post_annotations(
                        annotation_id, import_id, sample_run_id, source_post_id,
                        source_version, tourism_label, reason_codes_json,
                        guide_version, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)
                    """,
                    (
                        annotation_id,
                        import_id,
                        sample_run_id,
                        post_id,
                        source_version,
                        tourism_label,
                        guide_version,
                        _utcnow(),
                    ),
                )
    return ImportResult(import_id, "post_annotation", len(rows), False)


def import_post_final_reviews(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    guide_version: str,
    imported_by_hash: str,
) -> ImportResult:
    """追加导入帖子最终复核；参考标签必须引用同一帖子的审核证据。"""

    path = Path(csv_path)
    rows = _read_csv(path)
    source_hash = _file_sha256(path)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        with connection:
            import_id, reused = _start_import(
                connection,
                record_kind="post_final_review",
                guide_version=guide_version,
                source_sha256=source_hash,
                row_count=len(rows),
                imported_by_hash=imported_by_hash,
            )
            if reused:
                return ImportResult(import_id, "post_final_review", len(rows), True)
            for index, row in enumerate(rows, 1):
                tourism_label = _validated_tourism_label(row)
                post_id, source_version = int(row["source_post_id"]), int(row["source_version"])
                context = row.get("decision_context", "reference").strip()
                if context not in {"reference", "model_review", "manual_review"}:
                    raise AnnotationRepositoryError("invalid_final_review_context")
                sample_run_id = row.get("sample_run_id", "").strip() or None
                adjudicator_hash = _require_hash(
                    row["reviewer_hash"], "reviewer_hash"
                )
                model_run_id = row.get("model_run_id", "").strip() or None
                if context == "model_review":
                    if model_run_id is None:
                        raise AnnotationRepositoryError("model_review_requires_model_run")
                    prediction = connection.execute(
                        """
                        SELECT 1 FROM text_model_predictions
                        WHERE model_run_id = ? AND source_post_id = ? AND source_version = ?
                        """,
                        (model_run_id, post_id, source_version),
                    ).fetchone()
                    if prediction is None:
                        raise AnnotationRepositoryError("model_review_prediction_not_found")
                elif model_run_id is not None:
                    raise AnnotationRepositoryError("model_run_only_allowed_for_model_review")
                evidence = sorted(
                    {item.strip() for item in row.get("evidence_review_ids", "").split("|") if item.strip()}
                )
                if context == "reference" and not evidence:
                    raise AnnotationRepositoryError("reference_review_requires_evidence")
                if evidence:
                    placeholders = ",".join("?" for _ in evidence)
                    evidence_rows = connection.execute(
                        f"""
                        SELECT annotation_id, sample_run_id, source_post_id, source_version,
                               guide_version
                        FROM text_post_annotations WHERE annotation_id IN ({placeholders})
                        """,
                        evidence,
                    ).fetchall()
                    if len(evidence_rows) != len(evidence) or any(
                        int(item["source_post_id"]) != post_id
                        or int(item["source_version"]) != source_version
                        or str(item["guide_version"]) != guide_version
                        or (
                            sample_run_id is not None
                            and str(item["sample_run_id"]) != sample_run_id
                        )
                        for item in evidence_rows
                    ):
                        raise AnnotationRepositoryError("final_review_evidence_mismatch")
                adjudication_id = row.get("final_review_id", "").strip() or _sha256(
                    [import_id, index, post_id, source_version]
                )[:32]
                reviewed_at = _parse_utc(row["reviewed_at_utc"], "reviewed_at_utc")
                connection.execute(
                    """
                    INSERT INTO text_post_adjudications(
                        adjudication_id, import_id, sample_run_id, source_post_id,
                        source_version, adjudicator_hash, tourism_label, reason_codes_json,
                        evidence_annotation_ids_json, decision_context, guide_version,
                        adjudicated_at_utc, created_at_utc, model_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        adjudication_id,
                        import_id,
                        sample_run_id,
                        post_id,
                        source_version,
                        adjudicator_hash,
                        tourism_label,
                        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                        context,
                        guide_version,
                        reviewed_at.isoformat(timespec="seconds"),
                        _utcnow(),
                        model_run_id,
                    ),
                )
    return ImportResult(import_id, "post_final_review", len(rows), False)


def _validate_candidate_pair(
    connection: sqlite3.Connection,
    build_id: str,
    left_cluster_id: str,
    right_cluster_id: str,
) -> None:
    pair = connection.execute(
        """
        SELECT 1
        FROM text_near_candidate_pairs AS p
        JOIN text_candidate_builds AS b ON b.build_id = p.build_id
        WHERE p.build_id = ? AND p.left_cluster_id = ? AND p.right_cluster_id = ?
          AND b.status = 'finalized'
        """,
        (build_id, left_cluster_id, right_cluster_id),
    ).fetchone()
    if pair is None:
        raise AnnotationRepositoryError("duplicate_pair_not_in_finalized_build")


def _import_duplicate_records(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    guide_version: str,
    imported_by_hash: str,
    final_review: bool,
) -> ImportResult:
    path = Path(csv_path)
    rows = _read_csv(path)
    if not final_review and any(
        "annotator_hash" in row or "annotated_at_utc" in row for row in rows
    ):
        raise AnnotationRepositoryError("row_annotation_metadata_not_in_contract")
    source_hash = _file_sha256(path)
    kind = "duplicate_final_review" if final_review else "duplicate_annotation"
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        with connection:
            import_id, reused = _start_import(
                connection,
                record_kind=kind,
                guide_version=guide_version,
                source_sha256=source_hash,
                row_count=len(rows),
                imported_by_hash=imported_by_hash,
            )
            if reused:
                return ImportResult(import_id, kind, len(rows), True)
            for index, row in enumerate(rows, 1):
                build_id = row["build_id"].strip()
                left, right = row["left_cluster_id"].strip(), row["right_cluster_id"].strip()
                _validate_candidate_pair(connection, build_id, left, right)
                if final_review:
                    reviewed_at = _parse_utc(
                        row["reviewed_at_utc"], "reviewed_at_utc"
                    )
                    evidence = sorted(
                        {item.strip() for item in row.get("evidence_review_ids", "").split("|") if item.strip()}
                    )
                    if not evidence:
                        raise AnnotationRepositoryError("duplicate_final_review_requires_evidence")
                    placeholders = ",".join("?" for _ in evidence)
                    evidence_rows = connection.execute(
                        f"""
                        SELECT annotation_id, build_id, left_cluster_id, right_cluster_id
                        FROM text_near_duplicate_annotations
                        WHERE annotation_id IN ({placeholders})
                        """,
                        evidence,
                    ).fetchall()
                    if len(evidence_rows) != len(evidence) or any(
                        str(item["build_id"]) != build_id
                        or str(item["left_cluster_id"]) != left
                        or str(item["right_cluster_id"]) != right
                        for item in evidence_rows
                    ):
                        raise AnnotationRepositoryError("duplicate_evidence_mismatch")
                    connection.execute(
                        """
                        INSERT INTO text_near_duplicate_adjudications(
                            adjudication_id, import_id, build_id, left_cluster_id,
                            right_cluster_id, adjudicator_hash, decision, reason_code,
                            evidence_annotation_ids_json, guide_version,
                            adjudicated_at_utc, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row.get("final_review_id", "").strip()
                            or _sha256([import_id, index, build_id, left, right])[:32],
                            import_id,
                            build_id,
                            left,
                            right,
                            _require_hash(row["reviewer_hash"], "reviewer_hash"),
                            row["decision"].strip(),
                            row["reason_code"].strip(),
                            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                            guide_version,
                            reviewed_at.isoformat(timespec="seconds"),
                            _utcnow(),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO text_near_duplicate_annotations(
                            annotation_id, import_id, build_id, left_cluster_id,
                            right_cluster_id, decision, reason_code,
                            guide_version, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row.get("annotation_id", "").strip()
                            or _sha256([import_id, index, build_id, left, right])[:32],
                            import_id,
                            build_id,
                            left,
                            right,
                            row["decision"].strip(),
                            row["reason_code"].strip(),
                            guide_version,
                            _utcnow(),
                        ),
                    )
    return ImportResult(import_id, kind, len(rows), False)


def import_duplicate_annotations(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    guide_version: str,
    imported_by_hash: str,
) -> ImportResult:
    """追加导入候选对原始复核，不产生已确认重复关系。"""

    return _import_duplicate_records(
        derived_db,
        csv_path=csv_path,
        guide_version=guide_version,
        imported_by_hash=imported_by_hash,
        final_review=False,
    )


def import_duplicate_final_reviews(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    guide_version: str,
    imported_by_hash: str,
) -> ImportResult:
    """追加导入候选对最终复核；只有确认的 `duplicate` 可供泄漏分组消费。"""

    return _import_duplicate_records(
        derived_db,
        csv_path=csv_path,
        guide_version=guide_version,
        imported_by_hash=imported_by_hash,
        final_review=True,
    )
