"""文本抽样、盲标导出和追加式人工证据的持久化边界。"""

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

from .config import AnnotationConfig, annotation_config
from .sampling import (
    SamplingPost,
    build_initial_sample_plan,
    build_periodic_sample_plan,
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
    double_label_count: int
    periodic_round_number: int
    output_sha256: str


@dataclass(frozen=True)
class ImportResult:
    """一次追加式导入的身份、行数和幂等复用状态。"""

    import_id: str
    record_kind: str
    row_count: int
    reused: bool


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _stored_sampling_result(row: sqlite3.Row) -> SamplingRunResult:
    return SamplingRunResult(
        sample_run_id=str(row["sample_run_id"]),
        sample_kind=str(row["sample_kind"]),
        population_count=int(row["population_count"]),
        probability_count=int(row["probability_count"]),
        targeted_count=int(row["targeted_count"]),
        double_label_count=int(row["double_label_count"]),
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
            return _stored_sampling_result(existing)
        probability_count = sum(m.sample_frame == "probability" for m in plan.members)
        targeted_count = sum(m.sample_frame == "targeted" for m in plan.members)
        double_label_count = len(
            {
                (m.source_post_id, m.source_version)
                for m in plan.members
                if m.requires_double_label
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
                ) VALUES (?, ?, ?, ?, 'initial', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
                    double_label_count,
                    plan.output_sha256,
                    now,
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
                        int(member.requires_double_label),
                    )
                    for member in plan.members
                ],
            )
        return SamplingRunResult(
            sample_run_id,
            "initial",
            len(posts),
            probability_count,
            targeted_count,
            double_label_count,
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
    """达到累计新增阈值后创建一个 100 条概率复核轮次。

    调用方必须显式给出轮次；函数核对当前结构可用语料相对基线至少新增
    `round_number * periodic_increment_posts`，因此不会把批次序号误当科研轮次。
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
        posts = _sampling_population(connection, candidate_build_id)
        required_population = int(baseline["population_count"]) + (
            round_number * rules.periodic_increment_posts
        )
        if len(posts) < required_population:
            raise AnnotationRepositoryError("periodic_increment_not_reached")
        baseline_population = {
            int(row[0])
            for row in connection.execute(
                """
                SELECT source_post_id FROM text_candidate_corpus_members
                WHERE build_id = ? AND structure_status = 'usable'
                """,
                (str(baseline["candidate_build_id"]),),
            )
        }
        prior_periodic_samples = {
            int(row[0])
            for row in connection.execute(
                """
                SELECT m.source_post_id
                FROM text_sample_members AS m
                JOIN text_sampling_runs AS s ON s.sample_run_id = m.sample_run_id
                WHERE s.baseline_sample_run_id = ?
                """,
                (baseline_sample_run_id,),
            )
        }
        # 周期复核只从基线之后真正新增的帖子抽取；基线中未被首轮抽中的
        # 旧帖子也必须排除，否则“每新增 2,000 条”会被误解为全库补样。
        already_sampled = baseline_population | prior_periodic_samples
        plan = build_periodic_sample_plan(
            posts,
            already_sampled_ids=already_sampled,
            sample_size=rules.periodic_probability_size,
            random_seed=config.random_seed,
            round_number=round_number,
        )
        if len(plan.members) < rules.periodic_probability_size:
            raise AnnotationRepositoryError("periodic_sampling_population_exhausted")
        sample_run_id = _sha256(
            [candidate_build_id, baseline_sample_run_id, round_number, plan.output_sha256]
        )[:32]
        existing = connection.execute(
            "SELECT * FROM text_sampling_runs WHERE sample_run_id = ?", (sample_run_id,)
        ).fetchone()
        if existing is not None:
            return _stored_sampling_result(existing)
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
                ) VALUES (?, ?, ?, ?, ?, 'periodic_review', ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    sample_run_id,
                    str(build["run_id"]),
                    str(build["source_snapshot_id"]),
                    candidate_build_id,
                    baseline_sample_run_id,
                    config.text_label_guide_version,
                    config.random_seed,
                    plan.population_manifest_sha256,
                    len(posts),
                    len(plan.members),
                    round_number,
                    plan.output_sha256,
                    now,
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
        return SamplingRunResult(
            sample_run_id,
            "periodic_review",
            len(posts),
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
    assignment_slot: int,
    output_path: str | Path,
) -> int:
    """导出单个盲标槽位；第二槽只含双标子样本，不暴露其他标注。"""

    if assignment_slot not in (1, 2):
        raise AnnotationRepositoryError("invalid_assignment_slot")
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
            (sample_run_id, assignment_slot),
        ).fetchall()
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "task_id", "sample_run_id", "source_post_id", "source_version",
                "platform_key", "assignment_slot", "normalized_model_text",
                "structure_label", "tourism_label", "commercial_label",
                "reason_codes", "annotator_hash", "annotated_at_utc",
            ),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "task_id": _sha256(
                        [sample_run_id, int(row["source_post_id"]), assignment_slot]
                    )[:32],
                    "sample_run_id": sample_run_id,
                    "source_post_id": row["source_post_id"],
                    "source_version": row["source_version"],
                    "platform_key": row["platform_key"],
                    "assignment_slot": assignment_slot,
                    "normalized_model_text": row["normalized_model_text"],
                    "structure_label": "",
                    "tourism_label": "",
                    "commercial_label": "",
                    "reason_codes": "",
                    "annotator_hash": "",
                    "annotated_at_utc": "",
                }
            )
    return len(rows)


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
) -> ImportResult:
    """追加导入帖子原始标注；重复文件幂等复用，不更新既有行。"""

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
                sample_run_id = row.get("sample_run_id", "").strip() or None
                post_id, source_version = int(row["source_post_id"]), int(row["source_version"])
                slot_text = row.get("assignment_slot", "").strip()
                slot = int(slot_text) if slot_text else None
                if sample_run_id is not None:
                    member = connection.execute(
                        """
                        SELECT MAX(requires_double_label) AS double_label
                        FROM text_sample_members
                        WHERE sample_run_id = ? AND source_post_id = ? AND source_version = ?
                        """,
                        (sample_run_id, post_id, source_version),
                    ).fetchone()
                    if member is None or member["double_label"] is None:
                        raise AnnotationRepositoryError("annotation_post_not_in_sample")
                    if slot == 2 and not int(member["double_label"]):
                        raise AnnotationRepositoryError("second_slot_not_assigned")
                    if slot in (1, 2):
                        same_annotator = connection.execute(
                            """
                            SELECT 1 FROM text_post_annotations
                            WHERE sample_run_id = ? AND source_post_id = ?
                              AND source_version = ? AND annotator_hash = ?
                              AND assignment_slot IN (1, 2)
                            """,
                            (
                                sample_run_id,
                                post_id,
                                source_version,
                                _require_hash(row["annotator_hash"], "annotator_hash"),
                            ),
                        ).fetchone()
                        if same_annotator is not None:
                            raise AnnotationRepositoryError("double_label_annotators_must_differ")
                annotation_id = row.get("annotation_id", "").strip() or _sha256(
                    [import_id, index, post_id, source_version]
                )[:32]
                connection.execute(
                    """
                    INSERT INTO text_post_annotations(
                        annotation_id, import_id, sample_run_id, source_post_id,
                        source_version, annotator_hash, assignment_slot,
                        structure_label, tourism_label, commercial_label,
                        reason_codes_json, guide_version, annotated_at_utc, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        annotation_id,
                        import_id,
                        sample_run_id,
                        post_id,
                        source_version,
                        _require_hash(row["annotator_hash"], "annotator_hash"),
                        slot,
                        row["structure_label"].strip(),
                        row["tourism_label"].strip(),
                        row["commercial_label"].strip(),
                        _json_list(row.get("reason_codes", "")),
                        guide_version,
                        row["annotated_at_utc"].strip(),
                        _utcnow(),
                    ),
                )
    return ImportResult(import_id, "post_annotation", len(rows), False)


def import_post_adjudications(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    guide_version: str,
    imported_by_hash: str,
) -> ImportResult:
    """追加导入帖子仲裁；金标必须列出属于同一帖子的原始标注证据。"""

    path = Path(csv_path)
    rows = _read_csv(path)
    source_hash = _file_sha256(path)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        with connection:
            import_id, reused = _start_import(
                connection,
                record_kind="post_adjudication",
                guide_version=guide_version,
                source_sha256=source_hash,
                row_count=len(rows),
                imported_by_hash=imported_by_hash,
            )
            if reused:
                return ImportResult(import_id, "post_adjudication", len(rows), True)
            for index, row in enumerate(rows, 1):
                post_id, source_version = int(row["source_post_id"]), int(row["source_version"])
                context = row.get("decision_context", "gold").strip()
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
                    {item.strip() for item in row.get("evidence_annotation_ids", "").split("|") if item.strip()}
                )
                if context == "gold" and not evidence:
                    raise AnnotationRepositoryError("gold_adjudication_requires_evidence")
                if evidence:
                    placeholders = ",".join("?" for _ in evidence)
                    evidence_rows = connection.execute(
                        f"""
                        SELECT annotation_id, source_post_id, source_version
                        FROM text_post_annotations WHERE annotation_id IN ({placeholders})
                        """,
                        evidence,
                    ).fetchall()
                    if len(evidence_rows) != len(evidence) or any(
                        int(item["source_post_id"]) != post_id
                        or int(item["source_version"]) != source_version
                        for item in evidence_rows
                    ):
                        raise AnnotationRepositoryError("adjudication_evidence_mismatch")
                adjudication_id = row.get("adjudication_id", "").strip() or _sha256(
                    [import_id, index, post_id, source_version]
                )[:32]
                connection.execute(
                    """
                    INSERT INTO text_post_adjudications(
                        adjudication_id, import_id, sample_run_id, source_post_id,
                        source_version, adjudicator_hash, structure_label,
                        tourism_label, commercial_label, reason_codes_json,
                        evidence_annotation_ids_json, decision_context, guide_version,
                        adjudicated_at_utc, created_at_utc, model_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        adjudication_id,
                        import_id,
                        row.get("sample_run_id", "").strip() or None,
                        post_id,
                        source_version,
                        _require_hash(row["adjudicator_hash"], "adjudicator_hash"),
                        row["structure_label"].strip(),
                        row["tourism_label"].strip(),
                        row["commercial_label"].strip(),
                        _json_list(row.get("reason_codes", "")),
                        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                        context,
                        guide_version,
                        row["adjudicated_at_utc"].strip(),
                        _utcnow(),
                        model_run_id,
                    ),
                )
    return ImportResult(import_id, "post_adjudication", len(rows), False)


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
    adjudication: bool,
) -> ImportResult:
    path = Path(csv_path)
    rows = _read_csv(path)
    source_hash = _file_sha256(path)
    kind = "duplicate_adjudication" if adjudication else "duplicate_annotation"
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
                if adjudication:
                    evidence = sorted(
                        {item.strip() for item in row.get("evidence_annotation_ids", "").split("|") if item.strip()}
                    )
                    if not evidence:
                        raise AnnotationRepositoryError("duplicate_adjudication_requires_evidence")
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
                            row.get("adjudication_id", "").strip()
                            or _sha256([import_id, index, build_id, left, right])[:32],
                            import_id,
                            build_id,
                            left,
                            right,
                            _require_hash(row["adjudicator_hash"], "adjudicator_hash"),
                            row["decision"].strip(),
                            row["reason_code"].strip(),
                            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                            guide_version,
                            row["adjudicated_at_utc"].strip(),
                            _utcnow(),
                        ),
                    )
                else:
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
                            row["annotated_at_utc"].strip(),
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
        adjudication=False,
    )


def import_duplicate_adjudications(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    guide_version: str,
    imported_by_hash: str,
) -> ImportResult:
    """追加导入候选对仲裁；只有 `duplicate` 仲裁可供泄漏分组消费。"""

    return _import_duplicate_records(
        derived_db,
        csv_path=csv_path,
        guide_version=guide_version,
        imported_by_hash=imported_by_hash,
        adjudication=True,
    )
