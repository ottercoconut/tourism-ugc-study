"""图片决定快照与 SHA-only 标签传播的集成测试。"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from tourism_ugc_study.cleaning.image_decision_repository import (
    ImageDecisionRepositoryError,
    build_image_decisions,
    propagate_exact_sha_labels,
)
from tourism_ugc_study.cleaning.image_repository import (
    build_image_candidates,
    import_image_manifest,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.image_review_repository import (
    create_double_label_plan,
    create_image_review_run,
    evaluate_image_agreement,
    export_image_annotation_tasks,
    import_image_annotations,
)
from tourism_ugc_study.cleaning.schema import connect_derived
from tests.cleaning.test_image_repository import (
    _prepared_run,
    _route_map,
    _row,
    _sha,
    _write_manifest,
)


def _build_with_tiny_duplicate(tmp_path: Path):
    """构造技术小图精确重复和一张路线内容图，不读取正式图片。"""

    derived, root, config, snapshot = _prepared_run(tmp_path, "decision-sha")
    tiny = root / "tiny.png"
    route = root / "route.png"
    Image.new("RGBA", (24, 24), (0, 0, 0, 0)).save(tiny)
    _route_map(route)
    manifest = tmp_path / "manifest.csv"
    _write_manifest(
        manifest,
        [
            _row(1, "content", "tiny.png", _sha(tiny)),
            _row(2, "content", "tiny.png", _sha(tiny)),
            _row(3, "content", "route.png", _sha(route)),
            _row(4, "author_avatar", "route.png", _sha(route)),
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id="decision-sha",
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


def _fill_by_fingerprint(
    template: Path,
    output: Path,
    *,
    labels: dict[str, str],
    annotator: str,
) -> None:
    """按导出任务身份填充技术噪声标签，保持 CSV 列和顺序不变。"""

    with template.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or ())
        rows = list(reader)
    for row in rows:
        row["technical_noise_label"] = labels[row["fingerprint_id"]]
        row["reason_codes"] = "synthetic_fixture"
        row["technical_flags"] = ""
        row["annotator_hash"] = annotator
        row["annotated_at_utc"] = "2026-07-31T00:00:00Z"
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _complete_formal_review_gate(
    derived: Path,
    config,
    build,
    tmp_path: Path,
    *,
    prefix: str = "gate",
    boundary_disagreement: bool = False,
) -> tuple[str, str]:
    """经公开 API 完成 pilot 与 boundary 双标，不直接伪造评估证据。"""

    run_ids: list[str] = []
    for run_index, kind in enumerate(("pilot", "boundary"), start=1):
        review = create_image_review_run(
            derived,
            candidate_build_id=build.build_id,
            review_kind=kind,
            config=config,
        )
        create_double_label_plan(
            derived,
            review_run_id=review.review_run_id,
            plan_kind="boundary",
        )
        templates = [
            tmp_path / f"{prefix}-{kind}-slot{slot}-template.csv"
            for slot in (1, 2)
        ]
        completed = [
            tmp_path / f"{prefix}-{kind}-slot{slot}.csv" for slot in (1, 2)
        ]
        ids: list[str] = []
        for slot in (1, 2):
            export_image_annotation_tasks(
                derived,
                review_run_id=review.review_run_id,
                assignment_slot=slot,
                output_path=templates[slot - 1],
            )
            with templates[slot - 1].open(
                "r", encoding="utf-8", newline=""
            ) as stream:
                current_ids = [
                    row["fingerprint_id"] for row in csv.DictReader(stream)
                ]
            if not ids:
                ids = current_ids
            labels = {identity: "valid_content" for identity in current_ids}
            if kind == "boundary" and boundary_disagreement and slot == 2:
                labels[current_ids[0]] = "site_ui"
            _fill_by_fingerprint(
                templates[slot - 1],
                completed[slot - 1],
                labels=labels,
                annotator=str(run_index * 2 + slot) * 64,
            )
            import_image_annotations(
                derived,
                csv_path=completed[slot - 1],
                imported_by_hash=f"{run_index * 2 + slot + 4:x}" * 64,
            )
        evaluation = evaluate_image_agreement(
            derived,
            review_run_id=review.review_run_id,
            config=config,
        )
        if kind == "pilot" or not boundary_disagreement:
            assert evaluation.evaluation_status == "passed"
        else:
            assert evaluation.evaluation_status == "supplement_required"
        run_ids.append(review.review_run_id)
    return run_ids[0], run_ids[1]


def test_decision_build_requires_human_candidate_and_only_sha_propagates(
    tmp_path: Path,
) -> None:
    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    slot1_template = tmp_path / "slot1-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=1,
        output_path=slot1_template,
    )
    with sqlite3.connect(derived) as connection:
        tiny_rep = connection.execute(
            """
            SELECT c.representative_fingerprint_id
            FROM image_exact_clusters c
            JOIN image_exact_cluster_members m
              ON m.build_id = c.build_id AND m.cluster_id = c.cluster_id
            JOIN image_candidate_build_members bm
              ON bm.build_id = m.build_id AND bm.fingerprint_id = m.fingerprint_id
            WHERE c.build_id = ? AND bm.source_image_id = 1
            """,
            (build.build_id,),
        ).fetchone()[0]
    with slot1_template.open("r", encoding="utf-8", newline="") as stream:
        candidate_ids = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    labels = {fingerprint_id: "valid_content" for fingerprint_id in candidate_ids}
    labels[tiny_rep] = "site_ui"
    slot1 = tmp_path / "slot1.csv"
    _fill_by_fingerprint(slot1_template, slot1, labels=labels, annotator="1" * 64)
    import_image_annotations(derived, csv_path=slot1, imported_by_hash="a" * 64)
    create_double_label_plan(
        derived,
        review_run_id=review.review_run_id,
        plan_kind="proposed_exclusion",
    )
    slot2_template = tmp_path / "slot2-template.csv"
    assert export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=2,
        output_path=slot2_template,
    ) == 1
    slot2 = tmp_path / "slot2.csv"
    _fill_by_fingerprint(
        slot2_template,
        slot2,
        labels={tiny_rep: "site_ui"},
        annotator="2" * 64,
    )
    import_image_annotations(derived, csv_path=slot2, imported_by_hash="b" * 64)

    _complete_formal_review_gate(derived, config, build, tmp_path)

    decisions = build_image_decisions(
        derived, candidate_build_id=build.build_id, config=config
    )
    assert decisions.exclude_count == 1
    assert decisions.review_count == 0
    assert decisions.keep_count >= 1
    propagation = propagate_exact_sha_labels(
        derived, decision_build_id=decisions.decision_build_id
    )
    assert propagation.propagation_run_count == 1
    assert propagation.propagated_member_count == 2

    with sqlite3.connect(derived) as connection:
        propagated = connection.execute(
            """
            SELECT m.propagated_label, p.exact_cluster_id
            FROM image_sha_propagation_members m
            JOIN image_sha_propagation_runs p
              ON p.propagation_run_id = m.propagation_run_id
            ORDER BY m.fingerprint_id
            """
        ).fetchall()
        assert propagated == [("site_ui", propagated[0][1]), ("site_ui", propagated[0][1])]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(image_sha_propagation_runs)")
        }
        assert "exact_cluster_id" in columns
        assert not {"phash_group_id", "near_pair_id", "hamming_distance"} & columns

    with connect_derived(derived) as connection:
        link_counts = connection.execute(
            """
            SELECT d.provenance, COUNT(l.evidence_id)
            FROM image_decisions d
            LEFT JOIN image_decision_evidence_links l ON l.decision_id = d.decision_id
            WHERE d.decision_build_id = ?
            GROUP BY d.decision_id, d.provenance ORDER BY d.provenance
            """,
            (decisions.decision_build_id,),
        ).fetchall()
        assert ("double_agreement", 2) in [tuple(row) for row in link_counts]
        assert all(count in {0, 1, 2} for _, count in map(tuple, link_counts))

        # 构造另一张图片的真实 annotation；直接把它连到目标图片必须由证据
        # 子表触发器阻断，父 build 也不能在缺少合法链接时封存。
        annotations = connection.execute(
            """
            SELECT a.annotation_id, a.review_run_id, a.fingerprint_id
            FROM image_review_annotations a
            JOIN image_review_runs r ON r.review_run_id = a.review_run_id
            WHERE r.candidate_build_id = ? AND r.review_kind = 'candidate_review'
            ORDER BY a.fingerprint_id, a.assignment_slot
            """,
            (build.build_id,),
        ).fetchall()
        by_fingerprint = {}
        for annotation in annotations:
            by_fingerprint.setdefault(annotation["fingerprint_id"], annotation)
        assert len(by_fingerprint) >= 2
        target_fingerprint, wrong_fingerprint = sorted(by_fingerprint)[:2]
        wrong_evidence = by_fingerprint[wrong_fingerprint]
        direct_build = "direct-cross-image-evidence-build"
        direct_decision = "direct-cross-image-evidence"
        connection.execute(
            """
            INSERT INTO image_decision_builds(
              decision_build_id, candidate_build_id, guide_version,
              evidence_manifest_sha256, expected_decision_count,
              decision_manifest_sha256, seal_status, created_at_utc
            ) VALUES (?, ?, ?, ?, 1, ?, 'building', '2026-07-31T05:00:00Z')
            """,
            (
                direct_build,
                build.build_id,
                config.image_label_guide_version,
                "d" * 64,
                "e" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO image_decisions(
              decision_id, decision_build_id, fingerprint_id,
              technical_noise_label, decision_action, provenance,
              evidence_id, decision_sha256, created_at_utc
            ) VALUES (?, ?, ?, 'valid_content', 'keep', 'single_valid_content',
                      ?, ?, '2026-07-31T05:00:00Z')
            """,
            (
                direct_decision,
                direct_build,
                target_fingerprint,
                wrong_evidence["annotation_id"],
                "f" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_decision_evidence_links(
                  decision_id, evidence_id, evidence_kind,
                  review_run_id, fingerprint_id
                ) VALUES (?, ?, 'annotation', ?, ?)
                """,
                (
                    direct_decision,
                    wrong_evidence["annotation_id"],
                    wrong_evidence["review_run_id"],
                    target_fingerprint,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE image_decision_builds SET seal_status = 'finalized' WHERE decision_build_id = ?",
                (direct_build,),
            )

        single_valid = connection.execute(
            """
            SELECT d.decision_id, d.fingerprint_id, c.cluster_id, c.member_count
            FROM image_decisions d
            JOIN image_exact_clusters c
              ON c.build_id = ? AND c.representative_fingerprint_id = d.fingerprint_id
            WHERE d.decision_build_id = ? AND d.provenance = 'single_valid_content'
            LIMIT 1
            """,
            (build.build_id, decisions.decision_build_id),
        ).fetchone()
        assert single_valid is not None
        # 单人 valid_content 不能成为 SHA 标签传播源；传播只服务于已双人确认
        # 或仲裁确认的技术噪声排除。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_sha_propagation_runs(
                  propagation_run_id, decision_build_id, candidate_build_id,
                  exact_cluster_id, representative_decision_id,
                  technical_noise_label, expected_member_count,
                  member_manifest_sha256, seal_status, created_at_utc
                ) VALUES ('direct-valid-propagation', ?, ?, ?, ?, 'valid_content', ?,
                          ?, 'building', '2026-07-31T05:00:00Z')
                """,
                (
                    decisions.decision_build_id,
                    build.build_id,
                    single_valid["cluster_id"],
                    single_valid["decision_id"],
                    single_valid["member_count"],
                    "1" * 64,
                ),
            )


def test_default_non_candidate_decision_does_not_claim_valid_content(tmp_path: Path) -> None:
    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    _complete_formal_review_gate(derived, config, build, tmp_path)
    # 先只检查无人工候选会阻塞整个决定快照，防止自动排除或自动保留候选。
    try:
        build_image_decisions(derived, candidate_build_id=build.build_id, config=config)
    except ImageDecisionRepositoryError as exc:
        assert exc.reason_code == "image_decision_evidence_incomplete"
    else:  # pragma: no cover - 该分支表示候选被静默越过，是严重回归。
        raise AssertionError("unreviewed candidate must block decision build")


def test_pilot_annotations_do_not_collide_with_formal_candidate_evidence(
    tmp_path: Path,
) -> None:
    """共同试标与正式复核重叠时，只由正式运行内证据生成决定。"""

    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    pilot_id, _boundary_id = _complete_formal_review_gate(
        derived, config, build, tmp_path, prefix="overlap-gate"
    )
    candidate = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    template = tmp_path / "overlap-candidate-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=candidate.review_run_id,
        assignment_slot=1,
        output_path=template,
    )
    with template.open("r", encoding="utf-8", newline="") as stream:
        labels = {
            row["fingerprint_id"]: "valid_content" for row in csv.DictReader(stream)
        }
    completed = tmp_path / "overlap-candidate.csv"
    _fill_by_fingerprint(
        template,
        completed,
        labels=labels,
        annotator="1" * 64,
    )
    import_image_annotations(
        derived,
        csv_path=completed,
        imported_by_hash="2" * 64,
    )

    result = build_image_decisions(
        derived,
        candidate_build_id=build.build_id,
        config=config,
    )
    assert result.review_count == 0
    assert result.exclude_count == 0
    assert result.keep_count == result.decision_count
    assert pilot_id != candidate.review_run_id
    propagation = propagate_exact_sha_labels(
        derived, decision_build_id=result.decision_build_id
    )
    assert propagation.propagation_run_count == 0
    assert propagation.propagated_member_count == 0


def test_formal_decision_requires_pilot_boundary_and_raw_agreement(
    tmp_path: Path,
) -> None:
    """缺共同试标、缺边界或边界一致率不足时均不得生成决定。"""

    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    with pytest.raises(ImageDecisionRepositoryError) as no_pilot:
        build_image_decisions(
            derived, candidate_build_id=build.build_id, config=config
        )
    assert no_pilot.value.reason_code == "image_review_gate_pilot_missing"

    pilot = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="pilot",
        config=config,
    )
    create_double_label_plan(
        derived, review_run_id=pilot.review_run_id, plan_kind="boundary"
    )
    for slot in (1, 2):
        template = tmp_path / f"pilot-only-{slot}-template.csv"
        completed = tmp_path / f"pilot-only-{slot}.csv"
        export_image_annotation_tasks(
            derived,
            review_run_id=pilot.review_run_id,
            assignment_slot=slot,
            output_path=template,
        )
        with template.open("r", encoding="utf-8", newline="") as stream:
            ids = [row["fingerprint_id"] for row in csv.DictReader(stream)]
        _fill_by_fingerprint(
            template,
            completed,
            labels={identity: "valid_content" for identity in ids},
            annotator=str(slot + 6) * 64,
        )
        import_image_annotations(
            derived, csv_path=completed, imported_by_hash=f"{slot + 8:x}" * 64
        )
    evaluate_image_agreement(
        derived, review_run_id=pilot.review_run_id, config=config
    )
    with pytest.raises(ImageDecisionRepositoryError) as no_boundary:
        build_image_decisions(
            derived, candidate_build_id=build.build_id, config=config
        )
    assert no_boundary.value.reason_code == "image_review_gate_boundary_missing"


def test_formal_decision_rejects_boundary_below_raw_agreement_gate(
    tmp_path: Path,
) -> None:
    """完整双标不等于通过；原始一致率低于 0.80 仍是硬阻断。"""

    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    _complete_formal_review_gate(
        derived,
        config,
        build,
        tmp_path,
        prefix="failed-agreement",
        boundary_disagreement=True,
    )
    with pytest.raises(ImageDecisionRepositoryError) as failed:
        build_image_decisions(
            derived, candidate_build_id=build.build_id, config=config
        )
    assert failed.value.reason_code == "image_review_gate_boundary_agreement_failed"
