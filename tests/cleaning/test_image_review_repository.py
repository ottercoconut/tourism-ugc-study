"""图片人工复核运行、盲标导入、双标计划与仲裁的集成测试。"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from tourism_ugc_study.cleaning.image_repository import (
    build_image_candidates,
    import_image_manifest,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.image_review_repository import (
    ImageReviewRepositoryError,
    create_double_label_plan,
    create_image_review_run,
    evaluate_image_agreement,
    export_image_annotation_tasks,
    import_image_annotations,
    record_image_adjudication,
)
from tourism_ugc_study.cleaning.schema import connect_derived
from tests.cleaning.test_image_repository import (
    _prepared_run,
    _route_map,
    _row,
    _sha,
    _write_manifest,
)


def _candidate_build(tmp_path: Path, run_id: str):
    """以纯本地合成图片建立包含精确重复与小图信号的封存候选 build。"""

    derived, root, config, snapshot = _prepared_run(tmp_path, run_id)
    route = root / "route.png"
    tiny = root / "tiny.png"
    _route_map(route)
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(tiny)
    manifest = tmp_path / "manifest.csv"
    _write_manifest(
        manifest,
        [
            _row(1, "content", "route.png", _sha(route)),
            _row(2, "content", "route.png", _sha(route)),
            _row(3, "content", "tiny.png", _sha(tiny)),
            _row(4, "author_avatar", "route.png", _sha(route)),
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id=run_id,
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest,
        image_root=root,
        config=config,
    )
    process_image_fingerprints(
        derived, manifest_id=imported.manifest_id, image_root=root, config=config
    )
    build = build_image_candidates(
        derived, manifest_id=imported.manifest_id, config=config
    )
    return derived, config, build


def _complete_csv(
    source: Path,
    target: Path,
    *,
    annotator_hash: str,
    labels: list[str],
) -> None:
    """填充测试导出表；保持正式列契约，不引入图片路径。"""

    with source.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or ())
        rows = list(reader)
    assert len(rows) == len(labels)
    for row, label in zip(rows, labels, strict=True):
        row["technical_noise_label"] = label
        row["reason_codes"] = "synthetic_fixture"
        row["technical_flags"] = ""
        row["annotator_hash"] = annotator_hash
        row["annotated_at_utc"] = "2026-07-31T00:00:00Z"
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_candidate_review_only_double_labels_proposed_exclusion_or_uncertain(
    tmp_path: Path,
) -> None:
    derived, config, build = _candidate_build(tmp_path, "review-dynamic")
    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    slot1_template = tmp_path / "slot1-template.csv"
    assert export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=1,
        output_path=slot1_template,
    ) == 2
    slot1 = tmp_path / "slot1.csv"
    _complete_csv(
        slot1_template,
        slot1,
        annotator_hash="1" * 64,
        labels=["site_ui", "valid_content"],
    )
    imported = import_image_annotations(
        derived, csv_path=slot1, imported_by_hash="a" * 64
    )
    assert imported.accepted_count == 2

    plan = create_double_label_plan(
        derived,
        review_run_id=review.review_run_id,
        plan_kind="proposed_exclusion",
    )
    assert plan.member_count == 1
    slot2_template = tmp_path / "slot2-template.csv"
    assert export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=2,
        output_path=slot2_template,
    ) == 1
    slot2 = tmp_path / "slot2.csv"
    _complete_csv(
        slot2_template,
        slot2,
        annotator_hash="2" * 64,
        labels=["site_background"],
    )
    import_image_annotations(derived, csv_path=slot2, imported_by_hash="b" * 64)

    with sqlite3.connect(derived) as connection:
        evidence = connection.execute(
            """
            SELECT fingerprint_id, annotation_id, assignment_slot
            FROM image_review_annotations WHERE review_run_id = ?
              AND technical_noise_label != 'valid_content'
            ORDER BY assignment_slot
            """,
            (review.review_run_id,),
        ).fetchall()
    assert len(evidence) == 2
    with pytest.raises(ImageReviewRepositoryError) as same_person:
        record_image_adjudication(
            derived,
            review_run_id=review.review_run_id,
            fingerprint_id=evidence[0][0],
            left_annotation_id=evidence[0][1],
            right_annotation_id=evidence[1][1],
            adjudicator_hash="1" * 64,
            guide_version=config.image_label_guide_version,
            technical_noise_label="site_ui",
            reason_codes=("resolved_boundary",),
            adjudicated_at_utc="2026-07-31T01:00:00Z",
        )
    assert same_person.value.reason_code == "image_adjudication_integrity_error"

    for unsafe_reason in ("=WEBSERVICE(x)", "/tmp/private", "free text"):
        with pytest.raises(ImageReviewRepositoryError) as unsafe:
            record_image_adjudication(
                derived,
                review_run_id=review.review_run_id,
                fingerprint_id=evidence[0][0],
                left_annotation_id=evidence[0][1],
                right_annotation_id=evidence[1][1],
                adjudicator_hash="3" * 64,
                guide_version=config.image_label_guide_version,
                technical_noise_label="site_ui",
                reason_codes=(unsafe_reason,),
                adjudicated_at_utc="2026-07-31T01:00:00Z",
            )
        assert unsafe.value.reason_code == "image_adjudication_codes_invalid"

    adjudication = record_image_adjudication(
        derived,
        review_run_id=review.review_run_id,
        fingerprint_id=evidence[0][0],
        left_annotation_id=evidence[0][1],
        right_annotation_id=evidence[1][1],
        adjudicator_hash="3" * 64,
        guide_version=config.image_label_guide_version,
        technical_noise_label="site_ui",
        reason_codes=("resolved_boundary",),
        adjudicated_at_utc="2026-07-31T01:00:00Z",
    )
    assert adjudication.technical_noise_label == "site_ui"


def test_boundary_plan_requires_complete_pairs_and_reports_undefined_kappa(
    tmp_path: Path,
) -> None:
    derived, config, build = _candidate_build(tmp_path, "review-boundary")
    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="boundary",
        config=config,
    )
    plan = create_double_label_plan(
        derived, review_run_id=review.review_run_id, plan_kind="boundary"
    )
    assert plan.member_count == review.member_count == 2

    templates = [tmp_path / "b1-template.csv", tmp_path / "b2-template.csv"]
    completed = [tmp_path / "b1.csv", tmp_path / "b2.csv"]
    for slot in (1, 2):
        assert export_image_annotation_tasks(
            derived,
            review_run_id=review.review_run_id,
            assignment_slot=slot,
            output_path=templates[slot - 1],
        ) == 2
    _complete_csv(
        templates[0], completed[0], annotator_hash="4" * 64, labels=["valid_content"] * 2
    )
    import_image_annotations(derived, csv_path=completed[0], imported_by_hash="c" * 64)
    incomplete = evaluate_image_agreement(
        derived, review_run_id=review.review_run_id, config=config
    )
    assert incomplete.evaluation_status == "incomplete"

    _complete_csv(
        templates[1], completed[1], annotator_hash="5" * 64, labels=["valid_content"] * 2
    )
    import_image_annotations(derived, csv_path=completed[1], imported_by_hash="d" * 64)
    complete = evaluate_image_agreement(
        derived, review_run_id=review.review_run_id, config=config
    )
    assert complete.evaluation_status == "passed"
    assert complete.raw_agreement == 1.0
    assert complete.cohen_kappa is None
    assert complete.kappa_status == "undefined_single_category"

    with sqlite3.connect(derived) as connection:
        agreed = connection.execute(
            """
            SELECT fingerprint_id, annotation_id FROM image_review_annotations
            WHERE review_run_id = ? AND fingerprint_id = (
              SELECT fingerprint_id FROM image_review_members
              WHERE review_run_id = ? ORDER BY stable_rank LIMIT 1
            ) ORDER BY assignment_slot
            """,
            (review.review_run_id, review.review_run_id),
        ).fetchall()
    left_id, right_id = sorted((agreed[0][1], agreed[1][1]))
    with connect_derived(derived) as connection:
        # 应用层之外直接写库也不能给两个一致且明确的标签添加多余仲裁。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_review_adjudications(
                  adjudication_id, review_run_id, fingerprint_id,
                  left_annotation_id, right_annotation_id, adjudicator_hash,
                  guide_version, technical_noise_label, reason_codes_json,
                  adjudicated_at_utc, evidence_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'site_ui', '["not_required"]',
                          '2026-07-31T02:00:00Z', ?)
                """,
                (
                    "direct-unnecessary-adjudication",
                    review.review_run_id,
                    agreed[0][0],
                    left_id,
                    right_id,
                    "6" * 64,
                    config.image_label_guide_version,
                    "f" * 64,
                ),
            )
    with pytest.raises(ImageReviewRepositoryError) as unnecessary:
        record_image_adjudication(
            derived,
            review_run_id=review.review_run_id,
            fingerprint_id=agreed[0][0],
            left_annotation_id=agreed[0][1],
            right_annotation_id=agreed[1][1],
            adjudicator_hash="6" * 64,
            guide_version=config.image_label_guide_version,
            technical_noise_label="site_ui",
            reason_codes=("not_required",),
            adjudicated_at_utc="2026-07-31T02:00:00Z",
        )
    assert unnecessary.value.reason_code == "image_adjudication_not_required"


def test_direct_sql_rejects_cross_run_import_and_guide_mismatch(
    tmp_path: Path,
) -> None:
    """标注导入、复核运行与手册身份必须在 SQLite 层形成同一条谱系。"""

    derived, config, build = _candidate_build(tmp_path, "review-lineage")
    first = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    second = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
        random_seed=config.random_seed + 1,
    )
    template = tmp_path / "lineage-template.csv"
    completed = tmp_path / "lineage.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=first.review_run_id,
        assignment_slot=1,
        output_path=template,
    )
    _complete_csv(
        template,
        completed,
        annotator_hash="7" * 64,
        labels=["valid_content"] * first.member_count,
    )
    imported = import_image_annotations(
        derived,
        csv_path=completed,
        imported_by_hash="8" * 64,
    )

    with connect_derived(derived) as connection:
        common_fingerprint = connection.execute(
            """
            SELECT a.fingerprint_id FROM image_review_members a
            JOIN image_review_members b ON b.fingerprint_id = a.fingerprint_id
            WHERE a.review_run_id = ? AND b.review_run_id = ?
            ORDER BY a.fingerprint_id LIMIT 1
            """,
            (first.review_run_id, second.review_run_id),
        ).fetchone()[0]
        base_values = (
            common_fingerprint,
            "9" * 64,
            config.image_label_guide_version,
            "a" * 64,
        )
        # 第一轮导入父行不得承载第二轮成员的标注。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_review_annotations(
                  annotation_id, import_id, review_run_id, fingerprint_id,
                  assignment_slot, annotator_hash, guide_version,
                  technical_noise_label, reason_codes_json, technical_flags_json,
                  annotated_at_utc, row_sha256
                ) VALUES ('cross-run-annotation', ?, ?, ?, 1, ?, ?,
                          'valid_content', '["synthetic_fixture"]', '[]',
                          '2026-07-31T04:00:00Z', ?)
                """,
                (imported.import_id, second.review_run_id, *base_values),
            )

        second_import_id = "direct-second-import"
        connection.execute(
            """
            INSERT INTO image_annotation_imports(
              import_id, review_run_id, source_sha256, imported_by_hash,
              row_count, accepted_count, rejected_count, status, created_at_utc
            ) VALUES (?, ?, ?, ?, 1, 1, 0, 'accepted', '2026-07-31T04:00:00Z')
            """,
            (second_import_id, second.review_run_id, "b" * 64, "c" * 64),
        )
        # 即使导入父行属于该运行，也不能用不同手册版本写入。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_review_annotations(
                  annotation_id, import_id, review_run_id, fingerprint_id,
                  assignment_slot, annotator_hash, guide_version,
                  technical_noise_label, reason_codes_json, technical_flags_json,
                  annotated_at_utc, row_sha256
                ) VALUES ('wrong-guide-annotation', ?, ?, ?, 1, ?, 'image-noise-v0',
                          'valid_content', '["synthetic_fixture"]', '[]',
                          '2026-07-31T04:00:00Z', ?)
                """,
                (
                    second_import_id,
                    second.review_run_id,
                    common_fingerprint,
                    "d" * 64,
                    "e" * 64,
                ),
            )


def test_formula_injection_is_rejected_before_any_import_rows(tmp_path: Path) -> None:
    derived, config, build = _candidate_build(tmp_path, "review-formula")
    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    template = tmp_path / "formula-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=1,
        output_path=template,
    )
    malicious = tmp_path / "formula.csv"
    _complete_csv(
        template,
        malicious,
        annotator_hash="6" * 64,
        labels=["valid_content"] * review.member_count,
    )
    rows = list(csv.DictReader(malicious.open("r", encoding="utf-8", newline="")))
    rows[0]["reason_codes"] = "=WEBSERVICE(\"https://invalid\")"
    with malicious.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ImageReviewRepositoryError) as caught:
        import_image_annotations(derived, csv_path=malicious, imported_by_hash="e" * 64)
    assert caught.value.reason_code == "image_annotation_formula_rejected"
    with sqlite3.connect(derived) as connection:
        assert connection.execute("SELECT COUNT(*) FROM image_annotation_imports").fetchone()[0] == 0
