"""文本抽样、盲审导出和追加式人工审核证据的持久化边界。"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from tourism_ugc_study.cleaning.config import CleaningConfig
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived

from .agreement import evaluate_planned_agreement
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
    "review_round",
    "normalized_model_text",
    "structure_label",
    "tourism_label",
    "reason_codes",
    "annotator_hash",
    "annotated_at_utc",
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
    recheck_count: int
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
class AgreementWorkflowResult:
    """一次不可变复核稳定性评估及其补充复核轮次。"""

    evaluation_id: str
    status: str
    planned_pair_count: int
    complete_pair_count: int
    metrics: Mapping[str, object] | None
    additional_recheck_required: int
    supplement_run_id: str | None
    supplement_selected_count: int


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


def _require_hash(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise AnnotationRepositoryError(f"invalid_{field}")
    return normalized


def _json_list(value: str) -> str:
    """把 `|` 分隔的理由/证据列转成稳定 JSON 数组。"""

    items = sorted({item.strip() for item in value.split("|") if item.strip()})
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _validated_cleaning_labels(row: Mapping[str, str]) -> tuple[str, str]:
    """校验清洗双轴标签及其条件适用关系。

    商业属性已从清洗契约移除；即使旧文件把该列留空也拒绝导入，以免研究者
    继续沿用过期模板。结构无效与旅游不适用必须双向对应，数据库 CHECK 会在
    绕过 repository 写入时再次执行同一不变量。
    """

    if "commercial_label" in row:
        raise AnnotationRepositoryError("commercial_label_not_in_cleaning_contract")
    if "structure_label" not in row or "tourism_label" not in row:
        raise AnnotationRepositoryError("cleaning_label_fields_missing")
    structure = row["structure_label"].strip()
    tourism = row["tourism_label"].strip()
    if structure not in {"usable", "invalid", "uncertain"}:
        raise AnnotationRepositoryError("invalid_structure_label")
    if tourism not in {"related", "unrelated", "uncertain", "not_applicable"}:
        raise AnnotationRepositoryError("invalid_tourism_label")
    if (structure == "invalid") != (tourism == "not_applicable"):
        raise AnnotationRepositoryError("tourism_applicability_conflict")
    return structure, tourism


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
               c.exact_cluster_id, length(r.normalized_model_text) AS normalized_length
        FROM text_candidate_corpus_members AS c
        JOIN text_deterministic_results AS r ON r.task_id = c.task_id
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
               analysis_weight, requires_double_label
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
                "requires_recheck": bool(row["requires_double_label"]),
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
        recheck_count=int(row["double_label_count"]),
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
            [candidate_build_id, "initial", config.text_label_guide_version, config.random_seed,
             plan.population_manifest_sha256, plan.output_sha256]
        )[:32]
        existing = connection.execute(
            "SELECT * FROM text_sampling_runs WHERE sample_run_id = ?", (sample_run_id,)
        ).fetchone()
        if existing is not None:
            return _stored_sampling_result(connection, existing)
        probability_count = sum(m.sample_frame == "probability" for m in plan.members)
        targeted_count = sum(m.sample_frame == "targeted" for m in plan.members)
        recheck_count = len(
            {
                (m.source_post_id, m.source_version)
                for m in plan.members
                if m.requires_recheck
            }
        )
        now = _utcnow()
        with connection:
            connection.execute(
                """
                INSERT INTO text_sampling_runs(
                    sample_run_id, run_id, source_snapshot_id, candidate_build_id,
                    sample_kind, guide_version, random_seed,
                    population_manifest_sha256, population_count,
                    probability_count, targeted_count, double_label_count,
                    periodic_round_number, output_sha256, created_at_utc
                    , seal_status, member_manifest_sha256
                ) VALUES (?, ?, ?, ?, 'initial', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?,
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
                    recheck_count,
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
                    inclusion_probability_ppm, analysis_weight, requires_double_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        int(member.requires_recheck),
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
            recheck_count,
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
                    probability_count, targeted_count, double_label_count,
                    periodic_round_number, output_sha256, created_at_utc
                    , seal_status, member_manifest_sha256
                ) VALUES (?, ?, ?, ?, ?, 'periodic_review', ?, ?, ?, ?, ?, 0, 0,
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
                    inclusion_probability_ppm, analysis_weight, requires_double_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
            0,
            round_number,
            plan.output_sha256,
        )


def export_post_annotation_tasks(
    derived_db: str | Path,
    *,
    sample_run_id: str,
    review_round: int,
    output_path: str | Path,
) -> int:
    """导出一个盲审轮次；第二轮只含冻结复核样本且不暴露初审结果。"""

    if review_round not in (1, 2):
        raise AnnotationRepositoryError("invalid_review_round")
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
              AND (? = 1 OR m.requires_double_label = 1)
            ORDER BY m.source_post_id, m.source_version
            """,
            (sample_run_id, review_round),
        ).fetchall()
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
                        [sample_run_id, int(row["source_post_id"]), review_round]
                    )[:32],
                    "sample_run_id": sample_run_id,
                    "source_post_id": row["source_post_id"],
                    "source_version": row["source_version"],
                    "platform_key": row["platform_key"],
                    "review_round": review_round,
                    "normalized_model_text": row["normalized_model_text"],
                    "structure_label": "",
                    "tourism_label": "",
                    "reason_codes": "",
                    "annotator_hash": "",
                    "annotated_at_utc": "",
                }
            )
    return len(rows)


def export_supplement_annotation_tasks(
    derived_db: str | Path,
    *,
    supplement_run_id: str,
    review_round: int,
    output_path: str | Path,
) -> int:
    """导出补充复核的初审/复核轮次；两轮包含相同的冻结成员。"""

    if review_round not in (1, 2):
        raise AnnotationRepositoryError("invalid_review_round")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        _validate_supplement(connection, supplement_run_id)
        rows = connection.execute(
            """
            SELECT m.sample_run_id, m.source_post_id, m.source_version,
                   c.platform_key, r.normalized_model_text
            FROM text_double_label_supplement_members AS m
            JOIN text_double_label_supplements AS s
              ON s.supplement_run_id = m.supplement_run_id
             AND s.seal_status = 'finalized'
            JOIN text_sampling_runs AS sampling
              ON sampling.sample_run_id = m.sample_run_id
            JOIN text_candidate_corpus_members AS c
              ON c.build_id = sampling.candidate_build_id
             AND c.source_post_id = m.source_post_id
             AND c.source_version = m.source_version
            JOIN text_deterministic_results AS r ON r.task_id = c.task_id
            WHERE m.supplement_run_id = ?
            ORDER BY m.selection_rank
            """,
            (supplement_run_id,),
        ).fetchall()
        if not rows and connection.execute(
            """
            SELECT 1 FROM text_double_label_supplements
            WHERE supplement_run_id = ? AND seal_status = 'finalized'
            """,
            (supplement_run_id,),
        ).fetchone() is None:
            raise AnnotationRepositoryError("supplement_run_not_found")
    fields = (
        "task_id", "sample_run_id", "supplement_run_id", "source_post_id",
        "source_version", "platform_key", "review_round", "normalized_model_text",
        "structure_label", "tourism_label", "reason_codes",
        "annotator_hash", "annotated_at_utc",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "task_id": _sha256(
                        [supplement_run_id, int(row["source_post_id"]), review_round]
                    )[:32],
                    "sample_run_id": row["sample_run_id"],
                    "supplement_run_id": supplement_run_id,
                    "source_post_id": row["source_post_id"],
                    "source_version": row["source_version"],
                    "platform_key": row["platform_key"],
                    "review_round": review_round,
                    "normalized_model_text": row["normalized_model_text"],
                    "structure_label": "",
                    "tourism_label": "",
                    "reason_codes": "",
                    "annotator_hash": "",
                    "annotated_at_utc": "",
                }
            )
    return len(rows)


def _agreement_metrics(report: object) -> dict[str, object]:
    """把 dataclass 报告转成稳定、无正文的 JSON 投影。"""

    return {
        "structure": report.structure.__dict__,
        "tourism": report.tourism.__dict__,
    }


def _supplement_member_manifest(
    connection: sqlite3.Connection,
    supplement_run_id: str,
) -> str:
    """从补充轮次子行重建稳定成员 manifest。"""

    return _sha256(
        [
            [int(row["source_post_id"]), int(row["source_version"])]
            for row in connection.execute(
                """
                SELECT source_post_id, source_version
                FROM text_double_label_supplement_members
                WHERE supplement_run_id = ? ORDER BY selection_rank
                """,
                (supplement_run_id,),
            )
        ]
    )


def _validate_supplement(
    connection: sqlite3.Connection,
    supplement_run_id: str,
) -> sqlite3.Row:
    """读取补充轮次前验证封存状态、成员数与实际 manifest。"""

    row = connection.execute(
        "SELECT * FROM text_double_label_supplements WHERE supplement_run_id = ?",
        (supplement_run_id,),
    ).fetchone()
    if row is None or row["seal_status"] != "finalized":
        raise AnnotationRepositoryError("supplement_run_not_finalized")
    if (
        connection.execute(
            """
            SELECT COUNT(*) FROM text_double_label_supplement_members
            WHERE supplement_run_id = ?
            """,
            (supplement_run_id,),
        ).fetchone()[0]
        != int(row["selected_count"])
        or _supplement_member_manifest(connection, supplement_run_id)
        != row["member_manifest_sha256"]
    ):
        raise AnnotationRepositoryError("supplement_run_integrity_mismatch")
    return row


def _seal_supplement(
    connection: sqlite3.Connection,
    supplement_run_id: str,
    expected_manifest: str,
) -> None:
    """写完全部成员后重算 manifest 并封存补充轮次。"""

    if _supplement_member_manifest(connection, supplement_run_id) != expected_manifest:
        raise AnnotationRepositoryError("supplement_member_manifest_mismatch")
    connection.execute(
        """
        UPDATE text_double_label_supplements SET seal_status = 'finalized'
        WHERE supplement_run_id = ? AND seal_status = 'building'
        """,
        (supplement_run_id,),
    )


def _stored_agreement_workflow(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> AgreementWorkflowResult:
    supplement_count = 0
    if row["supplement_run_id"] is not None:
        supplement = _validate_supplement(
            connection, str(row["supplement_run_id"])
        )
        supplement_count = int(supplement["selected_count"])
    return AgreementWorkflowResult(
        evaluation_id=str(row["evaluation_id"]),
        status=str(row["status"]),
        planned_pair_count=int(row["planned_pair_count"]),
        complete_pair_count=int(row["complete_pair_count"]),
        metrics=json.loads(row["metrics_json"]) if row["metrics_json"] else None,
        additional_recheck_required=int(row["additional_double_label_required"]),
        supplement_run_id=(
            str(row["supplement_run_id"]) if row["supplement_run_id"] else None
        ),
        supplement_selected_count=supplement_count,
    )


def evaluate_agreement_workflow(
    derived_db: str | Path,
    *,
    sample_run_id: str,
    config: CleaningConfig,
) -> AgreementWorkflowResult:
    """核对完整双标计划并持久化通过/不完整/补充轮次状态。

    低于任一一致性门槛时，从原抽样并集中排除已计划对象后稳定抽取最多
    100 条，写入不可变 supplement 轮次。补充轮次一经建立即进入计划总集，
    因而下一次评估会先报告新增 pair 尚未完成，不会重复创建补充样本。
    """

    rules = annotation_config(config)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        sample = connection.execute(
            "SELECT * FROM text_sampling_runs WHERE sample_run_id = ?",
            (sample_run_id,),
        ).fetchone()
        if sample is None or sample["guide_version"] != config.text_label_guide_version:
            raise AnnotationRepositoryError("sampling_run_guide_mismatch")
        planned = tuple(
            (int(row[0]), int(row[1]))
            for row in connection.execute(
                """
                SELECT source_post_id, source_version FROM text_sample_members
                WHERE sample_run_id = ? AND requires_double_label = 1
                UNION
                SELECT m.source_post_id, m.source_version
                FROM text_double_label_supplement_members AS m
                JOIN text_double_label_supplements AS s
                  ON s.supplement_run_id = m.supplement_run_id
                 AND s.seal_status = 'finalized'
                WHERE m.sample_run_id = ?
                ORDER BY source_post_id, source_version
                """,
                (sample_run_id, sample_run_id),
            )
        )
        if not planned:
            raise AnnotationRepositoryError("double_label_plan_empty")
        records = connection.execute(
            """
            SELECT annotation_id, source_post_id, source_version, assignment_slot,
                   annotator_hash, structure_label, tourism_label,
                   guide_version
            FROM text_post_annotations
            WHERE sample_run_id = ? AND assignment_slot IN (1, 2)
            ORDER BY source_post_id, source_version, assignment_slot
            """,
            (sample_run_id,),
        ).fetchall()
        input_manifest = _sha256(
            {
                "planned": planned,
                "records": [dict(row) for row in records],
            }
        )
        evaluation_id = _sha256([sample_run_id, input_manifest])[:32]
        existing = connection.execute(
            "SELECT * FROM text_agreement_evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if existing is not None:
            return _stored_agreement_workflow(connection, existing)
        try:
            completion = evaluate_planned_agreement(
                records,
                planned_identities=planned,
                config=rules,
            )
        except ValueError as exc:
            raise AnnotationRepositoryError("invalid_double_label_records") from exc
        now = _utcnow()
        if not completion.is_complete:
            with connection:
                connection.execute(
                    """
                    INSERT INTO text_agreement_evaluations(
                        evaluation_id, sample_run_id, input_manifest_sha256, status,
                        planned_pair_count, complete_pair_count, metrics_json,
                        additional_double_label_required, supplement_run_id, created_at_utc
                    ) VALUES (?, ?, ?, 'incomplete', ?, ?, NULL, 0, NULL, ?)
                    """,
                    (
                        evaluation_id,
                        sample_run_id,
                        input_manifest,
                        completion.planned_pair_count,
                        completion.complete_pair_count,
                        now,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM text_agreement_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            return _stored_agreement_workflow(connection, row)

        assert completion.report is not None
        metrics = _agreement_metrics(completion.report)
        additional = completion.report.additional_recheck_required
        supplement_run_id: str | None = None
        selected: list[sqlite3.Row] = []
        status = "passed"
        if additional:
            planned_set = set(planned)
            candidates = [
                row
                for row in connection.execute(
                    """
                    SELECT DISTINCT source_post_id, source_version
                    FROM text_sample_members
                    WHERE sample_run_id = ?
                    ORDER BY source_post_id, source_version
                    """,
                    (sample_run_id,),
                )
                if (int(row[0]), int(row[1])) not in planned_set
            ]
            selected = sorted(
                candidates,
                key=lambda row: _sha256(
                    [
                        config.random_seed,
                        "agreement-supplement",
                        input_manifest,
                        int(row[0]),
                        int(row[1]),
                    ]
                ),
            )[: min(additional, len(candidates))]
            sequence_number = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM text_double_label_supplements
                    WHERE sample_run_id = ?
                    """,
                    (sample_run_id,),
                ).fetchone()[0]
            ) + 1
            member_manifest = _sha256(
                [[int(row[0]), int(row[1])] for row in selected]
            )
            supplement_run_id = _sha256(
                [sample_run_id, sequence_number, input_manifest, member_manifest]
            )[:32]
            status = "supplement_created" if selected else "supplement_exhausted"
        with connection:
            if additional and supplement_run_id is not None:
                connection.execute(
                    """
                    INSERT INTO text_double_label_supplements(
                        supplement_run_id, sample_run_id, sequence_number,
                        trigger_evaluation_sha256, requested_count, selected_count,
                        member_manifest_sha256, created_at_utc, seal_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'building')
                    """,
                    (
                        supplement_run_id,
                        sample_run_id,
                        sequence_number,
                        input_manifest,
                        additional,
                        len(selected),
                        member_manifest,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO text_double_label_supplement_members(
                        supplement_run_id, sample_run_id, source_post_id,
                        source_version, selection_rank
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            supplement_run_id,
                            sample_run_id,
                            int(row[0]),
                            int(row[1]),
                            rank,
                        )
                        for rank, row in enumerate(selected, 1)
                    ],
                )
                _seal_supplement(
                    connection, supplement_run_id, member_manifest
                )
            connection.execute(
                """
                INSERT INTO text_agreement_evaluations(
                    evaluation_id, sample_run_id, input_manifest_sha256, status,
                    planned_pair_count, complete_pair_count, metrics_json,
                    additional_double_label_required, supplement_run_id, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    sample_run_id,
                    input_manifest,
                    status,
                    completion.planned_pair_count,
                    completion.complete_pair_count,
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    additional,
                    supplement_run_id,
                    now,
                ),
            )
        row = connection.execute(
            "SELECT * FROM text_agreement_evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        return _stored_agreement_workflow(connection, row)


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
        "left_text", "right_text", "decision", "reason_code", "annotator_hash",
        "annotated_at_utc",
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
    minimum_recheck_interval_days: int = 14,
) -> ImportResult:
    """追加导入帖子审核；复核不得过早且重复文件只作幂等复用。

    公开 CSV 使用 ``review_round`` 表示初审和间隔盲复核。数据库中的
    ``assignment_slot`` 是历史存储字段，不承载参与人数含义。
    """

    if minimum_recheck_interval_days <= 0:
        raise AnnotationRepositoryError("invalid_recheck_interval_days")

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
                structure_label, tourism_label = _validated_cleaning_labels(row)
                sample_run_id = row.get("sample_run_id", "").strip() or None
                post_id, source_version = int(row["source_post_id"]), int(row["source_version"])
                slot_text = row.get("review_round", "").strip()
                slot = int(slot_text) if slot_text else None
                if slot not in (None, 1, 2):
                    raise AnnotationRepositoryError("invalid_review_round")
                annotator_hash = _require_hash(row["annotator_hash"], "annotator_hash")
                annotated_at = _parse_utc(row["annotated_at_utc"], "annotated_at_utc")
                if sample_run_id is not None:
                    member = connection.execute(
                        """
                        SELECT MAX(is_member) AS is_member, MAX(double_label) AS double_label
                        FROM (
                            SELECT 1 AS is_member, requires_double_label AS double_label
                            FROM text_sample_members
                            WHERE sample_run_id = ? AND source_post_id = ? AND source_version = ?
                            UNION ALL
                            SELECT 1, 1
                            FROM text_double_label_supplement_members AS m
                            JOIN text_double_label_supplements AS s
                              ON s.supplement_run_id = m.supplement_run_id
                             AND s.seal_status = 'finalized'
                            WHERE m.sample_run_id = ? AND m.source_post_id = ?
                              AND m.source_version = ?
                        )
                        """,
                        (
                            sample_run_id,
                            post_id,
                            source_version,
                            sample_run_id,
                            post_id,
                            source_version,
                        ),
                    ).fetchone()
                    if member is None or member["is_member"] is None:
                        raise AnnotationRepositoryError("annotation_post_not_in_sample")
                    if slot == 2 and not int(member["double_label"]):
                        raise AnnotationRepositoryError("recheck_round_not_assigned")
                    if slot == 2:
                        first_review = connection.execute(
                            """
                            SELECT annotated_at_utc FROM text_post_annotations
                            WHERE sample_run_id = ? AND source_post_id = ?
                              AND source_version = ? AND assignment_slot = 1
                            """,
                            (
                                sample_run_id,
                                post_id,
                                source_version,
                            ),
                        ).fetchone()
                        if first_review is None:
                            raise AnnotationRepositoryError("initial_review_missing")
                        first_at = _parse_utc(
                            str(first_review["annotated_at_utc"]), "initial_annotated_at_utc"
                        )
                        if annotated_at < first_at + timedelta(
                            days=minimum_recheck_interval_days
                        ):
                            raise AnnotationRepositoryError("recheck_interval_not_met")
                annotation_id = row.get("annotation_id", "").strip() or _sha256(
                    [import_id, index, post_id, source_version]
                )[:32]
                connection.execute(
                    """
                    INSERT INTO text_post_annotations(
                        annotation_id, import_id, sample_run_id, source_post_id,
                        source_version, annotator_hash, assignment_slot,
                        structure_label, tourism_label,
                        reason_codes_json, guide_version, annotated_at_utc, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        annotation_id,
                        import_id,
                        sample_run_id,
                        post_id,
                        source_version,
                        annotator_hash,
                        slot,
                        structure_label,
                        tourism_label,
                        _json_list(row.get("reason_codes", "")),
                        guide_version,
                        annotated_at.isoformat(timespec="seconds"),
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
                structure_label, tourism_label = _validated_cleaning_labels(row)
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
                               assignment_slot, annotator_hash, guide_version
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
                planned_double = False
                if sample_run_id is not None and context == "reference":
                    planned_double = connection.execute(
                        """
                        SELECT 1 FROM text_sample_members
                        WHERE sample_run_id = ? AND source_post_id = ?
                          AND source_version = ? AND requires_double_label = 1
                        UNION ALL
                        SELECT 1
                        FROM text_double_label_supplement_members AS m
                        JOIN text_double_label_supplements AS s
                          ON s.supplement_run_id = m.supplement_run_id
                         AND s.seal_status = 'finalized'
                        WHERE m.sample_run_id = ? AND m.source_post_id = ?
                          AND m.source_version = ?
                        LIMIT 1
                        """,
                        (
                            sample_run_id,
                            post_id,
                            source_version,
                            sample_run_id,
                            post_id,
                            source_version,
                        ),
                    ).fetchone() is not None
                if planned_double and (
                    len(evidence_rows) != 2
                    or {int(item["assignment_slot"]) for item in evidence_rows} != {1, 2}
                ):
                    raise AnnotationRepositoryError("recheck_evidence_invalid")
                adjudication_id = row.get("final_review_id", "").strip() or _sha256(
                    [import_id, index, post_id, source_version]
                )[:32]
                reviewed_at = _parse_utc(row["reviewed_at_utc"], "reviewed_at_utc")
                connection.execute(
                    """
                    INSERT INTO text_post_adjudications(
                        adjudication_id, import_id, sample_run_id, source_post_id,
                        source_version, adjudicator_hash, structure_label,
                        tourism_label, reason_codes_json,
                        evidence_annotation_ids_json, decision_context, guide_version,
                        adjudicated_at_utc, created_at_utc, model_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        adjudication_id,
                        import_id,
                        sample_run_id,
                        post_id,
                        source_version,
                        adjudicator_hash,
                        structure_label,
                        tourism_label,
                        _json_list(row.get("reason_codes", "")),
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
                    reviewed_at = _parse_utc(
                        row["annotated_at_utc"], "annotated_at_utc"
                    )
                    connection.execute(
                        """
                        INSERT INTO text_near_duplicate_annotations(
                            annotation_id, import_id, build_id, left_cluster_id,
                            right_cluster_id, annotator_hash, decision, reason_code,
                            guide_version, annotated_at_utc, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row.get("annotation_id", "").strip()
                            or _sha256([import_id, index, build_id, left, right])[:32],
                            import_id,
                            build_id,
                            left,
                            right,
                            _require_hash(row["annotator_hash"], "annotator_hash"),
                            row["decision"].strip(),
                            row["reason_code"].strip(),
                            guide_version,
                            reviewed_at.isoformat(timespec="seconds"),
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
