#!/usr/bin/env python3
"""领取批次任务、写入外部处理结果或查询批次状态。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning import (  # noqa: E402
    ConfigurationError,
    SchedulerError,
    StateTransitionError,
    claim_tasks,
    finish_task,
    get_batch_status,
    heartbeat_task,
    load_config,
)


def build_parser() -> argparse.ArgumentParser:
    """构造任务生命周期命令；四种动作必须且只能选择一种。"""

    parser = argparse.ArgumentParser(description="操作一个冻结批次的任务生命周期。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    parser.add_argument("--config", required=True, help="版本化清洗配置")
    parser.add_argument("--batch-id", required=True, help="冻结批次标识")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status", action="store_true", help="只读查询批次状态")
    actions.add_argument("--stage", help="领取指定处理名称的就绪任务")
    actions.add_argument("--heartbeat-task", help="更新一个运行中任务的心跳")
    actions.add_argument("--finish-task", help="登记一个运行中任务的处理结果")
    parser.add_argument(
        "--result",
        choices=("succeeded", "failed", "blocked", "skipped"),
        help="与 --finish-task 一起使用的结果状态",
    )
    parser.add_argument("--reason-code", help="失败、阻塞或跳过的机器理由代码")
    parser.add_argument("--output-sha256", help="成功输出的 SHA-256")
    parser.add_argument("--actor", help="操作者或进程标识，仅保存 SHA-256")
    return parser


def _status_payload(args: argparse.Namespace) -> dict[str, object]:
    """构造批次状态 JSON，保持嵌套映射可序列化。"""

    status = get_batch_status(args.derived_db, args.batch_id)
    return {
        "batch": status.batch.__dict__,
        "task_status_counts": dict(status.task_status_counts),
        "stage_status_counts": {
            stage: dict(counts) for stage, counts in status.stage_status_counts.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    """执行所选动作；真正的文本或图片计算由后续独立处理器接入。"""

    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.status:
            payload = _status_payload(args)
        elif args.stage:
            claims = claim_tasks(
                args.derived_db,
                args.batch_id,
                config,
                stage_name=args.stage,
                actor=args.actor,
            )
            payload = {
                "batch_id": args.batch_id,
                "claimed": len(claims),
                "tasks": [claim.__dict__ for claim in claims],
            }
        elif args.heartbeat_task:
            heartbeat_task(args.derived_db, args.heartbeat_task, config)
            payload = {"task_id": args.heartbeat_task, "status": "running"}
        else:
            if args.result is None:
                raise StateTransitionError("result_required")
            finish_task(
                args.derived_db,
                args.finish_task,
                args.result,
                config=config,
                reason_code=args.reason_code,
                output_sha256=args.output_sha256,
                actor=args.actor,
            )
            payload = {"task_id": args.finish_task, "status": args.result}
    except ConfigurationError:
        reason_code = "invalid_config"
    except (SchedulerError, StateTransitionError) as exc:
        reason_code = exc.reason_code
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    print(json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
