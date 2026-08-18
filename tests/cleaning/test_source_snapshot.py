from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.schema import connect_derived
from tourism_ugc_study.cleaning.snapshot import (
    RunAlreadyExistsError,
    open_source_readonly,
    _code_version,
    sha256_file,
    snapshot_source,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v3.1.yaml"


def build_source(
    path: Path,
    *,
    cities: list[str | None] | None = None,
    include_scope_migration: bool = True,
    post_count: int = 2,
) -> None:
    city_column = ", city_name TEXT" if cities is not None else ""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            f"""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE source_platforms (
                platform_key TEXT PRIMARY KEY
            );
            CREATE TABLE web_posts (
                id INTEGER PRIMARY KEY,
                platform_key TEXT NOT NULL REFERENCES source_platforms(platform_key),
                source_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title TEXT,
                author_platform_id TEXT,
                published_at TEXT,
                captured_at TEXT NOT NULL,
                content_text TEXT,
                status TEXT NOT NULL DEFAULT 'captured'
                {city_column}
            );
            """
        )
        connection.execute("INSERT INTO source_platforms(platform_key) VALUES ('xhs')")
        if include_scope_migration and cities is None:
            connection.execute(
                "INSERT INTO schema_migrations VALUES (13, 'remove_city_name', '2026-07-30')"
            )
        for index in range(1, post_count + 1):
            values: list[object] = [
                index,
                "xhs",
                "mediacrawler_search",
                f"https://example.invalid/post/{index}",
                f"敏感标题{index}",
                f"author-{index}",
                "2026-07-01T00:00:00+08:00",
                "2026-07-30T00:00:00+08:00",
                f"原始正文{index}",
                "captured",
            ]
            columns = (
                "id, platform_key, source_type, source_url, title, author_platform_id, "
                "published_at, captured_at, content_text, status"
            )
            if cities is not None:
                values.append(cities[index - 1])
                columns += ", city_name"
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO web_posts({columns}) VALUES ({placeholders})",
                values,
            )


def test_source_connection_is_os_read_only(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    build_source(source)

    with open_source_readonly(source) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden_write(id INTEGER)")


def test_current_source_schema_uses_upstream_scope_attestation(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    build_source(source, post_count=3)
    source_hash_before = sha256_file(source)

    result = snapshot_source(source, derived, load_config(CONFIG_PATH), "source-snapshot-current")

    assert result.run_status == "planned"
    assert result.input_contract_status == "accepted"
    assert result.input_contract_method == "upstream_scope_migration"
    assert result.post_count == 3
    assert sha256_file(source) == source_hash_before == result.source_sha256

    manifest_text = result.manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["counts"]["web_posts"] == 3
    assert manifest["source"]["sha256_before"] == manifest["source"]["sha256_after"]
    assert str(tmp_path) not in manifest_text
    assert "敏感标题" not in manifest_text
    assert "author-" not in manifest_text

    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'source-snapshot-current'"
        ).fetchone()[0] == "planned"
        row = connection.execute(
            "SELECT post_count, input_contract_status FROM source_snapshots"
        ).fetchone()
        assert row == (3, "accepted")


def test_run_and_snapshot_identity_are_frozen_but_status_updates_remain_legal(
    tmp_path: Path,
) -> None:
    """数据库必须冻结运行/快照谱系，同时允许状态机字段正常流转。"""

    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    build_source(source)
    config = load_config(CONFIG_PATH)
    first = snapshot_source(source, derived, config, "frozen-run-first")
    second = snapshot_source(source, derived, config, "frozen-run-second")

    with connect_derived(derived) as connection:
        connection.execute(
            """
            UPDATE cleaning_runs
            SET status = 'running', reason_code = NULL,
                started_at_utc = '2026-07-31T00:00:00+00:00',
                updated_at_utc = '2026-07-31T00:00:00+00:00'
            WHERE run_id = 'frozen-run-first'
            """
        )
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'frozen-run-first'"
        ).fetchone()[0] == "running"

        immutable_run_updates = (
            "protocol_version = 'other'",
            f"config_sha256 = '{'a' * 64}'",
            "random_seed = 1",
            "code_version = 'replacement'",
            "environment_json = '{}'",
            "created_at_utc = '2000-01-01T00:00:00+00:00'",
            "run_type = 'full'",
        )
        for assignment in immutable_run_updates:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE cleaning_runs SET {assignment} WHERE run_id = 'frozen-run-first'"
                )

        for assignment in (
            "source_path = 'replacement'",
            "snapshot_path = 'replacement'",
            f"snapshot_sha256 = '{'b' * 64}'",
            "table_counts_json = '{}'",
            "created_at_utc = '2000-01-01T00:00:00+00:00'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE source_snapshots SET {assignment} WHERE snapshot_id = ?",
                    (first.snapshot_id,),
                )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cleaning_runs SET source_snapshot_id = ? WHERE run_id = ?",
                (second.snapshot_id, "frozen-run-first"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cleaning_runs SET source_snapshot_id = NULL WHERE run_id = ?",
                ("frozen-run-first",),
            )


def test_legacy_mixed_city_input_is_rejected_without_city_labels(tmp_path: Path) -> None:
    source = tmp_path / "mixed.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    build_source(
        source,
        cities=["青岛市", "济南市"],
        include_scope_migration=False,
        post_count=2,
    )

    result = snapshot_source(source, derived, load_config(CONFIG_PATH), "source-snapshot-mixed")

    assert result.run_status == "input_rejected"
    assert result.input_contract_status == "rejected"
    assert result.input_contract_reason_code == "city_scope_mismatch"
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'source-snapshot-mixed'"
        ).fetchone()[0] == "input_rejected"
        # 这里防止把城市范围失败转写成逐条清洗标签或决定。
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE lower(name) LIKE '%city%'"
        ).fetchone()[0] == 0
        for table in ("text_post_annotations", "post_decisions"):
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0


def test_legacy_qingdao_only_input_is_accepted(tmp_path: Path) -> None:
    source = tmp_path / "qingdao.sqlite"
    build_source(
        source,
        cities=["青岛", "青岛市"],
        include_scope_migration=False,
        post_count=2,
    )

    result = snapshot_source(
        source,
        tmp_path / "processed" / "cleaning.sqlite",
        load_config(CONFIG_PATH),
        "source-snapshot-legacy-qingdao",
    )

    assert result.input_contract_status == "accepted"
    assert result.input_contract_method == "legacy_city_column"


def test_city_scope_without_column_or_migration_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "unattested.sqlite"
    build_source(source, include_scope_migration=False)

    result = snapshot_source(
        source,
        tmp_path / "processed" / "cleaning.sqlite",
        load_config(CONFIG_PATH),
        "source-snapshot-unattested",
    )

    assert result.input_contract_status == "rejected"
    assert result.input_contract_reason_code == "city_scope_unverifiable"


def test_foreign_key_damage_rejects_entire_input(tmp_path: Path) -> None:
    source = tmp_path / "broken-fk.sqlite"
    build_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE web_posts SET platform_key = 'missing' WHERE id = 1")

    result = snapshot_source(
        source,
        tmp_path / "processed" / "cleaning.sqlite",
        load_config(CONFIG_PATH),
        "source-snapshot-broken-fk",
    )

    assert result.run_status == "input_rejected"
    assert result.input_contract_reason_code == "source_foreign_key_failed"


def test_missing_required_table_rejects_entire_input(tmp_path: Path) -> None:
    source = tmp_path / "missing-table.sqlite"
    build_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TABLE web_posts")

    result = snapshot_source(
        source,
        tmp_path / "processed" / "cleaning.sqlite",
        load_config(CONFIG_PATH),
        "source-snapshot-missing-table",
    )

    assert result.run_status == "input_rejected"
    assert result.input_contract_reason_code == "source_schema_missing_table"


def test_snapshot_and_object_hashes_are_reproducible_and_counts_are_dynamic(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    build_source(source, post_count=2)
    config = load_config(CONFIG_PATH)

    first = snapshot_source(source, derived, config, "source-snapshot-repeat-a")
    second = snapshot_source(source, derived, config, "source-snapshot-repeat-b")

    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.object_manifest_sha256 == second.object_manifest_sha256

    with sqlite3.connect(source) as connection:
        connection.execute(
            """
            INSERT INTO web_posts(
                id, platform_key, source_type, source_url, title, author_platform_id,
                published_at, captured_at, content_text, status
            ) VALUES (3, 'xhs', 'mediacrawler_search', 'https://example.invalid/post/3',
                      '新增标题', 'author-3', '2026-07-01T00:00:00+08:00',
                      '2026-07-30T00:00:00+08:00', '新增正文', 'captured')
            """
        )

    third = snapshot_source(source, derived, config, "source-snapshot-repeat-c")
    assert third.post_count == 3
    assert third.snapshot_sha256 != first.snapshot_sha256
    assert third.object_manifest_sha256 != first.object_manifest_sha256


def test_run_id_cannot_overwrite_existing_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot_source(source, derived, config, "source-snapshot-no-overwrite")

    with pytest.raises(RunAlreadyExistsError):
        snapshot_source(source, derived, config, "source-snapshot-no-overwrite")


def test_code_version_distinguishes_a_dirty_worktree() -> None:
    revision = "a" * 40
    responses = [
        subprocess.CompletedProcess([], 0, stdout=f"{revision}\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=b" M tracked.py\n", stderr=b""),
        subprocess.CompletedProcess([], 0, stdout=b"diff --git a/tracked.py b/tracked.py\n", stderr=b""),
    ]

    with patch(
        "tourism_ugc_study.cleaning.snapshot.subprocess.run",
        side_effect=responses,
    ):
        value = _code_version()

    assert value.startswith(f"{revision}+dirty.")
    assert len(value) == 59


def test_cli_requires_explicit_paths_and_redacts_runtime_output(tmp_path: Path) -> None:
    source = tmp_path / "private-location" / "source.sqlite"
    source.parent.mkdir()
    derived = tmp_path / "processed" / "cleaning.sqlite"
    build_source(source)
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "cleaning_snapshot_source.py"),
        "--source-db",
        str(source),
        "--derived-db",
        str(derived),
        "--config",
        str(CONFIG_PATH),
        "--run-id",
        "source-snapshot-cli",
    ]

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["post_count"] == 2
    assert payload["manifest_name"] == "source-snapshot-cli.source-snapshot.json"
    assert str(tmp_path) not in completed.stdout
    assert "敏感标题" not in completed.stdout
    assert completed.stderr == ""


def test_cli_redacts_invalid_derived_path_errors(tmp_path: Path) -> None:
    source = tmp_path / "private-source.sqlite"
    build_source(source)
    blocking_file = tmp_path / "private-parent"
    blocking_file.write_text("not a directory", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "cleaning_snapshot_source.py"),
            "--source-db",
            str(source),
            "--derived-db",
            str(blocking_file / "cleaning.sqlite"),
            "--config",
            str(CONFIG_PATH),
            "--run-id",
            "source-snapshot-invalid-derived",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert json.loads(completed.stderr) == {
        "reason_code": "derived_database_unavailable",
        "status": "failed",
    }
    assert str(tmp_path) not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_returns_two_for_rejected_input_without_leaking_city_values(tmp_path: Path) -> None:
    source = tmp_path / "private-location" / "mixed.sqlite"
    source.parent.mkdir()
    derived = tmp_path / "processed" / "cleaning.sqlite"
    build_source(
        source,
        cities=["青岛市", "济南市"],
        include_scope_migration=False,
        post_count=2,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "cleaning_snapshot_source.py"),
            "--source-db",
            str(source),
            "--derived-db",
            str(derived),
            "--config",
            str(CONFIG_PATH),
            "--run-id",
            "source-snapshot-cli-rejected",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["run_status"] == "input_rejected"
    assert payload["input_contract_reason_code"] == "city_scope_mismatch"
    assert str(tmp_path) not in completed.stdout
    assert "济南" not in completed.stdout
    assert completed.stderr == ""
