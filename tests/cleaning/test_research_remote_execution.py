"""远端回传/释放门禁的合成验证；不联网、不加载模型、不读取研究正文。"""

import json
import hashlib
from pathlib import Path

import pytest
import numpy as np

from tourism_ugc_study.cleaning import research_remote_execution as execution
from tourism_ugc_study.cleaning import research_remote_worker as worker
from tourism_ugc_study.cleaning.research_round_artifacts import write_json
from tourism_ugc_study.cleaning.source_snapshot import file_sha256


VERSION = "a" * 40


def _native(root, name, batch, config):
    """生成最小原生完成包及所有待回传文件，并返回传输清单。"""
    directory = root / "inference" / name / "native-id"
    directory.mkdir(parents=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / f"{name}.log").write_text("finished\n")
    (root / "logs" / f"{name}-worker.log").write_text("")
    write_json(directory / "input-receipt.json", {"input_file_sha256": batch["sha256"], "count": batch["count"]})
    write_json(directory / "checkpoint-state.json", {"completed_count": batch["count"]})
    records = []
    namespace = "a" * 32
    for index in range(batch["count"]):
        text_sha = hashlib.sha256(f"synthetic-{index}".encode()).hexdigest()
        entry = root / "inference" / name / "embedding-cache" / namespace / text_sha[:2] / text_sha
        entry.mkdir(parents=True)
        values = np.zeros(2560, dtype=np.float32)
        values[0] = 1.0
        np.save(entry / "embedding.npy", values, allow_pickle=False)
        write_json(entry / "receipt.json", {
            "omitted_token_count": 0, "embedding_dimension": 2560, "normalized_sha256": text_sha,
            "cache_namespace_id": namespace, "plan_sha256": "f" * 64, "inference_execution_profile": None,
            "embedding_file_sha256": file_sha256(entry / "embedding.npy"),
            "embedding_value_sha256": hashlib.sha256(values.tobytes()).hexdigest()})
        records.append({"normalized_sha256": text_sha, "embedding_cache_key": namespace + ":" + text_sha,
                        "embedding_cache_receipt_sha256": file_sha256(entry / "receipt.json")})
    write_json(directory / "records.json", records)
    native = {"status": "NEW_BATCH_SCORED", "code_version": VERSION, "count": batch["count"],
              "policy_manifest_sha256": config["policy_manifest_sha256"],
              "snapshot_manifest_sha256": config["training_manifest_sha256"],
              "fit_call_count": 0, "source_database_write_count": 0, "training_member_overlap_count": 0,
              "action_counts": {"auto_keep": batch["count"]}, "embedding_cache_hit_count": 0,
              "embedding_cache_miss_count": batch["count"],
              "embedding_cache_namespace_id": namespace, "plan_sha256": "f" * 64,
              "artifacts": {"records": {"filename": "records.json", "sha256": file_sha256(directory / "records.json")},
                            "input_receipt": {"filename": "input-receipt.json",
                            "sha256": file_sha256(directory / "input-receipt.json")}}}
    write_json(directory / "incremental-inference-manifest.json", native)
    return {"batch": name, "round_manifest_sha256": "d" * 64,
            "native_manifest_sha256": file_sha256(directory / "incremental-inference-manifest.json"),
            "files": worker.transfer_inventory(root, name)}


@pytest.fixture
def remote(tmp_path):
    """固定测试作用域；相邻保留文件不属于可释放目标。"""
    code = tmp_path / "code"
    root = code / "results" / "research-cleaning-test"
    batch = {"filename": "inference-batches/batch-001.csv", "sha256": "b" * 64, "count": 2}
    config = {"vector_cache_root": "results/cleaning-model-inference-vector-cache-test",
              "policy_manifest_sha256": "c" * 64, "training_manifest_sha256": "e" * 64}
    root.mkdir(parents=True)
    ctx = worker.RemoteBatch(code, root, "batch-001", batch, config, VERSION, "d" * 64)
    ctx.job.mkdir(parents=True)
    ctx.cache.mkdir(parents=True)
    (ctx.cache / "vector.npy").write_bytes(b"vector")
    (root / "inference-batches").mkdir()
    (root / batch["filename"]).write_text("input")
    exported = _native(root, ctx.name, batch, config)
    write_json(ctx.job / "transfer-manifest.json", exported)
    digest = file_sha256(ctx.job / "transfer-manifest.json")
    write_json(ctx.job / "state.json", {"status": "COMPLETED", "round_manifest_sha256": ctx.manifest_sha256,
               "transfer_manifest_sha256": digest})
    return ctx, exported, digest


def test_transfer_inventory_covers_checkpoints_and_rejects_extra_files(remote):
    ctx, exported, _ = remote
    worker.verify_transfer(ctx.root, exported, ctx.name)
    assert any(item["filename"].endswith("checkpoint-state.json") for item in exported["files"])
    (ctx.output / "unexpected.txt").write_text("extra")
    with pytest.raises(ValueError, match="inventory_mismatch"):
        worker.verify_transfer(ctx.root, exported, ctx.name)


def test_release_requires_matching_local_acceptance_and_preserves_other_data(remote):
    ctx, exported, digest = remote
    preserved = ctx.code / "public-model.bin"
    preserved.write_bytes(b"public")
    with pytest.raises(ValueError, match="without_acceptance"):
        worker.release_remote_batch(ctx, "0" * 64)
    assert ctx.output.exists() and ctx.cache.exists()
    assert worker.release_remote_batch(ctx, digest)["status"] == "RELEASED"
    assert not ctx.output.exists() and not ctx.cache.exists()
    assert not (ctx.root / ctx.batch["filename"]).exists()
    assert preserved.read_bytes() == b"public"
    assert worker.release_remote_batch(ctx, digest)["status"] == "RELEASED"


def test_changed_remote_checkpoint_prevents_any_deletion(remote):
    ctx, exported, digest = remote
    (ctx.output / "native-id" / "checkpoint-state.json").write_text("changed")
    with pytest.raises(ValueError, match="inventory_mismatch"):
        worker.release_remote_batch(ctx, digest)
    assert ctx.cache.exists() and (ctx.root / ctx.batch["filename"]).exists()


def test_acceptance_binds_round_native_and_all_transferred_files(remote):
    ctx, exported, digest = remote
    path = ctx.job / "transfer-manifest.json"
    result = execution.accept_remote_result(ctx.root, ctx.batch, ctx.config, VERSION, path, digest, ctx.manifest_sha256)
    assert result["count"] == 2
    with pytest.raises(ValueError, match="round_mismatch"):
        execution.accept_remote_result(ctx.root, ctx.batch, ctx.config, VERSION, path, digest, "f" * 64)


def test_missing_archived_vector_is_not_recomputed_or_released(remote):
    """回执存在但向量丢失时拒绝验收，不以重新推理掩盖证据缺失。"""
    ctx, exported, digest = remote
    path = next((ctx.output / "embedding-cache").rglob("embedding.npy"))
    path.unlink()
    with pytest.raises(ValueError, match="inventory_mismatch"):
        worker.release_remote_batch(ctx, digest)
    assert ctx.cache.exists()


def test_existing_job_is_observed_without_spawning(remote, monkeypatch):
    ctx, _, _ = remote
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **kw: pytest.fail("duplicate launch"))
    assert worker.launch_remote_batch(ctx)["status"] == "COMPLETED"


def test_orphaned_process_is_not_restarted(remote, monkeypatch):
    ctx, _, _ = remote
    write_json(ctx.job / "state.json", {"status": "RUNNING", "round_manifest_sha256": ctx.manifest_sha256,
               "pid": 123, "process_start_ticks": "456"})
    monkeypatch.setattr(worker, "_process_ticks", lambda pid: None)
    assert worker.launch_remote_batch(ctx)["status"] == "ORPHANED"


@pytest.mark.parametrize("copy_fails", [False, True])
def test_next_batch_cannot_start_before_verified_return_and_release(tmp_path, monkeypatch, copy_fails):
    """模拟完整两批调度及回传失败；失败不得删除远端或开始下一批。"""
    code = tmp_path / "code"
    root = code / "results" / "round"
    root.mkdir(parents=True)
    (code / "source.sqlite").write_bytes(b"source")
    (code / "policy").mkdir()
    (code / "policy" / "frozen-routing-model.joblib").write_bytes(b"model")
    cfg = {"source_snapshot": "source.sqlite", "source_snapshot_sha256": file_sha256(code / "source.sqlite"),
           "policy_package": "policy", "model_sha256": file_sha256(code / "policy/frozen-routing-model.joblib"),
           "policy_manifest_sha256": "c" * 64, "training_manifest_sha256": "e" * 64}
    (root / "inference-batches").mkdir()
    batches = []
    for n in (1, 2):
        path = root / "inference-batches" / f"batch-{n:03d}.csv"
        path.write_text(f"input{n}")
        batches.append({"filename": str(path.relative_to(root)), "count": 2, "sha256": file_sha256(path)})
    write_json(root / "round-manifest.json", {"code_version": VERSION, "round_config": cfg,
               "batches": batches, "model_input_count": 4})
    digest = file_sha256(root / "round-manifest.json")
    monkeypatch.setattr(execution.subprocess, "check_output", lambda args, **kw: VERSION if args[1] == "rev-parse" else "")
    events = []

    class Transport:
        """只模拟服务器协议，不触及网络或GPU。"""
        manifest_sha256 = digest

        def upload_input(self, local, filename):
            events.append(("upload", Path(filename).stem))

        def action(self, action, name, accepted=None):
            events.append((action, name))
            if action == "inspect":
                return {"status": "NOT_STARTED"}
            if action == "release":
                # 本机完成账本必须先于远端删除请求持久化。
                saved = json.loads((root / "execution-state.json").read_text())
                assert name in saved["completed_batches"]
                return {"status": "RELEASED"}
            batch = next(item for item in batches if Path(item["filename"]).stem == name)
            exported = _native(root, name, batch, cfg)
            exported["round_manifest_sha256"] = digest
            self.path = root / f"{name}-transfer.json"
            write_json(self.path, exported)
            return {"status": "COMPLETED", "transfer_manifest_sha256": file_sha256(self.path)}

        def download_result(self, local, name):
            events.append(("download", name))
            if copy_fails:
                raise OSError("connection lost")
            return self.path

    if copy_fails:
        with pytest.raises(OSError, match="connection lost"):
            execution.execute_remote_round(code, code, root, digest, Transport())
        assert ("release", "batch-001") not in events
        assert ("upload", "batch-002") not in events
    else:
        state = execution.execute_remote_round(code, code, root, digest, Transport())
        assert state["status"] == "BATCHES_SCORED"
        assert events.index(("release", "batch-001")) < events.index(("upload", "batch-002"))
