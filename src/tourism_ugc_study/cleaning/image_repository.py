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

from .config import CleaningConfig, matches_frozen_run
from .image_manifest import (
    ImageManifestRow,
    parse_image_manifest,
    root_identity_sha256,
)
from .image_role import decide_image_role
from .schema import connect_derived, migrate_derived


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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
                    INSERT INTO image_role_decisions(
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
    return result
