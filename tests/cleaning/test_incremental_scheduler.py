from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.cleaning.config import CleaningConfig, load_config
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.scheduler import SchedulerError, create_batch, get_batch_status
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tourism_ugc_study.cleaning.state_machine import (
    StateTransitionError,
    claim_tasks,
    finish_task,
    heartbeat_task,
    resume_batch,
)
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v3.2.yaml"


def _prepared_run(
    tmp_path: Path,
    run_id: str = "scheduler-run",
) -> tuple[Path, CleaningConfig, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / f"{run_id}.source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, run_id)
    discover_increment(derived, snapshot.snapshot_id, config)
    return derived, config, run_id


def _changed_config(tmp_path: Path) -> CleaningConfig:
    """生成与运行冻结摘要不同、但仍满足公开配置契约的配置。"""

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["incremental"]["claim_size"] += 1
    path = tmp_path / "changed-config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(path)


def _complete_batch(
    derived: Path,
    config: CleaningConfig,
    batch_id: str,
) -> None:
    """按依赖顺序用稳定输出哈希完成一个纯文本批次。"""

    for stage_name in ("text_deterministic", "text_relevance", "finalize"):
        for task in claim_tasks(derived, batch_id, config, stage_name=stage_name):
            finish_task(
                derived,
                task.task_id,
                "succeeded",
                config=config,
                output_sha256="a" * 64,
            )


def test_batch_is_stable_limited_and_immutable(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    first = create_batch(derived, run_id, config, max_posts=2)

    assert first.post_count == 2
    assert first.task_count == 6
    assert first.sequence_number == 1
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
        batched_task = connection.execute(
            "SELECT task_id FROM stage_tasks WHERE batch_id = ? LIMIT 1",
            (first.batch_id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE stage_tasks SET source_version = source_version + 1 WHERE task_id = ?",
                (batched_task,),
            )
        unbatched_task = connection.execute(
            "SELECT task_id FROM stage_tasks WHERE batch_id IS NULL LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="cannot append"):
            connection.execute(
                "UPDATE stage_tasks SET batch_id = ? WHERE task_id = ?",
                (first.batch_id, unbatched_task),
            )

    second = create_batch(derived, run_id, config, max_posts=2)
    assert second.post_count == 2
    assert second.task_count == 6
    assert second.sequence_number == 2
    assert first.manifest_sha256 != second.manifest_sha256

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
    with pytest.raises(StateTransitionError) as missing_output:
        finish_task(derived, deterministic[0].task_id, "succeeded", config=config)
    assert missing_output.value.reason_code == "output_sha256_required"
    assert claim_tasks(
        derived, batch.batch_id, config, stage_name="text_relevance"
    ) == ()
    for task in deterministic:
        finish_task(
            derived,
            task.task_id,
            "succeeded",
            config=config,
            output_sha256="a" * 64,
        )

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
        config=config,
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
    finish_task(
        derived,
        task.task_id,
        "failed",
        config=config,
        reason_code="worker_error",
    )
    resume_batch(derived, batch.batch_id, config)
    third_claim = claim_tasks(
        derived, batch.batch_id, config, stage_name="text_deterministic"
    )
    retried = next(claim for claim in third_claim if claim.task_id == task.task_id)
    assert retried.attempt_count == 3
    finish_task(
        derived,
        task.task_id,
        "failed",
        config=config,
        reason_code="worker_error",
    )

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


def test_run_status_aggregates_completed_batches(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    first_batch = create_batch(derived, run_id, config, max_posts=1)
    successful_batch = create_batch(derived, run_id, config, max_posts=1)

    _complete_batch(derived, config, first_batch.batch_id)
    _complete_batch(derived, config, successful_batch.batch_id)

    with sqlite3.connect(derived) as connection:
        statuses = connection.execute(
            "SELECT status FROM cleaning_batches WHERE run_id = ? ORDER BY sequence_number",
            (run_id,),
        ).fetchall()
        assert statuses == [("completed",), ("completed",)]
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0] == "running"


def test_scheduler_rejects_config_different_from_frozen_run(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    changed_config = _changed_config(tmp_path)
    with pytest.raises(SchedulerError) as create_error:
        create_batch(derived, run_id, changed_config)
    assert create_error.value.reason_code == "run_config_mismatch"

    batch = create_batch(derived, run_id, config, max_posts=1)
    with pytest.raises(StateTransitionError) as claim_error:
        claim_tasks(derived, batch.batch_id, changed_config, stage_name="text_deterministic")
    assert claim_error.value.reason_code == "run_config_mismatch"
    with pytest.raises(StateTransitionError) as resume_error:
        resume_batch(derived, batch.batch_id, changed_config)
    assert resume_error.value.reason_code == "run_config_mismatch"
    running = claim_tasks(
        derived,
        batch.batch_id,
        config,
        stage_name="text_deterministic",
    )[0]
    with pytest.raises(StateTransitionError) as heartbeat_error:
        heartbeat_task(derived, running.task_id, changed_config)
    assert heartbeat_error.value.reason_code == "run_config_mismatch"
    with pytest.raises(StateTransitionError) as finish_error:
        finish_task(
            derived,
            running.task_id,
            "failed",
            config=changed_config,
            reason_code="worker_error",
        )
    assert finish_error.value.reason_code == "run_config_mismatch"


def test_terminal_run_prevents_other_batch_claim_and_resume(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    failed_batch = create_batch(derived, run_id, config, max_posts=1)
    pending_batch = create_batch(derived, run_id, config, max_posts=1)
    task = claim_tasks(
        derived,
        failed_batch.batch_id,
        config,
        stage_name="text_deterministic",
    )[0]
    with sqlite3.connect(derived) as connection:
        connection.execute(
            "UPDATE stage_tasks SET attempt_count = max_attempts WHERE task_id = ?",
            (task.task_id,),
        )
        connection.commit()
    finish_task(
        derived,
        task.task_id,
        "failed",
        config=config,
        reason_code="worker_error",
    )

    with pytest.raises(StateTransitionError) as claim_error:
        claim_tasks(
            derived,
            pending_batch.batch_id,
            config,
            stage_name="text_deterministic",
        )
    assert claim_error.value.reason_code == "run_not_claimable"
    with pytest.raises(StateTransitionError) as resume_error:
        resume_batch(derived, pending_batch.batch_id, config)
    assert resume_error.value.reason_code == "run_not_resumable"


def test_stale_running_requires_explicit_resume(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    batch = create_batch(derived, run_id, config, max_posts=1)
    deterministic = claim_tasks(
        derived, batch.batch_id, config, stage_name="text_deterministic"
    )
    finish_task(
        derived,
        deterministic[0].task_id,
        "succeeded",
        config=config,
        output_sha256="a" * 64,
    )
    relevance = claim_tasks(derived, batch.batch_id, config, stage_name="text_relevance")
    finish_task(
        derived,
        relevance[0].task_id,
        "succeeded",
        config=config,
        output_sha256="a" * 64,
    )
    post_finalize = list(
        claim_tasks(derived, batch.batch_id, config, stage_name="finalize")
    )
    finish_task(
        derived,
        post_finalize[0].task_id,
        "succeeded",
        config=config,
        output_sha256="a" * 64,
    )
    assert get_batch_status(derived, batch.batch_id).batch.status == "completed"
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == "running"

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


def test_successful_tasks_pause_until_explicit_release_acceptance(tmp_path: Path) -> None:
    """计算完成只能进入质量门等待态，不能由调度器自动接受运行。"""

    derived, config, run_id = _prepared_run(tmp_path)
    batch = create_batch(derived, run_id, config)

    _complete_batch(derived, config, batch.batch_id)

    with sqlite3.connect(derived) as connection:
        row = connection.execute(
            "SELECT status, reason_code, finished_at_utc FROM cleaning_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert row == ("paused", "quality_gate_pending", None)


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
    with sqlite3.connect(derived) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_invalid_direct_terminal_transition_is_rejected(tmp_path: Path) -> None:
    derived, config, run_id = _prepared_run(tmp_path)
    batch = create_batch(derived, run_id, config)
    with sqlite3.connect(derived) as connection:
        task_id = connection.execute(
            "SELECT task_id FROM stage_tasks WHERE batch_id = ? LIMIT 1",
            (batch.batch_id,),
        ).fetchone()[0]
    with pytest.raises(StateTransitionError) as error:
        finish_task(
            derived,
            task_id,
            "succeeded",
            config=config,
            output_sha256="a" * 64,
        )
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
        event_id = connection.execute("SELECT MAX(event_id) FROM stage_events").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE stage_events SET reason_code = 'tampered' WHERE event_id = ?",
                (event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM stage_events WHERE event_id = ?", (event_id,))
