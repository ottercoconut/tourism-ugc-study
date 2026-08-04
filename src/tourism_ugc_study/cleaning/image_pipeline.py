"""图片证据操作与增量任务状态机之间的薄编排层。

文件解析、指纹计算和候选算法分别留在专用模块；本模块只负责领取对应任务，
把已持久化 outcome 转成合法终态，并保留无 manifest 时的显式阻塞/恢复语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import CleaningConfig, validate_image_algorithm_contract
from .image_repository import (
    ImageRepositoryError,
    load_image_stage_snapshot,
    record_manifest_block,
)
from .scheduler import get_batch_status
from .state_machine import claim_tasks, finish_task


@dataclass(frozen=True)
class ImageStageRunResult:
    """一次图片子阶段与调度任务同步后的无敏感计数。

    `batch_id/stage` 标识目标批次和图片子阶段；`claimed_count` 是本次领取数，
    其余计数按成功、可恢复阻塞和来源角色跳过拆分。对象不包含任务原文、路径、
    URL 或图片内容，也不代表整个批次已经完成。
    """

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

    输入指定派生库、冻结批次、子阶段、可选 manifest/build、配置与去敏 actor。
    函数只负责领取图片任务并依据仓储层的不可变 outcome 合法完成、跳过或阻塞；
    它不解析清单、打开图片或计算候选。

    缺少 manifest 时只阻塞图片对象及其图片下游；文本对象依赖链独立，仍可
    正常领取和完成。下载完成后必须先显式 resume 再调用。manifest 与批次跨
    运行、阶段非法或领取到非图片任务时显式失败；重复执行只处理状态机允许领取
    的任务，不覆盖历史事件。非法图片算法配置在读取批次或领取任务前抛出
    `ConfigurationError`，不产生任务状态变化。
    """

    # 编排层可能在显式 blocking_reason_code 分支绕过仓储读取，因此必须在领取
    # 任务前独立校验固定图片算法契约，避免非法程序化配置改变任务状态。
    validate_image_algorithm_contract(config.image)
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
