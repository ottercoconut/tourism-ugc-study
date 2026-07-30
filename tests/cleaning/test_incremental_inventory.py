from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.inventory import InventoryError, discover_increment
from tourism_ugc_study.cleaning.snapshot import snapshot_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _build_source(path: Path) -> None:
    """构造覆盖文本、互动量和图片分轴变化的最小源库。"""

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (13, 'remove_city_name', '2026-07-30');
            CREATE TABLE source_platforms (platform_key TEXT PRIMARY KEY);
            INSERT INTO source_platforms VALUES ('xhs');
            CREATE TABLE web_posts (
                id INTEGER PRIMARY KEY,
                platform_key TEXT NOT NULL REFERENCES source_platforms(platform_key),
                platform_post_id TEXT,
                source_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                canonical_url TEXT,
                title TEXT,
                author_platform_id TEXT,
                published_at TEXT,
                captured_at TEXT NOT NULL,
                keyword TEXT,
                content_text TEXT,
                post_likes_count INTEGER,
                post_favorites_count INTEGER,
                post_comments_count INTEGER,
                post_shares_count INTEGER,
                post_reposts_count INTEGER,
                post_views_count INTEGER,
                post_images_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'captured'
            );
            CREATE TABLE web_post_images (
                id INTEGER PRIMARY KEY,
                web_post_id INTEGER NOT NULL REFERENCES web_posts(id),
                image_index INTEGER NOT NULL,
                image_url TEXT NOT NULL,
                image_role TEXT NOT NULL,
                local_path TEXT,
                width INTEGER,
                height INTEGER,
                mime_type TEXT,
                sha256 TEXT
            );
            INSERT INTO web_posts(
                id, platform_key, platform_post_id, source_type, source_url,
                title, author_platform_id, captured_at, content_text,
                post_likes_count, post_images_count
            ) VALUES
                (1, 'xhs', 'p1', 'search', 'https://invalid/1', '标题1', 'a1',
                 '2026-07-01', '正文1', 10, 1),
                (2, 'xhs', 'p2', 'search', 'https://invalid/2', '标题2', 'a2',
                 '2026-07-02', '正文2', 20, 1),
                (3, 'xhs', 'p3', 'search', 'https://invalid/3', '标题3', 'a3',
                 '2026-07-03', '正文3', 30, 1),
                (4, 'xhs', 'p4', 'search', 'https://invalid/4', '标题4', 'a4',
                 '2026-07-04', '正文4', 40, 1);
            INSERT INTO web_post_images(
                id, web_post_id, image_index, image_url, image_role, local_path,
                width, height, mime_type, sha256
            ) VALUES
                (1, 1, 0, 'https://invalid/i1', 'content', NULL, NULL, NULL, NULL, NULL),
                (2, 2, 0, 'https://invalid/i2', 'content', NULL, NULL, NULL, NULL, NULL),
                (3, 3, 0, 'https://invalid/i3', 'content', NULL, NULL, NULL, NULL, NULL),
                (4, 4, 0, 'https://invalid/i4', 'content', NULL, NULL, NULL, NULL, NULL);
            """
        )


def _snapshot_and_discover(
    source: Path,
    derived: Path,
    run_id: str,
) -> tuple[str, object]:
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, run_id)
    return snapshot.snapshot_id, discover_increment(derived, snapshot.snapshot_id, config)


def test_two_snapshots_classify_changes_without_duplicate_tasks(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    first_snapshot_id, first = _snapshot_and_discover(source, derived, "inventory-first")

    assert first.post_changes == {"new": 4}
    assert first.image_changes == {"new": 4}
    assert first.tasks_created == 28

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_posts SET content_text = '修订正文' WHERE id = 1")
        connection.execute("UPDATE web_posts SET post_likes_count = 999 WHERE id = 2")
        connection.execute("DELETE FROM web_post_images WHERE id = 3")
        connection.execute("DELETE FROM web_posts WHERE id = 3")
        connection.execute(
            """
            INSERT INTO web_posts(
                id, platform_key, platform_post_id, source_type, source_url,
                title, author_platform_id, captured_at, content_text,
                post_likes_count, post_images_count
            ) VALUES (5, 'xhs', 'p5', 'search', 'https://invalid/5', '标题5',
                      'a5', '2026-07-05', '正文5', 50, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO web_post_images(
                id, web_post_id, image_index, image_url, image_role
            ) VALUES (5, 5, 0, 'https://invalid/i5', 'content')
            """
        )

    second_snapshot_id, second = _snapshot_and_discover(source, derived, "inventory-second")
    assert second.post_changes == {
        "new": 1,
        "cleaning_changed": 1,
        "analysis_only": 1,
        "unchanged": 1,
        "missing": 1,
    }
    assert second.image_changes == {"new": 1, "unchanged": 3, "missing": 1}
    assert second.tasks_created == 10

    config = load_config(CONFIG_PATH)
    repeated = discover_increment(derived, second_snapshot_id, config)
    assert repeated.tasks_created == second.tasks_created

    third_snapshot_id, third = _snapshot_and_discover(source, derived, "inventory-third")
    assert third.post_changes == {"unchanged": 4, "missing": 1}
    assert third.image_changes == {"unchanged": 4, "missing": 1}
    assert third.tasks_created == 0

    with sqlite3.connect(derived) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT COUNT(*) FROM stage_tasks").fetchone()[0] == 38
        assert connection.execute(
            "SELECT COUNT(*) FROM stage_tasks WHERE run_id = 'inventory-second' AND source_object_id = 2"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT is_present FROM source_post_inventory WHERE source_post_id = 3"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM source_post_inventory WHERE source_post_id = 3"
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM source_post_inventory
            WHERE current_author_identity_present = 1
            """
        ).fetchone()[0] == 5
        stored = "\n".join(
            str(value)
            for row in connection.execute(
                "SELECT current_text_sha256, current_author_sha256 FROM source_post_inventory"
            )
            for value in row
        )
        assert "修订正文" not in stored
        assert "a1" not in stored
        assert first_snapshot_id != second_snapshot_id
        assert second_snapshot_id != third_snapshot_id


def test_image_file_change_only_enqueues_file_dependent_tasks(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    _snapshot_and_discover(source, derived, "image-file-first")
    with sqlite3.connect(source) as connection:
        connection.execute(
            """
            UPDATE web_post_images
            SET local_path = 'images/1.jpg', width = 1080, height = 1440,
                mime_type = 'image/jpeg', sha256 = ?
            WHERE id = 1
            """,
            ("b" * 64,),
        )
    _, result = _snapshot_and_discover(source, derived, "image-file-second")

    assert result.image_changes == {"cleaning_changed": 1, "unchanged": 3}
    with sqlite3.connect(derived) as connection:
        stages = {
            row[0]
            for row in connection.execute(
                """
                SELECT stage_name FROM stage_tasks
                WHERE run_id = 'image-file-second' AND object_type = 'image'
                """
            )
        }
        assert stages == {"image_fingerprint", "image_noise", "finalize"}


def test_empty_snapshot_discovery_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "empty.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM web_post_images")
        connection.execute("DELETE FROM web_posts")
    snapshot_id, first = _snapshot_and_discover(source, derived, "inventory-empty")
    second = discover_increment(derived, snapshot_id, load_config(CONFIG_PATH))

    assert first.post_changes == second.post_changes == {}
    assert first.image_changes == second.image_changes == {}
    assert first.tasks_created == second.tasks_created == 0
    with sqlite3.connect(derived) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inventory_discoveries").fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'inventory-empty'"
        ).fetchone()[0] == "accepted"


def test_engagement_only_run_waits_for_reusable_prior_results(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    _snapshot_and_discover(source, derived, "engagement-first")
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_posts SET post_likes_count = 999 WHERE id = 1")
    _, result = _snapshot_and_discover(source, derived, "engagement-second")

    assert result.post_changes == {"analysis_only": 1, "unchanged": 3}
    assert result.image_changes == {"unchanged": 4}
    assert result.tasks_created == 0
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'engagement-second'"
        ).fetchone()[0] == "paused"
        connection.execute(
            """
            UPDATE stage_tasks
            SET status = 'succeeded', output_sha256 = ?
            WHERE run_id = 'engagement-first'
            """,
            ("a" * 64,),
        )
        connection.commit()

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_posts SET post_likes_count = 1000 WHERE id = 1")
    _, completed = _snapshot_and_discover(source, derived, "engagement-third")
    assert completed.tasks_created == 0
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'engagement-third'"
        ).fetchone()[0] == "accepted"


def test_old_success_cannot_cover_unfinished_changed_text_version(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    _snapshot_and_discover(source, derived, "text-version-first")
    with sqlite3.connect(derived) as connection:
        connection.execute(
            """
            UPDATE stage_tasks SET status = 'succeeded', output_sha256 = ?
            WHERE run_id = 'text-version-first'
            """,
            ("a" * 64,),
        )
        connection.commit()

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_posts SET content_text = '新版本正文' WHERE id = 1")
    _snapshot_and_discover(source, derived, "text-version-second")
    _, unchanged = _snapshot_and_discover(source, derived, "text-version-third")

    assert unchanged.tasks_created == 0
    with sqlite3.connect(derived) as connection:
        row = connection.execute(
            """
            SELECT status, reason_code FROM cleaning_runs
            WHERE run_id = 'text-version-third'
            """
        ).fetchone()
        assert row == ("paused", "prior_tasks_incomplete")


def test_discovery_rejects_tampered_registered_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, "inventory-tampered")
    with sqlite3.connect(snapshot.manifest_path.parent.parent / "source-snapshots" / "inventory-tampered.sqlite") as connection:
        connection.execute("UPDATE web_posts SET title = '被篡改' WHERE id = 1")

    try:
        discover_increment(derived, snapshot.snapshot_id, config)
    except InventoryError as exc:
        assert exc.reason_code == "snapshot_hash_mismatch"
    else:
        raise AssertionError("篡改后的快照不应被扫描")


def test_upstream_algorithm_version_change_enqueues_downstream_tasks(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    _snapshot_and_discover(source, derived, "algorithm-first")

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["algorithm_versions"]["text_deterministic"] = "text-deterministic-v2"
    changed_config_path = tmp_path / "changed-config.yaml"
    changed_config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    changed_config = load_config(changed_config_path)
    snapshot = snapshot_source(source, derived, changed_config, "algorithm-second")
    result = discover_increment(derived, snapshot.snapshot_id, changed_config)

    assert result.tasks_created == 12
    with sqlite3.connect(derived) as connection:
        stages = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT stage_name FROM stage_tasks WHERE run_id = 'algorithm-second'"
            )
        }
        assert stages == {"text_deterministic", "text_relevance", "finalize"}
