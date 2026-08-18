from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.scheduler import create_batch
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tourism_ugc_study.cleaning.state_machine import claim_tasks
from tourism_ugc_study.cleaning.text_config import load_text_config
from tourism_ugc_study.cleaning.text_repository import (
    TextRepositoryError,
    build_text_candidates,
    process_text_tasks,
)
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v3.2.yaml"
TEXT_CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-text-normalization-v1.yaml"


def _prepared_text_run(tmp_path: Path) -> tuple[Path, object, object, str, str]:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO source_platforms VALUES ('weibo')")
        connection.execute(
            """
            UPDATE web_posts SET title = NULL, content_text = ?, platform_key = 'xhs'
            WHERE id = 1
            """,
            ("青岛三天两夜旅游路线推荐",),
        )
        connection.execute(
            """
            UPDATE web_posts SET title = NULL, content_text = ?, platform_key = 'weibo'
            WHERE id = 2
            """,
            ("青岛三天两夜旅游路线推荐",),
        )
        connection.execute(
            "UPDATE web_posts SET title = NULL, content_text = ? WHERE id = 3",
            ("青岛三天两夜旅游路线攻略",),
        )
        connection.execute(
            "UPDATE web_posts SET title = NULL, content_text = '请先登录' WHERE id = 4"
        )
    config = load_config(CONFIG_PATH)
    text_config = load_text_config(
        TEXT_CONFIG_PATH,
        expected_version_lock=str(config.algorithm_versions["text_normalization"]),
    )
    snapshot = snapshot_source(source, derived, config, "text-run")
    discover_increment(derived, snapshot.snapshot_id, config)
    batch = create_batch(derived, "text-run", config)
    return derived, config, text_config, snapshot.snapshot_id, batch.batch_id


def test_process_text_tasks_and_build_candidates_are_traceable(tmp_path: Path) -> None:
    derived, config, text_config, snapshot_id, batch_id = _prepared_text_run(tmp_path)
    claims = claim_tasks(derived, batch_id, config, stage_name="text_deterministic")

    processed = process_text_tasks(
        derived,
        claims,
        config=config,
        text_config=text_config,
        actor="test-worker",
    )
    build = build_text_candidates(
        derived,
        run_id="text-run",
        snapshot_id=snapshot_id,
        config=config,
        text_config=text_config,
    )
    repeated = build_text_candidates(
        derived,
        run_id="text-run",
        snapshot_id=snapshot_id,
        config=config,
        text_config=text_config,
    )

    assert len(processed.succeeded) == 4
    assert processed.failed_task_ids == ()
    assert build == repeated
    assert build.expected_post_count == build.processed_post_count == 4
    assert build.usable_post_count == 3
    assert build.is_complete_corpus is True
    assert build.exact_duplicate_cluster_count == 1
    assert build.exact_cross_platform_cluster_count == 1
    assert build.exact_cross_platform_member_count == 2
    assert build.near_candidate_pair_count == 1
    with sqlite3.connect(derived) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute(
            "SELECT COUNT(*) FROM text_candidate_corpus_members WHERE build_id = ?",
            (build.build_id,),
        ).fetchone()[0] == 4
        assert tuple(
            connection.execute(
                """
                SELECT structure_status, exact_canonical_sha256
                FROM text_deterministic_results WHERE source_post_id = 4
                """
            ).fetchone()
        ) == ("invalid", None)
        assert connection.execute(
            """
            SELECT structure_status FROM text_deterministic_results
            WHERE source_post_id = 2
            """
        ).fetchone()[0] == "usable"
        result_runtime = connection.execute(
            """
            SELECT runtime_versions_json, runtime_sha256
            FROM text_deterministic_results WHERE source_post_id = 1
            """
        ).fetchone()
        build_runtime = connection.execute(
            """
            SELECT status, library_versions_json, runtime_sha256
            FROM text_candidate_builds WHERE build_id = ?
            """,
            (build.build_id,),
        ).fetchone()
        assert json.loads(result_runtime["runtime_versions_json"])["regex"]
        assert result_runtime["runtime_sha256"] == build_runtime["runtime_sha256"]
        assert build_runtime["status"] == "finalized"
        assert json.loads(build_runtime["library_versions_json"])["scipy"]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE text_deterministic_results SET normalized_title = 'forbidden'"
            )


def test_complete_candidate_build_rejects_unprocessed_corpus(tmp_path: Path) -> None:
    derived, config, text_config, snapshot_id, batch_id = _prepared_text_run(tmp_path)
    claims = claim_tasks(derived, batch_id, config, stage_name="text_deterministic")
    process_text_tasks(
        derived,
        claims[:2],
        config=config,
        text_config=text_config,
    )

    with pytest.raises(TextRepositoryError) as error:
        build_text_candidates(
            derived,
            run_id="text-run",
            snapshot_id=snapshot_id,
            config=config,
            text_config=text_config,
        )
    assert error.value.reason_code == "candidate_corpus_incomplete"

    partial = build_text_candidates(
        derived,
        run_id="text-run",
        snapshot_id=snapshot_id,
        config=config,
        text_config=text_config,
        allow_partial=True,
    )
    assert partial.processed_post_count == 2
    assert partial.is_complete_corpus is False


def test_text_cli_processes_batch_and_builds_explicit_snapshot(tmp_path: Path) -> None:
    derived, _, _, snapshot_id, batch_id = _prepared_text_run(tmp_path)
    common = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "cleaning_process_text.py"),
        "--derived-db",
        str(derived),
        "--config",
        str(CONFIG_PATH),
        "--text-config",
        str(TEXT_CONFIG_PATH),
    ]

    process = subprocess.run(
        [*common, "process", "--batch-id", batch_id, "--drain"],
        check=True,
        capture_output=True,
        text=True,
    )
    candidates = subprocess.run(
        [
            *common,
            "build-candidates",
            "--run-id",
            "text-run",
            "--snapshot-id",
            snapshot_id,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    process_payload = json.loads(process.stdout)
    candidate_payload = json.loads(candidates.stdout)
    assert process_payload["succeeded"] == 4
    assert process_payload["structure_status_counts"] == {"invalid": 1, "usable": 3}
    assert candidate_payload["processed_post_count"] == 4
    assert "青岛三天两夜" not in process.stdout + candidates.stdout
    assert str(tmp_path) not in process.stdout + candidates.stdout


def test_candidate_corpus_only_reuses_successful_matching_checkpoints(tmp_path: Path) -> None:
    derived, config, text_config, snapshot_id, batch_id = _prepared_text_run(tmp_path)
    claims = claim_tasks(derived, batch_id, config, stage_name="text_deterministic")
    process_text_tasks(derived, claims, config=config, text_config=text_config)
    # 模拟“结果已落盘、任务完成事务尚未成功”的崩溃窗口。
    with sqlite3.connect(derived) as connection:
        connection.execute(
            """
            UPDATE stage_tasks
            SET status = 'running', output_sha256 = NULL, completed_at_utc = NULL
            WHERE task_id = ?
            """,
            (claims[0].task_id,),
        )

    with pytest.raises(TextRepositoryError) as error:
        build_text_candidates(
            derived,
            run_id="text-run",
            snapshot_id=snapshot_id,
            config=config,
            text_config=text_config,
        )
    assert error.value.reason_code == "candidate_corpus_incomplete"


def test_finalized_candidate_build_rejects_late_child_inserts(tmp_path: Path) -> None:
    derived, config, text_config, snapshot_id, batch_id = _prepared_text_run(tmp_path)
    claims = claim_tasks(derived, batch_id, config, stage_name="text_deterministic")
    process_text_tasks(derived, claims, config=config, text_config=text_config)
    build = build_text_candidates(
        derived,
        run_id="text-run",
        snapshot_id=snapshot_id,
        config=config,
        text_config=text_config,
    )

    with sqlite3.connect(derived) as connection:
        row = connection.execute(
            "SELECT * FROM text_candidate_corpus_members WHERE build_id = ? LIMIT 1",
            (build.build_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            connection.execute(
                """
                INSERT INTO text_candidate_corpus_members(
                    build_id, task_id, source_post_id, source_version,
                    platform_key, structure_status, exact_cluster_id,
                    is_near_representative
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )


def test_candidate_build_reuses_successful_result_across_snapshots(tmp_path: Path) -> None:
    derived, config, text_config, _, batch_id = _prepared_text_run(tmp_path)
    claims = claim_tasks(derived, batch_id, config, stage_name="text_deterministic")
    process_text_tasks(derived, claims, config=config, text_config=text_config)
    second = snapshot_source(
        tmp_path / "source.sqlite",
        derived,
        config,
        "text-run-second",
    )
    discover_increment(derived, second.snapshot_id, config)

    build = build_text_candidates(
        derived,
        run_id="text-run-second",
        snapshot_id=second.snapshot_id,
        config=config,
        text_config=text_config,
    )

    assert build.processed_post_count == build.expected_post_count == 4
    assert build.is_complete_corpus is True


def test_text_result_schema_has_no_relevance_or_final_decision_fields(tmp_path: Path) -> None:
    derived, _, _, _, _ = _prepared_text_run(tmp_path)
    forbidden = {"relevance", "commercial", "advertising", "cleaning_decision", "keep"}
    with sqlite3.connect(derived) as connection:
        columns = {
            str(row[1])
            for table in (
                "text_deterministic_results",
                "text_candidate_builds",
                "text_candidate_corpus_members",
                "text_exact_clusters",
                "text_near_candidate_pairs",
            )
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
    assert columns.isdisjoint(forbidden)
