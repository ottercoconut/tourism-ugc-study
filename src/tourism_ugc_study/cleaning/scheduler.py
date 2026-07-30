"""增量清洗任务的稳定批次冻结与只读状态汇总。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import CleaningConfig, matches_frozen_run
from .schema import connect_derived, migrate_derived


STAGE_ORDER: Mapping[str, int] = {
    "text_deterministic": 10,
    "text_relevance": 20,
    "image_role": 30,
    "image_fingerprint": 40,
    "image_noise": 50,
    "finalize": 60,
}


class SchedulerError(RuntimeError):
    """批次无法安全创建或读取时抛出的去敏异常。"""

    def __init__(self, reason_code: str, message: str = "scheduler operation failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class BatchSummary:
    """冻结批次的非敏感身份、规模与当前状态。"""

    batch_id: str
    run_id: str
    sequence_number: int
    status: str
    manifest_sha256: str
    post_count: int
    image_count: int
    task_count: int


@dataclass(frozen=True)
class BatchStatus:
    """供命令行查询的批次任务计数，不返回任务输入内容。"""

    batch: BatchSummary
    task_status_counts: Mapping[str, int]
    stage_status_counts: Mapping[str, Mapping[str, int]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _batch_row(connection: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM cleaning_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise SchedulerError("batch_not_found")
    return row


def _summary(row: sqlite3.Row) -> BatchSummary:
    return BatchSummary(
        batch_id=str(row["batch_id"]),
        run_id=str(row["run_id"]),
        sequence_number=int(row["sequence_number"]),
        status=str(row["status"]),
        manifest_sha256=str(row["manifest_sha256"]),
        post_count=int(row["post_count"]),
        image_count=int(row["image_count"]),
        task_count=int(row["task_count"]),
    )


def _select_post_ids(
    connection: sqlite3.Connection,
    run_id: str,
    max_posts: int,
) -> list[int]:
    """按配置约定的稳定键选择仍有未分批任务的帖子。"""

    rows = connection.execute(
        """
        SELECT DISTINCT t.source_post_id, p.captured_at_sort
        FROM stage_tasks AS t
        JOIN source_post_inventory AS p ON p.source_post_id = t.source_post_id
        WHERE t.run_id = ? AND t.status = 'pending' AND t.batch_id IS NULL
        ORDER BY p.captured_at_sort IS NULL, p.captured_at_sort, t.source_post_id
        LIMIT ?
        """,
        (run_id, max_posts),
    )
    return [int(row["source_post_id"]) for row in rows]


def _select_tasks(
    connection: sqlite3.Connection,
    run_id: str,
    source_post_ids: Sequence[int],
) -> list[sqlite3.Row]:
    """选择已入选帖子的全部待处理任务，并用显式阶段序稳定排序。"""

    placeholders = ", ".join("?" for _ in source_post_ids)
    rows = connection.execute(
        f"""
        SELECT task_id, object_type, source_object_id, source_post_id,
               source_version, stage_name, stage_version
        FROM stage_tasks
        WHERE run_id = ? AND status = 'pending' AND batch_id IS NULL
          AND source_post_id IN ({placeholders})
        """,
        (run_id, *source_post_ids),
    ).fetchall()
    return sorted(
        rows,
        key=lambda row: (
            source_post_ids.index(int(row["source_post_id"])),
            0 if row["object_type"] == "post" else 1,
            int(row["source_object_id"]),
            STAGE_ORDER.get(str(row["stage_name"]), 999),
            str(row["stage_name"]),
        ),
    )


def _manifest(tasks: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    """构造不含运行时标识的规范化任务清单，用于可复现哈希。"""

    return [
        {
            "object_type": str(row["object_type"]),
            "source_object_id": int(row["source_object_id"]),
            "source_post_id": int(row["source_post_id"]),
            "source_version": int(row["source_version"]),
            "stage_name": str(row["stage_name"]),
            "stage_version": str(row["stage_version"]),
        }
        for row in tasks
    ]


def create_batch(
    derived_db: str | Path,
    run_id: str,
    config: CleaningConfig,
    *,
    max_posts: int | None = None,
) -> BatchSummary:
    """在单一即时写事务中选择任务、写清单并冻结一个新批次。"""

    limit = config.incremental.max_posts_per_batch if max_posts is None else max_posts
    if limit <= 0 or limit > config.incremental.max_posts_per_batch:
        raise SchedulerError("invalid_batch_size")
    now_utc = _utcnow()
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT status, config_sha256, protocol_version
                FROM cleaning_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise SchedulerError("run_not_found")
            if run["status"] in {"input_rejected", "failed", "aborted", "accepted"}:
                raise SchedulerError("run_not_schedulable")
            if not matches_frozen_run(
                config,
                str(run["config_sha256"]),
                str(run["protocol_version"]),
            ):
                raise SchedulerError("run_config_mismatch")

            post_ids = _select_post_ids(connection, run_id, limit)
            if not post_ids:
                raise SchedulerError("no_pending_tasks")
            tasks = _select_tasks(connection, run_id, post_ids)
            if not tasks:
                raise SchedulerError("no_pending_tasks")

            sequence_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM cleaning_batches WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            manifest_sha256 = _canonical_sha256(_manifest(tasks))
            batch_id = _canonical_sha256(
                {
                    "run_id": run_id,
                    "sequence_number": sequence_number,
                    "manifest_sha256": manifest_sha256,
                }
            )[:32]
            post_count = len({int(row["source_post_id"]) for row in tasks})
            image_count = len(
                {
                    int(row["source_object_id"])
                    for row in tasks
                    if row["object_type"] == "image"
                }
            )
            # frozen_at_utc 先留空，使同一事务可以写入初始清单；事务末尾一次性冻结。
            connection.execute(
                """
                INSERT INTO cleaning_batches(
                    batch_id, run_id, sequence_number, status, manifest_sha256,
                    post_count, image_count, task_count, frozen_at_utc,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    batch_id,
                    run_id,
                    sequence_number,
                    "0" * 64,
                    post_count,
                    image_count,
                    len(tasks),
                    now_utc,
                    now_utc,
                ),
            )
            for item_index, row in enumerate(tasks):
                connection.execute(
                    "UPDATE stage_tasks SET batch_id = ?, updated_at_utc = ? WHERE task_id = ?",
                    (batch_id, now_utc, row["task_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO cleaning_batch_items(
                        batch_id, item_index, task_id, object_type,
                        source_object_id, source_version, stage_name, stage_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        item_index,
                        row["task_id"],
                        row["object_type"],
                        row["source_object_id"],
                        row["source_version"],
                        row["stage_name"],
                        row["stage_version"],
                    ),
                )
            connection.execute(
                """
                UPDATE cleaning_batches
                SET manifest_sha256 = ?, frozen_at_utc = ?, updated_at_utc = ?
                WHERE batch_id = ?
                """,
                (manifest_sha256, now_utc, now_utc, batch_id),
            )
            connection.commit()
        except SchedulerError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise SchedulerError("batch_transaction_failed") from exc
        return _summary(_batch_row(connection, batch_id))


def get_batch_status(derived_db: str | Path, batch_id: str) -> BatchStatus:
    """读取批次及按状态、处理名称聚合的任务计数。"""

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        batch = _summary(_batch_row(connection, batch_id))
        task_counts = Counter(
            {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM stage_tasks
                    WHERE batch_id = ? GROUP BY status
                    """,
                    (batch_id,),
                )
            }
        )
        stage_counts: dict[str, dict[str, int]] = {}
        for row in connection.execute(
            """
            SELECT stage_name, status, COUNT(*) AS count FROM stage_tasks
            WHERE batch_id = ? GROUP BY stage_name, status
            ORDER BY stage_name, status
            """,
            (batch_id,),
        ):
            stage_counts.setdefault(str(row["stage_name"]), {})[str(row["status"])] = int(
                row["count"]
            )
        return BatchStatus(batch, dict(task_counts), stage_counts)
