"""冻结快照文本读取、派生结果持久化与不可变候选构建。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import CleaningConfig, matches_frozen_run
from .fingerprints import post_fingerprints
from .schema import connect_derived, migrate_derived
from .snapshot import open_source_readonly, sha256_file
from .state_machine import TaskClaim, finish_task
from .task_plan import effective_stage_version
from .text_config import TextCleaningConfig
from .text_duplicates import DuplicatePlan, TextDocument, build_duplicate_plan
from .text_normalize import NormalizedText, normalize_post_text
from .text_runtime import (
    text_runtime_sha256,
    text_runtime_version_lock,
    text_runtime_versions,
)


class TextRepositoryError(RuntimeError):
    """文本输入或派生写入不满足冻结契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str, message: str = "text repository operation failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class TextTaskResult:
    """单个任务的非敏感处理回执。"""

    task_id: str
    source_post_id: int
    structure_status: str
    output_sha256: str


@dataclass(frozen=True)
class TextBatchResult:
    """批量处理计数以及逐任务回执，不包含正文。"""

    succeeded: tuple[TextTaskResult, ...]
    failed_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidateBuildResult:
    """不可变候选构建的身份和可公开统计。"""

    build_id: str
    corpus_manifest_sha256: str
    expected_post_count: int
    processed_post_count: int
    usable_post_count: int
    is_complete_corpus: bool
    exact_cluster_count: int
    exact_duplicate_cluster_count: int
    exact_cross_platform_cluster_count: int
    exact_cross_platform_member_count: int
    near_candidate_pair_count: int
    near_cross_platform_candidate_pair_count: int
    near_candidate_component_count: int
    output_sha256: str


@dataclass(frozen=True)
class _TaskInput:
    """从同一冻结快照中读取的最小文本任务输入。"""

    claim: TaskClaim
    snapshot_id: str
    platform_key: str
    title: object
    body: object
    source_status: object


@dataclass(frozen=True)
class _CorpusRow:
    """候选构建所需的结果投影，保留结构不可用记录以便追踪。"""

    task_id: str
    source_post_id: int
    source_version: int
    platform_key: str
    structure_status: str
    normalized_title: str
    normalized_body: str
    normalized_sha256: str
    exact_canonical_sha256: str | None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_rule_lock(config: CleaningConfig, text_config: TextCleaningConfig) -> None:
    """保证主配置的有效处理版本确实锁定当前规则文件字节。"""

    if config.algorithm_versions.get("text_normalization") != text_config.version_lock:
        raise TextRepositoryError("text_rules_version_mismatch")
    if config.algorithm_versions.get("text_runtime") != text_runtime_version_lock():
        raise TextRepositoryError("text_runtime_version_mismatch")


def _load_task_inputs(
    derived_db: str | Path,
    claims: Sequence[TaskClaim],
    config: CleaningConfig,
) -> tuple[_TaskInput, ...]:
    """一次校验快照哈希后读取多条任务，逐条复核发现阶段的文本指纹。"""

    if not claims:
        return ()
    expected_stage_version = effective_stage_version(
        config.algorithm_versions,
        "post",
        "text_deterministic",
    )
    with connect_derived(derived_db) as derived:
        migrate_derived(derived)
        rows: list[sqlite3.Row] = []
        for claim in claims:
            if (
                claim.stage_name != "text_deterministic"
                or claim.object_type != "post"
                or claim.stage_version != expected_stage_version
            ):
                raise TextRepositoryError("invalid_text_task_claim")
            row = derived.execute(
                """
                SELECT t.task_id, t.status, t.run_id, t.source_object_id,
                       t.source_version, t.stage_version,
                       r.source_snapshot_id, r.config_sha256, r.protocol_version,
                       s.snapshot_path, s.snapshot_sha256, s.input_contract_status,
                       v.text_sha256, p.platform_key
                FROM stage_tasks AS t
                JOIN cleaning_runs AS r ON r.run_id = t.run_id
                JOIN source_snapshots AS s ON s.snapshot_id = r.source_snapshot_id
                JOIN source_post_versions AS v
                  ON v.source_post_id = t.source_object_id
                 AND v.source_version = t.source_version
                JOIN source_post_inventory AS p
                  ON p.source_post_id = t.source_object_id
                WHERE t.task_id = ?
                """,
                (claim.task_id,),
            ).fetchone()
            if row is None:
                raise TextRepositoryError("text_task_input_not_found")
            if row["status"] != "running":
                raise TextRepositoryError("text_task_not_running")
            if not matches_frozen_run(config, str(row["config_sha256"]), str(row["protocol_version"])):
                raise TextRepositoryError("run_config_mismatch")
            if row["input_contract_status"] != "accepted":
                raise TextRepositoryError("snapshot_input_rejected")
            rows.append(row)
        snapshot_ids = {str(row["source_snapshot_id"]) for row in rows}
        snapshot_paths = {str(row["snapshot_path"]) for row in rows}
        snapshot_hashes = {str(row["snapshot_sha256"]) for row in rows}
        if len(snapshot_ids) != 1 or len(snapshot_paths) != 1 or len(snapshot_hashes) != 1:
            raise TextRepositoryError("mixed_task_snapshots")

    snapshot_path = Path(next(iter(snapshot_paths)))
    if not snapshot_path.is_file() or sha256_file(snapshot_path) != next(iter(snapshot_hashes)):
        raise TextRepositoryError("snapshot_sha256_mismatch")
    by_task_id = {str(row["task_id"]): row for row in rows}
    inputs: list[_TaskInput] = []
    with open_source_readonly(snapshot_path) as source:
        for claim in claims:
            task_row = by_task_id[claim.task_id]
            source_row = source.execute(
                "SELECT * FROM web_posts WHERE id = ?",
                (claim.source_object_id,),
            ).fetchone()
            if source_row is None:
                raise TextRepositoryError("source_post_missing_from_snapshot")
            text_sha256, _, _ = post_fingerprints(source_row)
            if text_sha256 != task_row["text_sha256"]:
                raise TextRepositoryError("source_text_fingerprint_mismatch")
            inputs.append(
                _TaskInput(
                    claim=claim,
                    snapshot_id=str(task_row["source_snapshot_id"]),
                    platform_key=str(task_row["platform_key"]),
                    title=source_row["title"],
                    body=source_row["content_text"],
                    source_status=source_row["status"],
                )
            )
    return tuple(inputs)


def _persist_text_result(
    derived_db: str | Path,
    task_input: _TaskInput,
    result: NormalizedText,
    text_config: TextCleaningConfig,
) -> None:
    """先幂等落盘再完成任务；崩溃恢复时只接受完全相同的既有输出。"""

    evidence_json = json.dumps(
        result.evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    runtime_versions_json = json.dumps(
        text_runtime_versions(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    now_utc = _utcnow()
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO text_deterministic_results(
                    task_id, run_id, source_snapshot_id, source_post_id,
                    source_version, platform_key, stage_version, rules_version,
                    rules_sha256, runtime_versions_json, runtime_sha256,
                    structure_status, structure_reason_code,
                    structure_evidence_json, normalized_title, normalized_body,
                    normalized_model_text, normalized_sha256,
                    exact_canonical_sha256, output_sha256, created_at_utc
                )
                SELECT task_id, run_id, ?, source_post_id, source_version, ?,
                       stage_version, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                FROM stage_tasks WHERE task_id = ? AND status = 'running'
                """,
                (
                    task_input.snapshot_id,
                    task_input.platform_key,
                    text_config.version,
                    text_config.sha256,
                    runtime_versions_json,
                    text_runtime_sha256(),
                    result.structure_status,
                    result.structure_reason_code,
                    evidence_json,
                    result.normalized_title,
                    result.normalized_body,
                    result.model_text,
                    result.normalized_sha256,
                    result.exact_canonical_sha256,
                    result.output_sha256,
                    now_utc,
                    task_input.claim.task_id,
                ),
            )
            stored = connection.execute(
                """
                SELECT output_sha256 FROM text_deterministic_results WHERE task_id = ?
                """,
                (task_input.claim.task_id,),
            ).fetchone()
            if stored is None:
                raise TextRepositoryError("text_result_insert_rejected")
            if stored["output_sha256"] != result.output_sha256:
                raise TextRepositoryError("text_result_idempotency_conflict")


def process_text_tasks(
    derived_db: str | Path,
    claims: Sequence[TaskClaim],
    *,
    config: CleaningConfig,
    text_config: TextCleaningConfig,
    actor: str | None = None,
) -> TextBatchResult:
    """处理已领取任务；失败只写去敏理由，成功结果可在中断后安全复用。"""

    _require_rule_lock(config, text_config)
    succeeded: list[TextTaskResult] = []
    failed: list[str] = []
    try:
        inputs = _load_task_inputs(derived_db, claims, config)
    except TextRepositoryError as exc:
        for claim in claims:
            finish_task(
                derived_db,
                claim.task_id,
                "failed",
                config=config,
                reason_code=exc.reason_code,
                error_summary=exc.reason_code,
                actor=actor,
            )
            failed.append(claim.task_id)
        return TextBatchResult((), tuple(failed))

    for task_input in inputs:
        try:
            result = normalize_post_text(
                task_input.title,
                task_input.body,
                source_status=task_input.source_status,
                config=text_config,
            )
            _persist_text_result(derived_db, task_input, result, text_config)
            finish_task(
                derived_db,
                task_input.claim.task_id,
                "succeeded",
                config=config,
                output_sha256=result.output_sha256,
                actor=actor,
            )
        except TextRepositoryError as exc:
            finish_task(
                derived_db,
                task_input.claim.task_id,
                "failed",
                config=config,
                reason_code=exc.reason_code,
                error_summary=exc.reason_code,
                actor=actor,
            )
            failed.append(task_input.claim.task_id)
        else:
            succeeded.append(
                TextTaskResult(
                    task_id=task_input.claim.task_id,
                    source_post_id=task_input.claim.source_object_id,
                    structure_status=result.structure_status,
                    output_sha256=result.output_sha256,
                )
            )
    return TextBatchResult(tuple(succeeded), tuple(failed))


def _candidate_context(
    derived_db: str | Path,
    run_id: str,
    snapshot_id: str,
    stage_version: str,
    config: CleaningConfig,
) -> tuple[int, tuple[_CorpusRow, ...]]:
    """读取显式快照的预期帖子及同版本规范化结果，不猜测最新构建。"""

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        snapshot = connection.execute(
            """
            SELECT s.input_contract_status, r.config_sha256, r.protocol_version
            FROM source_snapshots AS s
            JOIN cleaning_runs AS r ON r.run_id = s.run_id
            WHERE s.snapshot_id = ? AND s.run_id = ?
            """,
            (snapshot_id, run_id),
        ).fetchone()
        if snapshot is None:
            raise TextRepositoryError("snapshot_not_found")
        if snapshot["input_contract_status"] != "accepted":
            raise TextRepositoryError("snapshot_input_rejected")
        if not matches_frozen_run(
            config,
            str(snapshot["config_sha256"]),
            str(snapshot["protocol_version"]),
        ):
            raise TextRepositoryError("run_config_mismatch")
        expected_post_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM source_post_observations
                WHERE snapshot_id = ? AND change_kind != 'missing'
                """,
                (snapshot_id,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT result.task_id, result.source_post_id, result.source_version,
                   result.platform_key, result.structure_status,
                   result.normalized_title, result.normalized_body,
                   result.normalized_sha256, result.exact_canonical_sha256
            FROM source_post_observations AS observation
            JOIN text_deterministic_results AS result
              ON result.source_post_id = observation.source_post_id
             AND result.source_version = observation.source_version
             AND result.stage_version = ?
            JOIN stage_tasks AS completed_task
              ON completed_task.task_id = result.task_id
             AND completed_task.status = 'succeeded'
             AND completed_task.output_sha256 = result.output_sha256
             AND result.task_id = (
                 SELECT MIN(candidate.task_id)
                 FROM text_deterministic_results AS candidate
                 JOIN stage_tasks AS candidate_task
                   ON candidate_task.task_id = candidate.task_id
                  AND candidate_task.status = 'succeeded'
                  AND candidate_task.output_sha256 = candidate.output_sha256
                 WHERE candidate.source_post_id = observation.source_post_id
                   AND candidate.source_version = observation.source_version
                   AND candidate.stage_version = result.stage_version
             )
            WHERE observation.snapshot_id = ? AND observation.change_kind != 'missing'
            ORDER BY result.source_post_id, result.source_version
            """,
            (stage_version, snapshot_id),
        ).fetchall()
    return expected_post_count, tuple(
        _CorpusRow(
            task_id=str(row["task_id"]),
            source_post_id=int(row["source_post_id"]),
            source_version=int(row["source_version"]),
            platform_key=str(row["platform_key"]),
            structure_status=str(row["structure_status"]),
            normalized_title=str(row["normalized_title"]),
            normalized_body=str(row["normalized_body"]),
            normalized_sha256=str(row["normalized_sha256"]),
            exact_canonical_sha256=(
                None
                if row["exact_canonical_sha256"] is None
                else str(row["exact_canonical_sha256"])
            ),
        )
        for row in rows
    )


def _build_result(
    build_id: str,
    manifest_sha256: str,
    expected_count: int,
    corpus: Sequence[_CorpusRow],
    plan: DuplicatePlan,
    output_sha256: str,
) -> CandidateBuildResult:
    duplicate_clusters = [cluster for cluster in plan.exact_clusters if len(cluster.members) > 1]
    cross_clusters = [cluster for cluster in duplicate_clusters if cluster.is_cross_platform]
    return CandidateBuildResult(
        build_id=build_id,
        corpus_manifest_sha256=manifest_sha256,
        expected_post_count=expected_count,
        processed_post_count=len(corpus),
        usable_post_count=sum(row.structure_status == "usable" for row in corpus),
        is_complete_corpus=len(corpus) == expected_count,
        exact_cluster_count=len(plan.exact_clusters),
        exact_duplicate_cluster_count=len(duplicate_clusters),
        exact_cross_platform_cluster_count=len(cross_clusters),
        exact_cross_platform_member_count=sum(len(cluster.members) for cluster in cross_clusters),
        near_candidate_pair_count=len(plan.near_pairs),
        near_cross_platform_candidate_pair_count=sum(pair.is_cross_platform for pair in plan.near_pairs),
        near_candidate_component_count=len(plan.near_components),
        output_sha256=output_sha256,
    )


def build_text_candidates(
    derived_db: str | Path,
    *,
    run_id: str,
    snapshot_id: str,
    config: CleaningConfig,
    text_config: TextCleaningConfig,
    allow_partial: bool = False,
) -> CandidateBuildResult:
    """从显式规范化语料创建不可变候选构建，默认拒绝不完整语料。"""

    _require_rule_lock(config, text_config)
    stage_version = effective_stage_version(
        config.algorithm_versions,
        "post",
        "text_deterministic",
    )
    expected_count, corpus = _candidate_context(
        derived_db,
        run_id,
        snapshot_id,
        stage_version,
        config,
    )
    if len(corpus) != expected_count and not allow_partial:
        raise TextRepositoryError("candidate_corpus_incomplete")
    manifest = [
        {
            "task_id": row.task_id,
            "source_post_id": row.source_post_id,
            "source_version": row.source_version,
            "platform_key": row.platform_key,
            "structure_status": row.structure_status,
            "normalized_sha256": row.normalized_sha256,
            "exact_canonical_sha256": row.exact_canonical_sha256,
        }
        for row in corpus
    ]
    manifest_sha256 = _canonical_sha256(manifest)
    documents = [
        TextDocument(
            row.source_post_id,
            row.source_version,
            row.platform_key,
            row.normalized_title,
            row.normalized_body,
            str(row.exact_canonical_sha256),
        )
        for row in corpus
        if row.structure_status == "usable" and row.exact_canonical_sha256 is not None
    ]
    plan = build_duplicate_plan(documents, text_config.near_duplicate)
    build_id = _canonical_sha256(
        {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "stage_version": stage_version,
            "rules_sha256": text_config.sha256,
            "runtime_sha256": text_runtime_sha256(),
            "corpus_manifest_sha256": manifest_sha256,
        }
    )[:32]
    output_sha256 = _canonical_sha256(
        {
            "build_id": build_id,
            "corpus_manifest_sha256": manifest_sha256,
            "duplicate_plan_sha256": plan.output_sha256,
            "runtime_sha256": text_runtime_sha256(),
            "expected_post_count": expected_count,
            "processed_post_count": len(corpus),
        }
    )
    result = _build_result(
        build_id,
        manifest_sha256,
        expected_count,
        corpus,
        plan,
        output_sha256,
    )
    cluster_by_member = {
        (member.source_post_id, member.source_version): cluster
        for cluster in plan.exact_clusters
        for member in cluster.members
    }
    clusters_by_id = {cluster.cluster_id: cluster for cluster in plan.exact_clusters}
    now_utc = _utcnow()
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        existing = connection.execute(
            "SELECT * FROM text_candidate_builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        if existing is not None:
            expected_header = {
                "status": "finalized",
                "runtime_sha256": text_runtime_sha256(),
                "corpus_manifest_sha256": result.corpus_manifest_sha256,
                "processed_post_count": result.processed_post_count,
                "usable_post_count": result.usable_post_count,
                "exact_cluster_count": result.exact_cluster_count,
                "exact_duplicate_cluster_count": result.exact_duplicate_cluster_count,
                "near_candidate_pair_count": result.near_candidate_pair_count,
                "near_candidate_component_count": result.near_candidate_component_count,
                "library_versions_json": json.dumps(
                    plan.library_versions,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "output_sha256": output_sha256,
            }
            if any(existing[key] != value for key, value in expected_header.items()):
                raise TextRepositoryError("candidate_build_idempotency_conflict")
            stored_counts = {
                "corpus": connection.execute(
                    "SELECT COUNT(*) FROM text_candidate_corpus_members WHERE build_id = ?",
                    (build_id,),
                ).fetchone()[0],
                "exact_clusters": connection.execute(
                    "SELECT COUNT(*) FROM text_exact_clusters WHERE build_id = ?",
                    (build_id,),
                ).fetchone()[0],
                "near_pairs": connection.execute(
                    "SELECT COUNT(*) FROM text_near_candidate_pairs WHERE build_id = ?",
                    (build_id,),
                ).fetchone()[0],
                "near_components": connection.execute(
                    "SELECT COUNT(*) FROM text_near_candidate_components WHERE build_id = ?",
                    (build_id,),
                ).fetchone()[0],
            }
            if stored_counts != {
                "corpus": result.processed_post_count,
                "exact_clusters": result.exact_cluster_count,
                "near_pairs": result.near_candidate_pair_count,
                "near_components": result.near_candidate_component_count,
            }:
                raise TextRepositoryError("candidate_build_storage_mismatch")
            return result
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO text_candidate_builds(
                    build_id, run_id, source_snapshot_id, stage_version,
                    rules_version, rules_sha256, runtime_sha256, status,
                    corpus_manifest_sha256,
                    expected_post_count, processed_post_count, usable_post_count,
                    is_complete_corpus, exact_cluster_count,
                    exact_duplicate_cluster_count, exact_cross_platform_cluster_count,
                    exact_cross_platform_member_count, near_candidate_pair_count,
                    near_cross_platform_candidate_pair_count,
                    near_candidate_component_count, library_versions_json,
                    output_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'building', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.build_id,
                    run_id,
                    snapshot_id,
                    stage_version,
                    text_config.version,
                    text_config.sha256,
                    text_runtime_sha256(),
                    result.corpus_manifest_sha256,
                    result.expected_post_count,
                    result.processed_post_count,
                    result.usable_post_count,
                    int(result.is_complete_corpus),
                    result.exact_cluster_count,
                    result.exact_duplicate_cluster_count,
                    result.exact_cross_platform_cluster_count,
                    result.exact_cross_platform_member_count,
                    result.near_candidate_pair_count,
                    result.near_cross_platform_candidate_pair_count,
                    result.near_candidate_component_count,
                    json.dumps(plan.library_versions, sort_keys=True, separators=(",", ":")),
                    result.output_sha256,
                    now_utc,
                ),
            )
            for cluster in plan.exact_clusters:
                connection.execute(
                    """
                    INSERT INTO text_exact_clusters(
                        build_id, cluster_id, exact_canonical_sha256,
                        representative_source_post_id, member_count, is_cross_platform
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        cluster.cluster_id,
                        cluster.exact_canonical_sha256,
                        cluster.representative.source_post_id,
                        len(cluster.members),
                        int(cluster.is_cross_platform),
                    ),
                )
                for member in cluster.members:
                    connection.execute(
                        """
                        INSERT INTO text_exact_cluster_members(
                            build_id, cluster_id, source_post_id, source_version,
                            platform_key, is_representative
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            build_id,
                            cluster.cluster_id,
                            member.source_post_id,
                            member.source_version,
                            member.platform_key,
                            int(member == cluster.representative),
                        ),
                    )
            for row in corpus:
                cluster = cluster_by_member.get((row.source_post_id, row.source_version))
                connection.execute(
                    """
                    INSERT INTO text_candidate_corpus_members(
                        build_id, task_id, source_post_id, source_version,
                        platform_key, structure_status, exact_cluster_id,
                        is_near_representative
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        row.task_id,
                        row.source_post_id,
                        row.source_version,
                        row.platform_key,
                        row.structure_status,
                        None if cluster is None else cluster.cluster_id,
                        int(
                            cluster is not None
                            and row.source_post_id == cluster.representative.source_post_id
                            and row.source_version == cluster.representative.source_version
                        ),
                    ),
                )
            for pair in plan.near_pairs:
                connection.execute(
                    """
                    INSERT INTO text_near_candidate_pairs(
                        build_id, left_cluster_id, right_cluster_id,
                        left_source_post_id, right_source_post_id, similarity_ppm,
                        length_ratio_ppm, shared_block_key_count,
                        is_cross_platform, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        pair.left_cluster_id,
                        pair.right_cluster_id,
                        pair.left_source_post_id,
                        pair.right_source_post_id,
                        pair.similarity_ppm,
                        pair.length_ratio_ppm,
                        pair.shared_block_key_count,
                        int(pair.is_cross_platform),
                        json.dumps(
                            {
                                "candidate_only": True,
                                "metric": "cosine_tfidf",
                                "similarity_ppm": pair.similarity_ppm,
                                "length_ratio_ppm": pair.length_ratio_ppm,
                                "shared_block_key_count": pair.shared_block_key_count,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            for component in plan.near_components:
                connection.execute(
                    """
                    INSERT INTO text_near_candidate_components(
                        build_id, component_id, representative_count,
                        member_count, is_cross_platform
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        component.component_id,
                        len(component.cluster_ids),
                        len(component.members),
                        int(component.is_cross_platform),
                    ),
                )
                for cluster_id, member in component.members:
                    cluster = clusters_by_id[cluster_id]
                    connection.execute(
                        """
                        INSERT INTO text_near_candidate_component_members(
                            build_id, component_id, cluster_id, source_post_id,
                            source_version, platform_key, is_cluster_representative
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            build_id,
                            component.component_id,
                            cluster_id,
                            member.source_post_id,
                            member.source_version,
                            member.platform_key,
                            int(member == cluster.representative),
                        ),
                    )
            connection.execute(
                """
                UPDATE text_candidate_builds SET status = 'finalized'
                WHERE build_id = ? AND status = 'building'
                """,
                (build_id,),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise TextRepositoryError("candidate_build_write_failed") from exc
    return result
