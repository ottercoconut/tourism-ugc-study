"""远端单批生命周期：脱离SSH运行、核验产物、凭本机回执释放私有文件。

只接受已封存轮次中的一个最多500条批次，调用既有唯一预测入口；不读取源库、
不训练、不重试失败模型。释放操作只删除该批输入/输出/向量/日志，保留小型账本。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .research_round_artifacts import load_verified_json, write_json
from .research_round_execution import _run_batch, _utc, batch_command, verify_batch_output
from .source_snapshot import file_sha256


@dataclass(frozen=True)
class RemoteBatch:
    """由代码版本、轮次摘要和严格批次名绑定的远端路径；不携带密码。"""

    code: Path
    root: Path
    name: str
    batch: dict[str, Any]
    config: dict[str, Any]
    version: str
    manifest_sha256: str

    @property
    def job(self) -> Path:
        """返回不含正文的批次执行账本目录。"""
        return self.root / "remote-jobs" / self.name

    @property
    def output(self) -> Path:
        """返回唯一原生批次产物父目录。"""
        return self.root / "inference" / self.name

    @property
    def cache(self) -> Path:
        """返回该批专用缓存；从不释放其他批次或公共权重。"""
        return self.code / self.config["vector_cache_root"] / self.name


def bind_remote_batch(code: Path, root: Path, digest: str, name: str) -> RemoteBatch:
    """校验远端固定检出和作用域，拒绝路径穿越或把宽泛目录作为清理目标。"""
    code = code.resolve()
    manifest = load_verified_json(root / "round-manifest.json", digest)
    cfg = manifest["round_config"]
    if (not re.fullmatch(r"batch-\d{3}", name)
            or not re.fullmatch(r"research-cleaning-[a-z0-9-]+", cfg["round_name"])
            or root != code / "results" / cfg["round_name"] or root.resolve() != root
            or not re.fullmatch(r"results/cleaning-model-inference-vector-cache-[a-z0-9-]+", cfg["vector_cache_root"])):
        raise ValueError("remote_batch_scope_invalid")
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=code, text=True).strip()
    if version != manifest["code_version"] or subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=code, text=True).strip():
        raise ValueError("remote_batch_code_mismatch")
    selected = [item for item in manifest["batches"] if item["filename"] == f"inference-batches/{name}.csv"]
    if len(selected) != 1 or not 1 <= selected[0]["count"] <= 500:
        raise ValueError("remote_batch_selection_invalid")
    ctx = RemoteBatch(code, root, name, selected[0], cfg, version, digest)
    for path in (ctx.job, ctx.output, ctx.cache):
        if path.resolve() != path:
            raise ValueError("remote_batch_symlink_forbidden")
    return ctx


def _process_ticks(pid: int) -> str | None:
    """读取Linux进程启动时刻以识别PID复用；进程不存在时返回空。"""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def inspect_remote_batch(ctx: RemoteBatch) -> dict[str, Any]:
    """读取当前账本并核对真实进程；观测断连不能被解释成模型已停止。"""
    path = ctx.job / "state.json"
    if not path.exists():
        return {"status": "NOT_STARTED", "batch": ctx.name}
    state = json.loads(path.read_text())
    if state["round_manifest_sha256"] != ctx.manifest_sha256:
        raise ValueError("remote_state_binding_mismatch")
    if state["status"] == "RUNNING":
        state["process_alive"] = _process_ticks(state["pid"]) == state["process_start_ticks"]
        if not state["process_alive"]:
            state["status"] = "ORPHANED"
    return state


def launch_remote_batch(ctx: RemoteBatch) -> dict[str, Any]:
    """幂等启动独立单批进程；已有账本时只观察，绝不重复或重试推理。"""
    (ctx.root / "remote-jobs").mkdir(exist_ok=True)
    (ctx.root / "logs").mkdir(exist_ok=True)
    with (ctx.root / ".remote-launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ctx.job.exists():
            return inspect_remote_batch(ctx)
        if file_sha256(ctx.root / ctx.batch["filename"]) != ctx.batch["sha256"]:
            raise ValueError("remote_input_hash_mismatch")
        if shutil.disk_usage(ctx.root).free < 8 * 1024**3:
            raise ValueError("remote_free_disk_below_8gib")
        ctx.job.mkdir(mode=0o700)
        write_json(ctx.job / "state.json", {"status": "STARTING", "batch": ctx.name,
                   "round_manifest_sha256": ctx.manifest_sha256, "started_at_utc": _utc()})
        command = [str(ctx.code / ".venv/bin/python"), "-u",
                   str(ctx.code / "scripts/cleaning_remote_batch.py"), "run",
                   "--round-root", str(ctx.root), "--expected-manifest-sha256", ctx.manifest_sha256,
                   "--batch", ctx.name]
        with (ctx.root / "logs" / f"{ctx.name}-worker.log").open("ab") as log:
            child = subprocess.Popen(command, cwd=ctx.code, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        return {"status": "STARTING", "batch": ctx.name, "pid": child.pid}


def transfer_inventory(root: Path, name: str) -> list[dict[str, Any]]:
    """列出本批全部原生文件和固定日志，逐文件SHA覆盖包括断点在内的回传内容。"""
    paths = sorted(path for path in (root / "inference" / name).rglob("*") if path.is_file())
    paths.extend([root / "logs" / f"{name}.log", root / "logs" / f"{name}-worker.log"])
    items = []
    for path in paths:
        if path.resolve() != path or not path.is_file():
            raise ValueError("remote_transfer_symlink_or_missing_file")
        items.append({"filename": str(path.relative_to(root)), "sha256": file_sha256(path),
                      "size_bytes": path.stat().st_size})
    return items


def verify_transfer(root: Path, manifest: dict[str, Any], name: str) -> None:
    """重新枚举本批文件并逐字节比对清单；额外/丢失/损坏文件均拒绝清理。"""
    if manifest["batch"] != name or transfer_inventory(root, name) != manifest["files"]:
        raise ValueError("remote_transfer_inventory_mismatch")


def verify_archived_embeddings(output: Path, native: dict[str, Any]) -> None:
    """核验每条概率引用的向量/回执，保证回传后仍能独立审计零省略与数值身份。

只读取归档文件，不加载编码器或重算缺失向量；同正文共享一个条目是允许的。
任一引用、维度、精度、规范化或摘要失配均拒绝释放服务器副本。
    """
    import numpy as np

    manifest_path = Path(native["manifest_path"])
    manifest = load_verified_json(manifest_path, native["manifest_sha256"])
    spec = manifest["artifacts"]["records"]
    records = load_verified_json(manifest_path.parent / spec["filename"], spec["sha256"])
    if len(records) != manifest["count"]:
        raise ValueError("remote_embedding_record_count_mismatch")
    for record in records:
        namespace, text_sha = record["embedding_cache_key"].split(":")
        if (not re.fullmatch(r"[0-9a-f]{32}", namespace)
                or not re.fullmatch(r"[0-9a-f]{64}", text_sha)
                or namespace != manifest["embedding_cache_namespace_id"]
                or text_sha != record["normalized_sha256"]):
            raise ValueError("remote_embedding_reference_invalid")
        entry = output / "embedding-cache" / namespace / text_sha[:2] / text_sha
        receipt = load_verified_json(entry / "receipt.json", record["embedding_cache_receipt_sha256"])
        vector = entry / "embedding.npy"
        if (receipt["omitted_token_count"] != 0 or receipt["embedding_dimension"] != 2560
                or receipt["normalized_sha256"] != text_sha or receipt["cache_namespace_id"] != namespace
                or receipt["plan_sha256"] != manifest["plan_sha256"]
                or receipt["inference_execution_profile"] != manifest.get("inference_execution_profile")
                or file_sha256(vector) != receipt["embedding_file_sha256"]):
            raise ValueError("remote_embedding_receipt_invalid")
        values = np.load(vector, allow_pickle=False)
        if (values.dtype != np.float32 or values.shape != (2560,) or not np.isfinite(values).all()
                or not np.isclose(np.linalg.norm(values), 1.0, atol=1e-5)
                or hashlib.sha256(values.tobytes(order="C")).hexdigest() != receipt["embedding_value_sha256"]):
            raise ValueError("remote_embedding_vector_invalid")


def run_remote_batch(ctx: RemoteBatch) -> None:
    """运行一次原生推理并封存传输清单；崩溃、超时都记录失败且不自动重试。"""
    state = inspect_remote_batch(ctx)
    if state["status"] != "STARTING":
        raise ValueError("remote_worker_not_new")
    state.update(status="RUNNING", pid=os.getpid(), process_start_ticks=_process_ticks(os.getpid()))

    def report(event: dict[str, Any]) -> None:
        """保存去敏实时事件；不把正文写入状态或终端。"""
        state["last_event"] = {"time_utc": _utc(), **event}
        write_json(ctx.job / "state.json", state)

    try:
        with (ctx.root / ".remote-execution.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if file_sha256(ctx.code / ctx.config["policy_package"] / "frozen-routing-model.joblib") != ctx.config["model_sha256"]:
                raise ValueError("remote_frozen_model_hash_mismatch")
            cfg = dict(ctx.config, vector_cache_root=str(ctx.cache))
            command = batch_command(ctx.code, ctx.code, ctx.root, ctx.batch, cfg)
            report({"event": "BATCH_STARTED", "command": command})
            _run_batch(command, ctx.code, ctx.root / "logs" / f"{ctx.name}.log", ctx.output,
                       cfg["batch_timeout_seconds"], report)
            receipt = verify_batch_output(ctx.output, ctx.batch, cfg, ctx.version)
            # 向量可以重建，但概率引用的校验回执不可在回传前丢弃；二者一起归档。
            shutil.copytree(ctx.cache, ctx.output / "embedding-cache")
            verify_archived_embeddings(ctx.output, receipt)
            export = {"batch": ctx.name, "round_manifest_sha256": ctx.manifest_sha256,
                      "native_manifest_sha256": receipt["manifest_sha256"],
                      "files": transfer_inventory(ctx.root, ctx.name)}
            write_json(ctx.job / "transfer-manifest.json", export)
            state.update(status="COMPLETED", receipt=receipt,
                         transfer_manifest_sha256=file_sha256(ctx.job / "transfer-manifest.json"))
            report({"event": "BATCH_COMPLETED", "count": ctx.batch["count"]})
    except BaseException as exc:
        state.update(status="FAILED", error=type(exc).__name__ + ":" + str(exc))
        report({"event": "BATCH_FAILED"})
        raise


def release_remote_batch(ctx: RemoteBatch, accepted_sha256: str) -> dict[str, Any]:
    """本机验收并持久化后才允许调用；仅释放明确的一批，绝不清除公共模型/环境。"""
    state = inspect_remote_batch(ctx)
    if state["status"] == "RELEASED" and state["accepted_transfer_sha256"] == accepted_sha256:
        return state
    if state["status"] not in ("COMPLETED", "RELEASING") or state["transfer_manifest_sha256"] != accepted_sha256:
        raise ValueError("remote_release_without_acceptance")
    if state["status"] == "COMPLETED":
        export = load_verified_json(ctx.job / "transfer-manifest.json", accepted_sha256)
        verify_transfer(ctx.root, export, ctx.name)
        native = verify_batch_output(ctx.output, ctx.batch, ctx.config, ctx.version, export["native_manifest_sha256"])
        verify_archived_embeddings(ctx.output, native)
        state.update(status="RELEASING", accepted_transfer_sha256=accepted_sha256)
        write_json(ctx.job / "state.json", state)
    # 路径由已校验的批次和固定配置构造；缓存/输出仅限深层的batch-NNN目录。
    for directory in (ctx.output, ctx.cache):
        if directory.name != ctx.name or directory.resolve() != directory:
            raise ValueError("remote_release_scope_invalid")
        if directory.exists():
            shutil.rmtree(directory)
    for path in (ctx.root / ctx.batch["filename"], ctx.root / "logs" / f"{ctx.name}.log",
                 ctx.root / "logs" / f"{ctx.name}-worker.log", ctx.job / "transfer-manifest.json"):
        path.unlink(missing_ok=True)
    state.update(status="RELEASED", accepted_transfer_sha256=accepted_sha256, released_at_utc=_utc())
    write_json(ctx.job / "state.json", state)
    return state
