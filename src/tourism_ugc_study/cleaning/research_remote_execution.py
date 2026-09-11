"""本机调度单批远端推理，全部回传并验收后才允许服务器释放和下一批。

同一时刻远端最多保留一个500条原生批次；回传失败不删除服务器证据，不发布
部分候选。远端模型进程脱离SSH连接；本机重连只观察相同账本，不重试模型。
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .research_remote_transport import ResearchSSHTransport
from .research_remote_worker import verify_transfer
from .research_round_artifacts import load_verified_json, write_json
from .research_round_execution import _utc, verify_batch_output
from .source_snapshot import file_sha256


def accept_remote_result(local: Path, batch: dict[str, Any], config: dict[str, Any], version: str,
                         transfer_path: Path, transfer_sha256: str, round_sha256: str) -> dict[str, Any]:
    """按服务端封存SHA及本机冻结输入双重核验回传，返回可持久化的本机回执。"""
    name = Path(batch["filename"]).stem
    exported = load_verified_json(transfer_path, transfer_sha256)
    if exported["round_manifest_sha256"] != round_sha256:
        raise ValueError("remote_transfer_round_mismatch")
    verify_transfer(local, exported, name)
    receipt = verify_batch_output(local / "inference" / name, batch, config, version,
                                  exported["native_manifest_sha256"])
    receipt.update(transfer_manifest_path=str(transfer_path), transfer_manifest_sha256=transfer_sha256,
                   received_at_utc=_utc())
    return receipt


def execute_remote_round(workspace: Path, code: Path, root: Path, digest: str,
                         transport: ResearchSSHTransport, *, poll_seconds: int = 30) -> dict[str, Any]:
    """串行完成封存人口并维护兼容既有装配器的账本；不发布最终keep。

失败模型须人工判断；网络观测异常保留原PID/账本，恢复时只查询同一批次。
本机验收先持久化、服务器清理后再记账；任一阶段未完成都阻止上传下一批。
"""
    workspace, code, root = workspace.resolve(), code.resolve(), root.resolve()
    manifest = load_verified_json(root / "round-manifest.json", digest)
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=code, text=True).strip()
    if version != manifest["code_version"] or subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=code, text=True).strip():
        raise ValueError("remote_round_local_code_mismatch")
    config = manifest["round_config"]
    if (transport.manifest_sha256 != digest
            or file_sha256(workspace / config["source_snapshot"]) != config["source_snapshot_sha256"]
            or file_sha256(workspace / config["policy_package"] / "frozen-routing-model.joblib") != config["model_sha256"]):
        raise ValueError("remote_round_local_binding_mismatch")
    (root / "logs").mkdir(exist_ok=True)
    state_path = root / "execution-state.json"
    with (root / ".execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "round_manifest_sha256": digest, "code_version": version, "status": "READY",
            "started_at_utc": _utc(), "completed_batches": {}, "released_batches": [],
            "transport": "ssh-rsync-single-batch", "max_remote_completed_records": 500}
        if state["round_manifest_sha256"] != digest or state["code_version"] != version:
            raise ValueError("remote_round_state_binding_mismatch")

        def report(event: dict[str, Any]) -> None:
            """原子持久化账本并打印去敏进度；输出本身不作为完成证明。"""
            state["last_event"] = {"time_utc": _utc(), **event}
            write_json(state_path, state)
            print(json.dumps(state["last_event"], ensure_ascii=False), flush=True)

        for batch in manifest["batches"]:
            name = Path(batch["filename"]).stem
            try:
                if not 1 <= batch["count"] <= 500 or file_sha256(root / batch["filename"]) != batch["sha256"]:
                    raise ValueError("remote_round_input_mismatch")
                prior = state["completed_batches"].get(name)
                if prior:
                    accept_remote_result(root, batch, config, version, Path(prior["transfer_manifest_path"]),
                                         prior["transfer_manifest_sha256"], digest)
                if name in state["released_batches"]:
                    if not prior:
                        raise ValueError("remote_released_without_local_result")
                    continue
                state.update(status="RUNNING", active_batch=name)
                if not prior:
                    remote = transport.action("inspect", name)
                    if remote["status"] == "NOT_STARTED":
                        transport.upload_input(root, batch["filename"])
                        remote = transport.action("launch", name)
                    starting_checks = 0
                    while remote["status"] in ("STARTING", "RUNNING"):
                        state["remote_observation"] = remote
                        report({"event": "REMOTE_BATCH_PROGRESS", "batch": name,
                                "remote_status": remote["status"], "remote_pid": remote.get("pid"),
                                "completed_count": remote.get("last_event", {}).get("completed_count", 0),
                                "received_records": sum(item["count"] for item in state["completed_batches"].values())})
                        starting_checks = starting_checks + 1 if remote["status"] == "STARTING" else 0
                        if starting_checks > 4:
                            raise RuntimeError("remote_worker_start_not_confirmed")
                        time.sleep(poll_seconds)
                        remote = transport.action("inspect", name)
                    state["remote_observation"] = remote
                    if remote["status"] != "COMPLETED":
                        raise RuntimeError("remote_batch_not_completed:" + remote["status"])
                    path = transport.download_result(root, name)
                    prior = accept_remote_result(root, batch, config, version, path,
                                                 remote["transfer_manifest_sha256"], digest)
                    state["completed_batches"][name] = prior
                    report({"event": "REMOTE_BATCH_RECEIVED_VERIFIED", "batch": name,
                            "received_records": sum(item["count"] for item in state["completed_batches"].values())})
                released = transport.action("release", name, prior["transfer_manifest_sha256"])
                if released["status"] != "RELEASED":
                    raise ValueError("remote_release_not_confirmed")
                state["released_batches"].append(name)
                report({"event": "REMOTE_BATCH_RELEASED", "batch": name})
            except BaseException as exc:
                # 无法观察时不宣称远端模型终止；同一账本/PID必须重新核对。
                state["status"] = "INTERRUPTED_REQUIRES_INSPECTION"
                report({"event": "REMOTE_PIPELINE_INTERRUPTED", "batch": name,
                        "error": type(exc).__name__ + ":" + str(exc)})
                raise
        state.update(status="BATCHES_SCORED", active_batch=None, completed_at_utc=_utc())
        report({"event": "ALL_REMOTE_BATCHES_RECEIVED_VERIFIED", "count": manifest["model_input_count"]})
        return state
