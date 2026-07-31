"""图片清单与角色证据的 SQLite 持久化边界。

绝对根路径只在调用期间用于路径校验，派生库仅保存根目录摘要、相对路径和
manifest 行身份。正式采集库始终只读；本模块只写独立派生 SQLite。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

from .config import CleaningConfig, validate_image_algorithm_contract
from .fingerprints import post_author_identity_present, post_fingerprints
from .image_candidates import CandidateImage, ImageCandidatePlan, build_image_candidate_plan, url_has_role_hint
from .image_fingerprint import (
    ImageFingerprintError,
    current_image_library_versions,
    fingerprint_image_file,
    fingerprint_payload,
)
from .image_contract import (
    ImageContractError,
    ImageRunContract,
    open_image_snapshot_readonly,
    validate_image_run_contract,
)
from .image_manifest import (
    ImageManifestRow,
    parse_image_manifest,
    resolve_manifest_path,
    root_identity_sha256,
)
from .image_role import decide_image_role
from .schema import connect_derived, migrate_derived


class ImageRepositoryError(RuntimeError):
    """图片仓储操作不能安全继续时抛出的去敏异常。

    `reason_code` 表示运行契约、源映射、文件阻塞或构建冲突，不包含路径、URL、
    作者值或图片内容。契约与谱系错误会在写入/复用前失败；单文件缺失等可恢复
    情况由批处理记录为 `blocked`，而不是把本异常解释为模型预测失败。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("image repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ImageManifestImportResult:
    """清单导入的可公开、可幂等比较回执。

    `manifest_id/source_sha256` 绑定运行、根目录摘要和 CSV；`status` 与三类计数
    描述整份清单，`accepted_row_identities` 只返回可处理行的 SHA 身份。回执不
    包含机器路径、URL、作者或图片内容；重复导入同一身份返回相同字段。
    """

    manifest_id: str
    source_sha256: str
    status: str
    row_count: int
    accepted_row_count: int
    rejected_row_count: int
    accepted_row_identities: tuple[str, ...]


@dataclass(frozen=True)
class ImageFingerprintBatchResult:
    """一次 manifest 指纹处理的无敏感计数回执。

    三类计数分别表示成功（含安全复用）、可恢复阻塞和按来源角色跳过；
    `fingerprint_ids` 只列成功证据身份，`reason_counts` 汇总去敏原因。阻塞不记
    为模型失败，补齐文件后可重复调用；已存在指纹不会被覆盖。
    """

    manifest_id: str
    succeeded_count: int
    blocked_count: int
    skipped_count: int
    fingerprint_ids: tuple[str, ...]
    reason_counts: Mapping[str, int]


@dataclass(frozen=True)
class ImageCandidateBuildResult:
    """一个已封存候选构建的身份和无敏感统计。

    `build_id/input_manifest_sha256/output_sha256` 固定输入输出谱系，其余字段是
    指纹、精确簇、重复簇、近似对和信号计数。结果只表示候选构建成功，不表示
    图片已被删除或获得最终标签；相同输入与配置可幂等复用。
    """

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
    """将不可变 manifest 证据投影回一个图片调度任务。

    `source_image_id` 是任务键；`status` 仅取成功、阻塞或跳过；原因只在非成功
    情况出现，输出摘要只在成功时出现。对象不携带图片路径和内容，也不自行
    修改任务状态。
    """

    source_image_id: int
    status: str
    reason_code: str | None
    output_sha256: str | None


@dataclass(frozen=True)
class ImageStageSnapshot:
    """一个 manifest 所属运行及其逐图片阶段结果。

    `run_id` 用于阻止跨运行批次映射，`outcomes` 按源图片身份提供只读终态。
    快照是派生证据的内存投影，不是源 SQLite 快照，也不会产生新处理结果。
    """

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


def _require_contract(
    connection: sqlite3.Connection,
    *,
    config: CleaningConfig,
    run_id: str | None = None,
    source_snapshot_id: str | None = None,
    manifest_id: str | None = None,
    root_identity_sha256: str | None = None,
) -> ImageRunContract:
    """把统一契约异常转换为仓储层公开的去敏失败类型。"""

    try:
        return validate_image_run_contract(
            connection,
            config=config,
            run_id=run_id,
            source_snapshot_id=source_snapshot_id,
            manifest_id=manifest_id,
            root_identity_sha256=root_identity_sha256,
        )
    except ImageContractError as exc:
        raise ImageRepositoryError(exc.reason_code) from exc


@contextmanager
def _open_contract_snapshot(
    contract: ImageRunContract,
) -> Iterator[sqlite3.Connection]:
    """在仓储边界重开冻结快照，并把契约错误转换为公开仓储错误。"""

    try:
        with open_image_snapshot_readonly(contract) as source:
            yield source
    except ImageContractError as exc:
        # 只传播固定 reason_code；底层 SQLite/OSError 可能包含本机绝对路径，
        # 只能保留在异常 cause 链中供进程内调试，不能成为 CLI 或 attempt 文本。
        raise ImageRepositoryError(exc.reason_code) from exc


def _validate_source_mapping(
    connection: sqlite3.Connection,
    rows: tuple[ImageManifestRow, ...],
    contract: ImageRunContract,
) -> None:
    """按冻结快照核对图片、帖子和权威角色，不信任 CSV 或当前库存。"""

    source_rows: dict[int, tuple[int, str]] = {}
    with _open_contract_snapshot(contract) as source:
        for source_row in source.execute(
            "SELECT id, web_post_id, image_role FROM web_post_images"
        ):
            source_rows[int(source_row["id"])] = (
                int(source_row["web_post_id"]),
                str(source_row["image_role"]),
            )
    for row in rows:
        source = source_rows.get(row.source_image_id)
        if source is None:
            raise ImageRepositoryError("manifest_source_image_not_found")
        source_post_id, source_role = source
        if source_post_id != row.source_post_id:
            raise ImageRepositoryError("manifest_source_post_mismatch")
        if source_role != row.relation_role:
            raise ImageRepositoryError("manifest_relation_role_mismatch")
        # 当前库存只确认两个外键目标仍登记在派生库。图片后来改挂其他帖子、
        # 改变角色或已经缺失，都不能反向改写旧快照 manifest 的合法语义。
        image_inventory = connection.execute(
            "SELECT 1 FROM source_image_inventory WHERE source_image_id = ?",
            (row.source_image_id,),
        ).fetchone()
        post_inventory = connection.execute(
            "SELECT 1 FROM source_post_inventory WHERE source_post_id = ?",
            (source_post_id,),
        ).fetchone()
        if image_inventory is None or post_inventory is None:
            raise ImageRepositoryError("manifest_inventory_lineage_mismatch")


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

    输入必须显式给出冻结运行/快照、CSV、图片根目录和冻结配置。函数先验证
    运行契约，再以冻结 `web_post_images` 核对图片、帖子和权威角色，随后只向
    派生 SQLite 追加清单、角色与 attempt。文件可以尚未下载；这里只校验路径
    边界和上游声明，存在性、SHA 与解码由后续指纹 operation 形成 attempt。

    同一运行、CSV 与根目录身份可幂等复用；身份冲突或角色不一致在任何写入前
    失败。不回写源库，不保存绝对路径、URL 或图片字节。
    """

    validate_image_algorithm_contract(config.image)
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
        contract = _require_contract(
            connection,
            config=config,
            run_id=run_id,
            source_snapshot_id=source_snapshot_id,
        )
        _validate_source_mapping(connection, parsed.rows, contract)
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

    输入是已导入清单、同一图片根目录和冻结配置。函数在任何结果复用前核对
    运行/快照/config/root 与依赖版本，只读打开 `content` 文件并追加不可变指纹
    和 attempt。`author_avatar` 和 `page` 保留角色证据但不打开文件；内容图缺失、声明哈希
    冲突或解码失败统一标记为 `blocked`，不使用模型失败状态。

    已成功的同版本行幂等复用；阻塞行在文件补齐后可再次尝试。函数不联网、
    不修改图片或正式源库，也不产生最终图片清洗标签。
    """

    validate_image_algorithm_contract(config.image)
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
        contract = _require_contract(
            connection,
            config=config,
            manifest_id=manifest_id,
            root_identity_sha256=root_identity,
        )
        run_id = contract.run_id
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
    contract: ImageRunContract,
) -> tuple[CandidateImage, ...]:
    """读取完整内容指纹，并从绑定快照提取作者摘要与 URL 布尔信号。"""

    rows = connection.execute(
        """
        SELECT f.fingerprint_id, f.row_identity_sha256, f.file_sha256,
               f.phash_hex, f.byte_size, f.width_px, f.height_px,
               f.is_fully_transparent, r.source_image_id, r.source_post_id,
               i.source_snapshot_id
        FROM image_manifest_rows AS r
        JOIN image_role_results AS d ON d.manifest_row_id = r.manifest_row_id
        JOIN image_fingerprints AS f ON f.manifest_row_id = r.manifest_row_id
        JOIN image_manifest_imports AS i ON i.manifest_id = r.manifest_id
        WHERE r.manifest_id = ? AND r.validation_status = 'accepted'
          AND d.handling_action = 'inspect_content' AND f.fingerprint_version = ?
        ORDER BY f.fingerprint_id
        """,
        (manifest_id, fingerprint_version),
    ).fetchall()
    if not rows:
        return ()
    if {str(row["source_snapshot_id"]) for row in rows} != {contract.source_snapshot_id}:
        raise ImageRepositoryError("image_manifest_lineage_mismatch")
    image_urls: dict[int, str | None] = {}
    authors: dict[int, str | None] = {}
    with _open_contract_snapshot(contract) as source:
        for source_row in source.execute("SELECT id, image_url FROM web_post_images"):
            image_urls[int(source_row["id"])] = (
                None if source_row["image_url"] is None else str(source_row["image_url"])
            )
        for source_row in source.execute("SELECT * FROM web_posts"):
            authors[int(source_row["id"])] = (
                post_fingerprints(source_row)[1]
                if post_author_identity_present(source_row)
                else None
            )
    return tuple(
        CandidateImage(
            fingerprint_id=str(row["fingerprint_id"]),
            row_identity_sha256=str(row["row_identity_sha256"]),
            source_image_id=int(row["source_image_id"]),
            source_post_id=int(row["source_post_id"]),
            author_identity_sha256=authors.get(int(row["source_post_id"])),
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

    输入是一个已导入 manifest 和冻结配置；函数先验证运行契约，再从绑定源快照
    取得 URL 布尔信号及作者摘要，绝不读取后来变化的当前库存。任一内容文件仍
    阻塞时不创建半成品 build。所有阈值只产生候选关系或信号，
    不写最终图片标签，也不改变 manifest 或原文件。

    构建以完整输入摘要和配置为身份，在单事务内写完并由数据库校验后封存；
    相同身份只读复用，冲突则失败。输出不含 URL、路径、作者原值或图片内容。
    """

    validate_image_algorithm_contract(config.image)
    fingerprint_version = str(config.algorithm_versions["image_fingerprint"])
    candidate_version = str(config.algorithm_versions["image_noise"])
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        contract = _require_contract(
            connection,
            config=config,
            manifest_id=manifest_id,
        )
        run_id = contract.run_id
        expected = connection.execute(
            """
            SELECT COUNT(*) FROM image_manifest_rows AS r
            JOIN image_role_results AS d ON d.manifest_row_id = r.manifest_row_id
            WHERE r.manifest_id = ? AND r.validation_status = 'accepted'
              AND d.handling_action = 'inspect_content'
            """,
            (manifest_id,),
        ).fetchone()[0]
        records = _candidate_records(
            connection,
            manifest_id,
            fingerprint_version,
            contract,
        )
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
    """在没有 manifest ID 时追加运行级阻塞证据。

    输入限定为图片角色、指纹或候选 operation，并先核对运行绑定的快照与配置；
    成功时只追加去敏 `blocked_by_manifest` attempt，供图片下载及清单导入完成后
    显式恢复。它不创建伪 manifest、不读取源图，也不把阻塞记为算法失败。
    """

    validate_image_algorithm_contract(config.image)
    if operation not in {"roles", "fingerprints", "candidates"}:
        raise ImageRepositoryError("image_operation_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        run = connection.execute(
            "SELECT source_snapshot_id FROM cleaning_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None or run["source_snapshot_id"] is None:
            raise ImageRepositoryError("image_run_not_found")
        _require_contract(
            connection,
            config=config,
            run_id=run_id,
            source_snapshot_id=str(run["source_snapshot_id"]),
        )
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

    输入是 manifest、阶段、冻结配置及候选阶段必需的 `build_id`。函数在读取
    结果前校验统一运行契约，再将角色、指纹或已封存候选证据投影为逐图片终态。
    这里只读取已持久化结果，不执行图片文件 I/O。冲突或被拒绝的 manifest 行不会
    产生 outcome，调用方会把对应调度任务标记为映射缺失阻塞。

    函数不会追加 attempt 或改变任务；相同数据库状态重复读取结果一致。构建缺失、
    跨运行或配置漂移显式失败，输出不含本地路径与原始源字段。
    """

    validate_image_algorithm_contract(config.image)
    if stage not in {"roles", "fingerprints", "candidates"}:
        raise ImageRepositoryError("image_stage_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        contract = _require_contract(
            connection,
            config=config,
            manifest_id=manifest_id,
        )
        run_id = contract.run_id
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
