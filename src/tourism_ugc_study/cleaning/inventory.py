"""源对象分轴指纹、版本登记与增量任务发现。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .config import CleaningConfig
from .fingerprints import canonical_sha256, image_fingerprints, post_fingerprints
from .schema import DERIVED_SCHEMA_VERSION, connect_derived, migrate_derived
from .snapshot import open_source_readonly, sha256_file
from .task_plan import (
    IMAGE_STAGES,
    POST_STAGES,
    algorithm_affected_stages,
    effective_stage_version,
    image_affected_stages,
    post_affected_stages,
    stage_required,
)


class InventoryError(RuntimeError):
    """增量发现无法安全继续时抛出的去敏异常。"""

    def __init__(self, reason_code: str, message: str = "increment discovery failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class DiscoverySummary:
    """一次增量发现的计数摘要，不包含正文、作者或源路径。"""

    run_id: str
    snapshot_id: str
    post_changes: Mapping[str, int]
    image_changes: Mapping[str, int]
    tasks_created: int


def _task_id(
    run_id: str,
    stage_name: str,
    object_type: str,
    source_object_id: int,
    source_version: int,
    stage_version: str,
) -> str:
    """从任务身份字段生成稳定 ID，使重复发现天然幂等。"""

    return canonical_sha256(
        {
            "run_id": run_id,
            "stage_name": stage_name,
            "object_type": object_type,
            "source_object_id": source_object_id,
            "source_version": source_version,
            "stage_version": stage_version,
        }
    )[:32]


def _enqueue_tasks(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    object_type: str,
    source_object_id: int,
    source_post_id: int,
    source_version: int,
    stages: Iterable[str],
    affected_stages: set[str],
    is_new: bool,
    config: CleaningConfig,
    now_utc: str,
    known_stage_versions: set[tuple[str, str, int, str]],
) -> int:
    """借助预加载版本集合，仅为受影响处理或新算法版本建立任务。"""

    created = 0
    changed_algorithms = {
        stage_name
        for stage_name in stages
        if (
            stage_name,
            object_type,
            source_object_id,
            effective_stage_version(config.algorithm_versions, object_type, stage_name),
        )
        not in known_stage_versions
    }
    scheduled_stages = affected_stages | algorithm_affected_stages(
        object_type,
        changed_algorithms,
    )
    for stage_name in stages:
        stage_version = effective_stage_version(
            config.algorithm_versions,
            object_type,
            stage_name,
        )
        version_key = (stage_name, object_type, source_object_id, stage_version)
        if not (is_new or stage_name in scheduled_stages):
            continue
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO stage_tasks(
                task_id, run_id, stage_name, object_type, source_object_id,
                source_post_id, source_version, stage_version, required, status,
                max_attempts, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                _task_id(
                    run_id,
                    stage_name,
                    object_type,
                    source_object_id,
                    source_version,
                    stage_version,
                ),
                run_id,
                stage_name,
                object_type,
                source_object_id,
                source_post_id,
                source_version,
                stage_version,
                stage_required(object_type, stage_name),
                config.incremental.max_attempts,
                now_utc,
                now_utc,
            ),
        )
        created += cursor.rowcount
        known_stage_versions.add(version_key)
    return created


def _source_snapshot_row(
    connection: sqlite3.Connection,
    snapshot_id: str,
    config: CleaningConfig,
) -> sqlite3.Row:
    """解析显式快照并核对冻结配置；禁止猜测“最新快照”。"""

    row = connection.execute(
        """
        SELECT s.snapshot_id, s.run_id, s.snapshot_path, s.snapshot_sha256,
               s.input_contract_status, r.status AS run_status,
               r.config_sha256, r.protocol_version
        FROM source_snapshots AS s
        JOIN cleaning_runs AS r ON r.run_id = s.run_id
        WHERE s.snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise InventoryError("snapshot_not_found")
    if row["input_contract_status"] != "accepted" or row["run_status"] == "input_rejected":
        raise InventoryError("snapshot_input_rejected")
    if row["config_sha256"] != config.sha256 or row["protocol_version"] != config.protocol_version:
        raise InventoryError("run_config_mismatch")
    if config.algorithm_versions.get("derived_schema") != DERIVED_SCHEMA_VERSION:
        raise InventoryError("derived_schema_version_mismatch")
    return row


def _discover_posts(
    derived: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    snapshot_id: str,
    run_id: str,
    config: CleaningConfig,
    now_utc: str,
    known_stage_versions: set[tuple[str, str, int, str]],
) -> tuple[Counter[str], int, set[int]]:
    """登记帖子观察和版本，并返回变化计数、任务数及本次出现的 ID。"""

    counts: Counter[str] = Counter()
    tasks_created = 0
    seen: set[int] = set()
    rows = source.execute('SELECT * FROM web_posts ORDER BY id')
    for row in rows:
        source_post_id = int(row["id"])
        seen.add(source_post_id)
        text_sha, author_sha, analysis_sha = post_fingerprints(row)
        current = derived.execute(
            "SELECT * FROM source_post_inventory WHERE source_post_id = ?",
            (source_post_id,),
        ).fetchone()
        is_new = current is None
        changed_axes: set[str] = set()
        if is_new:
            source_version = 1
            change_kind = "new"
            changed_axes.update(("text", "author", "analysis"))
            derived.execute(
                """
                INSERT INTO source_post_inventory(
                    source_post_id, platform_key, first_seen_snapshot_id,
                    last_seen_snapshot_id, is_present, current_source_version,
                    current_text_sha256, current_author_sha256,
                    current_analysis_sha256, captured_at_sort, updated_at_utc
                ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?)
                """,
                (
                    source_post_id,
                    str(row["platform_key"]),
                    snapshot_id,
                    snapshot_id,
                    text_sha,
                    author_sha,
                    analysis_sha,
                    row["captured_at"],
                    now_utc,
                ),
            )
        else:
            source_version = int(current["current_source_version"])
            if text_sha != current["current_text_sha256"]:
                changed_axes.add("text")
            if author_sha != current["current_author_sha256"]:
                changed_axes.add("author")
            if analysis_sha != current["current_analysis_sha256"]:
                changed_axes.add("analysis")
            if changed_axes:
                source_version += 1
                change_kind = (
                    "cleaning_changed"
                    if changed_axes.intersection({"text", "author"})
                    else "analysis_only"
                )
            else:
                change_kind = "unchanged"
            derived.execute(
                """
                UPDATE source_post_inventory
                SET platform_key = ?, last_seen_snapshot_id = ?,
                    missing_since_snapshot_id = NULL, is_present = 1,
                    current_source_version = ?, current_text_sha256 = ?,
                    current_author_sha256 = ?, current_analysis_sha256 = ?,
                    captured_at_sort = ?, updated_at_utc = ?
                WHERE source_post_id = ?
                """,
                (
                    str(row["platform_key"]),
                    snapshot_id,
                    source_version,
                    text_sha,
                    author_sha,
                    analysis_sha,
                    row["captured_at"],
                    now_utc,
                    source_post_id,
                ),
            )

        if is_new or changed_axes:
            derived.execute(
                """
                INSERT INTO source_post_versions(
                    source_post_id, source_version, effective_snapshot_id,
                    text_sha256, author_sha256, analysis_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_post_id,
                    source_version,
                    snapshot_id,
                    text_sha,
                    author_sha,
                    analysis_sha,
                    now_utc,
                ),
            )
        derived.execute(
            """
            INSERT INTO source_post_observations(
                snapshot_id, source_post_id, source_version, change_kind,
                changed_axes_json, observed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                source_post_id,
                source_version,
                change_kind,
                json.dumps(sorted(changed_axes), ensure_ascii=False),
                now_utc,
            ),
        )
        counts[change_kind] += 1
        tasks_created += _enqueue_tasks(
            derived,
            run_id=run_id,
            object_type="post",
            source_object_id=source_post_id,
            source_post_id=source_post_id,
            source_version=source_version,
            stages=POST_STAGES,
            affected_stages=post_affected_stages(changed_axes),
            is_new=is_new,
            config=config,
            now_utc=now_utc,
            known_stage_versions=known_stage_versions,
        )

    missing_rows = derived.execute(
        "SELECT source_post_id, current_source_version FROM source_post_inventory"
    ).fetchall()
    for missing in missing_rows:
        source_post_id = int(missing["source_post_id"])
        if source_post_id in seen:
            continue
        derived.execute(
            """
            UPDATE source_post_inventory
            SET is_present = 0,
                missing_since_snapshot_id = COALESCE(missing_since_snapshot_id, ?),
                updated_at_utc = ?
            WHERE source_post_id = ?
            """,
            (snapshot_id, now_utc, source_post_id),
        )
        derived.execute(
            """
            INSERT INTO source_post_observations(
                snapshot_id, source_post_id, source_version, change_kind,
                changed_axes_json, observed_at_utc
            ) VALUES (?, ?, ?, 'missing', '[]', ?)
            """,
            (snapshot_id, source_post_id, int(missing["current_source_version"]), now_utc),
        )
        counts["missing"] += 1
    return counts, tasks_created, seen


def _discover_images(
    derived: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    snapshot_id: str,
    run_id: str,
    config: CleaningConfig,
    now_utc: str,
    known_stage_versions: set[tuple[str, str, int, str]],
) -> tuple[Counter[str], int]:
    """登记图片关系观察和版本，图片路径与 URL 均只进入指纹。"""

    counts: Counter[str] = Counter()
    tasks_created = 0
    seen: set[int] = set()
    rows = source.execute('SELECT * FROM web_post_images ORDER BY web_post_id, image_index, id')
    for row in rows:
        source_image_id = int(row["id"])
        source_post_id = int(row["web_post_id"])
        seen.add(source_image_id)
        relation_sha, file_sha = image_fingerprints(row)
        current = derived.execute(
            "SELECT * FROM source_image_inventory WHERE source_image_id = ?",
            (source_image_id,),
        ).fetchone()
        is_new = current is None
        changed_axes: set[str] = set()
        if is_new:
            source_version = 1
            change_kind = "new"
            changed_axes.update(("relation", "file"))
            derived.execute(
                """
                INSERT INTO source_image_inventory(
                    source_image_id, source_post_id, first_seen_snapshot_id,
                    last_seen_snapshot_id, is_present, current_source_version,
                    current_relation_sha256, current_file_sha256,
                    image_index_sort, updated_at_utc
                ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?)
                """,
                (
                    source_image_id,
                    source_post_id,
                    snapshot_id,
                    snapshot_id,
                    relation_sha,
                    file_sha,
                    int(row["image_index"]),
                    now_utc,
                ),
            )
        else:
            source_version = int(current["current_source_version"])
            if relation_sha != current["current_relation_sha256"]:
                changed_axes.add("relation")
            if file_sha != current["current_file_sha256"]:
                changed_axes.add("file")
            if changed_axes:
                source_version += 1
                change_kind = "cleaning_changed"
            else:
                change_kind = "unchanged"
            derived.execute(
                """
                UPDATE source_image_inventory
                SET source_post_id = ?, last_seen_snapshot_id = ?,
                    missing_since_snapshot_id = NULL, is_present = 1,
                    current_source_version = ?, current_relation_sha256 = ?,
                    current_file_sha256 = ?, image_index_sort = ?, updated_at_utc = ?
                WHERE source_image_id = ?
                """,
                (
                    source_post_id,
                    snapshot_id,
                    source_version,
                    relation_sha,
                    file_sha,
                    int(row["image_index"]),
                    now_utc,
                    source_image_id,
                ),
            )

        if is_new or changed_axes:
            derived.execute(
                """
                INSERT INTO source_image_versions(
                    source_image_id, source_version, effective_snapshot_id,
                    relation_sha256, file_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_image_id, source_version, snapshot_id, relation_sha, file_sha, now_utc),
            )
        derived.execute(
            """
            INSERT INTO source_image_observations(
                snapshot_id, source_image_id, source_version, change_kind,
                changed_axes_json, observed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                source_image_id,
                source_version,
                change_kind,
                json.dumps(sorted(changed_axes), ensure_ascii=False),
                now_utc,
            ),
        )
        counts[change_kind] += 1
        tasks_created += _enqueue_tasks(
            derived,
            run_id=run_id,
            object_type="image",
            source_object_id=source_image_id,
            source_post_id=source_post_id,
            source_version=source_version,
            stages=IMAGE_STAGES,
            affected_stages=image_affected_stages(changed_axes),
            is_new=is_new,
            config=config,
            now_utc=now_utc,
            known_stage_versions=known_stage_versions,
        )

    missing_rows = derived.execute(
        "SELECT source_image_id, current_source_version FROM source_image_inventory"
    ).fetchall()
    for missing in missing_rows:
        source_image_id = int(missing["source_image_id"])
        if source_image_id in seen:
            continue
        derived.execute(
            """
            UPDATE source_image_inventory
            SET is_present = 0,
                missing_since_snapshot_id = COALESCE(missing_since_snapshot_id, ?),
                updated_at_utc = ?
            WHERE source_image_id = ?
            """,
            (snapshot_id, now_utc, source_image_id),
        )
        derived.execute(
            """
            INSERT INTO source_image_observations(
                snapshot_id, source_image_id, source_version, change_kind,
                changed_axes_json, observed_at_utc
            ) VALUES (?, ?, ?, 'missing', '[]', ?)
            """,
            (snapshot_id, source_image_id, int(missing["current_source_version"]), now_utc),
        )
        counts["missing"] += 1
    return counts, tasks_created


def _existing_summary(
    connection: sqlite3.Connection,
    snapshot_id: str,
    run_id: str,
) -> DiscoverySummary | None:
    """重复调用同一快照时返回既有结果，避免新增版本或任务。"""

    discovery = connection.execute(
        """
        SELECT post_changes_json, image_changes_json, tasks_created
        FROM inventory_discoveries WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if discovery is None:
        return None
    return DiscoverySummary(
        run_id,
        snapshot_id,
        json.loads(discovery["post_changes_json"]),
        json.loads(discovery["image_changes_json"]),
        int(discovery["tasks_created"]),
    )


def _current_results_reusable(
    connection: sqlite3.Connection,
    config: CleaningConfig,
) -> bool:
    """核对当前在场对象的每个处理版本是否已有可复用完成结果。"""

    reusable: set[tuple[str, str, int, str, str, str]] = set()
    completed_clause = """
        (t.status = 'succeeded' AND t.output_sha256 IS NOT NULL)
        OR (t.status = 'skipped' AND t.error_code IS NOT NULL)
    """
    for row in connection.execute(
        f"""
        SELECT t.stage_name, t.source_object_id, t.stage_version,
               v.text_sha256, v.author_sha256
        FROM stage_tasks AS t
        JOIN source_post_versions AS v
          ON v.source_post_id = t.source_object_id
         AND v.source_version = t.source_version
        WHERE t.object_type = 'post' AND ({completed_clause})
        """
    ):
        stage_name = str(row["stage_name"])
        secondary = "" if stage_name == "text_deterministic" else str(row["author_sha256"])
        reusable.add(
            (
                stage_name,
                "post",
                int(row["source_object_id"]),
                str(row["stage_version"]),
                str(row["text_sha256"]),
                secondary,
            )
        )
    for row in connection.execute(
        f"""
        SELECT t.stage_name, t.source_object_id, t.stage_version,
               v.relation_sha256, v.file_sha256
        FROM stage_tasks AS t
        JOIN source_image_versions AS v
          ON v.source_image_id = t.source_object_id
         AND v.source_version = t.source_version
        WHERE t.object_type = 'image' AND ({completed_clause})
        """
    ):
        stage_name = str(row["stage_name"])
        secondary = "" if stage_name == "image_role" else str(row["file_sha256"])
        reusable.add(
            (
                stage_name,
                "image",
                int(row["source_object_id"]),
                str(row["stage_version"]),
                str(row["relation_sha256"]),
                secondary,
            )
        )

    required: set[tuple[str, str, int, str, str, str]] = set()
    for row in connection.execute(
        """
        SELECT source_post_id, current_text_sha256, current_author_sha256
        FROM source_post_inventory WHERE is_present = 1
        """
    ):
        source_object_id = int(row["source_post_id"])
        required.update(
            (
                stage_name,
                "post",
                source_object_id,
                effective_stage_version(config.algorithm_versions, "post", stage_name),
                str(row["current_text_sha256"]),
                "" if stage_name == "text_deterministic" else str(row["current_author_sha256"]),
            )
            for stage_name in POST_STAGES
        )
    for row in connection.execute(
        """
        SELECT source_image_id, current_relation_sha256, current_file_sha256
        FROM source_image_inventory WHERE is_present = 1
        """
    ):
        source_object_id = int(row["source_image_id"])
        required.update(
            (
                stage_name,
                "image",
                source_object_id,
                effective_stage_version(config.algorithm_versions, "image", stage_name),
                str(row["current_relation_sha256"]),
                "" if stage_name == "image_role" else str(row["current_file_sha256"]),
            )
            for stage_name in IMAGE_STAGES
        )
    return required <= reusable


def discover_increment(
    derived_db: str | Path,
    snapshot_id: str,
    config: CleaningConfig,
) -> DiscoverySummary:
    """比较显式源快照并原子登记对象、版本、观察和最小任务集合。"""

    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_derived(derived_db) as derived:
        migrate_derived(derived)
        snapshot = _source_snapshot_row(derived, snapshot_id, config)
        run_id = str(snapshot["run_id"])
        existing = _existing_summary(derived, snapshot_id, run_id)
        if existing is not None:
            return existing
        if snapshot["run_status"] != "planned":
            raise InventoryError("run_not_discoverable")
        try:
            source_path = Path(snapshot["snapshot_path"])
            if sha256_file(source_path) != snapshot["snapshot_sha256"]:
                raise InventoryError("snapshot_hash_mismatch")
            with open_source_readonly(source_path) as source:
                derived.execute("BEGIN IMMEDIATE")
                # 单次加载历史版本，将大批量发现从逐任务查询降为集合查找。
                known_stage_versions = {
                    (
                        str(row["stage_name"]),
                        str(row["object_type"]),
                        int(row["source_object_id"]),
                        str(row["stage_version"]),
                    )
                    for row in derived.execute(
                        """
                        SELECT stage_name, object_type, source_object_id, stage_version
                        FROM stage_tasks
                        """
                    )
                }
                post_counts, post_tasks, _ = _discover_posts(
                    derived,
                    source,
                    snapshot_id=snapshot_id,
                    run_id=run_id,
                    config=config,
                    now_utc=now_utc,
                    known_stage_versions=known_stage_versions,
                )
                image_counts, image_tasks = _discover_images(
                    derived,
                    source,
                    snapshot_id=snapshot_id,
                    run_id=run_id,
                    config=config,
                    now_utc=now_utc,
                    known_stage_versions=known_stage_versions,
                )
                tasks_created = post_tasks + image_tasks
                derived.execute(
                    """
                    INSERT INTO inventory_discoveries(
                        snapshot_id, run_id, post_changes_json, image_changes_json,
                        tasks_created, completed_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        run_id,
                        json.dumps(dict(post_counts), ensure_ascii=False, sort_keys=True),
                        json.dumps(dict(image_counts), ensure_ascii=False, sort_keys=True),
                        tasks_created,
                        now_utc,
                    ),
                )
                if tasks_created == 0:
                    reusable = _current_results_reusable(derived, config)
                    # 不复制未完成历史任务；明确暂停并要求恢复原运行。
                    derived.execute(
                        """
                        UPDATE cleaning_runs
                        SET status = ?, reason_code = ?,
                            started_at_utc = COALESCE(started_at_utc, ?),
                            finished_at_utc = ?, updated_at_utc = ?
                        WHERE run_id = ?
                        """,
                        (
                            "accepted" if reusable else "paused",
                            None if reusable else "prior_tasks_incomplete",
                            now_utc,
                            now_utc if reusable else None,
                            now_utc,
                            run_id,
                        ),
                    )
                derived.commit()
        except InventoryError:
            derived.rollback()
            raise
        except (OSError, sqlite3.Error) as exc:
            derived.rollback()
            raise InventoryError("inventory_transaction_failed") from exc
    return DiscoverySummary(
        run_id=run_id,
        snapshot_id=snapshot_id,
        post_changes=dict(post_counts),
        image_changes=dict(image_counts),
        tasks_created=tasks_created,
    )
