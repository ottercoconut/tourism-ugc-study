from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.scheduler import SchedulerError, create_batch, get_batch_status
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tourism_ugc_study.cleaning.state_machine import (
    StateTransitionError,
    claim_tasks,
    finish_task,
    resume_batch,
)
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _prepared_run(tmp_path: Path, run_id: str = "scheduler-run") -> tuple[Path, object, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / f"{run_id}.source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, run_id)
    discover_increment(derived, snapshot.snapshot_id, config)
    return derived, config, run_id


def test_batch_is_stable_limited_and_immutable(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    first = create_batch(derived, run_id, config, max_posts=2)
    second = create_batch(derived, run_id, config, max_posts=2)

    assert first.post_count == second.post_count == 2
    assert first.task_count == second.task_count == 14
    assert first.sequence_number == 1
    assert second.sequence_number == 2
    assert first.manifest_sha256 != second.manifest_sha256
    with sqlite3.connect(derived) as connection:
        first_posts = connection.execute(
            """
            SELECT DISTINCT t.source_post_id
            FROM cleaning_batch_items AS i JOIN stage_tasks AS t USING(task_id)
            WHERE i.batch_id = ? ORDER BY t.source_post_id
            """,
            (first.batch_id,),
        ).fetchall()
        assert first_posts == [(1,), (2,)]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE cleaning_batches SET manifest_sha256 = ? WHERE batch_id = ?",
                ("f" * 64, first.batch_id),
            )

    with pytest.raises(SchedulerError, match="scheduler operation failed") as error:
        create_batch(derived, run_id, config)
    assert error.value.reason_code == "no_pending_tasks"


def test_identical_inputs_produce_identical_manifest_hash(tmp_path: Path) -> None:
    first_db, config, first_run = _prepared_run(tmp_path / "first", "same-run")
    second_db, _, second_run = _prepared_run(tmp_path / "second", "same-run")

    first = create_batch(first_db, first_run, config)
    second = create_batch(second_db, second_run, config)
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.batch_id == second.batch_id


def test_claim_dependency_events_and_successful_completion(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    batch = create_batch(derived, run_id, config)

    deterministic = claim_tasks(
        derived,
        batch.batch_id,
        config,
        stage_name="text_deterministic",
        actor="worker-one",
    )
    assert len(deterministic) == 4
    assert claim_tasks(
        derived, batch.batch_id, config, stage_name="text_relevance"
    ) == ()
    for task in deterministic:
        finish_task(derived, task.task_id, "succeeded", output_sha256="a" * 64)

    relevance = claim_tasks(derived, batch.batch_id, config, stage_name="text_relevance")
    assert len(relevance) == 4
    with sqlite3.connect(derived) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute(
            "SELECT actor_sha256 FROM stage_events WHERE task_id = ? ORDER BY event_id LIMIT 1",
            (deterministic[0].task_id,),
        ).fetchone()
        assert event["actor_sha256"] is not None
        assert event["actor_sha256"] != "worker-one"


def test_explicit_resume_and_required_retry_exhaustion(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    batch = create_batch(derived, run_id, config)
    task = claim_tasks(
        derived, batch.batch_id, config, stage_name="text_deterministic"
    )[0]
    finish_task(
        derived,
        task.task_id,
        "failed",
        reason_code="worker_error",
        error_summary="正文和作者等敏感详情不应落库",
    )
    assert get_batch_status(derived, batch.batch_id).batch.status == "running"

    first_resume = resume_batch(derived, batch.batch_id, config)
    assert first_resume.requeued == 1
    with sqlite3.connect(derived) as connection:
        row = connection.execute(
            "SELECT status, attempt_count, error_summary FROM stage_tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        assert row[:2] == ("pending", 2)
        assert row[2] is None

    second_claim = claim_tasks(
        derived, batch.batch_id, config, stage_name="text_deterministic"
    )
    retried = next(claim for claim in second_claim if claim.task_id == task.task_id)
    assert retried.attempt_count == 2
    finish_task(derived, task.task_id, "failed", reason_code="worker_error")
    resume_batch(derived, batch.batch_id, config)
    third_claim = claim_tasks(
        derived, batch.batch_id, config, stage_name="text_deterministic"
    )
    retried = next(claim for claim in third_claim if claim.task_id == task.task_id)
    assert retried.attempt_count == 3
    finish_task(derived, task.task_id, "failed", reason_code="worker_error")

    status = get_batch_status(derived, batch.batch_id)
    assert status.batch.status == "failed"
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == "failed"
        summaries = [
            row[0]
            for row in connection.execute(
                "SELECT error_summary FROM stage_events WHERE error_summary IS NOT NULL"
            )
        ]
        assert all("敏感详情" not in value for value in summaries)


def test_stale_running_and_optional_block_require_explicit_resume(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    batch = create_batch(derived, run_id, config, max_posts=1)
    deterministic = claim_tasks(
        derived, batch.batch_id, config, stage_name="text_deterministic"
    )
    finish_task(derived, deterministic[0].task_id, "succeeded")
    relevance = claim_tasks(derived, batch.batch_id, config, stage_name="text_relevance")
    finish_task(derived, relevance[0].task_id, "succeeded")
    post_finalize = [
        task
        for task in claim_tasks(derived, batch.batch_id, config, stage_name="finalize")
        if task.object_type == "post"
    ]
    finish_task(derived, post_finalize[0].task_id, "succeeded")
    image_role = claim_tasks(derived, batch.batch_id, config, stage_name="image_role")
    for task in image_role:
        finish_task(derived, task.task_id, "succeeded")
    fingerprints = claim_tasks(
        derived, batch.batch_id, config, stage_name="image_fingerprint"
    )
    for task in fingerprints:
        finish_task(
            derived,
            task.task_id,
            "blocked",
            reason_code="image_manifest_missing",
        )

    # 图片指纹阻塞会递归阻塞图片噪声和图片 finalize，不残留永久 pending。
    assert get_batch_status(derived, batch.batch_id).batch.status == "completed_with_blocks"
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == "paused"

    resumed = resume_batch(derived, batch.batch_id, config, include_blocked=True)
    assert resumed.requeued == 3

    # 独立批次验证运行中任务只有超过 stale_after_minutes 才会恢复。
    other_db, other_config, other_run = _prepared_run(tmp_path / "stale", "stale-run")
    other_batch = create_batch(other_db, other_run, other_config)
    running = claim_tasks(
        other_db, other_batch.batch_id, other_config, stage_name="text_deterministic"
    )[0]
    recent = resume_batch(other_db, other_batch.batch_id, other_config)
    assert recent.requeued == 0
    stale_time = datetime.now(timezone.utc) - timedelta(
        minutes=other_config.incremental.stale_after_minutes + 1
    )
    with sqlite3.connect(other_db) as connection:
        connection.execute(
            "UPDATE stage_tasks SET heartbeat_at_utc = ? WHERE task_id = ?",
            (stale_time.isoformat(timespec="seconds"), running.task_id),
        )
        connection.commit()
    stale = resume_batch(other_db, other_batch.batch_id, other_config)
    assert stale.requeued == 1


def test_single_writer_lock_rejects_overlapping_batch_transaction(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    with sqlite3.connect(derived, timeout=0) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        # 第二写者受 SQLite 单写者约束；测试只验证锁而不等待 busy_timeout。
        contender = sqlite3.connect(derived, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()


def test_invalid_direct_terminal_transition_is_rejected(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    batch = create_batch(derived, run_id, config)
    with sqlite3.connect(derived) as connection:
        task_id = connection.execute(
            "SELECT task_id FROM stage_tasks WHERE batch_id = ? LIMIT 1",
            (batch.batch_id,),
        ).fetchone()[0]
    with pytest.raises(StateTransitionError) as error:
        finish_task(derived, task_id, "succeeded")
    assert error.value.reason_code == "invalid_task_transition"
    with sqlite3.connect(derived) as connection:
        event = connection.execute(
            """
            SELECT old_status, new_status, reason_code FROM stage_events
            WHERE task_id = ? ORDER BY event_id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        assert event == ("pending", "succeeded", "invalid_task_transition")
