"""清洗任务的显式状态转换、恢复和运行级状态汇总。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .config import CleaningConfig
from .schema import connect_derived, migrate_derived


TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"running"}),
    "running": frozenset({"succeeded", "failed", "blocked", "skipped"}),
    "succeeded": frozenset(),
    "failed": frozenset({"pending"}),
    "blocked": frozenset({"pending"}),
    "skipped": frozenset(),
}

DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    "text_relevance": ("text_deterministic",),
    "image_fingerprint": ("image_role",),
    "image_noise": ("image_fingerprint",),
    "finalize": ("text_relevance", "image_noise"),
}

ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class StateTransitionError(RuntimeError):
    """任务转换不满足状态、依赖或重试约束时抛出的异常。"""

    def __init__(self, reason_code: str, message: str = "state transition rejected") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class TaskClaim:
    """计算进程领取任务后获得的最小、去内容化任务描述。"""

    task_id: str
    batch_id: str
    stage_name: str
    object_type: str
    source_object_id: int
    source_version: int
    stage_version: str
    attempt_count: int


@dataclass(frozen=True)
class ResumeSummary:
    """显式恢复操作的结果计数。"""

    batch_id: str
    requeued: int
    exhausted: int
    ignored: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _actor_sha256(actor: str | None) -> str | None:
    if actor is None:
        return None
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()


def _error_digest(summary: str | None) -> str | None:
    """错误详情仅保存摘要哈希，避免正文、作者或令牌进入事件日志。"""

    if not summary:
        return None
    return f"sha256:{hashlib.sha256(summary.encode('utf-8')).hexdigest()}"


def _validate_error_code(error_code: str | None) -> None:
    if error_code is not None and not ERROR_CODE_PATTERN.fullmatch(error_code):
        raise StateTransitionError("invalid_error_code")


def _event(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    old_status: str,
    new_status: str,
    now_utc: str,
    *,
    reason_code: str | None = None,
    actor: str | None = None,
    error_summary: str | None = None,
) -> None:
    """追加一次任务状态事件，绝不修改历史事件。"""

    connection.execute(
        """
        INSERT INTO stage_events(
            run_id, batch_id, task_id, old_status, new_status,
            reason_code, actor_sha256, error_summary, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["run_id"],
            row["batch_id"],
            row["task_id"],
            old_status,
            new_status,
            reason_code,
            _actor_sha256(actor),
            _error_digest(error_summary),
            now_utc,
        ),
    )


def _task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM stage_tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise StateTransitionError("task_not_found")
    return row


def _dependency_ready(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """仅检查同批存在的依赖；未建任务表示沿用既有有效结果。"""

    dependencies = DEPENDENCIES.get(str(row["stage_name"]), ())
    if not dependencies:
        return True
    placeholders = ", ".join("?" for _ in dependencies)
    candidates = connection.execute(
        f"""
        SELECT status FROM stage_tasks
        WHERE batch_id = ? AND object_type = ? AND source_object_id = ?
          AND stage_name IN ({placeholders})
        """,
        (row["batch_id"], row["object_type"], row["source_object_id"], *dependencies),
    ).fetchall()
    return all(candidate["status"] in {"succeeded", "skipped"} for candidate in candidates)


def _propagate_block(
    connection: sqlite3.Connection,
    blocked_row: sqlite3.Row,
    now_utc: str,
    actor: str | None,
) -> None:
    """把前置阻塞递归传播给同对象的待处理下游，避免伪装成永久 pending。"""

    queue = [str(blocked_row["stage_name"])]
    while queue:
        blocked_stage = queue.pop(0)
        candidates = connection.execute(
            """
            SELECT * FROM stage_tasks
            WHERE batch_id = ? AND object_type = ? AND source_object_id = ?
              AND status = 'pending'
            ORDER BY stage_name, task_id
            """,
            (
                blocked_row["batch_id"],
                blocked_row["object_type"],
                blocked_row["source_object_id"],
            ),
        ).fetchall()
        for candidate in candidates:
            stage_name = str(candidate["stage_name"])
            if blocked_stage not in DEPENDENCIES.get(stage_name, ()):
                continue
            connection.execute(
                """
                UPDATE stage_tasks
                SET status = 'blocked', error_code = 'upstream_blocked',
                    completed_at_utc = ?, updated_at_utc = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (now_utc, now_utc, candidate["task_id"]),
            )
            _event(
                connection,
                candidate,
                "pending",
                "blocked",
                now_utc,
                reason_code="upstream_blocked",
                actor=actor,
            )
            queue.append(stage_name)


def _refresh_batch_and_run(
    connection: sqlite3.Connection,
    batch_id: str,
    now_utc: str,
) -> None:
    """根据任务事实更新批次和运行状态，不把科研决策混入调度状态。"""

    batch = connection.execute(
        "SELECT run_id FROM cleaning_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise StateTransitionError("batch_not_found")
    run_id = str(batch["run_id"])
    rows = connection.execute(
        "SELECT status, required, attempt_count, max_attempts FROM stage_tasks WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    exhausted_required = any(
        row["required"] and row["status"] == "failed" and row["attempt_count"] >= row["max_attempts"]
        for row in rows
    )
    recoverable_failure = any(
        row["status"] == "failed" and row["attempt_count"] < row["max_attempts"] for row in rows
    )
    has_block = any(row["status"] == "blocked" for row in rows)
    has_running = any(row["status"] == "running" for row in rows)
    has_pending = any(row["status"] == "pending" for row in rows)
    all_terminal = all(row["status"] in {"succeeded", "failed", "blocked", "skipped"} for row in rows)

    if exhausted_required:
        batch_status, run_status = "failed", "failed"
    elif all_terminal and has_block:
        batch_status, run_status = "completed_with_blocks", "paused"
    elif all_terminal and not recoverable_failure:
        batch_status, run_status = "completed", "running"
    elif recoverable_failure:
        batch_status, run_status = "running", "paused"
    elif has_running or not has_pending:
        batch_status, run_status = "running", "running"
    else:
        batch_status, run_status = "pending", "running"

    if batch_status == "completed":
        unfinished = connection.execute(
            """
            SELECT 1 FROM stage_tasks
            WHERE run_id = ? AND status NOT IN ('succeeded', 'skipped')
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if unfinished is None:
            blocked_batch = connection.execute(
                """
                SELECT 1 FROM cleaning_batches
                WHERE run_id = ? AND status = 'completed_with_blocks' LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            run_status = "paused" if blocked_batch else "accepted"

    connection.execute(
        "UPDATE cleaning_batches SET status = ?, updated_at_utc = ? WHERE batch_id = ?",
        (batch_status, now_utc, batch_id),
    )
    started = now_utc if run_status == "running" else None
    finished = now_utc if run_status in {"accepted", "failed"} else None
    connection.execute(
        """
        UPDATE cleaning_runs
        SET status = ?, started_at_utc = COALESCE(started_at_utc, ?),
            finished_at_utc = ?, updated_at_utc = ?
        WHERE run_id = ?
        """,
        (run_status, started, finished, now_utc, run_id),
    )


def claim_tasks(
    derived_db: str | Path,
    batch_id: str,
    config: CleaningConfig,
    *,
    stage_name: str | None = None,
    actor: str | None = None,
) -> tuple[TaskClaim, ...]:
    """在短事务中领取依赖已满足的任务，随后立即释放数据库写锁。"""

    now_utc = _timestamp(_utcnow())
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                "SELECT status FROM cleaning_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise StateTransitionError("batch_not_found")
            if batch["status"] in {"completed", "completed_with_blocks", "failed"}:
                raise StateTransitionError("batch_not_claimable")
            params: list[object] = [batch_id]
            stage_clause = ""
            if stage_name is not None:
                stage_clause = " AND stage_name = ?"
                params.append(stage_name)
            rows = connection.execute(
                f"""
                SELECT * FROM stage_tasks
                WHERE batch_id = ? AND status = 'pending'{stage_clause}
                ORDER BY source_post_id, object_type, source_object_id, stage_name, task_id
                """,
                params,
            ).fetchall()
            ready = [row for row in rows if _dependency_ready(connection, row)][
                : config.incremental.claim_size
            ]
            claims: list[TaskClaim] = []
            for row in ready:
                attempt_count = max(1, int(row["attempt_count"]))
                connection.execute(
                    """
                    UPDATE stage_tasks
                    SET status = 'running', attempt_count = ?, claimed_at_utc = ?,
                        heartbeat_at_utc = ?, updated_at_utc = ?, error_code = NULL,
                        error_summary = NULL
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (attempt_count, now_utc, now_utc, now_utc, row["task_id"]),
                )
                _event(connection, row, "pending", "running", now_utc, actor=actor)
                claims.append(
                    TaskClaim(
                        task_id=str(row["task_id"]),
                        batch_id=batch_id,
                        stage_name=str(row["stage_name"]),
                        object_type=str(row["object_type"]),
                        source_object_id=int(row["source_object_id"]),
                        source_version=int(row["source_version"]),
                        stage_version=str(row["stage_version"]),
                        attempt_count=attempt_count,
                    )
                )
            if claims:
                _refresh_batch_and_run(connection, batch_id, now_utc)
            connection.commit()
            return tuple(claims)
        except StateTransitionError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise StateTransitionError("claim_transaction_failed") from exc


def heartbeat_task(derived_db: str | Path, task_id: str) -> None:
    """仅更新运行中任务的心跳，不改变状态或尝试次数。"""

    now_utc = _timestamp(_utcnow())
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        with connection:
            cursor = connection.execute(
                """
                UPDATE stage_tasks SET heartbeat_at_utc = ?, updated_at_utc = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (now_utc, now_utc, task_id),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError("task_not_running")


def finish_task(
    derived_db: str | Path,
    task_id: str,
    new_status: str,
    *,
    reason_code: str | None = None,
    error_summary: str | None = None,
    output_sha256: str | None = None,
    actor: str | None = None,
) -> None:
    """把运行中任务置为终态，并立即刷新批次与运行状态。"""

    if new_status not in {"succeeded", "failed", "blocked", "skipped"}:
        raise StateTransitionError("invalid_target_status")
    _validate_error_code(reason_code)
    if output_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", output_sha256):
        raise StateTransitionError("invalid_output_sha256")
    if new_status in {"failed", "blocked", "skipped"} and reason_code is None:
        raise StateTransitionError("reason_code_required")
    now_utc = _timestamp(_utcnow())
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _task_row(connection, task_id)
            old_status = str(row["status"])
            if new_status not in TASK_TRANSITIONS[old_status]:
                _event(
                    connection,
                    row,
                    old_status,
                    new_status,
                    now_utc,
                    reason_code="invalid_task_transition",
                    actor=actor,
                )
                connection.commit()
                raise StateTransitionError("invalid_task_transition")
            error_digest = _error_digest(error_summary)
            connection.execute(
                """
                UPDATE stage_tasks
                SET status = ?, completed_at_utc = ?, error_code = ?,
                    error_summary = ?, output_sha256 = ?, updated_at_utc = ?
                WHERE task_id = ?
                """,
                (
                    new_status,
                    now_utc,
                    reason_code,
                    error_digest,
                    output_sha256,
                    now_utc,
                    task_id,
                ),
            )
            _event(
                connection,
                row,
                old_status,
                new_status,
                now_utc,
                reason_code=reason_code,
                actor=actor,
                error_summary=error_summary,
            )
            if new_status == "blocked":
                _propagate_block(connection, row, now_utc, actor)
            _refresh_batch_and_run(connection, str(row["batch_id"]), now_utc)
            connection.commit()
        except StateTransitionError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise StateTransitionError("finish_transaction_failed") from exc


def resume_batch(
    derived_db: str | Path,
    batch_id: str,
    config: CleaningConfig,
    *,
    include_blocked: bool = True,
    actor: str | None = None,
) -> ResumeSummary:
    """显式重排可恢复失败、已修复阻塞和超时运行任务。"""

    now = _utcnow()
    now_utc = _timestamp(now)
    stale_before = _timestamp(now - timedelta(minutes=config.incremental.stale_after_minutes))
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                "SELECT status FROM cleaning_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise StateTransitionError("batch_not_found")
            rows = connection.execute(
                "SELECT * FROM stage_tasks WHERE batch_id = ? ORDER BY task_id",
                (batch_id,),
            ).fetchall()
            requeued = exhausted = ignored = 0
            for row in rows:
                status = str(row["status"])
                is_stale = status == "running" and (
                    row["heartbeat_at_utc"] is None or row["heartbeat_at_utc"] <= stale_before
                )
                eligible = status == "failed" or is_stale or (include_blocked and status == "blocked")
                if not eligible:
                    ignored += 1
                    continue
                next_attempt = int(row["attempt_count"]) + 1
                if next_attempt > int(row["max_attempts"]):
                    exhausted += 1
                    if status != "failed":
                        connection.execute(
                            """
                            UPDATE stage_tasks
                            SET status = 'failed', error_code = 'max_attempts_exhausted',
                                completed_at_utc = ?, updated_at_utc = ?
                            WHERE task_id = ?
                            """,
                            (now_utc, now_utc, row["task_id"]),
                        )
                        _event(
                            connection,
                            row,
                            status,
                            "failed",
                            now_utc,
                            reason_code="max_attempts_exhausted",
                            actor=actor,
                        )
                    continue
                connection.execute(
                    """
                    UPDATE stage_tasks
                    SET status = 'pending', attempt_count = ?, claimed_at_utc = NULL,
                        heartbeat_at_utc = NULL, completed_at_utc = NULL,
                        error_code = NULL, error_summary = NULL, updated_at_utc = ?
                    WHERE task_id = ?
                    """,
                    (next_attempt, now_utc, row["task_id"]),
                )
                _event(
                    connection,
                    row,
                    status,
                    "pending",
                    now_utc,
                    reason_code="explicit_resume",
                    actor=actor,
                )
                requeued += 1
            _refresh_batch_and_run(connection, batch_id, now_utc)
            connection.commit()
            return ResumeSummary(batch_id, requeued, exhausted, ignored)
        except StateTransitionError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise StateTransitionError("resume_transaction_failed") from exc
