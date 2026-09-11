"""等待既有全量推理结束，验收候选并更新本轮私有指针。

这是同一次清洗流水线的单次后处理，不启动或重试模型。共享执行锁只负责等待
已启动的调度器退出；只有BATCHES_SCORED允许装配，失败或中断绝不发布部分结果。
"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .research_round_artifacts import load_verified_json, write_json
from .research_round_result_artifacts import assemble_round
from .source_snapshot import file_sha256


def publish_candidate_pointer(pointer_path: Path, round_root: Path, output: Path,
                              expected_input_sha256: str) -> dict[str, Any]:
    """仅把本轮已验收候选设为当前，拒绝跨轮覆盖或将候选标为最终研究集。

候选库和manifest须已存在且摘要有效；本轮原指针必须仍指向相同输入。若其他
任务已切换到另一个研究轮次，本函数失败而不是抢回指针。原最终keep文件不参与
写入，发布数必须仍为0。
    """
    pointer = json.loads(pointer_path.read_text())
    manifest_path = output / "candidate-manifest.json"
    digest = file_sha256(manifest_path)
    result = load_verified_json(manifest_path, digest)
    if (Path(pointer["round_root"]).resolve() != round_root.resolve()
            or pointer["round_manifest_sha256"] != expected_input_sha256
            or result["round_manifest_sha256"] != expected_input_sha256
            or result["source_snapshot_sha256"] != pointer["source_snapshot_sha256"]
            or result["status"] != "CANDIDATES_READY_AWAITING_HUMAN_REVIEW"
            or result["final_keep_published_count"] != 0
            or Path(result["database_filename"]).name != result["database_filename"]
            or file_sha256(output / result["database_filename"]) != result["database_sha256"]):
        raise ValueError("round_candidate_pointer_binding_mismatch")
    pointer.update(status=result["status"], candidate_database_path=str(output / result["database_filename"]),
                   candidate_database_sha256=result["database_sha256"],
                   candidate_manifest_path=str(manifest_path), candidate_manifest_sha256=digest,
                   updated_at_utc=datetime.now(timezone.utc).isoformat(),
                   decision_counts=result["decision_counts"], final_keep_published_count=0,
                   human_review_required=True)
    write_json(pointer_path, pointer)
    return pointer


def complete_when_ready(workspace: Path, round_root: Path, output: Path,
                        expected_input_sha256: str, *, code_version: str) -> dict[str, Any]:
    """等待正在运行的本轮，成功后装配候选；失败只记录异常，不重试或清空。

调用方须先验证代码已提交。单独的完成锁防止多个后处理进程竞争；等待的是
明确已存在的执行锁，不创建或触碰源库。状态文件只记录去敏进度和运行身份。
    """
    workspace, round_root, output = workspace.resolve(), round_root.resolve(), output.resolve()
    load_verified_json(round_root / "round-manifest.json", expected_input_sha256)
    if output.exists():
        raise FileExistsError("round_completion_output_exists")
    state_path = round_root / "completion-state.json"
    state = {"status": "WAITING_FOR_INFERENCE", "round_manifest_sha256": expected_input_sha256,
             "completion_code_version": code_version, "candidate_output": str(output),
             "started_at_utc": datetime.now(timezone.utc).isoformat()}
    with (round_root / ".completion.lock").open("a") as own_lock:
        fcntl.flock(own_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_json(state_path, state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        try:
            with (round_root / ".execution.lock").open("r") as execution_lock:
                # 在同次流水线里等待已启动进程，锁释放后仍须检验真实完成状态。
                fcntl.flock(execution_lock, fcntl.LOCK_SH)
                execution = json.loads((round_root / "execution-state.json").read_text())
                if execution["status"] != "BATCHES_SCORED" or execution["round_manifest_sha256"] != expected_input_sha256:
                    raise ValueError("round_completion_inference_not_successful")
                state["status"] = "ASSEMBLING_CANDIDATES"
                write_json(state_path, state)
                result = assemble_round(round_root, output, expected_input_sha256,
                                        workspace=workspace, code_version=code_version)
                publish_candidate_pointer(workspace / "data/processed/current-cleaning.json",
                                          round_root, output, expected_input_sha256)
            state.update(status=result["status"], completed_at_utc=datetime.now(timezone.utc).isoformat(),
                         candidate_manifest_sha256=file_sha256(output / "candidate-manifest.json"),
                         record_count=result["record_count"], decision_counts=result["decision_counts"])
            write_json(state_path, state)
            print(json.dumps(state, ensure_ascii=False), flush=True)
            return state
        except BaseException as exc:
            state.update(status="FAILED", error_type=type(exc).__name__, error=str(exc))
            write_json(state_path, state)
            raise
