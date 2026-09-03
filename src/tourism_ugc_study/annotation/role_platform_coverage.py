"""只读审计各平台作者身份材料和纵向历史覆盖。

本模块只汇总平台级计数与比例，不导出作者标识，也不根据覆盖结果自动决定
角色或证据状态。研究者仍须依据编码表冻结角色测量范围；审计结果只提供该
决定所需的可复现数据库事实。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

from tourism_ugc_study.annotation.role_pilot import (
    CODEBOOK_VERSION,
    ROLE_MEASUREMENT_PLATFORMS,
    RolePilotError,
)


AUDIT_SCHEMA_VERSION = "role-platform-coverage-v1"


@dataclass
class _AuthorCoverage:
    """单个平台作者的临时覆盖状态；实例不会被写入审计结果。"""

    post_count: int = 0
    has_display_name: bool = False
    has_profile_url: bool = False
    has_bio: bool = False
    has_verification: bool = False
    has_follower_count: bool = False
    dates: set[date] = field(default_factory=set)


def _has_text(value: object) -> bool:
    """把非空白文本视为已观测，避免空字符串虚增覆盖率。"""

    return value is not None and bool(str(value).strip())


def _parse_date(value: object) -> date | None:
    """从常见ISO时间或日期文本中读取日历日期，失败时返回None。"""

    if not _has_text(value):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _sha256_file(path: Path) -> str:
    """分块计算数据库哈希，供后续确认审计对应的精确快照。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _field_expression(columns: set[str], *names: str) -> str:
    """返回第一个存在的列名；完全缺失时提供SQL NULL。"""

    for name in names:
        if name in columns:
            return name
    return "NULL"


def _verification_expression(columns: set[str]) -> str:
    """兼容研究切片与原始快照的认证字段，不把缺列当作未认证。"""

    if "author_verification_raw" in columns:
        return "author_verification_raw"
    if "author_verified_text" in columns and "author_verified" in columns:
        return """
            COALESCE(
                NULLIF(TRIM(author_verified_text), ''),
                CASE
                    WHEN author_verified IS NULL THEN NULL
                    WHEN author_verified = 1 THEN 'VERIFIED'
                    ELSE 'UNVERIFIED'
                END
            )
        """
    if "author_verified_text" in columns:
        return "author_verified_text"
    if "author_verified" in columns:
        return """
            CASE
                WHEN author_verified IS NULL THEN NULL
                WHEN author_verified = 1 THEN 'VERIFIED'
                ELSE 'UNVERIFIED'
            END
        """
    return "NULL"


def _rate(numerator: int, denominator: int) -> float | None:
    """返回六位小数比例；无分母时显式返回None。"""

    return None if denominator == 0 else round(numerator / denominator, 6)


def audit_role_platform_coverage(database: Path) -> dict[str, object]:
    """按平台汇总作者资料、最低材料门和纵向历史覆盖。

    数据库以SQLite只读URI打开。输出只含聚合结果、字段来源和快照哈希，不含
    作者ID、昵称、主页URL或正文。最低材料门只检查主页URL、简介或认证至少
    一项存在；它不预判人工的``evidence_status``或最终角色。
    """

    database = database.resolve()
    if not database.is_file():
        raise RolePilotError(f"数据库不存在：{database}")
    uri = f"file:{database.as_posix()}?mode=ro"
    authors: dict[tuple[str, str], _AuthorCoverage] = defaultdict(_AuthorCoverage)
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "web_posts" not in tables:
            raise RolePilotError("数据库缺少web_posts表")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(web_posts)")
        }
        required = {"platform_key", "author_platform_id", "published_at"}
        missing = sorted(required - columns)
        if missing:
            raise RolePilotError(f"web_posts缺少字段：{', '.join(missing)}")

        expressions: Mapping[str, str] = {
            "display_name": _field_expression(columns, "author_display_name"),
            "profile_url": _field_expression(columns, "author_profile_url"),
            "bio": _field_expression(columns, "author_description"),
            "verification": _verification_expression(columns),
            "follower_count": _field_expression(columns, "author_followers_count"),
        }
        query = f"""
            SELECT
                platform_key,
                author_platform_id,
                published_at,
                {expressions['display_name']} AS display_name,
                {expressions['profile_url']} AS profile_url,
                {expressions['bio']} AS bio,
                {expressions['verification']} AS verification,
                {expressions['follower_count']} AS follower_count
            FROM web_posts
            WHERE author_platform_id IS NOT NULL
              AND TRIM(author_platform_id) <> ''
            ORDER BY platform_key, author_platform_id
        """
        for row in connection.execute(query):
            key = (str(row["platform_key"]), str(row["author_platform_id"]))
            item = authors[key]
            item.post_count += 1
            item.has_display_name |= _has_text(row["display_name"])
            item.has_profile_url |= _has_text(row["profile_url"])
            item.has_bio |= _has_text(row["bio"])
            item.has_verification |= _has_text(row["verification"])
            item.has_follower_count |= row["follower_count"] is not None
            parsed = _parse_date(row["published_at"])
            if parsed is not None:
                item.dates.add(parsed)

    by_platform: dict[str, list[_AuthorCoverage]] = defaultdict(list)
    for (platform, _), item in authors.items():
        by_platform[platform].append(item)

    platform_rows: list[dict[str, object]] = []
    for platform, items in sorted(by_platform.items()):
        author_count = len(items)
        counts = {
            "display_name": sum(item.has_display_name for item in items),
            "profile_url": sum(item.has_profile_url for item in items),
            "bio": sum(item.has_bio for item in items),
            "verification": sum(item.has_verification for item in items),
            "follower_count": sum(item.has_follower_count for item in items),
            "minimum_material": 0,
            "history_3_dates_30_days": 0,
            "none_code_eligible": 0,
        }
        for item in items:
            minimum_material = (
                item.has_profile_url or item.has_bio or item.has_verification
            )
            ordered_dates = sorted(item.dates)
            history_ready = (
                len(ordered_dates) >= 3
                and (ordered_dates[-1] - ordered_dates[0]).days >= 30
            )
            counts["minimum_material"] += int(minimum_material)
            counts["history_3_dates_30_days"] += int(history_ready)
            counts["none_code_eligible"] += int(minimum_material and history_ready)
        platform_rows.append(
            {
                "platform": platform,
                "configured_for_v0": platform in ROLE_MEASUREMENT_PLATFORMS,
                "author_count": author_count,
                "post_count": sum(item.post_count for item in items),
                "coverage": {
                    field: {
                        "author_count": count,
                        "author_rate": _rate(count, author_count),
                    }
                    for field, count in counts.items()
                },
            }
        )

    return {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "codebook_version": CODEBOOK_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": {
            "filename": database.name,
            "sha256": _sha256_file(database),
            "opened_read_only": True,
        },
        "privacy": {
            "aggregate_only": True,
            "author_identifiers_exported": False,
            "raw_profile_material_exported": False,
        },
        "configured_role_measurement_platforms": sorted(
            ROLE_MEASUREMENT_PLATFORMS
        ),
        "definitions": {
            "minimum_material": (
                "stable author key and at least one observed profile URL, bio, "
                "or verification value"
            ),
            "history_3_dates_30_days": (
                "at least three distinct source dates spanning at least 30 days"
            ),
            "none_code_eligible": (
                "minimum_material and history_3_dates_30_days; audit only, not "
                "an automatic human label"
            ),
        },
        "field_sources": dict(expressions),
        "platforms": platform_rows,
    }
