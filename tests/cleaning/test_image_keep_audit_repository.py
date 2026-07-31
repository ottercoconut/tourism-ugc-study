"""图片保留集两层审计仓储的合成人口集成测试。"""

from __future__ import annotations

import csv
from pathlib import Path

from tourism_ugc_study.cleaning.image_decision_repository import (
    build_image_decisions,
    propagate_exact_sha_labels,
)
from tourism_ugc_study.cleaning.image_keep_audit_repository import (
    create_keep_audit_round,
    evaluate_keep_audit_round,
    export_keep_audit_tasks,
    import_keep_audit_annotations,
)
from tourism_ugc_study.cleaning.image_review_repository import (
    create_double_label_plan,
    create_image_review_run,
    export_image_annotation_tasks,
    import_image_annotations,
)
from tests.cleaning.test_image_decision_repository import (
    _build_with_tiny_duplicate,
    _complete_formal_review_gate,
    _fill_by_fingerprint,
)


def _decisions(tmp_path: Path):
    """完成合成候选人工证据，生成决定与必要 SHA 传播。"""

    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    template1 = tmp_path / "review1-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=1,
        output_path=template1,
    )
    with template1.open("r", encoding="utf-8", newline="") as stream:
        ids = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    # 所有候选均明确标为有效，路线图/推荐计划图形式不会自动排除。
    completed1 = tmp_path / "review1.csv"
    _fill_by_fingerprint(
        template1,
        completed1,
        labels={identity: "valid_content" for identity in ids},
        annotator="1" * 64,
    )
    import_image_annotations(derived, csv_path=completed1, imported_by_hash="a" * 64)
    _complete_formal_review_gate(derived, config, build, tmp_path, prefix="audit-gate")
    # valid_content 不会生成动态第二槽；这里不创建空拟排除计划。
    decisions = build_image_decisions(
        derived, candidate_build_id=build.build_id, config=config
    )
    propagate_exact_sha_labels(derived, decision_build_id=decisions.decision_build_id)
    return derived, config, decisions


def _fill_audit(template: Path, output: Path, *, label: str) -> None:
    with template.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or ())
        rows = list(reader)
    for row in rows:
        row["technical_noise_label"] = label
        row["reason_codes"] = "synthetic_fixture"
        row["annotator_hash"] = "9" * 64
        row["annotated_at_utc"] = "2026-07-31T03:00:00Z"
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_small_keep_population_is_census_and_passes_only_after_complete_labels(
    tmp_path: Path,
) -> None:
    derived, config, decisions = _decisions(tmp_path)
    audit = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    assert audit.interval_method == "census"
    assert audit.primary_count == audit.population_count == 3
    assert audit.supplement_count == 0
    assert create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    ) == audit
    template = tmp_path / "audit-template.csv"
    assert export_keep_audit_tasks(
        derived, audit_round_id=audit.audit_round_id, output_path=template
    ) == 3
    incomplete = evaluate_keep_audit_round(
        derived, audit_round_id=audit.audit_round_id, config=config
    )
    assert incomplete.evaluation_status == "incomplete"

    completed = tmp_path / "audit.csv"
    _fill_audit(template, completed, label="valid_content")
    imported = import_keep_audit_annotations(derived, csv_path=completed)
    assert imported.imported_count == 3
    passed = evaluate_keep_audit_round(
        derived, audit_round_id=audit.audit_round_id, config=config
    )
    assert passed.evaluation_status == "passed"
    assert passed.primary_point_estimate == 0.0
    assert passed.one_sided_upper == 0.0


def test_any_census_noise_fails_round(tmp_path: Path) -> None:
    derived, config, decisions = _decisions(tmp_path)
    audit = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    template = tmp_path / "audit-fail-template.csv"
    export_keep_audit_tasks(
        derived, audit_round_id=audit.audit_round_id, output_path=template
    )
    completed = tmp_path / "audit-fail.csv"
    _fill_audit(template, completed, label="placeholder_or_error")
    import_keep_audit_annotations(derived, csv_path=completed)
    failed = evaluate_keep_audit_round(
        derived, audit_round_id=audit.audit_round_id, config=config
    )
    assert failed.evaluation_status == "failed"
    assert failed.primary_event_count == 3
