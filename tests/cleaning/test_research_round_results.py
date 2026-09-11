"""候选装配的完整性与人工终审隔离测试，不运行判别模型。"""

import copy
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.research_round import attach_duplicate_groups, prepare_records
from tourism_ugc_study.cleaning.research_round_artifacts import INPUT_COLUMNS, write_json
from tourism_ugc_study.cleaning.research_round_completion import complete_when_ready, publish_candidate_pointer
from tourism_ugc_study.cleaning.research_round_result_artifacts import _write_candidate_database, _write_review_task, assemble_round
from tourism_ugc_study.cleaning.research_round_results import candidate_summary, merge_candidates
from tourism_ugc_study.cleaning.source_snapshot import file_sha256
from tourism_ugc_study.cleaning.text_config import load_text_config


CONFIG = load_text_config(Path(__file__).resolve().parents[2] / "configs/cleaning-text-normalization-v1.yaml")


def _inputs():
    """四条合成输入分别覆盖人工、保留边界、排除边界和中间层。"""
    source = [{"id": i, "platform_key": "weibo", "platform_post_id": str(i), "canonical_url": "",
               "title": "", "content_text": f"合成的青岛旅游记录{i}", "status": "ok", "author_platform_id": ""}
              for i in range(1, 5)]
    records = prepare_records(source, {}, {}, {}, CONFIG)
    attach_duplicate_groups(records, CONFIG)
    records[0].update(prior_human_label="related", prior_human_origin="human_training_label",
                      prior_human_evidence_sha256="b" * 64)
    scores = [{**{key: row[key] for key in ("source_post_id", "source_version", "component_id", "normalized_sha256", "normalized_model_text")},
               "p_unrelated": probability, "provisional_action": action, "evidence_manifest_sha256": "c" * 64}
              for row, probability, action in zip(records[1:], (.31, .96, .5), ("auto_keep", "auto_exclude", "manual_review"))]
    return records, scores


def test_candidates_cover_input_and_do_not_fake_human_probabilities():
    records, scores = _inputs()
    result = merge_candidates(records, scores, T_keep=.31, T_exclude=.96)
    assert [row["decision"] for row in result] == ["keep", "keep", "exclude", "manual_review"]
    assert [row["p_unrelated"] for row in result] == [None, .31, .96, .5]
    assert candidate_summary(result)["human_final_review_pending_count"] == 4
    assert candidate_summary(result)["final_keep_published_count"] == 0
    assert records[0].get("decision") is None


@pytest.mark.parametrize("mutation,error", [
    ("missing", "prediction_missing"), ("duplicate", "prediction_duplicate"),
    ("changed_text", "text_or_component_mismatch"), ("changed_component", "text_or_component_mismatch"),
    ("bad_probability", "probability_invalid"), ("bad_action", "action_mismatch"),
    ("unexpected", "unexpected_prediction"), ("human_overlap", "human_prediction_overlap"),
    ("preapproved", "premature_final_review"),
])
def test_invalid_predictions_fail_closed(mutation, error):
    records, scores = _inputs()
    if mutation == "missing":
        scores.pop()
    elif mutation == "duplicate":
        scores.append(dict(scores[0]))
    elif mutation == "changed_text":
        scores[0]["normalized_model_text"] += "被篡改"
    elif mutation == "changed_component":
        scores[0]["component_id"] = "different"
    elif mutation == "bad_probability":
        scores[0]["p_unrelated"] = float("nan")
    elif mutation == "bad_action":
        scores[0]["provisional_action"] = "auto_exclude"
    elif mutation == "unexpected":
        scores.append(dict(scores[0], source_post_id=100))
    elif mutation == "human_overlap":
        scores.append(dict(scores[0], source_post_id=1))
    elif mutation == "preapproved":
        records[0]["human_final_review_status"] = "approved"
    with pytest.raises(ValueError, match=error):
        merge_candidates(records, scores, T_keep=.31, T_exclude=.96)


def test_conflicting_exact_cluster_is_flagged_not_removed():
    records, scores = _inputs()
    records[2]["exact_cluster_id"] = records[1]["exact_cluster_id"]
    result = merge_candidates(records, scores, T_keep=.31, T_exclude=.96)
    assert len(result) == 4
    assert result[1]["exact_cluster_decision_conflict"]
    assert result[2]["exact_cluster_decision_conflict"]
    assert candidate_summary(result)["exact_cluster_decision_conflict_count"] == 1


def test_private_candidate_database_is_complete_and_distinct_from_final_keep(tmp_path):
    records, scores = _inputs()
    merged = merge_candidates(records, scores, T_keep=.31, T_exclude=.96)
    path = tmp_path / "candidates.sqlite"
    _write_candidate_database(path, merged, {"status": "CANDIDATES_READY_AWAITING_HUMAN_REVIEW"})
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM cleaning_candidates").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM cleaning_candidates WHERE human_final_review_status='pending'").fetchone()[0] == 4
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='final_kept_posts'").fetchall()
        assert conn.execute("SELECT p_unrelated FROM cleaning_candidates WHERE source_post_id=1").fetchone()[0] is None


def test_review_is_four_columns_blank_labels_and_repeatable(tmp_path):
    records, scores = _inputs()
    merged = merge_candidates(records, scores, T_keep=.31, T_exclude=.96)
    first = _write_review_task(tmp_path, merged, round_name="synthetic", scope="keep", seed=19)
    original = (tmp_path / first["filename"]).read_bytes()
    second = _write_review_task(tmp_path, copy.deepcopy(merged), round_name="synthetic", scope="keep", seed=19)
    assert first == second
    assert original.startswith(b"\xef\xbb\xbf")
    with (tmp_path / first["filename"]).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ["task_id", "sample_run_id", "normalized_model_text", "tourism_label"]
        assert all(row["tourism_label"] == "" for row in reader)


def _complete_round_package(tmp_path):
    """构造完整私有轮次以覆盖持久化装配，不依赖任何真实UGC或模型权重。"""
    records, scores = _inputs()
    round_root = tmp_path / "round"
    round_root.mkdir()
    input_dir = round_root / "inference-batches"
    input_dir.mkdir()
    input_path = input_dir / "batch-001.csv"
    with input_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=INPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records[1:])
    policy_root = tmp_path / "policy"
    policy_root.mkdir()
    write_json(policy_root / "routing-policy-manifest.json", {"selected": {"T_keep": .31, "T_exclude": .96}})
    write_json(tmp_path / "source-receipt.json", {"synthetic": True})
    cfg = {"round_name": "synthetic", "random_seed": 20260911,
           "source_snapshot_sha256": file_sha256(tmp_path / "source-receipt.json"),
           "policy_package": "policy", "policy_manifest_sha256": file_sha256(policy_root / "routing-policy-manifest.json"),
           "training_manifest_sha256": "d" * 64}
    batch = {"filename": "inference-batches/batch-001.csv", "count": 3, "sha256": file_sha256(input_path)}
    package = round_root / "inference/batch-001/batch-id"
    package.mkdir(parents=True)
    write_json(package / "input-receipt.json", {"input_file_sha256": batch["sha256"], "count": 3})
    write_json(package / "incremental-scored-records.json", scores)
    batch_manifest = {"status": "NEW_BATCH_SCORED", "code_version": "a" * 40, "count": 3,
                      "policy_manifest_sha256": cfg["policy_manifest_sha256"], "snapshot_manifest_sha256": "d" * 64,
                      "fit_call_count": 0, "source_database_write_count": 0, "training_member_overlap_count": 0,
                      "action_counts": {"auto_keep": 1, "auto_exclude": 1, "manual_review": 1},
                      "embedding_cache_hit_count": 0, "embedding_cache_miss_count": 3,
                      "artifacts": {key: {"filename": name, "sha256": file_sha256(package / name)} for key, name in
                                    (("input_receipt", "input-receipt.json"), ("records", "incremental-scored-records.json"))}}
    write_json(package / "incremental-inference-manifest.json", batch_manifest)
    write_json(round_root / "records.json", records)
    write_json(round_root / "near-duplicate-candidates.json", [])
    manifest = {"round_config": cfg, "code_version": "a" * 40, "record_count": 4, "model_input_count": 3,
                "records_sha256": file_sha256(round_root / "records.json"), "batches": [batch],
                "near_candidates_sha256": file_sha256(round_root / "near-duplicate-candidates.json"),
                "source_pointer": {"snapshot_path": str(tmp_path / "source-receipt.json")}}
    write_json(round_root / "round-manifest.json", manifest)
    digest = file_sha256(round_root / "round-manifest.json")
    state = {"status": "BATCHES_SCORED", "round_manifest_sha256": digest,
             "completed_batches": {"batch-001": {"manifest_sha256": file_sha256(package / "incremental-inference-manifest.json")}}}
    write_json(round_root / "execution-state.json", state)
    return round_root, digest, state


def test_complete_round_assembles_once_with_all_artifacts_and_separate_code_ids(tmp_path):
    root, digest, _ = _complete_round_package(tmp_path)
    output = tmp_path / "candidate-release"
    result = assemble_round(root, output, digest, workspace=tmp_path, code_version="b" * 40)
    assert result["status"] == "CANDIDATES_READY_AWAITING_HUMAN_REVIEW"
    assert result["decision_counts"] == {"keep": 2, "exclude": 1, "manual_review": 1}
    assert result["record_count"] == 4
    assert result["inference_code_version"] == "a" * 40
    assert result["assembly_code_version"] == "b" * 40
    assert result["database_sha256"] == file_sha256(output / "cleaning-candidates.sqlite")
    assert result["review_tasks"]["manual_review"]["count"] == 1
    assert result["review_tasks"]["keep"]["count"] == 2
    assert json.loads((output / "candidate-manifest.json").read_text()) == result
    with pytest.raises(FileExistsError, match="output_exists"):
        assemble_round(root, output, digest, workspace=tmp_path, code_version="b" * 40)


def test_incomplete_round_cannot_create_candidate_output(tmp_path):
    root, digest, state = _complete_round_package(tmp_path)
    state["status"] = "RUNNING"
    write_json(root / "execution-state.json", state)
    output = tmp_path / "candidate-release"
    with pytest.raises(ValueError, match="inference_incomplete"):
        assemble_round(root, output, digest, workspace=tmp_path, code_version="b" * 40)
    assert not output.exists()


def test_completed_round_with_missing_batch_cannot_create_output(tmp_path):
    root, digest, state = _complete_round_package(tmp_path)
    state["completed_batches"] = {}
    write_json(root / "execution-state.json", state)
    output = tmp_path / "candidate-release"
    with pytest.raises(ValueError, match="batch_set_mismatch"):
        assemble_round(root, output, digest, workspace=tmp_path, code_version="b" * 40)
    assert not output.exists()


def _current_pointer(tmp_path, root, digest):
    """保存仅供本测试使用的当前轮次指针。"""
    pointer = tmp_path / "data/processed/current-cleaning.json"
    pointer.parent.mkdir(parents=True)
    write_json(pointer, {"round_root": str(root), "round_manifest_sha256": digest,
                         "source_snapshot_sha256": file_sha256(tmp_path / "source-receipt.json"),
                         "status": "FULL_RESEARCH_CLEANING_RUNNING"})
    return pointer


def test_single_completion_publishes_candidates_without_creating_final_keep(tmp_path):
    root, digest, _ = _complete_round_package(tmp_path)
    pointer = _current_pointer(tmp_path, root, digest)
    (root / ".execution.lock").touch()
    output = tmp_path / "data/processed/candidate-release"
    result = complete_when_ready(tmp_path, root, output, digest, code_version="b" * 40)
    assert result["status"] == "CANDIDATES_READY_AWAITING_HUMAN_REVIEW"
    current = json.loads(pointer.read_text())
    assert current["candidate_database_sha256"] == file_sha256(output / "cleaning-candidates.sqlite")
    assert current["human_review_required"] is True
    assert current["final_keep_published_count"] == 0
    assert not (pointer.parent / "final-kept.sqlite").exists()


def test_failed_inference_releases_waiter_without_publishing(tmp_path):
    root, digest, state = _complete_round_package(tmp_path)
    pointer = _current_pointer(tmp_path, root, digest)
    original = pointer.read_bytes()
    (root / ".execution.lock").touch()
    state["status"] = "FAILED"
    write_json(root / "execution-state.json", state)
    output = tmp_path / "candidate-release"
    with pytest.raises(ValueError, match="inference_not_successful"):
        complete_when_ready(tmp_path, root, output, digest, code_version="b" * 40)
    assert not output.exists()
    assert pointer.read_bytes() == original
    assert json.loads((root / "completion-state.json").read_text())["status"] == "FAILED"


@pytest.mark.parametrize("change", ["source", "round"])
def test_candidate_pointer_cannot_overwrite_another_research_round(tmp_path, change):
    root, digest, _ = _complete_round_package(tmp_path)
    pointer = _current_pointer(tmp_path, root, digest)
    output = tmp_path / "candidate-release"
    assemble_round(root, output, digest, workspace=tmp_path, code_version="b" * 40)
    current = json.loads(pointer.read_text())
    current["source_snapshot_sha256" if change == "source" else "round_manifest_sha256"] = "f" * 64
    write_json(pointer, current)
    original = pointer.read_bytes()
    with pytest.raises(ValueError, match="pointer_binding_mismatch"):
        publish_candidate_pointer(pointer, root, output, digest)
    assert pointer.read_bytes() == original
