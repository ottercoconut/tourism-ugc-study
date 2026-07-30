from __future__ import annotations

from pathlib import Path

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
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 10
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
