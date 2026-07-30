from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.annotation.agreement import calculate_agreement
from tourism_ugc_study.annotation.config import annotation_config
from tourism_ugc_study.annotation.leakage_groups import create_leakage_build
from tourism_ugc_study.annotation.repository import (
    AnnotationRepositoryError,
    create_initial_sampling_run,
    export_near_duplicate_candidates,
    export_post_annotation_tasks,
    import_duplicate_adjudications,
    import_duplicate_annotations,
    import_post_adjudications,
    import_post_annotations,
)
from tourism_ugc_study.annotation.sampling import SamplingPost, build_initial_sample_plan
from tourism_ugc_study.annotation.sampling import build_periodic_sample_plan
from tourism_ugc_study.cleaning.schema import connect_derived
from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.state_machine import claim_tasks
from tourism_ugc_study.cleaning.text_repository import (
    build_text_candidates,
    process_text_tasks,
)
from tests.cleaning.test_text_repository import _prepared_text_run


def _candidate_fixture(tmp_path: Path) -> tuple[Path, object, object, str]:
    derived, config, text_config, snapshot_id, batch_id = _prepared_text_run(tmp_path)
    claims = claim_tasks(derived, batch_id, config, stage_name="text_deterministic")
    process_text_tasks(derived, claims, config=config, text_config=text_config)
    build = build_text_candidates(
        derived,
        run_id="text-run",
        snapshot_id=snapshot_id,
        config=config,
        text_config=text_config,
    )
    return derived, config, text_config, build.build_id


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    assert rows
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_initial_sampling_is_reproducible_and_blind_exports_are_separate(
    tmp_path: Path,
) -> None:
    derived, config, _, build_id = _candidate_fixture(tmp_path)

    first = create_initial_sampling_run(
        derived, candidate_build_id=build_id, config=config
    )
    repeated = create_initial_sampling_run(
        derived, candidate_build_id=build_id, config=config
    )
    slot_one = tmp_path / "slot-one.csv"
    slot_two = tmp_path / "slot-two.csv"

    assert first == repeated
    assert first.population_count == 3
    assert first.probability_count == first.targeted_count == 3
    assert first.double_label_count == 3
    assert export_post_annotation_tasks(
        derived,
        sample_run_id=first.sample_run_id,
        assignment_slot=1,
        output_path=slot_one,
    ) == 3
    assert export_post_annotation_tasks(
        derived,
        sample_run_id=first.sample_run_id,
        assignment_slot=2,
        output_path=slot_two,
    ) == 3
    assert "annotated_at_utc" in slot_one.read_text(encoding="utf-8")
    with sqlite3.connect(derived) as connection:
        probability = connection.execute(
            """
            SELECT COUNT(*), MIN(inclusion_probability_ppm), MIN(analysis_weight)
            FROM text_sample_members
            WHERE sample_run_id = ? AND sample_frame = 'probability'
            """,
            (first.sample_run_id,),
        ).fetchone()
        assert probability == (3, 1_000_000, 1.0)


def test_probability_sample_records_platform_inclusion_weights() -> None:
    config = load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v2.4.yaml")
    posts = tuple(
        SamplingPost(
            source_post_id=platform_index * 200 + index + 1,
            source_version=1,
            platform_key=f"platform-{platform_index}",
            normalized_length=100,
            near_candidate_count=0,
            cross_platform_near_count=0,
        )
        for platform_index in range(5)
        for index in range(200)
    )

    plan = build_initial_sample_plan(
        posts,
        config=annotation_config(config),
        random_seed=config.random_seed,
    )
    probability = [item for item in plan.members if item.sample_frame == "probability"]

    assert len(probability) == 500
    assert {item.inclusion_probability_ppm for item in probability} == {500_000}
    assert {item.analysis_weight for item in probability} == {2.0}
    assert {
        platform: sum(item.platform_key == platform for item in probability)
        for platform in {item.platform_key for item in probability}
    } == {f"platform-{index}": 100 for index in range(5)}


def test_periodic_sample_only_uses_posts_after_baseline() -> None:
    posts = tuple(
        SamplingPost(index, 1, "xhs", 100, 0, 0) for index in range(1, 11)
    )

    plan = build_periodic_sample_plan(
        posts,
        already_sampled_ids=range(1, 9),
        sample_size=2,
        random_seed=20260728,
        round_number=1,
    )

    assert {item.source_post_id for item in plan.members} == {9, 10}


def test_post_annotations_and_adjudications_append_without_overwrite(tmp_path: Path) -> None:
    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(derived, candidate_build_id=build_id, config=config)
    annotations_path = tmp_path / "annotations.csv"
    _write_csv(
        annotations_path,
        [
            {
                "annotation_id": "post-a1",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "annotator_hash": "a" * 64,
                "assignment_slot": 1,
                "structure_label": "usable",
                "tourism_label": "related",
                "commercial_label": "organic",
                "reason_codes": "travel_main_subject",
                "annotated_at_utc": "2026-07-30T01:00:00+00:00",
            },
            {
                "annotation_id": "post-a2",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "annotator_hash": "b" * 64,
                "assignment_slot": 2,
                "structure_label": "usable",
                "tourism_label": "related",
                "commercial_label": "promotion",
                "reason_codes": "promotion_signal",
                "annotated_at_utc": "2026-07-30T01:01:00+00:00",
            },
        ],
    )
    imported = import_post_annotations(
        derived,
        csv_path=annotations_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="c" * 64,
    )
    reused = import_post_annotations(
        derived,
        csv_path=annotations_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="c" * 64,
    )
    adjudication_path = tmp_path / "adjudication.csv"
    _write_csv(
        adjudication_path,
        [
            {
                "adjudication_id": "post-gold-1",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "adjudicator_hash": "d" * 64,
                "structure_label": "usable",
                "tourism_label": "related",
                "commercial_label": "uncertain",
                "reason_codes": "commercial_disagreement",
                "evidence_annotation_ids": "post-a1|post-a2",
                "decision_context": "gold",
                "adjudicated_at_utc": "2026-07-30T02:00:00+00:00",
            }
        ],
    )
    import_post_adjudications(
        derived,
        csv_path=adjudication_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="e" * 64,
    )

    assert imported.reused is False
    assert reused.reused is True
    with connect_derived(derived) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM text_post_adjudications").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE text_post_annotations SET tourism_label = 'unrelated'"
            )


def test_double_label_slots_reject_the_same_annotator(tmp_path: Path) -> None:
    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(derived, candidate_build_id=build_id, config=config)
    path = tmp_path / "same-annotator.csv"
    _write_csv(
        path,
        [
            {
                "annotation_id": f"same-{slot}",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "annotator_hash": "a" * 64,
                "assignment_slot": slot,
                "structure_label": "usable",
                "tourism_label": "related",
                "commercial_label": "organic",
                "reason_codes": "test",
                "annotated_at_utc": f"2026-07-30T01:0{slot}:00+00:00",
            }
            for slot in (1, 2)
        ],
    )

    with pytest.raises(AnnotationRepositoryError) as error:
        import_post_annotations(
            derived,
            csv_path=path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="b" * 64,
        )
    assert error.value.reason_code == "double_label_annotators_must_differ"
    with sqlite3.connect(derived) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0] == 0


def test_duplicate_candidate_needs_separate_human_adjudication(tmp_path: Path) -> None:
    derived, config, _, build_id = _candidate_fixture(tmp_path)
    exported = tmp_path / "pairs.csv"
    assert export_near_duplicate_candidates(
        derived, candidate_build_id=build_id, output_path=exported
    ) == 1
    with exported.open("r", encoding="utf-8", newline="") as stream:
        pair = next(csv.DictReader(stream))
    annotation_path = tmp_path / "pair-annotations.csv"
    common = {
        "build_id": build_id,
        "left_cluster_id": pair["left_cluster_id"],
        "right_cluster_id": pair["right_cluster_id"],
        "decision": "duplicate",
        "reason_code": "same_trip_plan",
    }
    _write_csv(
        annotation_path,
        [
            {
                "annotation_id": "pair-a1",
                **common,
                "annotator_hash": "1" * 64,
                "annotated_at_utc": "2026-07-30T03:00:00+00:00",
            },
            {
                "annotation_id": "pair-a2",
                **common,
                "annotator_hash": "2" * 64,
                "annotated_at_utc": "2026-07-30T03:01:00+00:00",
            },
        ],
    )
    first = import_duplicate_annotations(
        derived,
        csv_path=annotation_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="3" * 64,
    )
    repeated = import_duplicate_annotations(
        derived,
        csv_path=annotation_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="3" * 64,
    )
    candidate_only = create_leakage_build(
        derived,
        candidate_build_id=build_id,
        duplicate_adjudication_ids=(),
    )
    adjudication_path = tmp_path / "pair-adjudication.csv"
    _write_csv(
        adjudication_path,
        [
            {
                "adjudication_id": "pair-gold",
                **common,
                "adjudicator_hash": "4" * 64,
                "evidence_annotation_ids": "pair-a1|pair-a2",
                "adjudicated_at_utc": "2026-07-30T04:00:00+00:00",
            }
        ],
    )
    import_duplicate_adjudications(
        derived,
        csv_path=adjudication_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="5" * 64,
    )
    confirmed = create_leakage_build(
        derived,
        candidate_build_id=build_id,
        duplicate_adjudication_ids=("pair-gold",),
    )

    assert first.reused is False and repeated.reused is True
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM text_near_duplicate_annotations"
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT COUNT(*) FROM text_near_duplicate_adjudications
            WHERE decision = 'duplicate'
            """
        ).fetchone()[0] == 1
        assert candidate_only.component_count == 2
        assert confirmed.component_count == 1


def test_duplicate_import_rejects_pair_outside_finalized_build(tmp_path: Path) -> None:
    derived, config, _, build_id = _candidate_fixture(tmp_path)
    bad_path = tmp_path / "bad-pair.csv"
    _write_csv(
        bad_path,
        [
            {
                "build_id": build_id,
                "left_cluster_id": "a",
                "right_cluster_id": "z",
                "decision": "duplicate",
                "reason_code": "invalid",
                "annotator_hash": "1" * 64,
                "annotated_at_utc": "2026-07-30T03:00:00+00:00",
            }
        ],
    )
    with pytest.raises(AnnotationRepositoryError) as error:
        import_duplicate_annotations(
            derived,
            csv_path=bad_path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="2" * 64,
        )
    assert error.value.reason_code == "duplicate_pair_not_in_finalized_build"
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM text_annotation_imports"
        ).fetchone()[0] == 0


def test_agreement_threshold_requests_fixed_additional_double_labels() -> None:
    config = annotation_config(
        # 复用正式配置可同时验证 0.80/0.70/100 的公开契约。
        load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v2.4.yaml")
    )
    records = [
        {
            "source_post_id": post_id,
            "source_version": 1,
            "assignment_slot": slot,
            "structure_label": "usable",
            "tourism_label": tourism,
            "commercial_label": "organic",
        }
        for post_id, pair in ((1, ("related", "related")), (2, ("related", "unrelated")))
        for slot, tourism in enumerate(pair, 1)
    ]

    report = calculate_agreement(records, config=config)

    assert report.structure.raw_agreement == 1.0
    assert report.structure.cohen_kappa == 1.0
    assert report.tourism.raw_agreement == 0.5
    assert report.additional_double_label_required == 100
