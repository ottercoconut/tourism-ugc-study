"""隐藏模型答案的标签一致性复核选择、盲化与回填测试。"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

import tourism_ugc_study.models.text.blind_label_review_artifacts as artifacts
from tourism_ugc_study.models.text.blind_label_review import (
    BlindLabelReviewError,
    DevelopmentReviewRecord,
    select_blind_label_review,
    summarize_blind_label_review,
)
from tourism_ugc_study.models.text.blind_label_review_artifacts import (
    TASK_FIELDS,
    apply_approved_blind_label_corrections,
    prepare_blind_label_review_package,
    summarize_blind_label_review_package,
    write_blind_label_review_summary,
)
from tourism_ugc_study.cleaning.reference_evidence import REFERENCE_FIELDS


def _record(
    post_id: int,
    *,
    split: str,
    label: str,
    probability: float,
    length: int,
) -> DevelopmentReviewRecord:
    """构造指定类别、概率和长度层的开发记录。"""

    if length < 300:
        band = "0000-0299"
    elif length < 600:
        band = "0300-0599"
    else:
        band = "0600-1199"
    return DevelopmentReviewRecord(
        source_post_id=post_id,
        source_version=1,
        split_name=split,
        original_label=label,
        p_unrelated=probability,
        review_text=(f"记录{post_id}" + "字" * length)[:length],
        length_band=band,
    )


def _records() -> tuple[DevelopmentReviewRecord, ...]:
    """构造两个目标及各自可精确匹配的随机正确对照池。"""

    return (
        _record(
            1,
            split="train_oof",
            label="related",
            probability=0.96,
            length=200,
        ),
        _record(
            2,
            split="train_oof",
            label="related",
            probability=0.10,
            length=210,
        ),
        _record(
            3,
            split="train_oof",
            label="related",
            probability=0.20,
            length=220,
        ),
        _record(
            4,
            split="validation",
            label="unrelated",
            probability=0.30,
            length=500,
        ),
        _record(
            5,
            split="validation",
            label="unrelated",
            probability=0.80,
            length=510,
        ),
        _record(
            6,
            split="validation",
            label="unrelated",
            probability=0.90,
            length=520,
        ),
    )


def _write_completed_task(path: Path, labels: dict[str, str]) -> None:
    """在测试中模拟人工只填写标签，不改固定列。"""

    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TASK_FIELDS)
        writer.writeheader()
        for row in rows:
            row["review_label"] = labels[row["review_key"]]
            writer.writerow(row)


def _write_reference_pair(csv_path: Path, manifest_path: Path) -> str:
    """构造足以测试显式原位改标的700条最终证据外壳。"""

    source_labels = {record.source_post_id: record.original_label for record in _records()}
    rows = []
    for index in range(1, 701):
        probability = index <= 500
        rows.append(
            {
                "task_id": f"task-{index}",
                "source_post_id": str(index),
                "source_version": "1",
                "normalized_model_text": f"[TITLE]\n标题{index}\n[BODY]\n正文{index}",
                "tourism_label": source_labels.get(
                    index, "related" if index % 2 else "unrelated"
                ),
                "sample_frame": "probability" if probability else "targeted",
                "selection_reason_code": "random" if probability else "targeted",
                "selection_rank": str(index if probability else index - 500),
                "inclusion_probability": "0.5" if probability else "",
                "analysis_weight": "2.0" if probability else "",
                "evidence_origin": "existing_representative",
                "duplicate_component_id": f"component-{index}",
            }
        )
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REFERENCE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    csv_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "artifact_contract": "final-nonduplicate-model-reference",
        "status": "finalized",
        "csv": {"sha256": csv_sha256},
        "hashes": {
            "output_csv_sha256": csv_sha256,
            "final_member_sha256": "a" * 64,
        },
        "label_counts": {
            "related": sum(row["tourism_label"] == "related" for row in rows),
            "unrelated": sum(row["tourism_label"] == "unrelated" for row in rows),
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return csv_sha256


def test_selection_is_reproducible_and_task_hides_all_model_answers() -> None:
    first = select_blind_label_review(
        _records(), model_id="model-1", random_seed=17
    )
    second = select_blind_label_review(
        _records(), model_id="model-1", random_seed=17
    )

    assert first == second
    assert first.target_count == 2
    assert first.control_count == 2
    assert all(set(row) == set(TASK_FIELDS) for row in first.task_rows)
    assert all(row["review_label"] == "" for row in first.task_rows)
    assert all("label" not in key or key == "review_label" for row in first.task_rows for key in row)
    assert {row["selection_group"] for row in first.mapping_records} == {
        "model_conflict_target",
        "matched_correct_control",
    }
    assert all("platform" not in row for row in first.mapping_records)


def test_selection_fails_when_exact_matched_control_is_unavailable() -> None:
    with pytest.raises(BlindLabelReviewError) as error:
        select_blind_label_review(
            _records()[:1], model_id="model-1", random_seed=17
        )

    assert error.value.reason_code == "blind_review_control_pool_insufficient"


def test_selection_reports_nearest_length_fallback_without_relaxing_label_or_split() -> None:
    records = (
        _record(
            1,
            split="train_oof",
            label="related",
            probability=0.96,
            length=200,
        ),
        _record(
            2,
            split="train_oof",
            label="related",
            probability=0.10,
            length=350,
        ),
    )

    selection = select_blind_label_review(
        records,
        model_id="model-1",
        random_seed=17,
    )
    control = next(
        row
        for row in selection.mapping_records
        if row["selection_group"] == "matched_correct_control"
    )

    assert selection.exact_match_control_count == 0
    assert selection.relaxed_match_control_count == 1
    assert control["matching_level"] == "nearest_length_same_label_split"
    assert control["original_label"] == "related"
    assert control["split_name"] == "train_oof"


def test_selection_rejects_any_test_split_record() -> None:
    records = (*_records(), _record(
        7,
        split="test",
        label="related",
        probability=0.1,
        length=300,
    ))

    with pytest.raises(BlindLabelReviewError) as error:
        select_blind_label_review(records, model_id="model-1", random_seed=17)

    assert error.value.reason_code == "blind_review_development_record_invalid"


def test_summary_reports_target_and_control_separately() -> None:
    selection = select_blind_label_review(
        _records(), model_id="model-1", random_seed=17
    )
    completed = []
    for row in selection.task_rows:
        mapping = next(
            item
            for item in selection.mapping_records
            if item["review_key"] == row["review_key"]
        )
        reviewed = (
            "unrelated"
            if mapping["selection_group"] == "model_conflict_target"
            else mapping["original_label"]
        )
        completed.append({**row, "review_label": str(reviewed)})

    summary = summarize_blind_label_review(
        selection.mapping_records,
        completed,
        model_id="model-1",
    )

    assert summary["group_summaries"]["model_conflict_target"]["changed_count"] == 1
    assert summary["group_summaries"]["matched_correct_control"]["changed_count"] == 0
    assert summary["automatic_reference_update"] is False
    assert summary["test_members_read"] is False


def test_private_package_round_trip_and_tamper_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.csv"
    reference.write_text("private-reference-placeholder\n", encoding="utf-8")
    monkeypatch.setattr(
        artifacts,
        "_load_development_records",
        lambda *_args: ("model-1", "a" * 64, "b" * 64, _records()),
    )
    review_dir = tmp_path / "blind-review"
    result = prepare_blind_label_review_package(
        tmp_path,
        reference,
        review_dir,
        random_seed=17,
    )
    mapping = json.loads((review_dir / "review-map.json").read_text(encoding="utf-8"))
    labels = {
        record["review_key"]: record["original_label"]
        for record in mapping["records"]
    }
    _write_completed_task(review_dir / "review-task.csv", labels)
    summary = summarize_blind_label_review_package(review_dir)
    written = write_blind_label_review_summary(
        review_dir,
        tmp_path / "summary.json",
    )

    assert result.total_count == 4
    assert summary == written
    assert summary["status"] == "completed_not_applied"
    assert not any("review_text" in record for record in summary["records"])

    task_path = review_dir / "review-task.csv"
    task_text = task_path.read_text(encoding="utf-8")
    task_path.write_text(task_text.replace("记录", "篡改", 1), encoding="utf-8")
    with pytest.raises(BlindLabelReviewError) as error:
        summarize_blind_label_review_package(review_dir)
    assert error.value.reason_code == "blind_review_task_fixed_content_changed"


def test_completed_task_rejects_illegal_or_missing_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.csv"
    reference.write_text("private-reference-placeholder\n", encoding="utf-8")
    monkeypatch.setattr(
        artifacts,
        "_load_development_records",
        lambda *_args: ("model-1", "a" * 64, "b" * 64, _records()),
    )
    review_dir = tmp_path / "blind-review"
    prepare_blind_label_review_package(
        tmp_path,
        reference,
        review_dir,
        random_seed=17,
    )
    mapping = json.loads((review_dir / "review-map.json").read_text(encoding="utf-8"))
    labels = {
        record["review_key"]: ""
        for record in mapping["records"]
    }
    _write_completed_task(review_dir / "review-task.csv", labels)

    with pytest.raises(BlindLabelReviewError) as error:
        summarize_blind_label_review_package(review_dir)
    assert error.value.reason_code == "blind_review_completed_row_invalid"


def test_private_mapping_hash_change_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.csv"
    reference.write_text("private-reference-placeholder\n", encoding="utf-8")
    monkeypatch.setattr(
        artifacts,
        "_load_development_records",
        lambda *_args: ("model-1", "a" * 64, "b" * 64, _records()),
    )
    review_dir = tmp_path / "blind-review"
    prepare_blind_label_review_package(
        tmp_path,
        reference,
        review_dir,
        random_seed=17,
    )
    mapping_path = review_dir / "review-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["records"][0]["original_label"] = "unrelated"
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(BlindLabelReviewError) as error:
        summarize_blind_label_review_package(review_dir)
    assert error.value.reason_code == "blind_review_mapping_hash_mismatch"


def test_explicit_approval_applies_only_selected_changes_and_records_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "final-reference.csv"
    reference_manifest = tmp_path / "final-reference.manifest.json"
    old_csv_sha256 = _write_reference_pair(reference, reference_manifest)
    monkeypatch.setattr(
        artifacts,
        "_load_development_records",
        lambda *_args: ("model-1", "a" * 64, "b" * 64, _records()),
    )
    review_dir = tmp_path / "blind-review"
    prepare_blind_label_review_package(
        tmp_path,
        reference,
        review_dir,
        random_seed=17,
    )
    mapping = json.loads((review_dir / "review-map.json").read_text(encoding="utf-8"))
    target_keys = [
        record["review_key"]
        for record in mapping["records"]
        if record["selection_group"] == "model_conflict_target"
    ]
    labels = {
        record["review_key"]: (
            "unrelated"
            if record["original_label"] == "related"
            else "related"
        )
        if record["review_key"] in target_keys
        else record["original_label"]
        for record in mapping["records"]
    }
    _write_completed_task(review_dir / "review-task.csv", labels)
    summary_path = tmp_path / "summary.json"
    write_blind_label_review_summary(review_dir, summary_path)

    receipt = apply_approved_blind_label_corrections(
        review_dir,
        summary_path,
        reference,
        reference_manifest,
        tmp_path / "receipt.json",
        approved_review_keys=[target_keys[0]],
        expected_reference_csv_sha256=old_csv_sha256,
    )

    with reference.open(encoding="utf-8", newline="") as stream:
        updated_rows = {
            int(row["source_post_id"]): row for row in csv.DictReader(stream)
        }
    updated_manifest = json.loads(reference_manifest.read_text(encoding="utf-8"))
    approved_mapping = next(
        record for record in mapping["records"] if record["review_key"] == target_keys[0]
    )
    rejected_mapping = next(
        record for record in mapping["records"] if record["review_key"] == target_keys[1]
    )

    assert receipt["approved_change_count"] == 1
    assert receipt["rejected_change_count"] == 1
    assert updated_rows[int(approved_mapping["source_post_id"])]["tourism_label"] == labels[target_keys[0]]
    assert updated_rows[int(rejected_mapping["source_post_id"])]["tourism_label"] == rejected_mapping["original_label"]
    assert updated_manifest["label_consistency_reviews"][0]["review_id"] == receipt["review_id"]
    assert updated_manifest["csv"]["sha256"] == receipt["reference_csv_sha256"]
    assert updated_manifest["hashes"]["final_member_sha256"] == receipt["reference_member_sha256"]
