"""全量轮次测试：结构、版本和人工证据不越界；不接触真实数据或判别模型。"""

from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.research_round import attach_duplicate_groups, prepare_records
from tourism_ugc_study.cleaning.research_round_artifacts import load_verified_json, write_json
from tourism_ugc_study.cleaning.source_snapshot import file_sha256
from tourism_ugc_study.cleaning.text_config import load_text_config


CONFIG = load_text_config(Path(__file__).resolve().parents[2] / "configs/cleaning-text-normalization-v1.yaml")


def _row(post_id=1, body="青岛旅游，海边散步。", author=""):
    """合成不含真实UGC的源记录。"""
    return {"id": post_id, "platform_key": "weibo", "platform_post_id": str(post_id),
            "canonical_url": "https://example.invalid/" + str(post_id), "title": "", "content_text": body,
            "status": "ok", "author_platform_id": author}


def test_versions_change_only_when_effective_inputs_change():
    old = _row()
    current = [dict(old, fetched_at="later"), _row(2), _row(3, author="new")]
    records = prepare_records(current, {1: old, 3: _row(3)}, {1: 2, 3: 1}, {}, CONFIG)
    assert [r["source_version"] for r in records] == [2, 1, 2]
    assert [r["input_change"] for r in records] == ["unchanged", "new", "changed"]
    assert all(r["human_final_review_status"] == "pending" for r in records)


def test_human_evidence_requires_same_normalized_text_not_old_keep():
    old = _row()
    original = prepare_records([old], {1: old}, {1: 1}, {}, CONFIG)[0]
    human = {1: {"tourism_label": "related", "normalized_sha256": original["normalized_sha256"],
                 "origin": "human_training_label", "evidence_sha256": "a" * 64}}
    inherited = prepare_records([dict(old, author_platform_id="updated")], {1: old}, {1: 1}, human, CONFIG)[0]
    changed = prepare_records([_row(body="招聘销售人员")], {1: old}, {1: 1}, human, CONFIG)[0]
    assert inherited["source_version"] == 2
    assert inherited["prior_human_label"] == "related"
    assert inherited["human_final_review_status"] == "pending"
    assert changed["prior_human_label"] is None


@pytest.mark.parametrize("current,historical,versions,error", [
    ([_row(), _row()], {}, {}, "identity_invalid"),
    ([_row()], {1: dict(_row(), platform_post_id="different")}, {1: 1}, "identity_reused"),
    ([_row()], {1: _row()}, {}, "version_missing"),
    ([_row(body="")], {}, {}, "structure_not_usable"),
])
def test_invalid_source_fails_without_dropping_rows(current, historical, versions, error):
    with pytest.raises(ValueError, match=error):
        prepare_records(current, historical, versions, {}, CONFIG)


def test_duplicate_and_author_edges_never_remove_or_inherit_labels():
    rows = [_row(1, author="same"), _row(2, author="different"), _row(3, body="青岛招聘", author="same")]
    records = prepare_records(rows, {}, {}, {}, CONFIG)
    result = attach_duplicate_groups(records, CONFIG)
    assert len(records) == 3
    assert result["exact_duplicate_extra_records"] == 1
    assert result["component_count"] == 1
    assert all(r["prior_human_label"] is None for r in records)
    assert [r["exact_representative_id"] for r in records] == [1, 1, 3]


def test_json_artifact_hash_detects_changes(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"count": 3})
    digest = file_sha256(path)
    assert load_verified_json(path, digest) == {"count": 3}
    write_json(path, {"count": 4})
    with pytest.raises(ValueError, match="hash_mismatch"):
        load_verified_json(path, digest)
