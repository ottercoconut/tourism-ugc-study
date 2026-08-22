from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.annotation.leakage_groups import (
    ConfirmedNearRelation,
    LeakageGroupError,
    LeakagePost,
    build_leakage_plan,
    create_leakage_build,
)


def test_missing_authors_stay_independent_while_confirmed_edges_merge() -> None:
    posts = (
        LeakagePost(1, 1, "missing", False, "exact-1"),
        LeakagePost(2, 1, "missing", False, "exact-2"),
        LeakagePost(3, 1, "author-a", True, "exact-3"),
        LeakagePost(4, 1, "author-a", True, "exact-4"),
        LeakagePost(5, 1, "author-b", True, "exact-5"),
        LeakagePost(6, 1, "author-c", True, "exact-5"),
        LeakagePost(7, 1, "author-d", True, "exact-7"),
    )
    plan = build_leakage_plan(
        posts,
        (ConfirmedNearRelation("human-gold", "exact-5", "exact-7"),),
    )
    by_post = {member.source_post_id: member for member in plan.members}

    assert by_post[1].component_id != by_post[2].component_id
    assert by_post[3].component_id == by_post[4].component_id
    assert by_post[5].component_id == by_post[6].component_id == by_post[7].component_id
    assert by_post[3].author_edge_used is True
    assert by_post[5].exact_edge_used is True
    assert by_post[7].confirmed_near_edge_used is True


def test_candidate_components_are_not_an_input_to_leakage_plan() -> None:
    """没有人工确认关系时，两个不同精确簇保持分离。"""

    posts = (
        LeakagePost(1, 1, "a", True, "exact-a"),
        LeakagePost(2, 1, "b", True, "exact-b"),
    )

    plan = build_leakage_plan(posts, ())

    assert len({member.component_id for member in plan.members}) == 2


def _build_lineage_database(path: Path, *, schema_version: int = 34) -> None:
    """建立当前泄漏分组入口所需的最小既有谱系库。

    Args:
        path: 待创建的测试 SQLite 路径。
        schema_version: 写入迁移记录的 schema 版本。
    """

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER, name TEXT, applied_at_utc TEXT);
        CREATE TABLE source_post_versions(
            source_post_id INTEGER, source_version INTEGER,
            author_sha256 TEXT, author_identity_present INTEGER
        );
        CREATE TABLE text_candidate_builds(
            build_id TEXT PRIMARY KEY, status TEXT, is_complete_corpus INTEGER
        );
        CREATE TABLE text_candidate_corpus_members(
            build_id TEXT, source_post_id INTEGER, source_version INTEGER,
            exact_cluster_id TEXT, structure_status TEXT
        );
        CREATE TABLE text_near_duplicate_adjudications(
            adjudication_id TEXT PRIMARY KEY, build_id TEXT,
            left_cluster_id TEXT, right_cluster_id TEXT, decision TEXT
        );
        CREATE TABLE text_leakage_builds(
            leakage_build_id TEXT PRIMARY KEY, candidate_build_id TEXT,
            adjudication_manifest_sha256 TEXT, input_post_count INTEGER,
            component_count INTEGER, output_sha256 TEXT,
            created_at_utc TEXT, seal_status TEXT
        );
        CREATE TABLE text_leakage_members(
            leakage_build_id TEXT, component_id TEXT,
            source_post_id INTEGER, source_version INTEGER,
            author_edge_used INTEGER, exact_edge_used INTEGER,
            confirmed_near_edge_used INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, 'lineage', '2026-08-22')",
        (schema_version,),
    )
    connection.execute(
        "INSERT INTO text_candidate_builds VALUES ('candidate-1', 'finalized', 1)"
    )
    connection.executemany(
        "INSERT INTO source_post_versions VALUES (?, 1, ?, 1)",
        [(1, "author-a"), (2, "author-b")],
    )
    connection.executemany(
        "INSERT INTO text_candidate_corpus_members VALUES ('candidate-1', ?, 1, ?, 'usable')",
        [(1, "exact-a"), (2, "exact-b")],
    )
    connection.commit()
    connection.close()


def test_create_leakage_build_only_appends_to_existing_schema_34(tmp_path: Path) -> None:
    """当前入口复用既有谱系，但不会隐式建库或执行旧迁移。"""

    database = tmp_path / "lineage.sqlite"
    _build_lineage_database(database)

    result = create_leakage_build(
        database,
        candidate_build_id="candidate-1",
        duplicate_adjudication_ids=(),
    )

    assert result.input_post_count == 2
    assert result.component_count == 2
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 34
    connection.close()


def test_create_leakage_build_rejects_missing_or_incompatible_database(
    tmp_path: Path,
) -> None:
    """缺失文件或非 schema 34 文件必须失败关闭。"""

    missing = tmp_path / "missing.sqlite"
    with pytest.raises(LeakageGroupError) as missing_error:
        create_leakage_build(
            missing,
            candidate_build_id="candidate-1",
            duplicate_adjudication_ids=(),
        )
    assert missing_error.value.reason_code == "lineage_database_not_found"
    assert not missing.exists()

    incompatible = tmp_path / "incompatible.sqlite"
    _build_lineage_database(incompatible, schema_version=33)
    with pytest.raises(LeakageGroupError) as version_error:
        create_leakage_build(
            incompatible,
            candidate_build_id="candidate-1",
            duplicate_adjudication_ids=(),
        )
    assert version_error.value.reason_code == "lineage_database_schema_mismatch"
