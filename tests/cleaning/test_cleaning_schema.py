from __future__ import annotations

from pathlib import Path

import tourism_ugc_study.cleaning.schema as schema_module
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived


def test_schema_migration_is_idempotent_and_preserves_rows(tmp_path: Path) -> None:
    database = tmp_path / "cleaning.sqlite"
    with connect_derived(database) as connection:
        migrate_derived(connection)
        connection.execute(
            """
            INSERT INTO cleaning_runs(
                run_id, protocol_version, config_sha256, random_seed, status,
                code_version, environment_json, created_at_utc, updated_at_utc
            ) VALUES ('existing', '2.4', ?, 20260728, 'planned', 'test', '{}', ?, ?)
            """,
            ("a" * 64, "2026-07-30T00:00:00+00:00", "2026-07-30T00:00:00+00:00"),
        )
        connection.commit()

        migrate_derived(connection)

        assert connection.execute("SELECT COUNT(*) FROM cleaning_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 12
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert {
            "source_post_inventory",
            "source_post_versions",
            "inventory_discoveries",
            "source_image_inventory",
            "source_image_versions",
            "cleaning_batches",
            "stage_tasks",
            "stage_events",
            "text_deterministic_results",
            "text_candidate_builds",
            "text_candidate_corpus_members",
            "text_exact_clusters",
            "text_near_candidate_pairs",
            "text_sampling_runs",
            "text_post_annotations",
            "text_post_adjudications",
            "text_near_duplicate_annotations",
            "text_near_duplicate_adjudications",
            "text_leakage_builds",
            "text_model_runs",
            "text_model_predictions",
            "text_double_label_supplements",
            "text_agreement_evaluations",
            "text_periodic_review_windows",
            "text_periodic_review_window_members",
            "image_manifest_imports",
            "image_manifest_rows",
            "image_role_results",
            "image_processing_attempts",
            "image_fingerprints",
            "image_candidate_builds",
            "image_candidate_build_members",
            "image_candidate_signals",
            "image_exact_clusters",
            "image_exact_cluster_members",
            "image_near_candidate_pairs",
        }.issubset(tables)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'view' AND name = 'text_ready'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT source_snapshot_id FROM cleaning_runs WHERE run_id = 'existing'"
        ).fetchone()[0] is None


def test_text_human_and_model_records_are_append_only(tmp_path: Path) -> None:
    """schema 必须从数据库层阻止人工证据和模型输出被覆盖。"""

    database = tmp_path / "cleaning.sqlite"
    with connect_derived(database) as connection:
        migrate_derived(connection)
        trigger_tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT tbl_name FROM sqlite_schema
                WHERE type = 'trigger' AND name LIKE 'prevent_text_%_update'
                """
            )
        }
    assert {
        "text_post_annotations",
        "text_post_adjudications",
        "text_near_duplicate_annotations",
        "text_near_duplicate_adjudications",
        "text_model_predictions",
    }.issubset(trigger_tables)


def test_connection_enables_wal_busy_timeout_and_foreign_keys(tmp_path: Path) -> None:
    with connect_derived(tmp_path / "settings.sqlite") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_image_evidence_tables_are_append_only_and_builds_are_sealed(tmp_path: Path) -> None:
    """图片证据只能追加，候选构建只能从 building 单向封存。"""

    with connect_derived(tmp_path / "image-schema.sqlite") as connection:
        migrate_derived(connection)
        immutable_tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT tbl_name FROM sqlite_schema
                WHERE type = 'trigger' AND name LIKE 'prevent_image_%_update'
                """
            )
        }
        assert {
            "image_manifest_imports",
            "image_manifest_rows",
            "image_role_results",
            "image_processing_attempts",
            "image_fingerprints",
            "image_candidate_builds",
            "image_candidate_build_members",
            "image_candidate_signals",
            "image_exact_clusters",
            "image_exact_cluster_members",
            "image_near_candidate_pairs",
        }.issubset(immutable_tables)

        image_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(image_manifest_rows)")
        }
        assert {
            "relation_role",
            "relative_path",
            "expected_file_sha256",
            "parent_file_sha256",
            "transform_json",
            "row_identity_sha256",
        }.issubset(image_columns)


def test_existing_v10_database_upgrades_without_losing_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """模拟已有 v10 文件，验证升级会新增图片表且保留既有运行。"""

    database = tmp_path / "upgrade-v10.sqlite"
    original_v11 = schema_module._SCHEMA_V11
    original_v12 = schema_module._SCHEMA_V12
    with connect_derived(database) as connection:
        # 首次迁移暂时跳过 v11/v12 DDL，再移除迁移标记，得到完整 v10 夹具。
        monkeypatch.setattr(schema_module, "_SCHEMA_V11", "")
        monkeypatch.setattr(schema_module, "_SCHEMA_V12", "")
        migrate_derived(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version = 11")
        connection.execute("DELETE FROM schema_migrations WHERE version = 12")
        connection.execute(
            """
            INSERT INTO cleaning_runs(
                run_id, protocol_version, config_sha256, random_seed, status,
                code_version, environment_json, created_at_utc, updated_at_utc
            ) VALUES ('v10-run', '2.4', ?, 20260728, 'planned', 'test', '{}', ?, ?)
            """,
            ("b" * 64, "2026-07-31T00:00:00+00:00", "2026-07-31T00:00:00+00:00"),
        )
        connection.commit()

        monkeypatch.setattr(schema_module, "_SCHEMA_V11", original_v11)
        monkeypatch.setattr(schema_module, "_SCHEMA_V12", original_v12)
        migrate_derived(connection)

        assert connection.execute(
            "SELECT COUNT(*) FROM cleaning_runs WHERE run_id = 'v10-run'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 12"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table' AND name = 'image_fingerprints'"
        ).fetchone()[0] == 1
