"""源库只读校验与可复现 SQLite 快照创建。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .config import CleaningConfig
from .schema import DERIVED_SCHEMA_VERSION, connect_derived, migrate_derived


SHANGHAI = ZoneInfo("Asia/Shanghai")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

REQUIRED_SOURCE_COLUMNS: Mapping[str, frozenset[str]] = {
    "schema_migrations": frozenset({"version", "name", "applied_at"}),
    "source_platforms": frozenset({"platform_key"}),
    "web_posts": frozenset(
        {
            "id",
            "platform_key",
            "source_type",
            "source_url",
            "title",
            "author_platform_id",
            "published_at",
            "captured_at",
            "content_text",
            "post_images_count",
            "status",
        }
    ),
    "web_post_images": frozenset(
        {
            "id",
            "web_post_id",
            "image_index",
            "image_url",
            "image_role",
            "local_path",
            "width",
            "height",
            "mime_type",
            "sha256",
        }
    ),
}


class SnapshotError(RuntimeError):
    """可安全暴露给命令行、且不含源数据的快照异常。"""

    def __init__(self, reason_code: str, message: str = "source snapshot failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class RunAlreadyExistsError(SnapshotError):
    """运行标识已存在时抛出，禁止覆盖历史运行。"""

    def __init__(self) -> None:
        super().__init__("run_id_exists", "run_id already exists")


@dataclass(frozen=True)
class InputContractResult:
    """运行级范围校验结果，不产生逐条城市判断。"""

    status: str
    method: str
    reason_code: str | None
    details: Mapping[str, Any]

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True)
class SnapshotResult:
    """返回给调用方和命令行的去敏摘要。"""

    run_id: str
    run_status: str
    snapshot_id: str
    input_contract_status: str
    input_contract_method: str
    input_contract_reason_code: str | None
    post_count: int
    image_count: int
    source_sha256: str
    snapshot_sha256: str
    object_manifest_sha256: str
    manifest_path: Path


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_source_readonly(path: str | Path) -> sqlite3.Connection:
    """通过 SQLite 只读 URI 打开源库，并再次启用 query_only。"""

    source_path = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _validate_schema(connection: sqlite3.Connection) -> None:
    integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        raise SnapshotError("source_integrity_failed")

    foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign_key_errors:
        raise SnapshotError("source_foreign_key_failed")

    existing_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing_tables = sorted(set(REQUIRED_SOURCE_COLUMNS) - existing_tables)
    if missing_tables:
        raise SnapshotError("source_schema_missing_table")

    for table, required in REQUIRED_SOURCE_COLUMNS.items():
        if not required.issubset(_table_columns(connection, table)):
            raise SnapshotError("source_schema_missing_column")


def _validate_scope(
    connection: sqlite3.Connection,
    config: CleaningConfig,
) -> InputContractResult:
    columns = _table_columns(connection, "web_posts")
    contract = config.input_contract

    if "city_name" in columns:
        placeholders = ", ".join("?" for _ in contract.accepted_legacy_city_values)
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN city_name IS NULL OR trim(city_name) = '' THEN 1 ELSE 0 END) AS missing,
                   SUM(CASE WHEN city_name IS NOT NULL
                                 AND trim(city_name) != ''
                                 AND city_name NOT IN ({placeholders})
                            THEN 1 ELSE 0 END) AS outside_scope
            FROM web_posts
            """,
            contract.accepted_legacy_city_values,
        ).fetchone()
        details = {
            "row_count": int(row["total"]),
            "missing_city_rows": int(row["missing"] or 0),
            "outside_scope_rows": int(row["outside_scope"] or 0),
        }
        if details["missing_city_rows"] or details["outside_scope_rows"]:
            return InputContractResult(
                status="rejected",
                method="legacy_city_column",
                reason_code="city_scope_mismatch",
                details=details,
            )
        return InputContractResult(
            status="accepted",
            method="legacy_city_column",
            reason_code=None,
            details=details,
        )

    migration = contract.upstream_scope_migration
    migration_row = connection.execute(
        "SELECT name FROM schema_migrations WHERE version = ?",
        (migration.version,),
    ).fetchone()
    if migration_row is None or migration_row["name"] != migration.name:
        return InputContractResult(
            status="rejected",
            method="upstream_scope_migration",
            reason_code="city_scope_unverifiable",
            details={"required_migration_version": migration.version},
        )
    return InputContractResult(
        status="accepted",
        method="upstream_scope_migration",
        reason_code=None,
        details={
            "migration_version": migration.version,
            "migration_name": migration.name,
        },
    )


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in sorted(REQUIRED_SOURCE_COLUMNS)
    }


def _object_manifest_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in ("web_posts", "web_post_images"):
        digest.update(table.encode("ascii"))
        digest.update(b"\0")
        for row in connection.execute(f'SELECT id FROM "{table}" ORDER BY id'):
            digest.update(str(row[0]).encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


def _code_version() -> str:
    repository = Path(__file__).resolve().parents[3]
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = revision.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        return "unknown"
    if not status.stdout:
        return value
    try:
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return f"{value}+dirty.unknown"
    dirty_sha256 = hashlib.sha256(status.stdout + diff.stdout).hexdigest()
    return f"{value}+dirty.{dirty_sha256[:12]}"


def _environment() -> dict[str, str]:
    try:
        pyyaml_version = importlib.metadata.version("PyYAML")
    except importlib.metadata.PackageNotFoundError:
        pyyaml_version = "unknown"
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "pyyaml_version": pyyaml_version,
        "operating_system": platform.system(),
    }


def _sidecar_hashes(path: Path) -> dict[str, str | None]:
    values: dict[str, str | None] = {"database": sha256_file(path)}
    for suffix, key in (("-wal", "wal"), ("-shm", "shm")):
        sidecar = Path(f"{path}{suffix}")
        values[key] = sha256_file(sidecar) if sidecar.exists() else None
    return values


def _create_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}.",
        suffix=".sqlite.tmp",
        dir=destination.parent,
        delete=False,
    )
    temporary_path = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        with open_source_readonly(source) as source_connection:
            with sqlite3.connect(temporary_path) as target_connection:
                source_connection.backup(target_connection)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.stem}.",
        suffix=".json.tmp",
        dir=path.parent,
        delete=False,
    )
    temporary_path = Path(temporary_handle.name)
    try:
        with temporary_handle:
            json.dump(payload, temporary_handle, ensure_ascii=False, sort_keys=True, indent=2)
            temporary_handle.write("\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _insert_run(
    connection: sqlite3.Connection,
    run_id: str,
    config: CleaningConfig,
    code_version: str,
    environment_json: str,
    created_at_utc: str,
) -> None:
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO cleaning_runs(
                    run_id, protocol_version, config_sha256, random_seed, status,
                    code_version, environment_json, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    config.protocol_version,
                    config.sha256,
                    config.random_seed,
                    code_version,
                    environment_json,
                    created_at_utc,
                    created_at_utc,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise RunAlreadyExistsError() from exc


def _mark_run(
    connection: sqlite3.Connection,
    run_id: str,
    status: str,
    reason_code: str | None,
    updated_at_utc: str,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE cleaning_runs
            SET status = ?, reason_code = ?, updated_at_utc = ?
            WHERE run_id = ?
            """,
            (status, reason_code, updated_at_utc, run_id),
        )


def snapshot_source(
    source_db: str | Path,
    derived_db: str | Path,
    config: CleaningConfig,
    run_id: str,
) -> SnapshotResult:
    """校验、冻结、计算指纹并登记一个源 SQLite 快照。"""

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise SnapshotError("invalid_run_id")
    if config.algorithm_versions.get("derived_schema") != DERIVED_SCHEMA_VERSION:
        raise SnapshotError("derived_schema_version_mismatch")

    try:
        source_path = Path(source_db).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("source_not_found") from exc
    if not source_path.is_file():
        raise SnapshotError("source_not_file")

    try:
        derived_path = Path(derived_db).expanduser().resolve()
    except OSError as exc:
        raise SnapshotError("derived_database_unavailable") from exc
    if source_path == derived_path:
        raise SnapshotError("source_and_derived_must_differ")

    snapshot_path = derived_path.parent / "source-snapshots" / f"{run_id}.sqlite"
    manifest_path = derived_path.parent / "manifests" / f"{run_id}.source-snapshot.json"
    if snapshot_path.exists() or manifest_path.exists():
        raise RunAlreadyExistsError()

    now_utc = datetime.now(timezone.utc)
    created_at_utc = now_utc.isoformat(timespec="seconds")
    created_at_shanghai = now_utc.astimezone(SHANGHAI).isoformat(timespec="seconds")
    code_version = _code_version()
    environment = _environment()
    environment_json = json.dumps(environment, ensure_ascii=False, sort_keys=True)

    try:
        derived_connection = connect_derived(derived_path)
    except (OSError, sqlite3.Error) as exc:
        raise SnapshotError("derived_database_unavailable") from exc
    try:
        migrate_derived(derived_connection)
        _insert_run(
            derived_connection,
            run_id,
            config,
            code_version,
            environment_json,
            created_at_utc,
        )

        source_size = source_path.stat().st_size
        source_hashes_before = _sidecar_hashes(source_path)
        _create_backup(source_path, snapshot_path)
        source_hashes_after = _sidecar_hashes(source_path)

        snapshot_sha256 = sha256_file(snapshot_path)
        snapshot_size = snapshot_path.stat().st_size
        with open_source_readonly(snapshot_path) as snapshot_connection:
            try:
                _validate_schema(snapshot_connection)
                scope_result = _validate_scope(snapshot_connection, config)
            except SnapshotError as exc:
                scope_result = InputContractResult(
                    status="rejected",
                    method="source_schema_contract",
                    reason_code=exc.reason_code,
                    details={},
                )
            table_counts = _table_counts(snapshot_connection) if scope_result.method != "source_schema_contract" else {}
            object_manifest_sha256 = (
                _object_manifest_sha256(snapshot_connection)
                if scope_result.method != "source_schema_contract"
                else hashlib.sha256(b"").hexdigest()
            )

        source_content_changed = any(
            source_hashes_before[key] != source_hashes_after[key]
            for key in ("database", "wal")
        )
        if source_content_changed:
            scope_result = InputContractResult(
                status="rejected",
                method="source_immutability_check",
                reason_code="source_changed_during_snapshot",
                details={},
            )

        post_count = table_counts.get("web_posts", 0)
        image_count = table_counts.get("web_post_images", 0)
        source_identity_sha256 = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()
        snapshot_id = hashlib.sha256(
            f"{run_id}\0{snapshot_sha256}".encode("utf-8")
        ).hexdigest()[:32]
        run_status = "planned" if scope_result.accepted else "input_rejected"

        manifest = {
            "manifest_version": "source-snapshot-v1",
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "protocol_version": config.protocol_version,
            "label_guide_versions": {
                "text": config.text_label_guide_version,
                "image": config.image_label_guide_version,
            },
            "random_seed": config.random_seed,
            "config_sha256": config.sha256,
            "algorithm_versions": dict(config.algorithm_versions),
            "source": {
                "logical_name": source_path.name,
                "identity_sha256": source_identity_sha256,
                "size_bytes": source_size,
                "sha256_before": source_hashes_before["database"],
                "sha256_after": source_hashes_after["database"],
                "sidecar_sha256_before": {
                    key: source_hashes_before[key] for key in ("wal", "shm")
                },
                "sidecar_sha256_after": {
                    key: source_hashes_after[key] for key in ("wal", "shm")
                },
            },
            "snapshot": {
                "logical_name": snapshot_path.name,
                "size_bytes": snapshot_size,
                "sha256": snapshot_sha256,
            },
            "counts": table_counts,
            "object_manifest_sha256": object_manifest_sha256,
            "input_contract": asdict(scope_result),
            "created_at_utc": created_at_utc,
            "created_at_asia_shanghai": created_at_shanghai,
            "code_version": code_version,
            "environment": environment,
        }
        _write_json_atomic(manifest_path, manifest)

        with derived_connection:
            derived_connection.execute(
                """
                INSERT INTO source_snapshots(
                    snapshot_id, run_id, source_path, source_identity_sha256,
                    source_sha256_before, source_sha256_after, source_size_bytes,
                    snapshot_path, snapshot_sha256, snapshot_size_bytes,
                    post_count, image_count, table_counts_json, object_manifest_sha256,
                    input_contract_status, input_contract_method,
                    input_contract_reason_code, input_contract_details_json,
                    manifest_path, created_at_utc, created_at_asia_shanghai,
                    code_version, environment_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    str(source_path),
                    source_identity_sha256,
                    source_hashes_before["database"],
                    source_hashes_after["database"],
                    source_size,
                    str(snapshot_path),
                    snapshot_sha256,
                    snapshot_size,
                    post_count,
                    image_count,
                    json.dumps(table_counts, ensure_ascii=False, sort_keys=True),
                    object_manifest_sha256,
                    scope_result.status,
                    scope_result.method,
                    scope_result.reason_code,
                    json.dumps(scope_result.details, ensure_ascii=False, sort_keys=True),
                    str(manifest_path),
                    created_at_utc,
                    created_at_shanghai,
                    code_version,
                    environment_json,
                ),
            )
            derived_connection.execute(
                "UPDATE cleaning_runs SET source_snapshot_id = ? WHERE run_id = ?",
                (snapshot_id, run_id),
            )
        _mark_run(
            derived_connection,
            run_id,
            run_status,
            scope_result.reason_code,
            created_at_utc,
        )
        return SnapshotResult(
            run_id=run_id,
            run_status=run_status,
            snapshot_id=snapshot_id,
            input_contract_status=scope_result.status,
            input_contract_method=scope_result.method,
            input_contract_reason_code=scope_result.reason_code,
            post_count=post_count,
            image_count=image_count,
            source_sha256=source_hashes_before["database"],
            snapshot_sha256=snapshot_sha256,
            object_manifest_sha256=object_manifest_sha256,
            manifest_path=manifest_path,
        )
    except (RunAlreadyExistsError, SnapshotError):
        raise
    except Exception as exc:
        try:
            _mark_run(
                derived_connection,
                run_id,
                "failed",
                "snapshot_failed",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except sqlite3.Error:
            pass
        raise SnapshotError("snapshot_failed") from exc
    finally:
        derived_connection.close()
