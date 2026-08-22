"""3.0 文本清洗派生 schema 测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.schema import (
    DERIVED_SCHEMA_VERSION,
    connect_derived,
    migrate_derived,
)


def test_schema_is_idempotent_and_contains_expected_release_objects(tmp_path: Path) -> None:
    database = tmp_path / "cleaning.sqlite"
    with connect_derived(database) as connection:
        migrate_derived(connection)
        connection.execute(
            """
            INSERT INTO cleaning_runs(
                run_id, protocol_version, config_sha256, random_seed, status,
                code_version, environment_json, created_at_utc, updated_at_utc
            ) VALUES ('existing', '3.0', ?, 20260728, 'planned', 'test', '{}', ?, ?)
            """,
            ("a" * 64, "2026-08-14T00:00:00+00:00", "2026-08-14T00:00:00+00:00"),
        )
        connection.commit()
        migrate_derived(connection)

        assert connection.execute("SELECT COUNT(*) FROM cleaning_runs").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM schema_migrations").fetchone()[0] == DERIVED_SCHEMA_VERSION
        release_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name LIKE 'analysis_%'"
            )
        }
        assert release_tables == {
            "analysis_posts_deduplicated",
            "analysis_posts_eligible",
            "analysis_release_acceptance_attestations",
            "analysis_release_builds",
            "analysis_release_manifests",
            "analysis_release_reports",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_text_human_and_model_records_are_append_only(tmp_path: Path) -> None:
    with connect_derived(tmp_path / "cleaning.sqlite") as connection:
        migrate_derived(connection)
        trigger_tables = {
            row[0]
            for row in connection.execute(
                "SELECT tbl_name FROM sqlite_schema "
                "WHERE type = 'trigger' AND name LIKE 'prevent_text_%_update'"
            )
        }
    assert {
        "text_post_annotations",
        "text_post_adjudications",
        "text_near_duplicate_annotations",
        "text_near_duplicate_adjudications",
        "text_model_predictions",
    }.issubset(trigger_tables)


def test_raw_cleaning_annotations_do_not_store_row_coder_metadata(tmp_path: Path) -> None:
    """单人清洗原始记录不保存无分析用途的逐行编码者或人工时间。"""

    with connect_derived(tmp_path / "cleaning.sqlite") as connection:
        migrate_derived(connection)
        for table in (
            "text_post_annotations",
            "text_near_duplicate_annotations",
            "text_keep_audit_annotations",
        ):
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert "annotator_hash" not in columns
            assert "annotated_at_utc" not in columns
            assert "created_at_utc" in columns


def test_connection_enables_wal_busy_timeout_and_foreign_keys(tmp_path: Path) -> None:
    with connect_derived(tmp_path / "settings.sqlite") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_legacy_derived_database_requires_explicit_rebuild(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at_utc TEXT)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES (25, 'legacy', 'now')")
    connection.commit()
    connection.close()

    with connect_derived(database) as derived:
        with pytest.raises(sqlite3.IntegrityError, match="requires_rebuild"):
            migrate_derived(derived)
