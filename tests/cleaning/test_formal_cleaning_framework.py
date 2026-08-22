"""正式清洗框架的配置、参考证据、迁移 manifest 与内部 schema 契约测试。"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.cleaning.config import ConfigurationError, load_stable_config
from tourism_ugc_study.cleaning.formal_schema import migrate_formal_schema
from tourism_ugc_study.cleaning.reference_evidence import (
    ReferenceEvidenceError,
    validate_reference_evidence,
    validate_sample_migration_manifest,
    write_sample_migration_manifest,
)


ROOT = Path(__file__).resolve().parents[2]


def test_stable_config_has_no_platform_controls_or_numeric_thresholds() -> None:
    config = load_stable_config(ROOT / "configs/cleaning.yaml")
    assert config.split["temporal_test_fraction"] == 0.20
    assert config.routing["T_keep"] == "UNSET"
    assert config.routing["T_exclude"] == "UNSET"
    assert config.audit["sample_size"] == "UNSET"
    assert "platform" not in str(config.raw).casefold()


def test_stable_config_rejects_threshold_change(tmp_path: Path) -> None:
    raw = yaml.safe_load((ROOT / "configs/cleaning.yaml").read_text(encoding="utf-8"))
    raw["routing"]["T_keep"] = 0.1

    path = tmp_path / "cleaning.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_stable_config(path)


def _build_reference_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """建立最小派生库，覆盖概率与定向两种样本框。"""

    db = tmp_path / "derived.sqlite"
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE text_sampling_runs(
            sample_run_id TEXT PRIMARY KEY, seal_status TEXT,
            member_manifest_sha256 TEXT, population_count INTEGER,
            probability_count INTEGER, targeted_count INTEGER,
            candidate_build_id TEXT, source_snapshot_id TEXT,
            guide_version TEXT, random_seed INTEGER,
            population_manifest_sha256 TEXT, sample_kind TEXT,
            created_at_utc TEXT
        );
        CREATE TABLE text_sample_members(
            sample_run_id TEXT, source_post_id INTEGER, source_version INTEGER,
            platform_key TEXT, sample_frame TEXT, selection_reason_code TEXT,
            selection_rank INTEGER, inclusion_probability_ppm INTEGER,
            analysis_weight REAL
        );
        CREATE TABLE text_candidate_corpus_members(
            task_id TEXT, source_post_id INTEGER, source_version INTEGER,
            platform_key TEXT
        );
        CREATE TABLE text_deterministic_results(
            task_id TEXT, source_post_id INTEGER, source_version INTEGER,
            normalized_model_text TEXT
        );
        CREATE TABLE text_post_annotations(annotation_id TEXT);
        """
    )
    members = [
        ("sample-1", 1, 1, "a", "probability", "probability", 1, 500_000, 2.0),
        ("sample-1", 2, 1, "b", "targeted", "targeted", 1, None, None),
    ]
    connection.executemany("INSERT INTO text_sample_members VALUES (?,?,?,?,?,?,?,?,?)", members)
    connection.executemany(
        "INSERT INTO text_candidate_corpus_members VALUES (?,?,?,?)",
        [("det-1", 1, 1, "a"), ("det-2", 2, 1, "b")],
    )
    connection.executemany(
        "INSERT INTO text_deterministic_results VALUES (?,?,?,?)",
        [("det-1", 1, 1, "[TITLE]\n游记"), ("det-2", 2, 1, "[TITLE]\n广告")],
    )
    connection.execute(
        "INSERT INTO text_sampling_runs VALUES (?, 'finalized', ?, 2, 1, 1, ?, ?, ?, ?, ?, 'initial', ?)",
        ("sample-1", "", "candidate-1", "snapshot-1", "text-cleaning-v1.5", 7, "population-hash", "2026-01-01"),
    )
    from tourism_ugc_study.cleaning.reference_evidence import _member_manifest

    member_hash = _member_manifest(connection, "sample-1")
    connection.execute(
        "UPDATE text_sampling_runs SET member_manifest_sha256=?", (member_hash,)
    )
    connection.commit()
    connection.close()

    csv_path = tmp_path / "tourism-relevance-completed.csv"
    rows = [
        ["task-a", "sample-1", "1", "1", "a", "[TITLE]\n游记", "related"],
        ["task-b", "sample-1", "2", "1", "b", "[TITLE]\n广告", "unrelated"],
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("task_id", "sample_run_id", "source_post_id", "source_version", "platform_key", "normalized_model_text", "tourism_label"))
        writer.writerows(rows)
    csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "round-manifest.json"
    manifest_path.write_text(json.dumps({
        "completed_csv": {
            "logical_name": csv_path.name,
            "field_contract": "text-cleaning-post-review-v1.5",
            "row_count": 2,
            "label_counts": {"related": 1, "unrelated": 1, "uncertain": 0},
            "sha256": csv_hash,
        },
        "sample_run": {
            "sample_run_id": "sample-1",
            "population_count": 2,
            "probability_count": 1,
            "targeted_count": 1,
            "member_manifest_sha256": member_hash,
        },
    }, ensure_ascii=False), encoding="utf-8")
    return csv_path, manifest_path, db


def test_reference_evidence_is_readonly_and_migration_manifest_roundtrips(tmp_path: Path) -> None:
    csv_path, manifest_path, db = _build_reference_fixture(tmp_path)
    result = validate_reference_evidence(csv_path, manifest_path, db)
    assert result.row_count == 2
    assert result.frame_counts == {"probability": 1, "targeted": 1}
    assert result.database_annotation_count == 0

    migration_path = tmp_path / "migration.json"
    migration = write_sample_migration_manifest(db, migration_path, sample_run_id="sample-1")
    assert migration.member_count == 2
    assert migration.probability_count == 1
    assert validate_sample_migration_manifest(migration_path, db).output_sha256 == migration.output_sha256

    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0] == 0
    connection.close()


def test_reference_evidence_rejects_normalized_text_tampering(tmp_path: Path) -> None:
    csv_path, manifest_path, db = _build_reference_fixture(tmp_path)
    text = csv_path.read_text(encoding="utf-8").replace("游记", "篡改")
    csv_path.write_text(text, encoding="utf-8")
    with pytest.raises(ReferenceEvidenceError) as error:
        validate_reference_evidence(csv_path, manifest_path, db)
    assert error.value.reason_code == "reference_csv_hash_mismatch"


def test_formal_schema_separates_model_policy_inference_and_evidence(tmp_path: Path) -> None:
    db = tmp_path / "formal.sqlite"
    connection = sqlite3.connect(db)
    migrate_formal_schema(connection)
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "cleaning_model_artifacts",
        "cleaning_threshold_policies",
        "cleaning_inference_runs",
        "cleaning_predictions",
        "cleaning_manual_evidence",
        "cleaning_final_decisions",
    } <= tables
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cleaning_predictions)")
    }
    assert {"p_unrelated", "route_action", "model_id", "policy_id"} <= columns
    assert "platform_key" not in columns
    connection.close()
