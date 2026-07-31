"""图片决定快照与 SHA-only 标签传播的集成测试。"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from PIL import Image

from tourism_ugc_study.cleaning.image_decision_repository import (
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
    export_image_annotation_tasks,
    import_image_annotations,
)
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


def test_default_non_candidate_decision_does_not_claim_valid_content(tmp_path: Path) -> None:
    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    # 先只检查无人工候选会阻塞整个决定快照，防止自动排除或自动保留候选。
    from tourism_ugc_study.cleaning.image_decision_repository import (
        ImageDecisionRepositoryError,
    )

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
    pilot = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="pilot",
        config=config,
    )
    candidate = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    for index, review in enumerate((pilot, candidate), start=1):
        template = tmp_path / f"overlap-{index}-template.csv"
        export_image_annotation_tasks(
            derived,
            review_run_id=review.review_run_id,
            assignment_slot=1,
            output_path=template,
        )
        with template.open("r", encoding="utf-8", newline="") as stream:
            labels = {
                row["fingerprint_id"]: "valid_content"
                for row in csv.DictReader(stream)
            }
        completed = tmp_path / f"overlap-{index}.csv"
        _fill_by_fingerprint(
            template,
            completed,
            labels=labels,
            annotator=str(index) * 64,
        )
        import_image_annotations(
            derived,
            csv_path=completed,
            imported_by_hash=str(index + 2) * 64,
        )

    result = build_image_decisions(
        derived,
        candidate_build_id=build.build_id,
        config=config,
    )
    assert result.review_count == 0
    assert result.exclude_count == 0
    assert result.keep_count == result.decision_count
