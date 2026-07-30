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
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
