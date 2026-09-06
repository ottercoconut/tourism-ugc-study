"""KOL/KOC型作者试点规则、抽样隔离与研究切片验收测试。"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tourism_ugc_study.annotation.role_pilot import (
    CODEBOOK_VERSION,
    ROLE_RULE_VERSION,
    RolePilotError,
    create_research_slice,
    derive_creator_role,
    prepare_pilot_package,
    select_quantile_posts,
)
from tourism_ugc_study.annotation.role_pilot_reporting import (
    reliability_for_pairs,
    validate_completed_role_rows,
)


def _create_source_database(path: Path, *, author_count: int = 100) -> None:
    """创建覆盖五平台、足以测试15/5/5抽样框的最小快照。"""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE web_posts (
            id INTEGER PRIMARY KEY,
            platform_key TEXT NOT NULL,
            platform_post_id TEXT,
            source_type TEXT NOT NULL,
            source_url TEXT NOT NULL,
            canonical_url TEXT,
            title TEXT,
            author_display_name TEXT,
            author_platform_id TEXT,
            author_profile_url TEXT,
            author_description TEXT,
            published_at TEXT,
            captured_at TEXT NOT NULL,
            keyword TEXT,
            content_text TEXT,
            author_followers_count INTEGER,
            author_verified INTEGER,
            author_verified_text TEXT,
            status TEXT NOT NULL
        );
        """
    )
    platforms = ("bilibili", "douyin", "weibo", "xhs", "zhihu")
    post_number = 0
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for author_index in range(author_count):
        platform = platforms[author_index % len(platforms)]
        platform_author_index = author_index // len(platforms)
        if platform in {"xhs", "zhihu"} and platform_author_index < 10:
            dates = (0, 31, 62, 93, 124, 155)
            profile_url = f"https://example.invalid/{platform}/a{author_index}"
            description = "持续发布旅行内容"
        elif platform in {"xhs", "zhihu"} and platform_author_index < 15:
            dates = (0, 10)
            profile_url = f"https://example.invalid/{platform}/a{author_index}"
            description = "旅行内容创作者"
        elif platform in {"xhs", "zhihu"}:
            dates = (0,)
            profile_url = f"https://example.invalid/{platform}/a{author_index}"
            description = "旅行记录"
        else:
            dates = (0,)
            profile_url = None
            description = None
        for day in dates:
            post_number += 1
            published = (start + timedelta(days=day)).isoformat()
            connection.execute(
                """
                INSERT INTO web_posts (
                    platform_key, platform_post_id, source_type, source_url,
                    canonical_url, title, author_display_name, author_platform_id,
                    author_profile_url, author_description, published_at, captured_at,
                    keyword, content_text, author_followers_count, author_verified,
                    author_verified_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    f"post-{post_number}",
                    "search",
                    f"https://example.invalid/post/{post_number}",
                    None,
                    "青岛旅行记录",
                    f"作者{author_index}",
                    f"author-{author_index}",
                    profile_url,
                    description,
                    published,
                    "2026-08-24T13:02:25+08:00",
                    "青岛旅游",
                    "海边很舒服。交通也方便！",
                    10 if author_index % 2 == 0 else 1_000_000,
                    None,
                    None,
                    "captured",
                ),
            )
    connection.commit()
    connection.close()


def _snapshot_manifest(source: Path) -> dict[str, object]:
    import hashlib

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with sqlite3.connect(source) as connection:
        count = connection.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]
    return {
        "snapshot_sha256": digest,
        "created_at": "2026-08-24T13:02:25+08:00",
        "schema_version": 18,
        "table_row_counts": {"web_posts": count},
    }


def _create_slice(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.sqlite"
    source_manifest = tmp_path / "manifest.json"
    target = tmp_path / "role-pilot.sqlite"
    metadata = tmp_path / "role-pilot.snapshot.json"
    _create_source_database(source)
    source_manifest.write_text(
        json.dumps(_snapshot_manifest(source)), encoding="utf-8"
    )
    create_research_slice(
        source, target, metadata, source_manifest=source_manifest
    )
    return target, metadata


def test_research_slice_contains_only_required_web_posts(tmp_path: Path) -> None:
    """研究切片必须排除全库的调度、图片、评论和控制表。"""

    database, metadata = _create_slice(tmp_path)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    tables = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    )
    assert tables == ("web_posts",)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] > 0
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(web_posts)")
    }
    assert "author_followers_count" in columns
    assert "follower_observation_status" in columns
    assert "post_likes_count" not in columns
    assert "raw_sample_json" not in columns
    connection.close()
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload["sqlite_integrity_check"] == ["ok"]
    assert payload["read_only_delivery"] is True


def test_role_derivation_acceptance_rules() -> None:
    """机构、粉丝无关、HYBRID和材料不足规则必须按方案执行。"""

    organization = derive_creator_role(
        actor_scope="ORGANIZATION",
        expert_authority_codes=("EA_CREDENTIAL",),
        consumer_experience_codes=("CE_NONE",),
        distinct_source_dates=5,
        source_span_days=120,
        profile_material_available=True,
    )
    assert organization.creator_role == "NA"
    kol = derive_creator_role(
        actor_scope="PERSONAL_CREATOR",
        expert_authority_codes=("EA_CREDENTIAL",),
        consumer_experience_codes=("CE_NONE",),
        distinct_source_dates=3,
        source_span_days=30,
        profile_material_available=True,
    )
    assert kol.creator_role == "KOL_TYPE"
    hybrid = derive_creator_role(
        actor_scope="PERSONAL_CREATOR",
        expert_authority_codes=("EA_SPECIALIST_HISTORY",),
        consumer_experience_codes=(
            "CE_FIRSTHAND_REPEAT",
            "CE_PEER_ORIENTATION",
        ),
        distinct_source_dates=4,
        source_span_days=60,
        profile_material_available=True,
    )
    assert hybrid.creator_role == "HYBRID"
    with pytest.raises(RolePilotError, match="不得填写EA_NONE"):
        derive_creator_role(
            actor_scope="PERSONAL_CREATOR",
            expert_authority_codes=("EA_NONE",),
            consumer_experience_codes=("CE_UNK",),
            distinct_source_dates=2,
            source_span_days=10,
            profile_material_available=False,
        )


def test_quantile_posts_include_earliest_and_latest() -> None:
    """五篇展示上限必须稳定保留最早、最晚和时间分位点。"""

    rows = tuple(
        {"published_at": f"2025-01-{day:02d}", "platform_post_id": f"p{day}"}
        for day in range(1, 11)
    )
    selected = select_quantile_posts(rows, 5)
    assert len(selected) == 5
    assert selected[0]["platform_post_id"] == "p1"
    assert selected[-1]["platform_post_id"] == "p10"
    assert select_quantile_posts(rows, 5) == selected


def test_package_isolates_role_and_text_and_refuses_overwrite(tmp_path: Path) -> None:
    """同轮两任务必须作者不重叠、界面不串字段且原始表不可覆盖。"""

    database, metadata = _create_slice(tmp_path)
    output = tmp_path / "round_20260824_role_text_calibration_v01"
    result = prepare_pilot_package(
        database,
        output,
        database_metadata=metadata,
        content_workbook_template=None,
    )
    assert result.role_author_count == 25
    assert result.text_post_count == 25
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["round_status"] == "PILOT_ONLY"
    assert manifest["codebook_version"] == "v3.15.0"
    assert manifest["role_rule_version"] == "role-pilot-v0.2-draft"
    assert manifest["isolation_assertions"] == {
        "role_and_text_author_sets_disjoint": True,
        "role_task_contains_t1_labels_or_engagement": False,
        "text_task_contains_author_profile_followers_or_role": False,
        "linkage_visible_to_coders": False,
        "role_task_limited_to_measurement_platforms": True,
    }
    role_task = manifest["tasks"][0]
    assert role_task["sampling_audit"]["role_measurement_platforms"] == [
        "xhs",
        "zhihu",
    ]
    assert role_task["sampling_audit"]["minimum_material_gate"] == {
        "stable_author_key_required": True,
        "profile_url_or_bio_or_verification_required": True,
        "evidence_status_preselected": False,
        "creator_role_preselected": False,
    }
    assert role_task["sampling_audit"]["actual_by_sampling_bucket"] == {
        "BOUNDARY": 5,
        "HISTORY_RICH": 15,
        "RANDOM_REMAINDER": 5,
    }
    with (output / "role/coder_a-role-coding.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        role_rows = tuple(csv.DictReader(stream))
    assert len(role_rows) == 25
    assert len({row["author_snapshot_id"] for row in role_rows}) == 25
    assert "post_likes_count" not in role_rows[0]
    assert "cs_inf" not in role_rows[0]
    assert "raw_role_response" not in role_rows[0]
    assert "creator_role_manual" in role_rows[0]
    assert {row["platform"] for row in role_rows} == {"xhs", "zhihu"}
    assert {row["community_relation_status"] for row in role_rows} == {
        "UNAVAILABLE"
    }
    with (output / "text/coder_a-calibration-coding.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        text_reader = csv.DictReader(stream)
        assert "author_platform_id" not in text_reader.fieldnames
        assert "author_profile_url" not in text_reader.fieldnames
        assert "author_followers_count" not in text_reader.fieldnames
        first_text_row = next(text_reader)
    assert first_text_row["dimension_code"] in {"V1", "V2", "V3", "V4", "V5", "V6"}
    assert (output / "role/coder_b-role-coding.csv").is_file()
    assert (output / "text/coder_b-calibration-coding.csv").is_file()
    assert (output / "role/adjudication.csv").is_file()
    with pytest.raises(RolePilotError, match="不可覆盖|已存在"):
        prepare_pilot_package(database, output, database_metadata=metadata)


def test_completed_role_row_requires_all_manual_judgments() -> None:
    """V0总体组件、证据状态和最终角色都必须由编码员直接完成。"""

    row = {
        "author_snapshot_id": "AUTH-001",
        "annotator_id": "CODER_A",
        "actor_scope": "PERSONAL_CREATOR",
        "actor_scope_confidence": "4",
        "content_vertical": "TRAVEL",
        "content_vertical_confidence": "4",
        "expert_authority_criterion_codes": "EA_NONE",
        "expert_authority_confidence": "4",
        "ev_expert_authority": "0",
        "ev_expert_authority_confidence": "4",
        "consumer_experience_criterion_codes": "CE_FIRSTHAND_REPEAT|CE_PEER_ORIENTATION",
        "consumer_experience_confidence": "4",
        "ev_consumer_experience": "1",
        "ev_consumer_experience_confidence": "4",
        "ev_sustained_creation": "1",
        "ev_sustained_creation_confidence": "4",
        "evidence_status": "SUFFICIENT",
        "evidence_status_confidence": "4",
        "creator_role_manual": "KOC_TYPE",
        "creator_role_manual_confidence": "4",
        "community_relation_status": "UNAVAILABLE",
        "low_confidence_note": "",
        "role_rule_version": ROLE_RULE_VERSION,
        "codebook_version": CODEBOOK_VERSION,
    }
    validate_completed_role_rows((row,), expected_annotator="CODER_A")
    incomplete = dict(row)
    incomplete["creator_role_manual"] = ""
    with pytest.raises(RolePilotError, match="人工角色未完成"):
        validate_completed_role_rows((incomplete,), expected_annotator="CODER_A")


def test_nominal_alpha_reports_support_confusion_and_gate() -> None:
    """盲试标信度报告不能只给单一alpha数字。"""

    result = reliability_for_pairs(
        "actor_scope",
        (("A", "A"), ("A", "A"), ("B", "B"), ("B", "A")),
        coder_ids=("CODER_A", "CODER_B"),
        bootstrap_samples=100,
        seed=7,
    )
    assert result.alpha is not None
    assert result.alpha < 0.80
    assert result.gate_status == "RETURN_TO_CALIBRATION"
    assert result.support_by_coder["CODER_A"] == {"A": 2, "B": 2}
    assert result.confusion_matrix["B"] == {"A": 1, "B": 1}
    assert result.ci95_low is not None
    assert result.ci95_high is not None
