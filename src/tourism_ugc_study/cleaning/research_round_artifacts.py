"""新清洗轮次的输入封存与私有批次持久化；不加载模型或发布最终keep。

新输入、历史人工证据、规则和各批次均绑定摘要。原始/派生库只读打开；输出
原子发布到新的运行目录，既有目录不能覆盖。临时失败不会更换当前研究输入。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from .research_round import attach_duplicate_groups, canonical_hash, prepare_records
from .source_snapshot import file_sha256
from .text_config import load_text_config
from .text_normalize import normalize_post_text


INPUT_COLUMNS = ("source_post_id", "source_version", "component_id", "title", "body", "source_status")


def write_json(path: Path, data: Any) -> None:
    """同目录原子写入私有JSON；用于明确可更新的运行状态或新输出。"""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def load_verified_json(path: Path, expected_sha256: str) -> Any:
    """校验外部摘要再解析JSON；文件变化或不匹配时失败关闭。"""
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("round_artifact_hash_mismatch:" + path.name)
    return json.loads(raw)


def load_round_config(config_path: Path) -> dict[str, Any]:
    """加载本轮绑定，不接受任意阈值或模型替换。"""
    config = yaml.safe_load(config_path.read_text())
    if config["human_evidence_policy"] != "identical_normalized_text_only" or config["final_release_policy"] != "human_review_required":
        raise ValueError("round_policy_invalid")
    if type(config["batch_size"]) is not int or not 1 <= config["batch_size"] <= 500:
        raise ValueError("round_batch_size_invalid")
    if type(config["batch_timeout_seconds"]) is not int or not 120 <= config["batch_timeout_seconds"] <= 1800:
        raise ValueError("round_timeout_invalid")
    return config


def _read_source(path: Path) -> list[dict[str, Any]]:
    """只读冻结源的模型/作者必要列，拒绝SQLite旁文件，避免遗漏WAL。"""
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("round_source_sidecar_present")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return [dict(row) for row in conn.execute(
            "SELECT id,platform_key,platform_post_id,canonical_url,title,content_text,status,author_platform_id "
            "FROM web_posts ORDER BY id"
        )]


def prepare_round(workspace: Path, config_path: Path, output: Path, *, code_version: str) -> dict[str, Any]:
    """封存完整人口及模型批次；历史人工标签只在同规范正文时沿用为候选证据。

    输出保留全部帖子和精确/近重复信息，不进行自动删除，不把人工证据变成
    本轮最终审查完成。任何缺失成员、摘要漂移、近重复旧真值未承接均拒绝运行。
    """
    workspace = workspace.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("round_output_exists")
    cfg = load_round_config(config_path)
    pointer = json.loads((workspace / cfg["source_pointer"]).read_text())
    source_path = Path(pointer["snapshot_path"])
    if pointer["snapshot_sha256"] != cfg["source_snapshot_sha256"] or file_sha256(source_path) != cfg["source_snapshot_sha256"]:
        raise ValueError("round_source_hash_mismatch")
    source_manifest = load_verified_json(Path(pointer["manifest_path"]), pointer["manifest_sha256"])
    if source_manifest["filter"]["sql"] != "topic_relevant = 1":
        raise ValueError("round_source_filter_mismatch")
    old_path = workspace / cfg["historical_source"]
    if file_sha256(old_path) != cfg["historical_source_sha256"]:
        raise ValueError("round_historical_source_hash_mismatch")
    normalization_path = workspace / cfg["normalization_config"]
    if file_sha256(normalization_path) != cfg["normalization_sha256"]:
        raise ValueError("round_normalization_hash_mismatch")
    normalization = load_text_config(normalization_path)
    current = _read_source(source_path)
    if len(current) != cfg["source_count"]:
        raise ValueError("round_source_count_mismatch")
    historical = {row["id"]: row for row in _read_source(old_path)}
    with sqlite3.connect((workspace / cfg["historical_lineage"]).as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        versions = dict(conn.execute("SELECT source_post_id,max(source_version) FROM source_post_versions GROUP BY source_post_id"))
        if conn.execute("SELECT count(*) FROM text_near_duplicate_adjudications").fetchone()[0] or conn.execute("SELECT count(*) FROM text_leakage_members WHERE confirmed_near_edge_used=1").fetchone()[0]:
            raise ValueError("round_historical_near_truth_requires_mapping")
    training_root = workspace / cfg["training_package"]
    training_manifest = load_verified_json(training_root / "snapshot-manifest.json", cfg["training_manifest_sha256"])
    spec = training_manifest["artifacts"]["records"]
    training = load_verified_json(training_root / spec["filename"], spec["sha256"])
    if len(training) != training_manifest["count"] or len({row["source_post_id"] for row in training}) != len(training):
        raise ValueError("round_training_evidence_count_invalid")
    human = {r["source_post_id"]: {"tourism_label": r["tourism_label"], "normalized_sha256": r["normalized_sha256"],
                                      "origin": "human_training_label", "evidence_sha256": spec["sha256"]} for r in training}
    audit_root = workspace / cfg["audit_package"]
    audit_manifest = load_verified_json(audit_root / "audit-assessment-manifest.json", cfg["audit_manifest_sha256"])
    audit_spec = audit_manifest["artifacts"]["labels"]
    audit = load_verified_json(audit_root / audit_spec["filename"], audit_spec["sha256"])
    if len(audit) != audit_manifest["count"]:
        raise ValueError("round_audit_evidence_count_invalid")
    for row in audit:
        post_id = row["source_post_id"]
        if post_id in human or post_id not in historical or row["tourism_label"] not in ("related", "unrelated"):
            raise ValueError("round_human_evidence_identity_invalid")
        if row["source_version"] != versions.get(post_id):
            raise ValueError("round_audit_evidence_version_mismatch")
        old = historical[post_id]
        normalized = normalize_post_text(old["title"], old["content_text"], source_status=old["status"], config=normalization)
        human[post_id] = {"tourism_label": row["tourism_label"], "normalized_sha256": normalized.normalized_sha256,
                          "origin": "human_audit_label", "evidence_sha256": audit_spec["sha256"]}
    records = prepare_records(current, historical, versions, human, normalization)
    print(json.dumps({"stage": "normalized", "records": len(records)}, ensure_ascii=False), flush=True)
    duplicate_summary = attach_duplicate_groups(records, normalization)
    model_records = [row for row in records if row["prior_human_label"] is None]
    training_identities = {(row["source_post_id"], row["source_version"]) for row in training}
    if any((row["source_post_id"], row["source_version"]) in training_identities for row in model_records):
        raise ValueError("round_training_member_overlap")
    if file_sha256(source_path) != cfg["source_snapshot_sha256"] or file_sha256(old_path) != cfg["historical_source_sha256"]:
        raise ValueError("round_source_changed_during_preparation")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".round-prepare-", dir=output.parent) as stage:
        directory = Path(stage)
        write_json(directory / "records.json", records)
        write_json(directory / "near-duplicate-candidates.json", duplicate_summary.pop("near_pairs"))
        batches = []
        batch_dir = directory / "inference-batches"
        batch_dir.mkdir()
        for offset in range(0, len(model_records), cfg["batch_size"]):
            batch = model_records[offset:offset + cfg["batch_size"]]
            name = f"batch-{len(batches) + 1:03d}.csv"
            path = batch_dir / name
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                os.chmod(path, 0o600)
                writer = csv.DictWriter(stream, fieldnames=INPUT_COLUMNS, lineterminator="\n", extrasaction="ignore")
                writer.writeheader()
                writer.writerows(batch)
            batches.append({"filename": "inference-batches/" + name, "count": len(batch), "sha256": file_sha256(path)})
        manifest = {"artifact_kind": "full-research-cleaning-round-input", "status": "PREPARED",
                    "round_name": cfg["round_name"], "code_version": code_version,
                    "round_config": cfg, "round_config_sha256": file_sha256(config_path),
                    "source_pointer": pointer, "record_count": len(records),
                    "model_input_count": len(model_records), "prior_human_count": len(records) - len(model_records),
                    "prior_human_origins": dict(Counter(row["prior_human_origin"] for row in records if row["prior_human_label"])),
                    "input_changes": dict(Counter(row["input_change"] for row in records)),
                    "records_sha256": file_sha256(directory / "records.json"),
                    "near_candidates_sha256": file_sha256(directory / "near-duplicate-candidates.json"),
                    "source_versions_sha256": canonical_hash([[r["source_post_id"], r["source_version"], r["raw_input_sha256"]] for r in records]),
                    "batches": batches, "classifier_fit_call_count": 0, "predict_call_count": 0,
                    "source_write_count": 0, "final_keep_published_count": 0, **duplicate_summary}
        write_json(directory / "round-manifest.json", manifest)
        os.rename(directory, output)
    return manifest
