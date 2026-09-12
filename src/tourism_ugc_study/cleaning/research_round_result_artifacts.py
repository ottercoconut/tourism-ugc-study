"""验收原生预测包并原子生成独立候选SQLite与盲审材料。

输出是不可覆盖的清洗候选快照，不操作管理端final_kept_posts或任何源库。
所有正文、身份和预测只存放于私有目录，公开报告只引用汇总与摘要。
"""

from __future__ import annotations

import csv
import json
import os
import random
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .research_round import canonical_hash
from .research_round_artifacts import load_verified_json, write_json
from .research_round_execution import verify_batch_output
from .research_round_results import candidate_summary, merge_candidates
from .source_snapshot import file_sha256


_REVIEW_COLUMNS = ("task_id", "sample_run_id", "normalized_model_text", "tourism_label")
_DB_COLUMNS = ("source_post_id", "source_version", "platform_key", "title", "body", "source_status",
               "normalized_model_text", "normalized_sha256", "raw_input_sha256", "input_change",
               "component_id", "exact_cluster_id", "exact_representative_id", "exact_cluster_size",
               "exact_cluster_decision_conflict", "decision", "decision_origin", "p_unrelated",
               "prior_human_label", "decision_evidence_sha256", "human_final_review_status")


def _write_candidate_database(path: Path, records: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    """新建仅含候选和运行元数据的SQLite，一条post一个候选，不改变成员数。"""
    with sqlite3.connect(path) as conn:
        os.chmod(path, 0o600)
        conn.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE cleaning_candidates (
                source_post_id INTEGER PRIMARY KEY CHECK(source_post_id>0),
                source_version INTEGER NOT NULL CHECK(source_version>0),
                platform_key TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                source_status TEXT NOT NULL, normalized_model_text TEXT NOT NULL,
                normalized_sha256 TEXT NOT NULL, raw_input_sha256 TEXT NOT NULL,
                input_change TEXT NOT NULL CHECK(input_change IN ('new','changed','unchanged')),
                component_id TEXT NOT NULL, exact_cluster_id TEXT NOT NULL,
                exact_representative_id INTEGER NOT NULL, exact_cluster_size INTEGER NOT NULL,
                exact_cluster_decision_conflict INTEGER NOT NULL CHECK(exact_cluster_decision_conflict IN (0,1)),
                decision TEXT NOT NULL CHECK(decision IN ('keep','exclude','manual_review')),
                decision_origin TEXT NOT NULL, p_unrelated REAL CHECK(p_unrelated BETWEEN 0 AND 1),
                prior_human_label TEXT CHECK(prior_human_label IN ('related','unrelated')),
                decision_evidence_sha256 TEXT NOT NULL,
                human_final_review_status TEXT NOT NULL CHECK(human_final_review_status='pending')
            );
            CREATE INDEX idx_candidates_decision ON cleaning_candidates(decision,source_post_id);
            CREATE INDEX idx_candidates_exact ON cleaning_candidates(exact_cluster_id);
            CREATE INDEX idx_candidates_component ON cleaning_candidates(component_id);
            CREATE TABLE round_metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
        """)
        columns = ",".join(_DB_COLUMNS)
        values = ",".join("?" for _ in _DB_COLUMNS)
        conn.executemany(f"INSERT INTO cleaning_candidates({columns}) VALUES ({values})",
                         [tuple(row[column] for column in _DB_COLUMNS) for row in records])
        conn.executemany("INSERT INTO round_metadata VALUES (?,?)",
                         [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in metadata.items()])
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or conn.execute("SELECT count(*) FROM cleaning_candidates").fetchone()[0] != len(records):
            raise ValueError("round_candidate_database_integrity_failed")


def _write_review_task(directory: Path, records: list[dict[str, Any]], *,
                       round_name: str, scope: str, seed: int) -> dict[str, Any]:
    """导出随机顺序盲四列表；概率、动作、身份只留在配对私有映射中。

按scope选择中间层或keep候选，不预填任何人工标签，不进行自动标注。
    """
    selected = [row for row in records if row["decision"] == scope]
    random.Random(seed).shuffle(selected)
    filename = scope.replace("_", "-") + "-review.csv"
    private = []
    with (directory / filename).open("w", encoding="utf-8-sig", newline="") as stream:
        os.chmod(directory / filename, 0o600)
        writer = csv.DictWriter(stream, fieldnames=_REVIEW_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in selected:
            task_id = canonical_hash([round_name, scope, row["source_post_id"], row["source_version"], row["normalized_sha256"]])[:24]
            writer.writerow({"task_id": task_id, "sample_run_id": round_name,
                             "normalized_model_text": row["normalized_model_text"], "tourism_label": ""})
            private.append({"task_id": task_id, **{key: row[key] for key in ("source_post_id", "source_version", "normalized_sha256", "decision", "decision_origin")}})
    map_name = filename.removesuffix(".csv") + "-private-map.json"
    write_json(directory / map_name, private)
    return {"filename": filename, "sha256": file_sha256(directory / filename), "count": len(selected),
            "private_map_filename": map_name, "private_map_sha256": file_sha256(directory / map_name),
            "columns": list(_REVIEW_COLUMNS), "random_seed": seed}


def assemble_round(round_root: Path, output: Path, expected_manifest_sha256: str,
                   *, workspace: Path, code_version: str) -> dict[str, Any]:
    """全部模型批次完成后生成候选发布包；任何未完成或缺失记录均拒绝发布。

不修改执行账本或输入manifest。装配代码可以晚于固定推理代码，两个提交身份
分别记录，避免长运行期间改文档导致运行身份漂移。完整候选仍等待人工审查。
    """
    round_root, output, workspace = round_root.resolve(), output.resolve(), workspace.resolve()
    if output.exists():
        raise FileExistsError("round_result_output_exists")
    manifest = load_verified_json(round_root / "round-manifest.json", expected_manifest_sha256)
    state = json.loads((round_root / "execution-state.json").read_text())
    if state["status"] != "BATCHES_SCORED" or state["round_manifest_sha256"] != expected_manifest_sha256:
        raise ValueError("round_inference_incomplete")
    config = manifest["round_config"]
    records = load_verified_json(round_root / "records.json", manifest["records_sha256"])
    if len(records) != manifest["record_count"]:
        raise ValueError("round_result_input_count_mismatch")
    names = {Path(batch["filename"]).stem for batch in manifest["batches"]}
    if set(state["completed_batches"]) != names:
        raise ValueError("round_result_batch_set_mismatch")
    scored = []
    receipts = {}
    for batch in manifest["batches"]:
        name = Path(batch["filename"]).stem
        if file_sha256(round_root / batch["filename"]) != batch["sha256"]:
            raise ValueError("round_result_input_batch_changed")
        receipt = verify_batch_output(round_root / "inference" / name, batch, config,
                                      manifest["code_version"], state["completed_batches"][name]["manifest_sha256"])
        receipts[name] = receipt
        path = Path(receipt["manifest_path"])
        batch_manifest = load_verified_json(path, receipt["manifest_sha256"])
        artifact = batch_manifest["artifacts"]["records"]
        batch_records = load_verified_json(path.parent / artifact["filename"], artifact["sha256"])
        if len(batch_records) != batch["count"]:
            raise ValueError("round_result_batch_count_mismatch")
        previous_limit = csv.field_size_limit()
        try:
            input_path = round_root / batch["filename"]
            csv.field_size_limit(max(previous_limit, input_path.stat().st_size))
            with input_path.open(encoding="utf-8-sig", newline="") as stream:
                expected_identities = {(int(row["source_post_id"]), int(row["source_version"])) for row in csv.DictReader(stream)}
        finally:
            csv.field_size_limit(previous_limit)
        if {(row["source_post_id"], row["source_version"]) for row in batch_records} != expected_identities:
            raise ValueError("round_result_batch_identity_mismatch")
        for row in batch_records:
            row["evidence_manifest_sha256"] = receipt["manifest_sha256"]
            scored.append(row)
    if len(scored) != manifest["model_input_count"]:
        raise ValueError("round_result_model_count_mismatch")
    policy = load_verified_json(workspace / config["policy_package"] / "routing-policy-manifest.json", config["policy_manifest_sha256"])
    candidates = merge_candidates(records, scored, T_keep=policy["selected"]["T_keep"], T_exclude=policy["selected"]["T_exclude"])
    source_path = workspace / config.get("source_snapshot", manifest["source_pointer"]["snapshot_path"])
    if file_sha256(source_path) != config["source_snapshot_sha256"]:
        raise ValueError("round_result_source_changed")
    metadata = {"status": "CANDIDATES_READY_AWAITING_HUMAN_REVIEW", "round_name": config["round_name"],
                "source_snapshot_sha256": config["source_snapshot_sha256"],
                "round_manifest_sha256": expected_manifest_sha256, "inference_code_version": manifest["code_version"],
                "assembly_code_version": code_version, "policy_manifest_sha256": config["policy_manifest_sha256"],
                "final_keep_published_count": 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".round-result-", dir=output.parent) as stage:
        directory = Path(stage)
        database = directory / "cleaning-candidates.sqlite"
        _write_candidate_database(database, candidates, metadata)
        near = load_verified_json(round_root / "near-duplicate-candidates.json", manifest["near_candidates_sha256"])
        write_json(directory / "near-duplicate-candidates.json", near)
        tasks = {scope: _write_review_task(directory, candidates, round_name=config["round_name"], scope=scope,
                                           seed=config["random_seed"] + offset)
                 for offset, scope in enumerate(("manual_review", "keep"))}
        result = {"artifact_kind": "full-research-cleaning-candidates", **metadata, **candidate_summary(candidates),
                  "database_filename": database.name, "database_sha256": file_sha256(database),
                  "review_tasks": tasks, "near_candidate_count": len(near),
                  "near_candidates_sha256": file_sha256(directory / "near-duplicate-candidates.json"),
                  "batch_receipts": receipts, "fit_call_count": 0, "source_database_write_count": 0,
                  "source_records_deleted": 0, "execution_state_sha256": file_sha256(round_root / "execution-state.json")}
        write_json(directory / "candidate-manifest.json", result)
        os.rename(directory, output)
    return result
