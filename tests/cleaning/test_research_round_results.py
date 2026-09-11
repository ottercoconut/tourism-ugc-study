"""候选装配的完整性与人工终审隔离测试，不运行判别模型。"""

import copy
import csv
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.research_round import attach_duplicate_groups, prepare_records
from tourism_ugc_study.cleaning.research_round_result_artifacts import _write_candidate_database, _write_review_task
from tourism_ugc_study.cleaning.research_round_results import candidate_summary, merge_candidates
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
