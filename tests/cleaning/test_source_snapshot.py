"""筛选快照的身份保留、只读、关系完整性和失败关闭测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.source_snapshot import (
    create_archival_snapshot, create_current_snapshot, file_sha256, verify_topic_subset,
)


def _source(path: Path, *, invalid_flag: bool = False) -> None:
    """创建只含合成数据的采集契约夹具，不读取真实 UGC。"""
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
            CREATE TABLE source_platforms(platform_key TEXT PRIMARY KEY);
            CREATE TABLE ctf_captures(id INTEGER PRIMARY KEY, site_key TEXT REFERENCES source_platforms(platform_key));
            CREATE TABLE web_posts(id INTEGER PRIMARY KEY, platform_key TEXT REFERENCES source_platforms(platform_key), source_capture_id INTEGER REFERENCES ctf_captures(id), topic_relevant INTEGER, content_text TEXT, post_images_count INTEGER);
            CREATE TABLE web_post_images(id INTEGER PRIMARY KEY, web_post_id INTEGER REFERENCES web_posts(id), image_url TEXT);
            CREATE UNIQUE INDEX images_id ON web_post_images(id);
            CREATE TABLE post_detail_repair_waivers(id INTEGER PRIMARY KEY, web_post_id INTEGER REFERENCES web_posts(id));
            CREATE TABLE crawl_jobs(secret TEXT);
            INSERT INTO schema_migrations VALUES(20);
            INSERT INTO source_platforms VALUES('xhs');
            INSERT INTO ctf_captures VALUES(9,'xhs');
            INSERT INTO ctf_captures VALUES(10,'xhs');
            INSERT INTO web_posts VALUES(101,'xhs',9,1,'保留原文。',1);
            INSERT INTO web_posts VALUES(205,'xhs',10,0,'不复制原文。',1);
            INSERT INTO web_post_images VALUES(7,101,'keep-image');
            INSERT INTO web_post_images VALUES(8,205,'excluded-image');
            INSERT INTO post_detail_repair_waivers VALUES(1,101);
            INSERT INTO post_detail_repair_waivers VALUES(2,205);
        """)
        if invalid_flag:
            conn.execute("UPDATE web_posts SET topic_relevant=2 WHERE id=205")


def test_snapshot_filters_preserves_and_activates(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    target = tmp_path / "output.sqlite"
    pointer = tmp_path / "current.json"
    _source(source)
    before = file_sha256(source)
    result = create_current_snapshot(source, target, pointer, code_version="test")
    assert result["selected_posts"] == 1
    assert file_sha256(source) == before
    assert result == json.loads(pointer.read_text())
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT id,content_text FROM web_posts").fetchall() == [(101, "保留原文。")]
        assert conn.execute("SELECT id FROM web_post_images").fetchall() == [(7,)]
        assert conn.execute("SELECT id FROM ctf_captures").fetchall() == [(9,)]
        assert conn.execute("SELECT id FROM post_detail_repair_waivers").fetchall() == [(1,)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='crawl_jobs'").fetchone()
    assert not any(Path(str(target) + s).exists() for s in ("-wal", "-shm", "-journal"))
    with pytest.raises(FileExistsError):
        create_current_snapshot(source, target, pointer, code_version="test")


def test_invalid_flag_leaves_previous_pointer_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _source(source, invalid_flag=True)
    pointer = tmp_path / "current.json"
    pointer.write_text('{"previous":true}')
    with pytest.raises(ValueError, match="source_topic_relevant_invalid"):
        create_current_snapshot(source, tmp_path / "out.sqlite", pointer, code_version="test")
    assert pointer.read_text() == '{"previous":true}'
    assert not (tmp_path / "out.sqlite").exists()


def test_snapshot_rejects_overlapping_paths(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _source(source)
    with pytest.raises(ValueError, match="snapshot_paths_overlap"):
        create_current_snapshot(source, source, tmp_path / "current.json", code_version="test")


def test_snapshot_includes_committed_wal(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _source(source)
    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE web_posts SET content_text='WAL已提交正文' WHERE id=101")
        writer.commit()
        create_current_snapshot(source, tmp_path / "out.sqlite", tmp_path / "current.json", code_version="test")
        with sqlite3.connect(tmp_path / "out.sqlite") as result:
            assert result.execute("SELECT content_text FROM web_posts").fetchone()[0] == "WAL已提交正文"


def test_snapshot_refuses_dangling_images_count(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _source(source)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE web_posts SET post_images_count=5 WHERE id=101")
    with pytest.raises(ValueError, match="snapshot_post_images_count_mismatch"):
        create_current_snapshot(source, tmp_path / "out.sqlite", tmp_path / "current.json", code_version="test")


def test_archive_preserves_all_topic_flags_and_matches_research(tmp_path: Path) -> None:
    source, archive, research = (tmp_path / name for name in ("source.sqlite", "archive.sqlite", "research.sqlite"))
    _source(source)
    before = file_sha256(source)
    create_current_snapshot(source, research, tmp_path / "pointer.json", code_version="test")
    result = create_archival_snapshot(source, archive, code_version="test")
    assert result["selected_posts"] == 2
    assert result["topic_counts"] == {0: 1, 1: 1}
    assert file_sha256(source) == before
    assert archive.stat().st_nlink == 1
    assert verify_topic_subset(archive, research)["web_posts"]["count"] == 1
    with sqlite3.connect(archive) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='crawl_jobs'").fetchone()
        connection.execute("UPDATE web_posts SET content_text='changed' WHERE id=101")
    with pytest.raises(ValueError, match="archive_research_content_mismatch"):
        verify_topic_subset(archive, research)
    with pytest.raises(FileExistsError):
        create_archival_snapshot(source, archive, code_version="test")


def test_archive_reads_committed_wal_without_touching_live_database(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _source(source)
    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE web_posts SET content_text='已提交变更' WHERE id=205")
        writer.commit()
        create_archival_snapshot(source, tmp_path / "archive.sqlite", code_version="test")
        with sqlite3.connect(tmp_path / "archive.sqlite") as archived:
            assert archived.execute("SELECT content_text FROM web_posts WHERE id=205").fetchone()[0] == "已提交变更"
