"""图片清单与角色证据的 SQLite 持久化边界。

绝对根路径只在调用期间用于路径校验，派生库仅保存根目录摘要、相对路径和
manifest 行身份。正式采集库始终只读；本模块只写独立派生 SQLite。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .config import CleaningConfig, matches_frozen_run
from .image_candidates import CandidateImage, ImageCandidatePlan, build_image_candidate_plan, url_has_role_hint
from .image_fingerprint import (
    ImageFingerprintError,
    current_image_library_versions,
    fingerprint_image_file,
    fingerprint_payload,
)
from .image_manifest import (
    ImageManifestRow,
    parse_image_manifest,
    resolve_manifest_path,
    root_identity_sha256,
)
from .image_role import decide_image_role
from .schema import connect_derived, migrate_derived
from .snapshot import open_source_readonly, sha256_file


class ImageRepositoryError(RuntimeError):
    """图片证据无法满足冻结运行或源对象约束时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("image repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ImageManifestImportResult:
    """清单导入的可公开回执，不包含机器路径和图片内容。"""

    manifest_id: str
    source_sha256: str
    status: str
    row_count: int
    accepted_row_count: int
    rejected_row_count: int
    accepted_row_identities: tuple[str, ...]


@dataclass(frozen=True)
class ImageFingerprintBatchResult:
    """一次 manifest 指纹处理的计数回执；阻塞不记为模型失败。"""

    manifest_id: str
    succeeded_count: int
    blocked_count: int
    skipped_count: int
    fingerprint_ids: tuple[str, ...]
    reason_counts: Mapping[str, int]


@dataclass(frozen=True)
class ImageCandidateBuildResult:
    """已封存候选构建的身份和无敏感统计。"""

    build_id: str
    input_manifest_sha256: str
    fingerprint_count: int
    exact_cluster_count: int
    exact_duplicate_cluster_count: int
    near_pair_count: int
    signal_count: int
    output_sha256: str


@dataclass(frozen=True)
class ImageStageOutcome:
    """将 manifest 证据映射回单个调度任务的终态。"""

    source_image_id: int
    status: str
    reason_code: str | None
    output_sha256: str | None


@dataclass(frozen=True)
class ImageStageSnapshot:
    """一个 manifest 所属运行及其逐图片处理结果。"""

    run_id: str
    outcomes: tuple[ImageStageOutcome, ...]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_image_library_lock(config: CleaningConfig) -> None:
    """在打开图片前核对依赖版本，避免同一算法版本产生漂移结果。"""

    actual = current_image_library_versions()
    if (
        actual["Pillow"] != config.image.pillow_version
        or actual["ImageHash"] != config.image.imagehash_version
    ):
        raise ImageRepositoryError("image_library_version_mismatch")


def _next_attempt_number(
    connection: sqlite3.Connection,
    run_id: str,
    operation: str,
    manifest_row_id: str | None,
) -> int:
    """按运行、操作和清单行递增 attempt，支持显式阻塞恢复。"""

    row = connection.execute(
        """
        SELECT COALESCE(MAX(attempt_number), 0) + 1
        FROM image_processing_attempts
        WHERE run_id = ? AND operation = ? AND manifest_row_id IS ?
        """,
        (run_id, operation, manifest_row_id),
    ).fetchone()
    return int(row[0])


def _record_attempt(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    manifest_id: str | None,
    manifest_row_id: str | None,
    operation: str,
    status: str,
    reason_code: str,
    details: Mapping[str, object] | None = None,
) -> None:
    """追加去敏 attempt；详情只允许调用方传入计数、哈希或布尔证据。"""

    attempt_number = _next_attempt_number(connection, run_id, operation, manifest_row_id)
    attempt_id = _canonical_sha256(
        {
            "run_id": run_id,
            "operation": operation,
            "manifest_id": manifest_id,
            "manifest_row_id": manifest_row_id,
            "attempt_number": attempt_number,
        }
    )[:32]
    connection.execute(
        """
        INSERT INTO image_processing_attempts(
            attempt_id, run_id, manifest_id, manifest_row_id, operation,
            attempt_number, status, reason_code, details_json, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            run_id,
            manifest_id,
            manifest_row_id,
            operation,
            attempt_number,
            status,
            reason_code,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            _utcnow(),
        ),
    )


def _validate_run(
    connection: sqlite3.Connection,
    run_id: str,
    source_snapshot_id: str,
    config: CleaningConfig,
) -> None:
    """核对显式运行和快照，禁止把清单悄悄挂到“最新运行”。"""

    row = connection.execute(
        """
        SELECT r.config_sha256, r.protocol_version, r.source_snapshot_id,
               s.input_contract_status
        FROM cleaning_runs AS r
        JOIN source_snapshots AS s ON s.snapshot_id = ? AND s.run_id = r.run_id
        WHERE r.run_id = ?
        """,
        (source_snapshot_id, run_id),
    ).fetchone()
    if row is None:
        raise ImageRepositoryError("image_run_or_snapshot_not_found")
    if row["source_snapshot_id"] != source_snapshot_id:
        raise ImageRepositoryError("image_snapshot_mismatch")
    if not matches_frozen_run(config, str(row["config_sha256"]), str(row["protocol_version"])):
        raise ImageRepositoryError("run_config_mismatch")
    if row["input_contract_status"] != "accepted":
        raise ImageRepositoryError("snapshot_input_rejected")


def _validate_source_mapping(
    connection: sqlite3.Connection,
    rows: tuple[ImageManifestRow, ...],
) -> None:
    """清单源 ID 必须存在且帖子关系一致；不信任 CSV 自报外键。"""

    for row in rows:
        source = connection.execute(
            """
            SELECT source_post_id FROM source_image_inventory
            WHERE source_image_id = ? AND is_present = 1
            """,
            (row.source_image_id,),
        ).fetchone()
        if source is None:
            raise ImageRepositoryError("manifest_source_image_not_found")
        if int(source["source_post_id"]) != row.source_post_id:
            raise ImageRepositoryError("manifest_source_post_mismatch")


def _result_from_rows(
    manifest_id: str,
    source_sha256: str,
    rows: tuple[ImageManifestRow, ...],
) -> ImageManifestImportResult:
    accepted = tuple(
        row.row_identity_sha256 for row in rows if row.validation_status == "accepted"
    )
    rejected_count = len(rows) - len(accepted)
    status = (
        "accepted"
        if rejected_count == 0
        else "accepted_with_rejections"
        if accepted
        else "rejected"
    )
    return ImageManifestImportResult(
        manifest_id=manifest_id,
        source_sha256=source_sha256,
        status=status,
        row_count=len(rows),
        accepted_row_count=len(accepted),
        rejected_row_count=rejected_count,
        accepted_row_identities=accepted,
    )


def import_image_manifest(
    derived_db: str | Path,
    *,
    run_id: str,
    source_snapshot_id: str,
    manifest_path: str | Path,
    image_root: str | Path,
    config: CleaningConfig,
) -> ImageManifestImportResult:
    """校验并幂等导入本地清单，同时生成不可变图片角色分流证据。

    文件可以尚未下载；本函数只校验路径边界和上游声明。实际文件是否存在、
    是否与声明 SHA 一致以及能否解码，由后续指纹 operation 形成 attempt。
    """

    parsed = parse_image_manifest(manifest_path, image_root)
    root_identity = root_identity_sha256(image_root)
    manifest_id = _canonical_sha256(
        {
            "run_id": run_id,
            "source_snapshot_id": source_snapshot_id,
            "source_sha256": parsed.source_sha256,
            "root_identity_sha256": root_identity,
            "manifest_version": "image-manifest-v1",
        }
    )[:32]
    result = _result_from_rows(manifest_id, parsed.source_sha256, parsed.rows)
    now = _utcnow()
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        _validate_run(connection, run_id, source_snapshot_id, config)
        _validate_source_mapping(connection, parsed.rows)
        existing = connection.execute(
            """
            SELECT manifest_id, root_identity_sha256
            FROM image_manifest_imports WHERE run_id = ? AND source_sha256 = ?
            """,
            (run_id, parsed.source_sha256),
        ).fetchone()
        if existing is not None:
            if existing["manifest_id"] != manifest_id or existing["root_identity_sha256"] != root_identity:
                raise ImageRepositoryError("manifest_root_identity_conflict")
            return result
        with connection:
            connection.execute(
                """
                INSERT INTO image_manifest_imports(
                    manifest_id, run_id, source_snapshot_id, manifest_version,
                    source_sha256, root_identity_sha256, row_count,
                    accepted_row_count, rejected_row_count, status, reason_code,
                    created_at_utc
                ) VALUES (?, ?, ?, 'image-manifest-v1', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest_id,
                    run_id,
                    source_snapshot_id,
                    parsed.source_sha256,
                    root_identity,
                    result.row_count,
                    result.accepted_row_count,
                    result.rejected_row_count,
                    result.status,
                    "manifest_contains_rejected_rows" if result.rejected_row_count else None,
                    now,
                ),
            )
            for row in parsed.rows:
                manifest_row_id = _canonical_sha256(
                    {
                        "manifest_id": manifest_id,
                        "row_number": row.row_number,
                        "row_identity_sha256": row.row_identity_sha256,
                    }
                )[:32]
                connection.execute(
                    """
                    INSERT INTO image_manifest_rows(
                        manifest_row_id, manifest_id, row_number, source_image_id,
                        source_post_id, relation_role, relative_path,
                        expected_file_sha256, parent_file_sha256, transform_json,
                        row_identity_sha256, validation_status, reason_code, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest_row_id,
                        manifest_id,
                        row.row_number,
                        row.source_image_id,
                        row.source_post_id,
                        row.relation_role,
                        row.relative_path,
                        row.expected_file_sha256,
                        row.parent_file_sha256,
                        row.transform_json,
                        row.row_identity_sha256,
                        row.validation_status,
                        row.reason_code,
                        now,
                    ),
                )
                if row.validation_status != "accepted":
                    continue
                decision = decide_image_role(row.relation_role)
                output_sha = _canonical_sha256(
                    {
                        "row_identity_sha256": row.row_identity_sha256,
                        "role_version": str(config.algorithm_versions["image_role"]),
                        "relation_role": decision.relation_role,
                        "handling_action": decision.handling_action,
                        "reason_code": decision.reason_code,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO image_role_results(
                        role_decision_id, manifest_row_id, role_version,
                        relation_role, handling_action, reason_code,
                        output_sha256, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        output_sha[:32],
                        manifest_row_id,
                        str(config.algorithm_versions["image_role"]),
                        decision.relation_role,
                        decision.handling_action,
                        decision.reason_code,
                        output_sha,
                        now,
                    ),
                )
            _record_attempt(
                connection,
                run_id=run_id,
                manifest_id=manifest_id,
                manifest_row_id=None,
                operation="import_manifest",
                status="succeeded",
                reason_code="image_manifest_imported",
                details={
                    "row_count": result.row_count,
                    "accepted_row_count": result.accepted_row_count,
                    "rejected_row_count": result.rejected_row_count,
                },
            )
    return result


def process_image_fingerprints(
    derived_db: str | Path,
    *,
    manifest_id: str,
    image_root: str | Path,
    config: CleaningConfig,
) -> ImageFingerprintBatchResult:
    """处理一个已导入清单的内容图片，并为跳过或阻塞逐行追加 attempt。

    `author_avatar` 和 `page` 保留角色证据但不打开文件；内容图缺失、声明哈希
    冲突或解码失败统一标记为 `blocked`，不使用模型失败状态。
    """

    _require_image_library_lock(config)
    root_identity = root_identity_sha256(image_root)
    fingerprint_version = str(config.algorithm_versions["image_fingerprint"])
    succeeded = 0
    blocked = 0
    skipped = 0
    fingerprint_ids: list[str] = []
    reason_counts: dict[str, int] = {}
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        manifest = connection.execute(
            """
            SELECT manifest_id, run_id, root_identity_sha256
            FROM image_manifest_imports WHERE manifest_id = ?
            """,
            (manifest_id,),
        ).fetchone()
        if manifest is None:
            raise ImageRepositoryError("image_manifest_missing")
        if manifest["root_identity_sha256"] != root_identity:
            raise ImageRepositoryError("manifest_root_identity_conflict")
        run_id = str(manifest["run_id"])
        rows = connection.execute(
            """
            SELECT r.manifest_row_id, r.relative_path, r.expected_file_sha256,
                   r.row_identity_sha256, r.validation_status, d.handling_action
            FROM image_manifest_rows AS r
            LEFT JOIN image_role_results AS d ON d.manifest_row_id = r.manifest_row_id
            WHERE r.manifest_id = ? ORDER BY r.row_number
            """,
            (manifest_id,),
        ).fetchall()
        for row in rows:
            row_id = str(row["manifest_row_id"])
            if row["validation_status"] != "accepted":
                continue
            action = str(row["handling_action"])
            if action != "inspect_content":
                reason = (
                    "author_avatar_skipped"
                    if action == "exclude_from_content"
                    else "page_evidence_skipped"
                )
                with connection:
                    _record_attempt(
                        connection,
                        run_id=run_id,
                        manifest_id=manifest_id,
                        manifest_row_id=row_id,
                        operation="fingerprints",
                        status="skipped",
                        reason_code=reason,
                    )
                skipped += 1
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                continue
            existing = connection.execute(
                """
                SELECT fingerprint_id FROM image_fingerprints
                WHERE manifest_row_id = ? AND fingerprint_version = ?
                """,
                (row_id, fingerprint_version),
            ).fetchone()
            if existing is not None:
                succeeded += 1
                fingerprint_ids.append(str(existing["fingerprint_id"]))
                continue
            try:
                file_path = resolve_manifest_path(image_root, str(row["relative_path"]))
                fingerprint = fingerprint_image_file(
                    file_path,
                    expected_file_sha256=str(row["expected_file_sha256"]),
                    hash_size=config.image.phash_hash_size,
                    highfreq_factor=config.image.phash_highfreq_factor,
                )
            except ImageFingerprintError as exc:
                with connection:
                    _record_attempt(
                        connection,
                        run_id=run_id,
                        manifest_id=manifest_id,
                        manifest_row_id=row_id,
                        operation="fingerprints",
                        status="blocked",
                        reason_code=exc.reason_code,
                    )
                blocked += 1
                reason_counts[exc.reason_code] = reason_counts.get(exc.reason_code, 0) + 1
                continue
            payload = fingerprint_payload(fingerprint)
            output_sha = _canonical_sha256(
                {
                    "row_identity_sha256": row["row_identity_sha256"],
                    "fingerprint_version": fingerprint_version,
                    "fingerprint": payload,
                }
            )
            fingerprint_id = output_sha[:32]
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_fingerprints(
                        fingerprint_id, manifest_row_id, fingerprint_version,
                        row_identity_sha256, file_sha256, mime_type, byte_size,
                        width_px, height_px, has_alpha, is_fully_transparent,
                        sanitized_exif_json, phash_hex, phash_hash_size,
                        phash_highfreq_factor, library_versions_json,
                        output_sha256, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint_id,
                        row_id,
                        fingerprint_version,
                        row["row_identity_sha256"],
                        fingerprint.file_sha256,
                        fingerprint.mime_type,
                        fingerprint.byte_size,
                        fingerprint.width_px,
                        fingerprint.height_px,
                        int(fingerprint.has_alpha),
                        int(fingerprint.is_fully_transparent),
                        json.dumps(
                            fingerprint.sanitized_exif,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        fingerprint.phash_hex,
                        fingerprint.phash_hash_size,
                        fingerprint.phash_highfreq_factor,
                        json.dumps(
                            fingerprint.library_versions,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        output_sha,
                        _utcnow(),
                    ),
                )
                _record_attempt(
                    connection,
                    run_id=run_id,
                    manifest_id=manifest_id,
                    manifest_row_id=row_id,
                    operation="fingerprints",
                    status="succeeded",
                    reason_code="fingerprint_created",
                    details={"output_sha256": output_sha},
                )
            succeeded += 1
            fingerprint_ids.append(fingerprint_id)
            reason_counts["fingerprint_created"] = reason_counts.get("fingerprint_created", 0) + 1
    return ImageFingerprintBatchResult(
        manifest_id=manifest_id,
        succeeded_count=succeeded,
        blocked_count=blocked,
        skipped_count=skipped,
        fingerprint_ids=tuple(fingerprint_ids),
        reason_counts=dict(sorted(reason_counts.items())),
    )


def _candidate_records(
    connection: sqlite3.Connection,
    manifest_id: str,
    fingerprint_version: str,
) -> tuple[CandidateImage, ...]:
    """读取完整内容指纹，并从冻结源快照只提取 URL 布尔信号。"""

    rows = connection.execute(
        """
        SELECT f.fingerprint_id, f.row_identity_sha256, f.file_sha256,
               f.phash_hex, f.byte_size, f.width_px, f.height_px,
               f.is_fully_transparent, r.source_image_id, r.source_post_id,
               p.current_author_sha256, p.current_author_identity_present,
               i.source_snapshot_id, s.snapshot_path, s.snapshot_sha256
        FROM image_manifest_rows AS r
        JOIN image_role_results AS d ON d.manifest_row_id = r.manifest_row_id
        JOIN image_fingerprints AS f ON f.manifest_row_id = r.manifest_row_id
        JOIN source_post_inventory AS p ON p.source_post_id = r.source_post_id
        JOIN image_manifest_imports AS i ON i.manifest_id = r.manifest_id
        JOIN source_snapshots AS s ON s.snapshot_id = i.source_snapshot_id
        WHERE r.manifest_id = ? AND r.validation_status = 'accepted'
          AND d.handling_action = 'inspect_content' AND f.fingerprint_version = ?
        ORDER BY f.fingerprint_id
        """,
        (manifest_id, fingerprint_version),
    ).fetchall()
    if not rows:
        return ()
    snapshot_paths = {str(row["snapshot_path"]) for row in rows}
    snapshot_hashes = {str(row["snapshot_sha256"]) for row in rows}
    if len(snapshot_paths) != 1 or len(snapshot_hashes) != 1:
        raise ImageRepositoryError("mixed_image_snapshots")
    snapshot_path = Path(next(iter(snapshot_paths)))
    if not snapshot_path.is_file() or sha256_file(snapshot_path) != next(iter(snapshot_hashes)):
        raise ImageRepositoryError("snapshot_sha256_mismatch")
    image_urls: dict[int, str | None] = {}
    with open_source_readonly(snapshot_path) as source:
        for row in source.execute("SELECT id, image_url FROM web_post_images"):
            image_urls[int(row["id"])] = None if row["image_url"] is None else str(row["image_url"])
    return tuple(
        CandidateImage(
            fingerprint_id=str(row["fingerprint_id"]),
            row_identity_sha256=str(row["row_identity_sha256"]),
            source_image_id=int(row["source_image_id"]),
            source_post_id=int(row["source_post_id"]),
            author_identity_sha256=(
                str(row["current_author_sha256"])
                if int(row["current_author_identity_present"]) == 1
                else None
            ),
            file_sha256=str(row["file_sha256"]),
            phash_hex=str(row["phash_hex"]),
            byte_size=int(row["byte_size"]),
            width_px=int(row["width_px"]),
            height_px=int(row["height_px"]),
            is_fully_transparent=bool(row["is_fully_transparent"]),
            url_role_hint=url_has_role_hint(image_urls.get(int(row["source_image_id"]))),
        )
        for row in rows
    )


def _candidate_result(build_id: str, plan: ImageCandidatePlan) -> ImageCandidateBuildResult:
    return ImageCandidateBuildResult(
        build_id=build_id,
        input_manifest_sha256=plan.input_manifest_sha256,
        fingerprint_count=len(plan.members),
        exact_cluster_count=len(plan.exact_clusters),
        exact_duplicate_cluster_count=sum(
            len(cluster.member_fingerprint_ids) > 1 for cluster in plan.exact_clusters
        ),
        near_pair_count=len(plan.near_pairs),
        signal_count=len(plan.signals),
        output_sha256=plan.output_sha256,
    )


def build_image_candidates(
    derived_db: str | Path,
    *,
    manifest_id: str,
    config: CleaningConfig,
) -> ImageCandidateBuildResult:
    """从完整内容指纹构建并封存精确簇、近似对和技术信号。

    任一内容文件仍阻塞时不创建半成品 build。所有阈值只产生候选关系或信号，
    不写最终图片标签，也不改变 manifest 或原文件。
    """

    fingerprint_version = str(config.algorithm_versions["image_fingerprint"])
    candidate_version = str(config.algorithm_versions["image_noise"])
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        manifest = connection.execute(
            "SELECT run_id FROM image_manifest_imports WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        if manifest is None:
            raise ImageRepositoryError("image_manifest_missing")
        run_id = str(manifest["run_id"])
        expected = connection.execute(
            """
            SELECT COUNT(*) FROM image_manifest_rows AS r
            JOIN image_role_results AS d ON d.manifest_row_id = r.manifest_row_id
            WHERE r.manifest_id = ? AND r.validation_status = 'accepted'
              AND d.handling_action = 'inspect_content'
            """,
            (manifest_id,),
        ).fetchone()[0]
        records = _candidate_records(connection, manifest_id, fingerprint_version)
        if len(records) != int(expected):
            with connection:
                _record_attempt(
                    connection,
                    run_id=run_id,
                    manifest_id=manifest_id,
                    manifest_row_id=None,
                    operation="candidates",
                    status="blocked",
                    reason_code="image_fingerprints_incomplete",
                    details={"expected_count": int(expected), "available_count": len(records)},
                )
            raise ImageRepositoryError("image_fingerprints_incomplete")
        plan = build_image_candidate_plan(records, config.image)
        build_id = _canonical_sha256(
            {
                "run_id": run_id,
                "manifest_id": manifest_id,
                "candidate_version": candidate_version,
                "fingerprint_version": fingerprint_version,
                "config_sha256": config.sha256,
                "input_manifest_sha256": plan.input_manifest_sha256,
            }
        )[:32]
        result = _candidate_result(build_id, plan)
        existing = connection.execute(
            "SELECT seal_status, output_sha256 FROM image_candidate_builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        if existing is not None:
            if existing["seal_status"] != "finalized" or existing["output_sha256"] != plan.output_sha256:
                raise ImageRepositoryError("image_candidate_build_conflict")
            return result
        now = _utcnow()
        with connection:
            connection.execute(
                """
                INSERT INTO image_candidate_builds(
                    build_id, run_id, manifest_id, candidate_version,
                    fingerprint_version, config_sha256, input_manifest_sha256,
                    expected_fingerprint_count, exact_cluster_count,
                    exact_duplicate_cluster_count, near_pair_count, signal_count,
                    seal_status, output_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?, ?)
                """,
                (
                    build_id,
                    run_id,
                    manifest_id,
                    candidate_version,
                    fingerprint_version,
                    config.sha256,
                    plan.input_manifest_sha256,
                    result.fingerprint_count,
                    result.exact_cluster_count,
                    result.exact_duplicate_cluster_count,
                    result.near_pair_count,
                    result.signal_count,
                    result.output_sha256,
                    now,
                ),
            )
            for record in plan.members:
                connection.execute(
                    """
                    INSERT INTO image_candidate_build_members(
                        build_id, fingerprint_id, source_image_id, source_post_id,
                        row_identity_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        record.fingerprint_id,
                        record.source_image_id,
                        record.source_post_id,
                        record.row_identity_sha256,
                    ),
                )
            for signal in plan.signals:
                connection.execute(
                    """
                    INSERT INTO image_candidate_signals(
                        build_id, fingerprint_id, signal_code, evidence_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        signal.fingerprint_id,
                        signal.signal_code,
                        json.dumps(
                            signal.evidence,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            for cluster in plan.exact_clusters:
                connection.execute(
                    """
                    INSERT INTO image_exact_clusters(
                        build_id, cluster_id, file_sha256,
                        representative_fingerprint_id, member_count
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        cluster.cluster_id,
                        cluster.file_sha256,
                        cluster.representative_fingerprint_id,
                        len(cluster.member_fingerprint_ids),
                    ),
                )
                for fingerprint_id in cluster.member_fingerprint_ids:
                    connection.execute(
                        """
                        INSERT INTO image_exact_cluster_members(
                            build_id, cluster_id, fingerprint_id, is_representative
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            build_id,
                            cluster.cluster_id,
                            fingerprint_id,
                            int(fingerprint_id == cluster.representative_fingerprint_id),
                        ),
                    )
            for pair in plan.near_pairs:
                connection.execute(
                    """
                    INSERT INTO image_near_candidate_pairs(
                        build_id, left_fingerprint_id, right_fingerprint_id,
                        hamming_distance
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        pair.left_fingerprint_id,
                        pair.right_fingerprint_id,
                        pair.hamming_distance,
                    ),
                )
            connection.execute(
                "UPDATE image_candidate_builds SET seal_status = 'finalized' WHERE build_id = ?",
                (build_id,),
            )
            _record_attempt(
                connection,
                run_id=run_id,
                manifest_id=manifest_id,
                manifest_row_id=None,
                operation="candidates",
                status="succeeded",
                reason_code="image_candidates_created",
                details={"output_sha256": result.output_sha256},
            )
    return result


def record_manifest_block(
    derived_db: str | Path,
    *,
    run_id: str,
    operation: str,
    config: CleaningConfig,
) -> None:
    """在没有 manifest ID 时追加运行级阻塞证据，供下载完成后显式恢复。"""

    if operation not in {"roles", "fingerprints", "candidates"}:
        raise ImageRepositoryError("image_operation_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        run = connection.execute(
            "SELECT config_sha256, protocol_version FROM cleaning_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ImageRepositoryError("image_run_not_found")
        if not matches_frozen_run(config, str(run["config_sha256"]), str(run["protocol_version"])):
            raise ImageRepositoryError("run_config_mismatch")
        with connection:
            _record_attempt(
                connection,
                run_id=run_id,
                manifest_id=None,
                manifest_row_id=None,
                operation=operation,
                status="blocked",
                reason_code="blocked_by_manifest",
            )


def load_image_stage_snapshot(
    derived_db: str | Path,
    *,
    manifest_id: str,
    stage: str,
    config: CleaningConfig,
    build_id: str | None = None,
) -> ImageStageSnapshot:
    """将不可变图片证据投影为调度任务可消费的逐图片终态。

    这里只读取已持久化结果，不执行文件 I/O。冲突或被拒绝的 manifest 行不会
    产生 outcome，调用方会把对应调度任务标记为映射缺失阻塞。
    """

    if stage not in {"roles", "fingerprints", "candidates"}:
        raise ImageRepositoryError("image_stage_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        manifest = connection.execute(
            "SELECT run_id FROM image_manifest_imports WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        if manifest is None:
            raise ImageRepositoryError("image_manifest_missing")
        run_id = str(manifest["run_id"])
        rows = connection.execute(
            """
            SELECT r.manifest_row_id, r.source_image_id, r.row_identity_sha256,
                   d.handling_action, d.reason_code AS role_reason,
                   d.output_sha256 AS role_output
            FROM image_manifest_rows AS r
            JOIN image_role_results AS d ON d.manifest_row_id = r.manifest_row_id
            WHERE r.manifest_id = ? AND r.validation_status = 'accepted'
              AND d.role_version = ?
            ORDER BY r.source_image_id
            """,
            (manifest_id, str(config.algorithm_versions["image_role"])),
        ).fetchall()
        if stage == "roles":
            return ImageStageSnapshot(
                run_id,
                tuple(
                    ImageStageOutcome(
                        source_image_id=int(row["source_image_id"]),
                        status="succeeded",
                        reason_code=None,
                        output_sha256=str(row["role_output"]),
                    )
                    for row in rows
                ),
            )

        fingerprint_version = str(config.algorithm_versions["image_fingerprint"])
        fingerprints = {
            str(row["manifest_row_id"]): row
            for row in connection.execute(
                """
                SELECT f.manifest_row_id, f.fingerprint_id, f.output_sha256
                FROM image_fingerprints AS f
                JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
                WHERE r.manifest_id = ? AND f.fingerprint_version = ?
                """,
                (manifest_id, fingerprint_version),
            )
        }
        latest_attempts: dict[str, sqlite3.Row] = {}
        for attempt in connection.execute(
            """
            SELECT manifest_row_id, status, reason_code, attempt_number
            FROM image_processing_attempts
            WHERE manifest_id = ? AND operation = 'fingerprints'
              AND manifest_row_id IS NOT NULL
            ORDER BY attempt_number
            """,
            (manifest_id,),
        ):
            latest_attempts[str(attempt["manifest_row_id"])] = attempt

        if stage == "fingerprints":
            outcomes: list[ImageStageOutcome] = []
            for row in rows:
                row_id = str(row["manifest_row_id"])
                source_image_id = int(row["source_image_id"])
                if row["handling_action"] != "inspect_content":
                    reason = (
                        "author_avatar_skipped"
                        if row["handling_action"] == "exclude_from_content"
                        else "page_evidence_skipped"
                    )
                    outcomes.append(ImageStageOutcome(source_image_id, "skipped", reason, None))
                elif row_id in fingerprints:
                    outcomes.append(
                        ImageStageOutcome(
                            source_image_id,
                            "succeeded",
                            None,
                            str(fingerprints[row_id]["output_sha256"]),
                        )
                    )
                else:
                    attempt = latest_attempts.get(row_id)
                    reason = (
                        str(attempt["reason_code"])
                        if attempt is not None
                        else "image_fingerprint_unavailable"
                    )
                    outcomes.append(ImageStageOutcome(source_image_id, "blocked", reason, None))
            return ImageStageSnapshot(run_id, tuple(outcomes))

        if build_id is None:
            raise ImageRepositoryError("image_candidate_build_required")
        build = connection.execute(
            """
            SELECT output_sha256, seal_status FROM image_candidate_builds
            WHERE build_id = ? AND run_id = ? AND manifest_id = ?
            """,
            (build_id, run_id, manifest_id),
        ).fetchone()
        if build is None or build["seal_status"] != "finalized":
            raise ImageRepositoryError("image_candidate_build_missing")
        member_ids = {
            str(row["fingerprint_id"])
            for row in connection.execute(
                "SELECT fingerprint_id FROM image_candidate_build_members WHERE build_id = ?",
                (build_id,),
            )
        }
        outcomes = []
        for row in rows:
            source_image_id = int(row["source_image_id"])
            if row["handling_action"] != "inspect_content":
                reason = (
                    "author_avatar_skipped"
                    if row["handling_action"] == "exclude_from_content"
                    else "page_evidence_skipped"
                )
                outcomes.append(ImageStageOutcome(source_image_id, "skipped", reason, None))
                continue
            fingerprint = fingerprints.get(str(row["manifest_row_id"]))
            if fingerprint is None or str(fingerprint["fingerprint_id"]) not in member_ids:
                outcomes.append(
                    ImageStageOutcome(
                        source_image_id,
                        "blocked",
                        "image_candidate_member_missing",
                        None,
                    )
                )
                continue
            # 每个任务输出同时绑定全局 build 与 manifest 行，不能用统一 build 哈希
            # 掩盖某行映射变化。
            output_sha = _canonical_sha256(
                {
                    "build_id": build_id,
                    "build_output_sha256": build["output_sha256"],
                    "row_identity_sha256": row["row_identity_sha256"],
                }
            )
            outcomes.append(ImageStageOutcome(source_image_id, "succeeded", None, output_sha))
        return ImageStageSnapshot(run_id, tuple(outcomes))
