"""按封存批次调用唯一冻结模型入口，维护可恢复的执行状态。

本模块只负责任务调度，不计算概率、不调整模型或阈值。每批独立子进程且有
硬超时；失败后停止，重试必须显式指定。已完成批次以摘要校验后复用，未完成
批次由原生产入口校验并恢复逐条checkpoint。锁阻止两个调度器并发写同一轮。
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .research_round_artifacts import load_verified_json, write_json
from .source_snapshot import file_sha256


def _utc() -> str:
    """返回带时区的运行事件时间；不用于结果身份计算。"""
    return datetime.now(timezone.utc).isoformat()


def batch_command(code_root: Path, workspace: Path, round_root: Path,
                  batch: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """生成无shell插值的原生预测命令；配置/代码与私有数据根分离。"""
    return [str(code_root / ".venv/bin/python"), "-u",
            str(code_root / "scripts/cleaning_predict_tourism_relevance.py"),
            "--input-csv", str(round_root / batch["filename"]),
            "--snapshot-package", str(workspace / config["training_package"]),
            "--expected-snapshot-manifest-sha256", config["training_manifest_sha256"],
            "--policy-package", str(workspace / config["policy_package"]),
            "--expected-policy-manifest-sha256", config["policy_manifest_sha256"],
            "--model-dir", str((workspace / config["model_directory"]).resolve()),
            "--vector-cache-root", str(workspace / "results/cleaning-model-inference-vector-cache"),
            "--artifact-root", str(round_root / "inference" / Path(batch["filename"]).stem),
            "--execute-prediction", "--output-format", "json"]


def verify_batch_output(root: Path, batch: dict[str, Any], config: dict[str, Any],
                        code_version: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """核验唯一原生批次包及输入回执，拒绝换输入、换策略或损坏产物。

逐条记录与完整研究人口的一一核验由结果装配层负责，本层验证产物完整性和
执行绑定。返回仅含路径、摘要和计数的恢复凭据，不输出原文。
    """
    manifests = list(root.glob("*/incremental-inference-manifest.json"))
    if len(manifests) != 1:
        raise ValueError("round_batch_manifest_not_unique")
    path = manifests[0]
    digest = file_sha256(path)
    manifest = load_verified_json(path, expected_sha256 or digest)
    if (manifest["status"] != "NEW_BATCH_SCORED"
            or manifest["code_version"] != code_version
            or manifest["count"] != batch["count"]
            or manifest["policy_manifest_sha256"] != config["policy_manifest_sha256"]
            or manifest["snapshot_manifest_sha256"] != config["training_manifest_sha256"]
            or manifest["fit_call_count"] != 0
            or manifest["source_database_write_count"] != 0
            or manifest["training_member_overlap_count"] != 0
            or sum(manifest["action_counts"].values()) != batch["count"]):
        raise ValueError("round_batch_binding_mismatch")
    for item in manifest["artifacts"].values():
        if Path(item["filename"]).name != item["filename"] or file_sha256(path.parent / item["filename"]) != item["sha256"]:
            raise ValueError("round_batch_output_hash_mismatch")
    receipt_spec = manifest["artifacts"]["input_receipt"]
    receipt = load_verified_json(path.parent / receipt_spec["filename"], receipt_spec["sha256"])
    if receipt["input_file_sha256"] != batch["sha256"] or receipt["count"] != batch["count"]:
        raise ValueError("round_batch_input_mismatch")
    return {"manifest_path": str(path), "manifest_sha256": digest,
            "count": manifest["count"], "action_counts": manifest["action_counts"],
            "embedding_cache_hit_count": manifest["embedding_cache_hit_count"],
            "embedding_cache_miss_count": manifest["embedding_cache_miss_count"]}


def _run_batch(command: list[str], code_root: Path, log: Path, output_root: Path,
               timeout_seconds: int, report: Any) -> None:
    """运行本任务的子进程，30秒记录一次进度，超时前60秒告警。

发生超时、取消或异常时只终止自己创建的进程组，保留所有日志和逐条断点。
不自动更改超时、不隐藏重试；模型错误由调用者记录为失败状态。
    """
    started = time.monotonic()
    warned = False
    with log.open("a", encoding="utf-8") as stream:
        os.chmod(log, 0o600)
        process = subprocess.Popen(command, cwd=code_root, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                elapsed = time.monotonic() - started
                if not warned and elapsed >= timeout_seconds - 60:
                    warned = True
                    report({"event": "BATCH_TIMEOUT_WARNING", "pid": process.pid, "seconds_remaining": max(0, int(timeout_seconds - elapsed))})
                if elapsed >= timeout_seconds:
                    raise TimeoutError("round_batch_timeout")
                checkpoints = list(output_root.glob("*/checkpoint-state.json"))
                completed = sum(json.loads(path.read_text())["completed_count"] for path in checkpoints)
                report({"event": "BATCH_PROGRESS", "pid": process.pid,
                        "elapsed_seconds": round(elapsed, 1), "completed_count": completed})
                try:
                    code = process.wait(timeout=min(30, timeout_seconds - elapsed))
                    if code:
                        raise RuntimeError(f"round_batch_exit_{code}")
                    return
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)


def execute_round(workspace: Path, code_root: Path, round_root: Path,
                  expected_manifest_sha256: str, *, retry_failed: bool = False) -> dict[str, Any]:
    """校验清洁代码与封存输入，顺序执行所有批次并原子保存执行账本。

可重复调用恢复，但失败状态须显式确认重试。中断前已封存的完整原生包可直接
核验接续；没有完成包时使用原入口断点。最终状态只代表模型批次完成，不代表
人工审查、最终keep发布或研究数据冻结。
    """
    workspace, code_root, round_root = workspace.resolve(), code_root.resolve(), round_root.resolve()
    manifest = load_verified_json(round_root / "round-manifest.json", expected_manifest_sha256)
    code_version = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=code_root, text=True).strip()
    if manifest["code_version"] != code_version or subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=code_root, text=True
    ).strip():
        raise ValueError("round_runtime_code_mismatch")
    config = manifest["round_config"]
    if file_sha256(workspace / config["policy_package"] / "frozen-routing-model.joblib") != config["model_sha256"]:
        raise ValueError("round_frozen_model_hash_mismatch")
    if file_sha256(Path(manifest["source_pointer"]["snapshot_path"])) != config["source_snapshot_sha256"]:
        raise ValueError("round_snapshot_hash_mismatch")
    logs = round_root / "logs"
    logs.mkdir(exist_ok=True)
    state_path = round_root / "execution-state.json"
    with (round_root / ".execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "round_manifest_sha256": expected_manifest_sha256, "code_version": code_version,
            "started_at_utc": _utc(), "completed_batches": {}, "status": "READY"}
        if state["round_manifest_sha256"] != expected_manifest_sha256 or state["code_version"] != code_version:
            raise ValueError("round_execution_binding_mismatch")
        if state["status"] == "FAILED" and not retry_failed:
            raise ValueError("round_failed_requires_explicit_retry")

        def report(event: dict[str, Any]) -> None:
            """保存和打印无原文的实时事件，便于审计及人工监测。"""
            state["last_event"] = {"time_utc": _utc(), **event}
            write_json(state_path, state)
            print(json.dumps(state["last_event"], ensure_ascii=False), flush=True)

        for index, batch in enumerate(manifest["batches"], 1):
            name = Path(batch["filename"]).stem
            try:
                if file_sha256(round_root / batch["filename"]) != batch["sha256"]:
                    raise ValueError("round_batch_csv_hash_mismatch")
                output_root = round_root / "inference" / name
                prior = state["completed_batches"].get(name)
                if prior or list(output_root.glob("*/incremental-inference-manifest.json")):
                    state["completed_batches"][name] = verify_batch_output(
                        output_root, batch, config, code_version, prior["manifest_sha256"] if prior else None)
                    continue
                state["status"] = "RUNNING"
                state["active_batch"] = name
                command = batch_command(code_root, workspace, round_root, batch, config)
                report({"event": "BATCH_STARTED", "batch": name, "index": index,
                        "total_batches": len(manifest["batches"]), "command": command})
                _run_batch(command, code_root, logs / (name + ".log"), output_root,
                           config["batch_timeout_seconds"], report)
                state["completed_batches"][name] = verify_batch_output(output_root, batch, config, code_version)
                report({"event": "BATCH_COMPLETED", "batch": name,
                        "completed_records": sum(item["count"] for item in state["completed_batches"].values())})
            except BaseException as exc:
                state["status"] = "FAILED"
                report({"event": "BATCH_FAILED", "batch": name, "error": type(exc).__name__ + ":" + str(exc)})
                raise
        state["status"] = "BATCHES_SCORED"
        state["active_batch"] = None
        state["completed_at_utc"] = _utc()
        report({"event": "ALL_BATCHES_COMPLETED", "count": manifest["model_input_count"]})
        return state
