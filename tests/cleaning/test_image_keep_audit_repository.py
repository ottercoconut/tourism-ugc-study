"""图片保留集两层审计仓储的合成人口集成测试。"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.image_decision_repository import (
    build_image_decisions,
    propagate_exact_sha_labels,
)
from tourism_ugc_study.cleaning.image_keep_audit_repository import (
    ImageKeepAuditRepositoryError,
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
from tourism_ugc_study.cleaning.schema import connect_derived
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


def test_next_audit_round_requires_completed_failed_previous_round(
    tmp_path: Path,
) -> None:
    """上一轮未评估或已通过时，都不能为了凑轮次继续抽样。"""

    derived, config, decisions = _decisions(tmp_path)
    first = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    with pytest.raises(ImageKeepAuditRepositoryError) as incomplete:
        create_keep_audit_round(
            derived,
            decision_build_id=decisions.decision_build_id,
            round_number=2,
            config=config,
        )
    assert incomplete.value.reason_code == "previous_image_keep_audit_not_evaluated"

    template = tmp_path / "passed-round-template.csv"
    export_keep_audit_tasks(
        derived, audit_round_id=first.audit_round_id, output_path=template
    )
    completed = tmp_path / "passed-round.csv"
    _fill_audit(template, completed, label="valid_content")
    import_keep_audit_annotations(derived, csv_path=completed)
    assert evaluate_keep_audit_round(
        derived, audit_round_id=first.audit_round_id, config=config
    ).evaluation_status == "passed"
    with pytest.raises(ImageKeepAuditRepositoryError) as passed:
        create_keep_audit_round(
            derived,
            decision_build_id=decisions.decision_build_id,
            round_number=2,
            config=config,
        )
    assert passed.value.reason_code == "previous_image_keep_audit_not_failed"


def test_failed_round_requires_changed_decisions_before_nonoverlap_retry(
    tmp_path: Path,
) -> None:
    """失败后必须形成决定内容不同的新 build，且旧样本仍不得复抽。"""

    derived, config, decisions = _decisions(tmp_path)
    first = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    template = tmp_path / "failed-round-template.csv"
    export_keep_audit_tasks(
        derived, audit_round_id=first.audit_round_id, output_path=template
    )
    completed = tmp_path / "failed-round.csv"
    _fill_audit(template, completed, label="site_ui")
    import_keep_audit_annotations(derived, csv_path=completed)
    assert evaluate_keep_audit_round(
        derived, audit_round_id=first.audit_round_id, config=config
    ).evaluation_status == "failed"

    with pytest.raises(ImageKeepAuditRepositoryError) as unchanged:
        create_keep_audit_round(
            derived,
            decision_build_id=decisions.decision_build_id,
            round_number=2,
            config=config,
        )
    assert unchanged.value.reason_code == "revised_image_decision_build_required"
    with connect_derived(derived) as connection:
        # 即使绕开仓储直接 INSERT，未形成新决定 build 的第二轮也必须失败。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_rounds(
                  audit_round_id, decision_build_id, round_number, random_seed,
                  population_count, population_manifest_sha256, primary_count,
                  supplement_count, primary_manifest_sha256,
                  supplement_manifest_sha256, interval_method, seal_status,
                  created_at_utc
                ) VALUES ('direct-unchanged-round', ?, 2, 20260732, 0, ?, 0, 0,
                          ?, ?, 'census', 'building', '2026-07-31T06:00:00Z')
                """,
                (decisions.decision_build_id, "1" * 64, "2" * 64, "3" * 64),
            )

    revised_review = create_image_review_run(
        derived,
        candidate_build_id=decisions.candidate_build_id,
        review_kind="candidate_review",
        config=config,
        random_seed=config.random_seed + 1,
    )
    revised_slot1_template = tmp_path / "revised-slot1-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=revised_review.review_run_id,
        assignment_slot=1,
        output_path=revised_slot1_template,
    )
    with revised_slot1_template.open("r", encoding="utf-8", newline="") as stream:
        identities = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    excluded_id = identities[0]
    revised_slot1 = tmp_path / "revised-slot1.csv"
    labels = {identity: "valid_content" for identity in identities}
    labels[excluded_id] = "site_ui"
    _fill_by_fingerprint(
        revised_slot1_template,
        revised_slot1,
        labels=labels,
        annotator="b" * 64,
    )
    import_image_annotations(
        derived, csv_path=revised_slot1, imported_by_hash="c" * 64
    )
    create_double_label_plan(
        derived,
        review_run_id=revised_review.review_run_id,
        plan_kind="proposed_exclusion",
    )
    revised_slot2_template = tmp_path / "revised-slot2-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=revised_review.review_run_id,
        assignment_slot=2,
        output_path=revised_slot2_template,
    )
    revised_slot2 = tmp_path / "revised-slot2.csv"
    _fill_by_fingerprint(
        revised_slot2_template,
        revised_slot2,
        labels={excluded_id: "site_ui"},
        annotator="d" * 64,
    )
    import_image_annotations(
        derived, csv_path=revised_slot2, imported_by_hash="e" * 64
    )
    revised = build_image_decisions(
        derived,
        candidate_build_id=decisions.candidate_build_id,
        config=config,
        candidate_review_run_id=revised_review.review_run_id,
    )
    assert revised.decision_manifest_sha256 != decisions.decision_manifest_sha256
    propagate_exact_sha_labels(
        derived, decision_build_id=revised.decision_build_id
    )
    with pytest.raises(ImageKeepAuditRepositoryError) as exhausted:
        create_keep_audit_round(
            derived,
            decision_build_id=revised.decision_build_id,
            round_number=2,
            config=config,
        )
    assert exhausted.value.reason_code == "image_keep_audit_population_exhausted"
