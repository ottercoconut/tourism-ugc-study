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
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"
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
        assert connection.execute(
            "SELECT COUNT(*) FROM text_candidate_corpus_members WHERE build_id = ?",
            (build.build_id,),
        ).fetchone()[0] == 4
        assert connection.execute(
            """
            SELECT structure_status, exact_canonical_sha256
            FROM text_deterministic_results WHERE source_post_id = 4
            """
        ).fetchone() == ("invalid", None)
        assert connection.execute(
            """
            SELECT structure_status FROM text_deterministic_results
            WHERE source_post_id = 2
            """
        ).fetchone()[0] == "usable"
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
