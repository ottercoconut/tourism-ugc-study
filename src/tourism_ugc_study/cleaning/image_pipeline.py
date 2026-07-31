"""图片证据操作与增量任务状态机之间的薄编排层。

文件解析、指纹计算和候选算法分别留在专用模块；本模块只负责领取对应任务，
把已持久化 outcome 转成合法终态，并保留无 manifest 时的显式阻塞/恢复语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import CleaningConfig
from .image_repository import (
    ImageRepositoryError,
    load_image_stage_snapshot,
    record_manifest_block,
)
from .scheduler import get_batch_status
from .state_machine import claim_tasks, finish_task


@dataclass(frozen=True)
class ImageStageRunResult:
    """一次图片子阶段与调度任务同步后的计数。"""

    batch_id: str
    stage: str
    claimed_count: int
    succeeded_count: int
    blocked_count: int
    skipped_count: int


_TASK_STAGE = {
    "roles": "image_role",
    "fingerprints": "image_fingerprint",
    "candidates": "image_noise",
}


def sync_image_stage_tasks(
    derived_db: str | Path,
    *,
    batch_id: str,
    stage: str,
    manifest_id: str | None,
    config: CleaningConfig,
    build_id: str | None = None,
    blocking_reason_code: str | None = None,
    actor: str | None = None,
) -> ImageStageRunResult:
    """领取一个图片子阶段并依据不可变证据完成、跳过或阻塞任务。

    缺少 manifest 时只阻塞图片对象及其图片下游；文本对象依赖链独立，仍可
    正常领取和完成。下载完成后必须先显式 resume，再次调用本函数。
    """

    try:
        task_stage = _TASK_STAGE[stage]
    except KeyError as exc:
        raise ImageRepositoryError("image_stage_invalid") from exc
    batch = get_batch_status(derived_db, batch_id).batch
    if blocking_reason_code is not None:
        outcome_by_id = None
    elif manifest_id is None:
        record_manifest_block(
            derived_db,
            run_id=batch.run_id,
            operation=stage,
            config=config,
        )
        outcome_by_id = None
    else:
        snapshot = load_image_stage_snapshot(
            derived_db,
            manifest_id=manifest_id,
            stage=stage,
            config=config,
            build_id=build_id,
        )
        if snapshot.run_id != batch.run_id:
            raise ImageRepositoryError("manifest_batch_run_mismatch")
        outcome_by_id = {outcome.source_image_id: outcome for outcome in snapshot.outcomes}

    claims = claim_tasks(
        derived_db,
        batch_id,
        config,
        stage_name=task_stage,
        actor=actor,
    )
    counts = {"succeeded": 0, "blocked": 0, "skipped": 0}
    for claim in claims:
        if claim.object_type != "image":
            raise ImageRepositoryError("non_image_task_claimed")
        if outcome_by_id is None:
            status = "blocked"
            reason_code = blocking_reason_code or "blocked_by_manifest"
            output_sha256 = None
        else:
            outcome = outcome_by_id.get(claim.source_object_id)
            if outcome is None:
                status = "blocked"
                reason_code = "image_manifest_row_missing"
                output_sha256 = None
            else:
                status = outcome.status
                reason_code = outcome.reason_code
                output_sha256 = outcome.output_sha256
        finish_task(
            derived_db,
            claim.task_id,
            status,
            config=config,
            reason_code=reason_code,
            output_sha256=output_sha256,
            actor=actor,
        )
        counts[status] += 1
    return ImageStageRunResult(
        batch_id=batch_id,
        stage=stage,
        claimed_count=len(claims),
        succeeded_count=counts["succeeded"],
        blocked_count=counts["blocked"],
        skipped_count=counts["skipped"],
    )
