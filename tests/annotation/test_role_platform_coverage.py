"""作者身份字段平台覆盖审计的只读、聚合和口径测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tourism_ugc_study.annotation.role_platform_coverage import (
    audit_role_platform_coverage,
)


def _create_database(path: Path) -> None:
    """创建两个平台、三种材料与历史覆盖组合的最小快照。"""

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE web_posts (
                platform_key TEXT NOT NULL,
                author_platform_id TEXT,
                author_display_name TEXT,
                author_profile_url TEXT,
                author_description TEXT,
                author_verification_raw TEXT,
                author_followers_count INTEGER,
                published_at TEXT
            )
            """
        )
        rows = [
            ("xhs", "a1", "甲", "https://example.invalid/a1", None, None, 10, "2025-01-01"),
            ("xhs", "a1", "甲", None, None, None, 10, "2025-02-01"),
            ("xhs", "a1", "甲", None, None, None, 10, "2025-03-01"),
            ("xhs", "a2", "乙", None, "旅行记录", None, None, "2025-01-01"),
            ("weibo", "b1", "丙", None, None, None, 20, "2025-01-01"),
        ]
        connection.executemany(
            "INSERT INTO web_posts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )


def test_audit_reports_aggregate_platform_coverage_only(tmp_path: Path) -> None:
    """输出应复现最低材料门和3日期/30天门，但不泄漏作者标识。"""

    database = tmp_path / "snapshot.sqlite"
    _create_database(database)
    payload = audit_role_platform_coverage(database)
    by_platform = {row["platform"]: row for row in payload["platforms"]}

    assert payload["codebook_version"] == "v3.15.0"
    assert payload["database"]["opened_read_only"] is True
    assert payload["privacy"]["author_identifiers_exported"] is False
    assert by_platform["xhs"]["configured_for_v0"] is True
    assert by_platform["xhs"]["author_count"] == 2
    assert by_platform["xhs"]["coverage"]["minimum_material"] == {
        "author_count": 2,
        "author_rate": 1.0,
    }
    assert by_platform["xhs"]["coverage"]["none_code_eligible"] == {
        "author_count": 1,
        "author_rate": 0.5,
    }
    assert by_platform["weibo"]["configured_for_v0"] is False
    assert by_platform["weibo"]["coverage"]["minimum_material"]["author_count"] == 0
    rendered = str(payload)
    assert "a1" not in rendered
    assert "https://example.invalid/a1" not in rendered
