"""封存前的清洗谱系验收：只读检查完整批次和候选，不调用模型或决定人工标签。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .research_freeze_files import owned_path
from .research_remote_execution import accept_remote_result
from .source_snapshot import file_sha256


def validate_completed_round(workspace: Path, config: dict[str, Any]) -> dict[str, Any]:
    """核对显式SHA锚、全部回传向量、完成账本和逐post覆盖，返回无正文汇总。

    冻结不恢复或重跑推理。任何缺批、未释放、人工终审已变更或人口错配均失败；
    SHA锚由版本化配置给出，不能由当前可疑文件自行生成期待值。
    """
    for relative, digest in config["expected_sha256"].items():
        if file_sha256(owned_path(workspace, relative)) != digest:
            raise ValueError("freeze_anchor_mismatch:" + relative)
    round_root = owned_path(workspace, config["round_root"])
    candidate_root = owned_path(workspace, config["candidate_root"])
    source = owned_path(workspace, config["source_snapshot"])
    manifest = json.loads((round_root / "round-manifest.json").read_text())
    state = json.loads((round_root / "execution-state.json").read_text())
    candidate = json.loads((candidate_root / "candidate-manifest.json").read_text())
    digest = file_sha256(round_root / "round-manifest.json")
    names = [Path(batch["filename"]).stem for batch in manifest["batches"]]
    if (state["status"] != "BATCHES_SCORED" or state.get("active_batch") is not None
            or set(names) != set(state["completed_batches"]) or len(names) != len(set(names))
            or sorted(names) != sorted(state["released_batches"])
            or state["round_manifest_sha256"] != digest or candidate["round_manifest_sha256"] != digest
            or candidate["source_snapshot_sha256"] != file_sha256(source)
            or candidate["execution_state_sha256"] != file_sha256(round_root / "execution-state.json")
            or candidate["status"] != "CANDIDATES_READY_AWAITING_HUMAN_REVIEW"
            or candidate["final_keep_published_count"] != 0):
        raise ValueError("freeze_round_not_completed_or_binding_mismatch")
    for batch in manifest["batches"]:
        name = Path(batch["filename"]).stem
        receipt = state["completed_batches"][name]
        if file_sha256(round_root / batch["filename"]) != batch["sha256"]:
            raise ValueError("freeze_batch_input_mismatch")
        # 调用只读原生验收器；返回的新观测时间不写回旧回执。
        accept_remote_result(round_root, batch, manifest["round_config"], manifest["code_version"],
                             round_root / "remote-receipts" / (name + "-transfer.json"),
                             receipt["transfer_manifest_sha256"], digest)
    database = candidate_root / "cleaning-candidates.sqlite"
    if file_sha256(database) != candidate["database_sha256"]:
        raise ValueError("freeze_candidate_hash_mismatch")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("freeze_candidate_integrity_failed")
        counts = dict(conn.execute("SELECT decision,count(*) FROM cleaning_candidates GROUP BY decision"))
        pending = conn.execute("SELECT count(*) FROM cleaning_candidates WHERE human_final_review_status='pending'").fetchone()[0]
        ids = [r[0] for r in conn.execute("SELECT source_post_id FROM cleaning_candidates ORDER BY source_post_id")]
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as conn:
        source_ids = [r[0] for r in conn.execute("SELECT id FROM web_posts ORDER BY id")]
    if (ids != source_ids or len(ids) != manifest["record_count"] or pending != len(ids)
            or counts != candidate["decision_counts"]):
        raise ValueError("freeze_candidate_population_or_review_mismatch")
    return {"record_count": len(ids), "decision_counts": counts, "human_final_review_pending_count": pending,
            "verified_batches": len(names), "inference_code_version": manifest["code_version"],
            "independent_model_accuracy_validation": False, "model_rerun": False,
            "source_write_count": 0, "final_keep_published_by_freeze": 0}
