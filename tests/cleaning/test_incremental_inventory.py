"""纯文本帖子增量库存与任务发现测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.snapshot import snapshot_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v3.1.yaml"


def _build_source(path: Path) -> None:
    """构造覆盖文本、作者和分析字段变化的最小源库。"""

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
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
                status TEXT NOT NULL DEFAULT 'captured'
            );
            INSERT INTO web_posts(
                id, platform_key, platform_post_id, source_type, source_url,
                title, author_platform_id, captured_at, content_text, post_likes_count
            ) VALUES
                (1, 'xhs', 'p1', 'search', 'https://invalid/1', '标题1', 'a1',
                 '2026-07-01', '正文1', 10),
                (2, 'xhs', 'p2', 'search', 'https://invalid/2', '标题2', 'a2',
                 '2026-07-02', '正文2', 20),
                (3, 'xhs', 'p3', 'search', 'https://invalid/3', '标题3', 'a3',
                 '2026-07-03', '正文3', 30),
                (4, 'xhs', 'p4', 'search', 'https://invalid/4', '标题4', 'a4',
                 '2026-07-04', '正文4', 40);
            """
        )


def _snapshot_and_discover(source: Path, derived: Path, run_id: str):
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, run_id)
    return snapshot.snapshot_id, discover_increment(derived, snapshot.snapshot_id, config)


def test_two_snapshots_classify_post_changes_without_duplicate_tasks(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    _, first = _snapshot_and_discover(source, derived, "inventory-first")
    assert first.post_changes == {"new": 4}
    assert first.tasks_created == 12

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_posts SET content_text = '修订正文' WHERE id = 1")
        connection.execute("UPDATE web_posts SET post_likes_count = 999 WHERE id = 2")
        connection.execute("DELETE FROM web_posts WHERE id = 3")
        connection.execute(
            "INSERT INTO web_posts VALUES "
            "(5,'xhs','p5','search','https://invalid/5',NULL,'标题5','a5',NULL,"
            "'2026-07-05',NULL,'正文5',50,'captured')"
        )
    snapshot_id, second = _snapshot_and_discover(source, derived, "inventory-second")
    assert second.post_changes == {
        "new": 1,
        "cleaning_changed": 1,
        "analysis_only": 1,
        "unchanged": 1,
        "missing": 1,
    }
    assert second.tasks_created == 6
    assert discover_increment(derived, snapshot_id, load_config(CONFIG_PATH)) == second


def test_empty_snapshot_discovery_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "empty.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM web_posts")
    snapshot_id, first = _snapshot_and_discover(source, derived, "inventory-empty")
    second = discover_increment(derived, snapshot_id, load_config(CONFIG_PATH))
    assert first == second
    assert first.post_changes == {}
    assert first.tasks_created == 0


def test_engagement_only_change_creates_no_cleaning_task(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    _snapshot_and_discover(source, derived, "engagement-first")
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_posts SET post_likes_count = 999 WHERE id = 1")
    _, result = _snapshot_and_discover(source, derived, "engagement-second")
    assert result.post_changes == {"analysis_only": 1, "unchanged": 3}
    assert result.tasks_created == 0


def test_old_success_cannot_cover_unfinished_changed_text_version(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    _snapshot_and_discover(source, derived, "text-version-first")
    with sqlite3.connect(derived) as connection:
        connection.execute(
            "UPDATE stage_tasks SET status = 'succeeded', output_sha256 = ?",
            ("a" * 64,),
        )
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_posts SET content_text = '新版本正文' WHERE id = 1")
    _snapshot_and_discover(source, derived, "text-version-second")
    _, unchanged = _snapshot_and_discover(source, derived, "text-version-third")
    assert unchanged.tasks_created == 0
