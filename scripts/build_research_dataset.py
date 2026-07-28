#!/usr/bin/env python3
"""Build a non-destructive, analysis-ready derivative of TripPostCollect.

The source SQLite database is opened in read-only mode and its SHA-256 is checked
before and after extraction.  The output intentionally keeps quality flags and
analysis-specific eligibility fields instead of silently deleting uncertain data.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT.parent / "TripPostCollect" / "data" / "trippostcollect.sqlite"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "processed" / "trippost_research.sqlite"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "data_quality_report.md"
DEFAULT_MANIFEST = PROJECT_ROOT / "data" / "processed" / "cleaning_manifest.json"

CLEANING_VERSION = "1.0.0"
SHANGHAI = ZoneInfo("Asia/Shanghai")
CORE_PLATFORMS = frozenset({"bilibili", "weibo", "zhihu", "xhs", "douyin"})
EXPECTED_FOLLOWER_SOURCES = {
    "bilibili": "relation_stat",
    "weibo": "search_author",
    "zhihu": "search_author",
    "xhs": "creator_profile",
    "douyin": "creator_profile",
}
REQUIRED_METRICS = {
    "bilibili": ("post_likes_count", "post_comments_count", "post_views_count"),
    "weibo": ("post_likes_count", "post_comments_count", "post_shares_count"),
    "zhihu": ("post_likes_count", "post_comments_count"),
    "xhs": (
        "post_likes_count",
        "post_favorites_count",
        "post_comments_count",
        "post_shares_count",
    ),
    "douyin": (
        "post_likes_count",
        "post_favorites_count",
        "post_comments_count",
        "post_shares_count",
    ),
}
COUNT_FIELDS = (
    "author_followers_count",
    "author_following_count",
    "author_posts_count",
    "post_likes_count",
    "post_favorites_count",
    "post_comments_count",
    "post_shares_count",
    "post_reposts_count",
    "post_views_count",
)
METRIC_FIELDS = tuple(field for field in COUNT_FIELDS if field.startswith("post_"))
TRACKING_QUERY_KEYS = frozenset(
    {
        "feature",
        "from",
        "isappinstalled",
        "share_app_name",
        "share_channel",
        "share_medium",
        "share_plat",
        "share_source",
        "share_tag",
        "source",
        "spm",
        "spm_id_from",
        "timestamp",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(namespace: str, *values: Any) -> str:
    payload = "\x1f".join([f"trippost-research:{namespace}:v1", *(str(v or "") for v in values)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_text(value: Any, *, preserve_newlines: bool = False) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        "" if unicodedata.category(char) == "Cf" else (" " if ord(char) < 32 and char not in "\n\t" else char)
        for char in text
    )
    if preserve_newlines:
        lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(r"\s+", " ", text).strip()


def normalize_canonical_url(value: Any) -> tuple[str | None, bool, bool]:
    """Return normalized URL, whether it changed, and whether it is structurally valid."""
    if value is None or not str(value).strip():
        return None, False, False
    original = str(value).strip()
    try:
        parts = urlsplit(original)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return original, False, False
        hostname = parts.hostname.lower()
        port = ""
        if parts.port and not (
            (parts.scheme.lower() == "http" and parts.port == 80)
            or (parts.scheme.lower() == "https" and parts.port == 443)
        ):
            port = f":{parts.port}"
        query = [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
        ]
        normalized = urlunsplit(
            (
                parts.scheme.lower(),
                hostname + port,
                parts.path.rstrip("/") or "/",
                urlencode(sorted(query)),
                "",
            )
        )
        return normalized, normalized != original, True
    except (TypeError, ValueError):
        return original, False, False


def parse_timestamp(value: Any) -> tuple[str | None, dt.datetime | None, bool]:
    """Normalize an ISO timestamp to Asia/Shanghai without imputing missing values."""
    if value is None or not str(value).strip():
        return None, None, True
    raw = str(value).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        parsed = parsed.astimezone(SHANGHAI)
        return parsed.isoformat(timespec="seconds"), parsed, True
    except ValueError:
        return None, None, False


def nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    return None


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def outlier_cutoffs(posts: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """Compute conservative log-scale cutoffs using positive observations only."""
    grouped: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for post in posts:
        for field in METRIC_FIELDS:
            value = nonnegative_int(post.get(field))
            if value is not None and value > 0:
                grouped[(post["platform_key"], field)].append(math.log1p(value))
    result: dict[tuple[str, str], float] = {}
    for key, values in grouped.items():
        if len(values) < 20:
            continue
        q1, q3 = quantile(values, 0.25), quantile(values, 0.75)
        iqr = q3 - q1
        if iqr <= 0:
            continue
        result[key] = math.expm1(q3 + 3 * iqr)
    return result


def is_video_record(platform_key: str, record: dict[str, Any]) -> bool:
    """Mirror the source importer policy: Douyin aweme_type=68 is an image note."""
    if platform_key == "bilibili" and any(record.get(key) not in (None, "") for key in ("video_id", "bvid", "aid")):
        return True
    if platform_key == "zhihu":
        content_type = str(record.get("content_type") or record.get("type") or "").strip().lower()
        content_url = str(record.get("content_url") or record.get("url") or "")
        return "zvideo" in content_type or content_type == "video" or "/zvideo/" in content_url
    if platform_key == "douyin":
        note_images = record.get("note_download_url") or record.get("image_list") or record.get("images")
        aweme_type = str(
            record.get("aweme_type") or record.get("video_type") or record.get("media_type") or record.get("type") or ""
        ).strip().lower()
        if note_images or aweme_type in {"68", "note", "image", "images", "image_text", "图文"}:
            return False
        return bool(record.get("video_download_url") or record.get("video_url"))
    generic_type = str(record.get("video_type") or record.get("media_type") or record.get("type") or "").lower()
    return "video" in generic_type or "视频" in generic_type


def add_flag(flags: list[dict[str, str]], code: str, severity: str, detail: str) -> None:
    if not any(item["code"] == code for item in flags):
        flags.append({"code": code, "severity": severity, "detail": detail})


def read_source(source: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], str]:
    uri = f"file:{source.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"source SQLite integrity_check failed: {integrity}")
        posts = [dict(row) for row in connection.execute("SELECT * FROM web_posts ORDER BY id")]
        images = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM web_post_images ORDER BY web_post_id, image_role, image_index, id"
            )
        ]
        valid_cities = {row[0] for row in connection.execute("SELECT city_name FROM cities")}
        return posts, images, valid_cities, integrity
    finally:
        connection.close()


def prepare_content_images(images: list[dict[str, Any]]) -> tuple[dict[int, list[dict[str, Any]]], int]:
    by_post: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    dropped_duplicates = 0
    for image in images:
        if image.get("image_role") != "content":
            continue
        url = str(image.get("image_url") or "").strip()
        if not url:
            continue
        if any(existing["image_url_clean"] == url for existing in by_post[image["web_post_id"]]):
            dropped_duplicates += 1
            continue
        by_post[image["web_post_id"]].append(
            {
                "source_image_id": image["id"],
                "source_image_index": image["image_index"],
                "image_url_clean": url,
                "width": nonnegative_int(image.get("width")),
                "height": nonnegative_int(image.get("height")),
                "mime_type": normalize_text(image.get("mime_type")) or None,
                "sha256": normalize_text(image.get("sha256")) or None,
            }
        )
    return by_post, dropped_duplicates


def follower_evidence(post: dict[str, Any], author: dict[str, Any]) -> tuple[bool, bool, str | None]:
    observed = author.get("followers_observed") is True
    source = normalize_text(author.get("followers_source")) or None
    expected = EXPECTED_FOLLOWER_SOURCES.get(post["platform_key"])
    count_valid = nonnegative_int(post.get("author_followers_count")) is not None
    valid = bool(expected and count_valid and observed and source == expected)
    return valid, observed, source


def completeness_score(item: dict[str, Any]) -> int:
    return (
        10 * int(item["scope_core_sample"])
        + 4 * int(item["follower_evidence_valid"])
        + 3 * int(item["content_images_count_clean"] > 0)
        + sum(item[field] is not None for field in REQUIRED_METRICS.get(item["platform_key"], ()))
        + int(item["content_length_clean"] >= 10)
    )


def clean_posts(
    posts: list[dict[str, Any]],
    images_by_post: dict[int, list[dict[str, Any]]],
    valid_cities: set[str],
) -> tuple[list[dict[str, Any]], list[tuple[int, str, str, str]]]:
    cleaned: list[dict[str, Any]] = []
    cutoffs = outlier_cutoffs(posts)
    now = dt.datetime.now(SHANGHAI)

    for post in posts:
        flags: list[dict[str, str]] = []
        try:
            raw = json.loads(post.get("raw_sample_json") or "{}")
            if not isinstance(raw, dict):
                raw = {}
        except json.JSONDecodeError:
            raw = {}
            add_flag(flags, "raw_json_invalid", "warning", "raw_sample_json 不是有效 JSON 对象")
        try:
            author = json.loads(post.get("author_json") or "{}")
            if not isinstance(author, dict):
                author = {}
        except json.JSONDecodeError:
            author = {}
            add_flag(flags, "author_json_invalid", "warning", "author_json 不是有效 JSON 对象")

        title_clean = normalize_text(post.get("title")) or None
        content_clean = normalize_text(post.get("content_text"), preserve_newlines=True)
        keyword_clean = normalize_text(post.get("keyword")) or None
        city_clean = normalize_text(post.get("city_name")) or None
        canonical, url_changed, url_valid = normalize_canonical_url(post.get("canonical_url") or post.get("source_url"))
        published_clean, published_dt, published_valid = parse_timestamp(post.get("published_at"))
        captured_clean, captured_dt, captured_valid = parse_timestamp(post.get("captured_at"))
        evidence_valid, followers_observed, followers_source = follower_evidence(post, author)
        content_images = images_by_post.get(post["id"], [])

        identifier = post.get("platform_post_id") or canonical
        source_record_key = stable_hash("post", post["platform_key"], identifier or post["id"])
        author_identifier = (
            post.get("author_platform_id")
            or post.get("author_profile_url")
            or post.get("author_display_name")
            or f"unknown:{post['id']}"
        )
        author_key = stable_hash("author", post["platform_key"], author_identifier)

        if title_clean is None:
            add_flag(flags, "title_missing", "info", "标题缺失；部分平台以正文作为主文本")
        if len(content_clean) < 10:
            add_flag(flags, "text_too_short", "warning", "标准化后正文少于 10 个字符")
        if normalize_text(post.get("title")) != str(post.get("title") or "").strip() or content_clean != str(
            post.get("content_text") or ""
        ).strip():
            add_flag(flags, "text_normalized", "info", "执行了 Unicode/不可见字符/空白标准化")
        if post.get("content_length") != len(str(post.get("content_text") or "")):
            add_flag(flags, "source_content_length_mismatch", "warning", "源字段 content_length 与原正文长度不一致")
        if city_clean is None:
            add_flag(flags, "city_missing", "warning", "城市字段缺失")
        elif city_clean not in valid_cities:
            add_flag(flags, "city_invalid", "warning", "城市不在源库山东省城市字典中")
        if keyword_clean is None:
            add_flag(flags, "keyword_missing", "warning", "检索关键词缺失")
        if not post.get("author_platform_id"):
            add_flag(flags, "author_id_missing", "warning", "平台作者 ID 缺失")
        if not post.get("author_display_name"):
            add_flag(flags, "author_name_missing", "warning", "作者显示名缺失")
        if not published_valid:
            add_flag(flags, "published_at_invalid", "error", "发布时间无法解析；清洗字段置空")
        elif published_dt and published_dt > now + dt.timedelta(days=1):
            add_flag(flags, "published_at_future", "warning", "发布时间晚于构建时间一天以上")
        if not captured_valid:
            add_flag(flags, "captured_at_invalid", "error", "采集时间无法解析；清洗字段置空")
        if published_dt and captured_dt and published_dt > captured_dt + dt.timedelta(days=1):
            add_flag(flags, "published_after_capture", "warning", "发布时间晚于采集时间一天以上")
        if not url_valid:
            add_flag(flags, "canonical_url_invalid", "warning", "主 URL 不是有效的 HTTP(S) URL")
        elif url_changed:
            add_flag(flags, "canonical_url_normalized", "info", "主 URL 已去片段或常见追踪参数")

        expected_source = EXPECTED_FOLLOWER_SOURCES.get(post["platform_key"])
        if expected_source:
            if post.get("author_followers_count") is None:
                add_flag(flags, "followers_count_missing", "warning", "粉丝数缺失且不做插补")
            elif nonnegative_int(post.get("author_followers_count")) is None:
                add_flag(flags, "followers_count_invalid", "warning", "粉丝数为负值或类型异常；清洗字段置空")
            if not followers_observed:
                add_flag(flags, "followers_not_observed", "warning", "缺少 followers_observed=true 证据")
            if followers_source != expected_source:
                add_flag(
                    flags,
                    "followers_source_untrusted",
                    "warning",
                    f"期望来源 {expected_source}，实际为 {followers_source or 'missing'}",
                )

        cleaned_counts: dict[str, int | None] = {}
        for field in COUNT_FIELDS:
            cleaned_counts[field] = nonnegative_int(post.get(field))
            if post.get(field) is not None and cleaned_counts[field] is None:
                add_flag(flags, f"invalid_count:{field}", "warning", "负值或非整数；清洗字段置空")
        followers_clean = cleaned_counts["author_followers_count"] if evidence_valid else None

        for field in REQUIRED_METRICS.get(post["platform_key"], ()):
            if cleaned_counts[field] is None:
                add_flag(flags, f"required_metric_missing:{field}", "warning", "平台必需指标缺失")
        for field in METRIC_FIELDS:
            value = cleaned_counts[field]
            cutoff = cutoffs.get((post["platform_key"], field))
            if value is not None and cutoff is not None and value > cutoff:
                add_flag(
                    flags,
                    f"metric_extreme:{field}",
                    "warning",
                    "超过平台内正值 log1p 三倍 IQR 阈值；保留原值，不缩尾",
                )

        if not content_images:
            add_flag(flags, "content_image_missing", "warning", "没有 content 角色的图片关系")
        if len(content_images) != nonnegative_int(post.get("post_images_count")):
            add_flag(
                flags,
                "source_image_count_mismatch",
                "warning",
                "源 post_images_count 与 content 角色图片关系数不一致；采用关系表重算值",
            )
        video = is_video_record(post["platform_key"], raw)
        if video:
            add_flag(flags, "video_record", "error", "违反图文研究范围的视频记录")
        no_identity = not post.get("platform_post_id") and not canonical
        if no_identity:
            add_flag(flags, "identity_missing", "error", "平台内容 ID 与主 URL 均缺失")
        if not title_clean and not content_clean:
            add_flag(flags, "substantive_text_missing", "error", "标题与正文均为空")

        scope_core = (
            post["platform_key"] in CORE_PLATFORMS
            and post.get("source_type") == "mediacrawler_search"
            and city_clean in valid_cities
            and keyword_clean is not None
        )
        if not scope_core:
            add_flag(flags, "outside_core_sample", "info", "不属于五平台检索形成的默认科研样本")
        hard_excluded = video or no_identity or (not title_clean and not content_clean)
        exclusion_reasons = [
            item["code"]
            for item in flags
            if item["code"] in {"video_record", "identity_missing", "substantive_text_missing"}
        ]

        text_material = normalize_text(f"{title_clean or ''} {content_clean}")
        text_hash = stable_hash("text", text_material) if text_material else None
        item = {
            "research_post_id": post["id"],
            "source_web_post_id": post["id"],
            "source_record_key": source_record_key,
            "platform_key": post["platform_key"],
            "source_type": post["source_type"],
            "title_clean": title_clean,
            "content_text_clean": content_clean or None,
            "content_length_clean": len(content_clean),
            "city_name": city_clean,
            "keyword_clean": keyword_clean,
            "published_at_shanghai": published_clean,
            "captured_at_shanghai": captured_clean,
            "author_key": author_key,
            "author_followers_count_clean": followers_clean,
            "author_following_count_clean": cleaned_counts["author_following_count"],
            "author_posts_count_clean": cleaned_counts["author_posts_count"],
            "followers_observed": int(followers_observed),
            "followers_source": followers_source,
            "follower_evidence_valid": int(evidence_valid),
            "post_likes_count_clean": cleaned_counts["post_likes_count"],
            "post_favorites_count_clean": cleaned_counts["post_favorites_count"],
            "post_comments_count_clean": cleaned_counts["post_comments_count"],
            "post_shares_count_clean": cleaned_counts["post_shares_count"],
            "post_reposts_count_clean": cleaned_counts["post_reposts_count"],
            "post_views_count_clean": cleaned_counts["post_views_count"],
            "post_images_count_source": nonnegative_int(post.get("post_images_count")),
            "content_images_count_clean": len(content_images),
            "has_content_image": int(bool(content_images)),
            "text_hash": text_hash,
            "scope_core_sample": int(scope_core),
            "hard_excluded": int(hard_excluded),
            "exclusion_reasons_json": json.dumps(exclusion_reasons, ensure_ascii=False),
            "_published_dt": published_dt,
            "_flags": flags,
        }
        for field in REQUIRED_METRICS.get(post["platform_key"], ()):
            item[field] = cleaned_counts[field]
        cleaned.append(item)

    clusters: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for item in cleaned:
        if item["text_hash"] and item["content_length_clean"] >= 10:
            clusters[(item["platform_key"], item["text_hash"])].append(item)
    for rows in clusters.values():
        ordered = sorted(
            rows,
            key=lambda item: (
                -completeness_score(item),
                item["_published_dt"] or dt.datetime.max.replace(tzinfo=SHANGHAI),
                item["source_web_post_id"],
            ),
        )
        for rank, item in enumerate(ordered, start=1):
            item["text_cluster_size"] = len(rows)
            item["text_cluster_rank"] = rank
            item["text_dedup_preferred"] = int(rank == 1)
            if len(rows) > 1:
                add_flag(
                    item["_flags"],
                    "normalized_text_duplicate",
                    "warning",
                    f"平台内标准化文本簇含 {len(rows)} 条；不删除，rank={rank}",
                )
    for item in cleaned:
        item.setdefault("text_cluster_size", 1 if item["text_hash"] else 0)
        item.setdefault("text_cluster_rank", 1 if item["text_hash"] else None)
        item.setdefault("text_dedup_preferred", 1 if item["text_hash"] else 0)
        required = REQUIRED_METRICS.get(item["platform_key"], ())
        base = bool(item["scope_core_sample"] and not item["hard_excluded"])
        item["eligible_content_analysis"] = int(base and item["content_length_clean"] >= 10)
        item["eligible_content_deduplicated"] = int(
            item["eligible_content_analysis"] and item["text_dedup_preferred"]
        )
        item["eligible_temporal_analysis"] = int(base and item["published_at_shanghai"] is not None)
        item["eligible_engagement_analysis"] = int(base and all(item.get(field) is not None for field in required))
        item["eligible_author_analysis"] = int(base and item["follower_evidence_valid"])
        item["eligible_multimodal_analysis"] = int(base and item["has_content_image"])
        item["quality_flag_count"] = len(item["_flags"])
        item["quality_flags_json"] = json.dumps(
            [flag["code"] for flag in item["_flags"]], ensure_ascii=False, separators=(",", ":")
        )

    quality_rows = [
        (item["research_post_id"], flag["code"], flag["severity"], flag["detail"])
        for item in cleaned
        for flag in item["_flags"]
    ]
    return cleaned, quality_rows


SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE build_manifest (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE research_posts (
    research_post_id INTEGER PRIMARY KEY,
    source_web_post_id INTEGER NOT NULL UNIQUE,
    source_record_key TEXT NOT NULL UNIQUE,
    platform_key TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title_clean TEXT,
    content_text_clean TEXT,
    content_length_clean INTEGER NOT NULL CHECK(content_length_clean >= 0),
    city_name TEXT,
    keyword_clean TEXT,
    published_at_shanghai TEXT,
    captured_at_shanghai TEXT,
    author_key TEXT NOT NULL,
    author_followers_count_clean INTEGER CHECK(author_followers_count_clean >= 0),
    author_following_count_clean INTEGER CHECK(author_following_count_clean >= 0),
    author_posts_count_clean INTEGER CHECK(author_posts_count_clean >= 0),
    followers_observed INTEGER NOT NULL CHECK(followers_observed IN (0,1)),
    followers_source TEXT,
    follower_evidence_valid INTEGER NOT NULL CHECK(follower_evidence_valid IN (0,1)),
    post_likes_count_clean INTEGER CHECK(post_likes_count_clean >= 0),
    post_favorites_count_clean INTEGER CHECK(post_favorites_count_clean >= 0),
    post_comments_count_clean INTEGER CHECK(post_comments_count_clean >= 0),
    post_shares_count_clean INTEGER CHECK(post_shares_count_clean >= 0),
    post_reposts_count_clean INTEGER CHECK(post_reposts_count_clean >= 0),
    post_views_count_clean INTEGER CHECK(post_views_count_clean >= 0),
    post_images_count_source INTEGER CHECK(post_images_count_source >= 0),
    content_images_count_clean INTEGER NOT NULL CHECK(content_images_count_clean >= 0),
    has_content_image INTEGER NOT NULL CHECK(has_content_image IN (0,1)),
    text_hash TEXT,
    text_cluster_size INTEGER NOT NULL CHECK(text_cluster_size >= 0),
    text_cluster_rank INTEGER,
    text_dedup_preferred INTEGER NOT NULL CHECK(text_dedup_preferred IN (0,1)),
    scope_core_sample INTEGER NOT NULL CHECK(scope_core_sample IN (0,1)),
    hard_excluded INTEGER NOT NULL CHECK(hard_excluded IN (0,1)),
    exclusion_reasons_json TEXT NOT NULL CHECK(json_valid(exclusion_reasons_json)),
    eligible_content_analysis INTEGER NOT NULL CHECK(eligible_content_analysis IN (0,1)),
    eligible_content_deduplicated INTEGER NOT NULL CHECK(eligible_content_deduplicated IN (0,1)),
    eligible_temporal_analysis INTEGER NOT NULL CHECK(eligible_temporal_analysis IN (0,1)),
    eligible_engagement_analysis INTEGER NOT NULL CHECK(eligible_engagement_analysis IN (0,1)),
    eligible_author_analysis INTEGER NOT NULL CHECK(eligible_author_analysis IN (0,1)),
    eligible_multimodal_analysis INTEGER NOT NULL CHECK(eligible_multimodal_analysis IN (0,1)),
    quality_flag_count INTEGER NOT NULL CHECK(quality_flag_count >= 0),
    quality_flags_json TEXT NOT NULL CHECK(json_valid(quality_flags_json))
);

CREATE TABLE research_post_images (
    research_image_id INTEGER PRIMARY KEY,
    research_post_id INTEGER NOT NULL REFERENCES research_posts(research_post_id) ON DELETE CASCADE,
    source_image_id INTEGER NOT NULL UNIQUE,
    source_image_index INTEGER NOT NULL,
    image_index_clean INTEGER NOT NULL,
    image_url_clean TEXT NOT NULL,
    width INTEGER CHECK(width >= 0),
    height INTEGER CHECK(height >= 0),
    mime_type TEXT,
    sha256 TEXT,
    UNIQUE(research_post_id, image_index_clean),
    UNIQUE(research_post_id, image_url_clean)
);

CREATE TABLE quality_flags (
    research_post_id INTEGER NOT NULL REFERENCES research_posts(research_post_id) ON DELETE CASCADE,
    flag_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
    detail TEXT NOT NULL,
    PRIMARY KEY(research_post_id, flag_code)
);

CREATE TABLE cleaning_actions (
    action_code TEXT PRIMARY KEY,
    affected_rows INTEGER NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE platform_quality_summary (
    platform_key TEXT PRIMARY KEY,
    total_posts INTEGER NOT NULL,
    core_posts INTEGER NOT NULL,
    hard_excluded_posts INTEGER NOT NULL,
    follower_evidence_valid_posts INTEGER NOT NULL,
    content_eligible_posts INTEGER NOT NULL,
    content_deduplicated_posts INTEGER NOT NULL,
    temporal_eligible_posts INTEGER NOT NULL,
    engagement_eligible_posts INTEGER NOT NULL,
    author_eligible_posts INTEGER NOT NULL,
    multimodal_eligible_posts INTEGER NOT NULL
);

CREATE TABLE city_quality_summary (
    city_name TEXT PRIMARY KEY,
    total_posts INTEGER NOT NULL,
    core_posts INTEGER NOT NULL
);

CREATE INDEX idx_research_posts_platform_city ON research_posts(platform_key, city_name);
CREATE INDEX idx_research_posts_published ON research_posts(published_at_shanghai);
CREATE INDEX idx_research_posts_author ON research_posts(platform_key, author_key);
CREATE INDEX idx_research_posts_text_hash ON research_posts(platform_key, text_hash);
CREATE INDEX idx_quality_flags_code ON quality_flags(flag_code, severity);
CREATE INDEX idx_research_images_post ON research_post_images(research_post_id, image_index_clean);

CREATE VIEW analysis_posts_core AS
SELECT * FROM research_posts WHERE scope_core_sample=1 AND hard_excluded=0;

CREATE VIEW analysis_posts_content AS
SELECT * FROM research_posts WHERE eligible_content_analysis=1;

CREATE VIEW analysis_posts_content_deduplicated AS
SELECT * FROM research_posts WHERE eligible_content_deduplicated=1;

CREATE VIEW analysis_posts_temporal AS
SELECT * FROM research_posts WHERE eligible_temporal_analysis=1;

CREATE VIEW analysis_posts_engagement AS
SELECT * FROM research_posts WHERE eligible_engagement_analysis=1;

CREATE VIEW analysis_posts_author AS
SELECT * FROM research_posts WHERE eligible_author_analysis=1;

CREATE VIEW analysis_posts_multimodal AS
SELECT * FROM research_posts WHERE eligible_multimodal_analysis=1;
"""


POST_COLUMNS = (
    "research_post_id",
    "source_web_post_id",
    "source_record_key",
    "platform_key",
    "source_type",
    "title_clean",
    "content_text_clean",
    "content_length_clean",
    "city_name",
    "keyword_clean",
    "published_at_shanghai",
    "captured_at_shanghai",
    "author_key",
    "author_followers_count_clean",
    "author_following_count_clean",
    "author_posts_count_clean",
    "followers_observed",
    "followers_source",
    "follower_evidence_valid",
    "post_likes_count_clean",
    "post_favorites_count_clean",
    "post_comments_count_clean",
    "post_shares_count_clean",
    "post_reposts_count_clean",
    "post_views_count_clean",
    "post_images_count_source",
    "content_images_count_clean",
    "has_content_image",
    "text_hash",
    "text_cluster_size",
    "text_cluster_rank",
    "text_dedup_preferred",
    "scope_core_sample",
    "hard_excluded",
    "exclusion_reasons_json",
    "eligible_content_analysis",
    "eligible_content_deduplicated",
    "eligible_temporal_analysis",
    "eligible_engagement_analysis",
    "eligible_author_analysis",
    "eligible_multimodal_analysis",
    "quality_flag_count",
    "quality_flags_json",
)


def insert_clean_database(
    output: Path,
    posts: list[dict[str, Any]],
    images_by_post: dict[int, list[dict[str, Any]]],
    quality_rows: list[tuple[int, str, str, str]],
    manifest: dict[str, Any],
    duplicate_images_removed: int,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(SCHEMA)
        placeholders = ",".join("?" for _ in POST_COLUMNS)
        connection.executemany(
            f"INSERT INTO research_posts ({','.join(POST_COLUMNS)}) VALUES ({placeholders})",
            [[post[column] for column in POST_COLUMNS] for post in posts],
        )

        image_rows: list[tuple[Any, ...]] = []
        research_image_id = 1
        for post in posts:
            for clean_index, image in enumerate(images_by_post.get(post["research_post_id"], [])):
                image_rows.append(
                    (
                        research_image_id,
                        post["research_post_id"],
                        image["source_image_id"],
                        image["source_image_index"],
                        clean_index,
                        image["image_url_clean"],
                        image["width"],
                        image["height"],
                        image["mime_type"],
                        image["sha256"],
                    )
                )
                research_image_id += 1
        connection.executemany(
            """INSERT INTO research_post_images (
                research_image_id,research_post_id,source_image_id,source_image_index,
                image_index_clean,image_url_clean,width,height,mime_type,sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            image_rows,
        )
        connection.executemany(
            "INSERT INTO quality_flags(research_post_id,flag_code,severity,detail) VALUES (?,?,?,?)",
            quality_rows,
        )
        connection.executemany(
            "INSERT INTO build_manifest(key,value) VALUES (?,?)",
            [(key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value) for key, value in manifest.items()],
        )

        action_rows = [
            (
                "text_normalized",
                sum(any(flag["code"] == "text_normalized" for flag in post["_flags"]) for post in posts),
                "Unicode NFKC、不可见格式字符与段内空白标准化；保留段落换行，源文本不改",
            ),
            (
                "url_normalized",
                sum(any(flag["code"] == "canonical_url_normalized" for flag in post["_flags"]) for post in posts),
                "仅用于生成不可逆记录键；移除片段和常见追踪参数",
            ),
            (
                "followers_withheld_untrusted",
                sum(
                    post["platform_key"] in CORE_PLATFORMS
                    and post["author_followers_count_clean"] is None
                    for post in posts
                ),
                "缺失同批次已观测/可信来源证据的粉丝数在清洗字段中置空，不插补",
            ),
            (
                "negative_or_invalid_counts_to_null",
                sum(
                    flag["code"].startswith("invalid_count:")
                    for post in posts
                    for flag in post["_flags"]
                ),
                "负值或非整数计数字段置空",
            ),
            (
                "duplicate_text_rows_flagged",
                sum(any(flag["code"] == "normalized_text_duplicate" for flag in post["_flags"]) for post in posts),
                "平台内标准化文本重复仅分簇和排序，不物理删除",
            ),
            (
                "content_image_count_recomputed",
                sum(
                    any(flag["code"] == "source_image_count_mismatch" for flag in post["_flags"])
                    for post in posts
                ),
                "从 web_post_images 的 content 角色关系重算图片数",
            ),
            (
                "duplicate_content_image_rows_removed",
                duplicate_images_removed,
                "同一帖子内完全相同的 content 图片 URL 仅保留一条",
            ),
            (
                "author_identifiers_pseudonymized",
                len(posts),
                "作者 ID/主页/显示名不复制到分析主表，仅保留稳定 SHA-256 联结键",
            ),
        ]
        connection.executemany("INSERT INTO cleaning_actions VALUES (?,?,?)", action_rows)
        connection.execute(
            """INSERT INTO platform_quality_summary
            SELECT platform_key, COUNT(*), SUM(scope_core_sample), SUM(hard_excluded),
                   SUM(follower_evidence_valid), SUM(eligible_content_analysis),
                   SUM(eligible_content_deduplicated), SUM(eligible_temporal_analysis),
                   SUM(eligible_engagement_analysis), SUM(eligible_author_analysis),
                   SUM(eligible_multimodal_analysis)
            FROM research_posts GROUP BY platform_key"""
        )
        connection.execute(
            """INSERT INTO city_quality_summary
            SELECT COALESCE(city_name, '[缺失]'), COUNT(*), SUM(scope_core_sample)
            FROM research_posts GROUP BY city_name"""
        )
        connection.commit()

        checks = {
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "post_count": connection.execute("SELECT COUNT(*) FROM research_posts").fetchone()[0],
            "image_count": connection.execute("SELECT COUNT(*) FROM research_post_images").fetchone()[0],
            "image_count_mismatches": connection.execute(
                """SELECT COUNT(*) FROM research_posts p
                WHERE p.content_images_count_clean != (
                    SELECT COUNT(*) FROM research_post_images i WHERE i.research_post_id=p.research_post_id
                )"""
            ).fetchone()[0],
        }
        if checks != {
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "post_count": len(posts),
            "image_count": len(image_rows),
            "image_count_mismatches": 0,
        }:
            raise RuntimeError(f"derived database validation failed: {checks}")
    finally:
        connection.close()
    os.replace(temporary, output)


def fetch_dicts(connection: sqlite3.Connection, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(sql, tuple(parameters))]


def markdown_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def write_report(report: Path, output: Path, manifest: dict[str, Any]) -> None:
    connection = sqlite3.connect(f"file:{output.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        total = connection.execute("SELECT COUNT(*) FROM research_posts").fetchone()[0]
        core = connection.execute("SELECT COUNT(*) FROM analysis_posts_core").fetchone()[0]
        hard = connection.execute("SELECT SUM(hard_excluded) FROM research_posts").fetchone()[0]
        content = connection.execute("SELECT COUNT(*) FROM analysis_posts_content").fetchone()[0]
        content_dedup = connection.execute("SELECT COUNT(*) FROM analysis_posts_content_deduplicated").fetchone()[0]
        temporal = connection.execute("SELECT COUNT(*) FROM analysis_posts_temporal").fetchone()[0]
        engagement = connection.execute("SELECT COUNT(*) FROM analysis_posts_engagement").fetchone()[0]
        author = connection.execute("SELECT COUNT(*) FROM analysis_posts_author").fetchone()[0]
        multimodal = connection.execute("SELECT COUNT(*) FROM analysis_posts_multimodal").fetchone()[0]
        images = connection.execute("SELECT COUNT(*) FROM research_post_images").fetchone()[0]
        duplicate_rows = connection.execute(
            "SELECT COUNT(*) FROM research_posts WHERE text_cluster_size>1"
        ).fetchone()[0]
        duplicate_excess = connection.execute(
            "SELECT COUNT(*) FROM research_posts WHERE text_cluster_size>1 AND text_cluster_rank>1"
        ).fetchone()[0]
        duplicate_groups = connection.execute(
            "SELECT COUNT(DISTINCT platform_key || ':' || text_hash) FROM research_posts WHERE text_cluster_size>1"
        ).fetchone()[0]
        invalid_times = connection.execute(
            """SELECT COUNT(*) FROM quality_flags
            WHERE flag_code IN ('published_at_invalid','captured_at_invalid','published_at_future','published_after_capture')"""
        ).fetchone()[0]
        no_images = connection.execute(
            "SELECT COUNT(*) FROM research_posts WHERE has_content_image=0"
        ).fetchone()[0]
        image_mismatch = connection.execute(
            "SELECT COUNT(*) FROM quality_flags WHERE flag_code='source_image_count_mismatch'"
        ).fetchone()[0]
        video_rows = connection.execute(
            "SELECT COUNT(*) FROM quality_flags WHERE flag_code='video_record'"
        ).fetchone()[0]
        platform_rows = fetch_dicts(connection, "SELECT * FROM platform_quality_summary ORDER BY total_posts DESC")
        city_rows = fetch_dicts(connection, "SELECT * FROM city_quality_summary ORDER BY total_posts DESC")
        keyword_rows = fetch_dicts(
            connection,
            """SELECT keyword_clean, COUNT(*) AS n
               FROM research_posts WHERE scope_core_sample=1
               GROUP BY keyword_clean ORDER BY n DESC,keyword_clean""",
        )
        time_rows = fetch_dicts(
            connection,
            """SELECT platform_key, COUNT(*) AS n,
                      MIN(published_at_shanghai) AS published_min,
                      MAX(published_at_shanghai) AS published_max
               FROM research_posts WHERE scope_core_sample=1
               GROUP BY platform_key ORDER BY platform_key""",
        )
        metric_rows = fetch_dicts(
            connection,
            """SELECT platform_key, COUNT(*) AS n,
                      SUM(post_likes_count_clean IS NOT NULL) AS likes_n,
                      SUM(post_favorites_count_clean IS NOT NULL) AS favorites_n,
                      SUM(post_comments_count_clean IS NOT NULL) AS comments_n,
                      SUM(post_shares_count_clean IS NOT NULL) AS shares_n,
                      SUM(post_reposts_count_clean IS NOT NULL) AS reposts_n,
                      SUM(post_views_count_clean IS NOT NULL) AS views_n
               FROM research_posts WHERE scope_core_sample=1
               GROUP BY platform_key ORDER BY platform_key""",
        )
        follower_rows = fetch_dicts(
            connection,
            """SELECT platform_key, COUNT(*) total,
                      SUM(follower_evidence_valid) valid,
                      COUNT(*)-SUM(follower_evidence_valid) invalid
               FROM research_posts WHERE scope_core_sample=1
               GROUP BY platform_key ORDER BY platform_key""",
        )
        flag_rows = fetch_dicts(
            connection,
            """SELECT flag_code, severity, COUNT(*) AS n FROM quality_flags
               GROUP BY flag_code,severity ORDER BY n DESC,flag_code LIMIT 20""",
        )
        action_rows = fetch_dicts(connection, "SELECT * FROM cleaning_actions ORDER BY action_code")
    finally:
        connection.close()

    pct = lambda value, denominator: f"{100 * value / denominator:.1f}%" if denominator else "0.0%"
    generated = manifest["generated_at_shanghai"]
    text = f"""# TripPostCollect 科研数据质量与清洗报告

生成时间：{generated}  
清洗规则版本：{CLEANING_VERSION}  
源库 SHA-256：`{manifest['source_sha256']}`  
源库完整性：`{manifest['source_integrity_check']}`；构建前后哈希一致  
派生库 SHA-256：`{manifest['output_sha256']}`

## 结论摘要

- 源主表共 {total:,} 条记录；科研默认核心队列为 {core:,} 条，来自 B站、微博、知乎、小红书、抖音五个平台的检索采集。其余 {total-core:,} 条保留在派生库中，但标记为范围外。
- 硬排除 {hard:,} 条；检测到视频记录 {video_rows:,} 条。缺失、重复、极端值均未被静默删除。
- 文本分析可用 {content:,} 条；平台内标准化文本去重视图可用 {content_dedup:,} 条。发现 {duplicate_groups:,} 个重复文本簇、涉及 {duplicate_rows:,} 条，其中非首选记录 {duplicate_excess:,} 条。
- 时间分析可用 {temporal:,} 条，时间异常标记 {invalid_times:,} 条；未对发布时间做任何插补。
- 互动指标分析可用 {engagement:,} 条；作者粉丝分析可用 {author:,} 条；多模态分析可用 {multimodal:,} 条。
- 派生库保存 {images:,} 条 content 图片关系。{no_images:,} 条帖子没有 content 图片，{image_mismatch:,} 条源图片计数与关系表不一致，清洗后以关系表重算。

## 样本结构

### 平台

{markdown_table(
    ['平台','总数','核心队列','文本','文本去重','时间','互动','作者','多模态'],
    ([r['platform_key'],r['total_posts'],r['core_posts'],r['content_eligible_posts'],r['content_deduplicated_posts'],r['temporal_eligible_posts'],r['engagement_eligible_posts'],r['author_eligible_posts'],r['multimodal_eligible_posts']] for r in platform_rows),
)}

### 城市

{markdown_table(
    ['城市','总数','核心队列','占核心队列'],
    ([r['city_name'],r['total_posts'],r['core_posts'],pct(r['core_posts'],core)] for r in city_rows),
)}

源库虽然有山东 16 市字典，但现有内容只覆盖青岛、济南、烟台 3 市。该分布是检索与采集设计造成的样本边界，不应通过过采样或删除记录伪装成全省代表性样本。

### 检索关键词

{markdown_table(
    ['关键词','核心队列记录数','占核心队列'],
    ([r['keyword_clean'],r['n'],pct(r['n'],core)] for r in keyword_rows),
)}

城市与关键词高度绑定，因此“城市差异”同时混入了检索词差异，不能把观察到的差别完全解释为城市效应。

### 发布时间范围

{markdown_table(
    ['平台','记录数','最早发布时间','最晚发布时间'],
    ([r['platform_key'],r['n'],r['published_min'],r['published_max']] for r in time_rows),
)}

各平台时间覆盖明显不一致。跨平台模型应限定共同时间窗，或显式控制年、月及平台，不能直接比较全时段均值。

## 互动指标可用性

表中为“非缺失记录数（平台内比例）”；空缺通常是平台结构性不可得，不应填 0。

{markdown_table(
    ['平台','总数','点赞','收藏','评论','分享','转发','播放'],
    ([r['platform_key'],r['n'],f"{r['likes_n']} ({pct(r['likes_n'],r['n'])})",f"{r['favorites_n']} ({pct(r['favorites_n'],r['n'])})",f"{r['comments_n']} ({pct(r['comments_n'],r['n'])})",f"{r['shares_n']} ({pct(r['shares_n'],r['n'])})",f"{r['reposts_n']} ({pct(r['reposts_n'],r['n'])})",f"{r['views_n']} ({pct(r['views_n'],r['n'])})"] for r in metric_rows),
)}

## 粉丝证据可用性

只有粉丝数非负、`followers_observed=true` 且来源等于平台契约指定来源时，`author_followers_count_clean` 才保留数值；否则置空但保留质量标记，真实且有证据的 0 不会被当成缺失。

{markdown_table(
    ['平台','总数','证据有效','证据无效/缺失','有效率'],
    ([r['platform_key'],r['total'],r['valid'],r['invalid'],pct(r['valid'],r['total'])] for r in follower_rows),
)}

## 主要质量标记

{markdown_table(
    ['标记','级别','记录数'],
    ([r['flag_code'],r['severity'],r['n']] for r in flag_rows),
)}

极端互动量使用“平台内、指标内、仅正值的 `log1p` 三倍 IQR”标记。它只提示敏感性分析需求，不代表数据错误，因此没有缩尾或删除。

## 实际清洗动作

{markdown_table(
    ['动作','影响行数','说明'],
    ([r['action_code'],r['affected_rows'],r['description']] for r in action_rows),
)}

作者显示名、作者平台 ID 和主页地址没有复制到分析主表，替换为稳定 SHA-256 联结键。此处理属于假名化，不是匿名化：正文与图片 URL 仍可能导致再识别，派生库不应在未完成伦理、平台条款与版权审查前公开分发。

## 推荐分析视图

| 视图 | 用途 |
| --- | --- |
| `analysis_posts_core` | 五平台检索形成的完整核心队列，不按缺失或极端值删样本 |
| `analysis_posts_content` | 标准化正文不少于 10 字的文本分析 |
| `analysis_posts_content_deduplicated` | 每个平台内每个标准化文本簇保留质量更完整、时间更早的一条 |
| `analysis_posts_temporal` | 发布时间可解析的时间分析 |
| `analysis_posts_engagement` | 平台规定的必需互动指标均存在；仍应按平台分层建模 |
| `analysis_posts_author` | 粉丝证据通过来源契约的作者层分析 |
| `analysis_posts_multimodal` | 至少一张 content 图片的图文分析 |

## 科研使用限制

1. 这是关键词检索得到的观察性便利样本，不是概率样本；不能直接外推到山东全省游客或全部平台内容。
2. 平台时间窗差异很大，跨平台比较必须控制年份/月份或使用共同时间窗。
3. 点赞、收藏、评论、分享、播放的定义及缺失机制因平台不同，禁止把结构性缺失填 0 后直接合并比较。
4. 粉丝数只在证据有效子样本中分析，并报告各平台有效率；否则会产生选择偏差。
5. 重复文本可能是转载、同作者重复发布或模板化内容。主库保留全部记录，去重视图仅适合文本主题频数的敏感性分析。
6. 建议论文同时报告“完整核心队列”和相应资格视图的结果，以展示清洗选择对结论的影响。
"""
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8")


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(source: Path, output: Path, report: Path, manifest_path: Path) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    report = report.resolve()
    manifest_path = manifest_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    source_hash_before = sha256_file(source)
    posts, images, valid_cities, integrity = read_source(source)
    source_hash_after_read = sha256_file(source)
    if source_hash_before != source_hash_after_read:
        raise RuntimeError("source database changed while it was being read; aborting")

    images_by_post, duplicate_images_removed = prepare_content_images(images)
    cleaned, quality_rows = clean_posts(posts, images_by_post, valid_cities)
    generated_utc = dt.datetime.now(dt.timezone.utc)
    manifest: dict[str, Any] = {
        "cleaning_version": CLEANING_VERSION,
        "generated_at_utc": generated_utc.isoformat(timespec="seconds"),
        "generated_at_shanghai": generated_utc.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "python_version": sys.version.split()[0],
        "source_path": str(source),
        "source_size_bytes": source.stat().st_size,
        "source_mtime_utc": dt.datetime.fromtimestamp(source.stat().st_mtime, dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "source_sha256": source_hash_before,
        "source_integrity_check": integrity,
        "source_posts": len(posts),
        "source_image_relations": len(images),
        "source_city_dictionary_size": len(valid_cities),
        "output_path": str(output),
        "report_path": str(report),
        "author_key_policy": "domain-separated unsalted SHA-256; pseudonymization, not anonymization",
        "source_open_mode": "SQLite URI mode=ro",
    }
    insert_clean_database(output, cleaned, images_by_post, quality_rows, manifest, duplicate_images_removed)
    source_hash_final = sha256_file(source)
    if source_hash_final != source_hash_before:
        output.unlink(missing_ok=True)
        raise RuntimeError("source database hash changed during build; derivative removed")
    manifest["source_hash_unchanged_after_build"] = True
    manifest["output_size_bytes"] = output.stat().st_size
    manifest["output_sha256"] = sha256_file(output)
    write_manifest(manifest_path, manifest)
    write_report(report, output, manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = build(args.source, args.output, args.report, args.manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
