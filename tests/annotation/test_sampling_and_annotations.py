from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tourism_ugc_study.annotation.agreement import calculate_agreement
from tourism_ugc_study.annotation.config import annotation_config
from tourism_ugc_study.annotation.leakage_groups import create_leakage_build
from tourism_ugc_study.annotation.repository import (
    AnnotationRepositoryError,
    POST_ANNOTATION_TASK_FIELDS,
    create_initial_sampling_run,
    create_periodic_sampling_run,
    evaluate_agreement_workflow,
    export_near_duplicate_candidates,
    export_post_annotation_tasks,
    export_supplement_annotation_tasks,
    import_duplicate_final_reviews,
    import_duplicate_annotations,
    import_post_final_reviews,
    import_post_annotations,
)
from tourism_ugc_study.annotation.sampling import SamplingPost, build_initial_sample_plan
from tourism_ugc_study.annotation.sampling import (
    build_periodic_sample_plan,
    freeze_periodic_source_id_window,
)
from tourism_ugc_study.cleaning.schema import connect_derived
from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.scheduler import create_batch
from tourism_ugc_study.cleaning.snapshot import snapshot_source
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


def _small_annotation_config(config: object) -> object:
    """缩小纯测试抽样量，避免把正式 500/200 配额混入单元测试夹具。"""

    annotation = {
        **config.raw["annotation"],
        "initial_probability_size": 3,
        "probability_min_per_platform": 1,
        "initial_targeted_size": 3,
        "initial_recheck_size": 1,
        "minimum_recheck_interval_days": 1,
        "additional_recheck_size": 2,
    }
    return replace(config, raw={**config.raw, "annotation": annotation})


def test_committed_post_review_template_matches_export_contract() -> None:
    """提交的空白模板必须与实际盲标导出使用同一列契约。"""

    template = (
        Path(__file__).resolve().parents[2]
        / "data/annotations/templates/text-cleaning-post-reviews.csv"
    )
    with template.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows == [list(POST_ANNOTATION_TASK_FIELDS)]


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
    assert first.recheck_count == 3
    assert export_post_annotation_tasks(
        derived,
        sample_run_id=first.sample_run_id,
        review_round=1,
        output_path=slot_one,
    ) == 3
    assert export_post_annotation_tasks(
        derived,
        sample_run_id=first.sample_run_id,
        review_round=2,
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
        assert connection.execute(
            "SELECT seal_status FROM text_sampling_runs WHERE sample_run_id = ?",
            (first.sample_run_id,),
        ).fetchone()[0] == "finalized"
        original_manifest = connection.execute(
            """
            SELECT member_manifest_sha256 FROM text_sampling_runs
            WHERE sample_run_id = ?
            """,
            (first.sample_run_id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE text_sampling_runs SET member_manifest_sha256 = ?
                WHERE sample_run_id = ?
                """,
                ("0" * 64, first.sample_run_id),
            )
        assert connection.execute(
            """
            SELECT member_manifest_sha256 FROM text_sampling_runs
            WHERE sample_run_id = ?
            """,
            (first.sample_run_id,),
        ).fetchone()[0] == original_manifest
        with pytest.raises(sqlite3.IntegrityError, match="rows are sealed"):
            connection.execute(
                """
                INSERT INTO text_sample_members(
                    sample_run_id, source_post_id, source_version, platform_key,
                    sample_frame, selection_reason_code, selection_rank,
                    inclusion_probability_ppm, analysis_weight, requires_double_label
                ) VALUES (?, 1, 1, 'xhs', 'periodic_probability', 'late',
                          999, 1000000, 1.0, 0)
                """,
                (first.sample_run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM text_sample_members WHERE sample_run_id = ?",
                (first.sample_run_id,),
            )
        connection.execute(
            """
            INSERT INTO text_sampling_runs(
                sample_run_id, run_id, source_snapshot_id, candidate_build_id,
                sample_kind, guide_version, random_seed,
                population_manifest_sha256, population_count,
                probability_count, targeted_count, double_label_count,
                periodic_round_number, output_sha256, created_at_utc,
                seal_status, member_manifest_sha256
            )
            SELECT 'bad-sample-seal', run_id, source_snapshot_id,
                   candidate_build_id, 'periodic_review', guide_version,
                   random_seed, ?, 1, 1, 0, 0, 999, ?, created_at_utc,
                   'building', ?
            FROM text_sampling_runs WHERE sample_run_id = ?
            """,
            ("e" * 64, "f" * 64, "f" * 64, first.sample_run_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="seal validation failed"):
            connection.execute(
                """
                UPDATE text_sampling_runs SET seal_status = 'finalized'
                WHERE sample_run_id = 'bad-sample-seal'
                """
            )


def test_probability_sample_records_platform_inclusion_weights() -> None:
    config = load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v3.1.yaml")
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


def test_periodic_window_counts_2000_first_seen_posts_despite_old_deletions() -> None:
    """删掉 100 条旧帖不会把 2,000 个真正新增对象误算成净增 1,900。"""

    baseline_ids = set(range(1, 2101))
    deleted_old_ids = set(range(1, 101))
    new_ids = tuple(range(2101, 4101))
    current_ids = (baseline_ids - deleted_old_ids) | set(new_ids)

    # 旧的“当前行数 - 基线行数”判断只有 1,900，会错误阻止轮次；冻结窗口
    # 直接消费 first_seen source_post_id，因此仍得到完整且不重复的 2,000 条。
    assert len(current_ids) - len(baseline_ids) == 1900
    window = freeze_periodic_source_id_window(
        new_ids,
        round_number=1,
        increment_posts=2000,
    )
    assert window == new_ids
    assert len(window) == len(set(window)) == 2000
    assert freeze_periodic_source_id_window(
        new_ids,
        round_number=2,
        increment_posts=2000,
    ) == ()


def test_periodic_sample_uses_available_cap_when_window_has_fewer_usable_posts() -> None:
    """冻结窗口中当前 usable 少于 100 时全取，并保留正确纳入概率。"""

    posts = tuple(
        SamplingPost(index, 1, "xhs", 100, 0, 0) for index in range(1, 51)
    )
    plan = build_periodic_sample_plan(
        posts,
        already_sampled_ids=(),
        sample_size=100,
        random_seed=20260728,
        round_number=1,
    )

    assert len(plan.members) == 50
    assert {item.inclusion_probability_ppm for item in plan.members} == {1_000_000}
    assert {item.analysis_weight for item in plan.members} == {1.0}


def test_periodic_source_windows_are_contiguous_and_non_overlapping() -> None:
    first_seen = tuple(range(1, 4001))

    first = freeze_periodic_source_id_window(
        first_seen, round_number=1, increment_posts=2000
    )
    second = freeze_periodic_source_id_window(
        first_seen, round_number=2, increment_posts=2000
    )

    assert first[0] == 1 and first[-1] == 2000
    assert second[0] == 2001 and second[-1] == 4000
    assert not set(first) & set(second)


def test_periodic_repository_run_seals_real_sample_and_window(tmp_path: Path) -> None:
    """真实 repository 链应封存周期抽样，而不是被初始抽样计数规则阻断。"""

    derived, config, text_config, initial_snapshot_id, initial_batch_id = (
        _prepared_text_run(tmp_path)
    )
    initial_claims = claim_tasks(
        derived, initial_batch_id, config, stage_name="text_deterministic"
    )
    process_text_tasks(
        derived, initial_claims, config=config, text_config=text_config
    )
    initial_build = build_text_candidates(
        derived,
        run_id="text-run",
        snapshot_id=initial_snapshot_id,
        config=config,
        text_config=text_config,
    )
    baseline = create_initial_sampling_run(
        derived, candidate_build_id=initial_build.build_id, config=config
    )

    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as source_connection:
        source_connection.executemany(
            """
            INSERT INTO web_posts(
                id, platform_key, platform_post_id, source_type, source_url,
                title, author_platform_id, captured_at, content_text, status
            ) VALUES (?, 'xhs', ?, 'search', ?, NULL, ?, ?, ?, 'captured')
            """,
            [
                (
                    post_id,
                    f"periodic-{post_id}",
                    f"https://invalid/periodic/{post_id}",
                    f"author-{post_id}",
                    f"2026-07-31T{(post_id - 5) // 60 % 24:02d}:{(post_id - 5) % 60:02d}:00",
                    hashlib.sha256(f"synthetic-{post_id}".encode()).hexdigest(),
                )
                for post_id in range(5, 2005)
            ],
        )
    snapshot = snapshot_source(source, derived, config, "periodic-run")
    discover_increment(derived, snapshot.snapshot_id, config)
    processed_count = 0
    for _ in range(2):
        batch = create_batch(derived, "periodic-run", config)
        while claims := claim_tasks(
            derived, batch.batch_id, config, stage_name="text_deterministic"
        ):
            processed = process_text_tasks(
                derived, claims, config=config, text_config=text_config
            )
            processed_count += len(processed.succeeded)
    assert processed_count == 2000
    current_build = build_text_candidates(
        derived,
        run_id="periodic-run",
        snapshot_id=snapshot.snapshot_id,
        config=config,
        text_config=text_config,
    )

    periodic = create_periodic_sampling_run(
        derived,
        candidate_build_id=current_build.build_id,
        baseline_sample_run_id=baseline.sample_run_id,
        round_number=1,
        config=config,
    )
    repeated = create_periodic_sampling_run(
        derived,
        candidate_build_id=current_build.build_id,
        baseline_sample_run_id=baseline.sample_run_id,
        round_number=1,
        config=config,
    )

    assert periodic == repeated
    assert periodic.population_count == 2000
    assert periodic.probability_count == 100
    with connect_derived(derived) as connection:
        statuses = connection.execute(
            """
            SELECT s.seal_status, w.seal_status
            FROM text_sampling_runs AS s
            JOIN text_periodic_review_windows AS w USING (sample_run_id)
            WHERE s.sample_run_id = ?
            """,
            (periodic.sample_run_id,),
        ).fetchone()
        assert tuple(statuses) == ("finalized", "finalized")
        assert connection.execute(
            """
            SELECT COUNT(*) FROM text_sample_members
            WHERE sample_run_id = ? AND sample_frame = 'periodic_probability'
            """,
            (periodic.sample_run_id,),
        ).fetchone()[0] == 100
        assert connection.execute(
            """
            SELECT COUNT(*) FROM text_periodic_review_window_members
            WHERE sample_run_id = ?
            """,
            (periodic.sample_run_id,),
        ).fetchone()[0] == 2000
    with connect_derived(derived) as second_connection:
        with pytest.raises(sqlite3.IntegrityError):
            second_connection.execute(
                """
                INSERT INTO text_periodic_review_window_members(
                    sample_run_id, source_post_id, source_version, window_rank,
                    eligible_in_candidate_build
                ) VALUES (?, 5, 1, 2001, 1)
                """,
                (periodic.sample_run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            second_connection.execute(
                """
                UPDATE text_periodic_review_window_members
                SET eligible_in_candidate_build = 0
                WHERE sample_run_id = ? AND window_rank = 1
                """,
                (periodic.sample_run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            second_connection.execute(
                """
                DELETE FROM text_periodic_review_window_members
                WHERE sample_run_id = ? AND window_rank = 1
                """,
                (periodic.sample_run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            second_connection.execute(
                """
                UPDATE text_periodic_review_windows
                SET member_manifest_sha256 = ? WHERE sample_run_id = ?
                """,
                ("0" * 64, periodic.sample_run_id),
            )


def test_post_reviews_and_final_reviews_append_without_overwrite(tmp_path: Path) -> None:
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
                "review_round": 1,
                "structure_label": "usable",
                "tourism_label": "related",
                "reason_codes": "travel_main_subject",
                "annotated_at_utc": "2026-07-30T01:00:00+00:00",
            },
            {
                "annotation_id": "post-a2",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "annotator_hash": "b" * 64,
                "review_round": 2,
                "structure_label": "usable",
                "tourism_label": "related",
                "reason_codes": "travel_main_subject",
                "annotated_at_utc": "2026-08-14T01:01:00+00:00",
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
    final_review_path = tmp_path / "final-review.csv"
    _write_csv(
        final_review_path,
        [
            {
                "final_review_id": "post-final-1",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "reviewer_hash": "a" * 64,
                "structure_label": "usable",
                "tourism_label": "related",
                "reason_codes": "travel_main_subject",
                "evidence_review_ids": "post-a1|post-a2",
                "decision_context": "reference",
                "reviewed_at_utc": "2026-08-14T02:00:00+00:00",
            }
        ],
    )
    import_post_final_reviews(
        derived,
        csv_path=final_review_path,
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


def test_post_annotation_contract_removes_commercial_and_binds_applicability(
    tmp_path: Path,
) -> None:
    """旧商业列必须失败，结构无效只能与旅游不适用成对出现。"""

    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(
        derived, candidate_build_id=build_id, config=config
    )
    common = {
        "sample_run_id": sample.sample_run_id,
        "source_post_id": 1,
        "source_version": 1,
        "annotator_hash": "a" * 64,
        "review_round": 1,
        "reason_codes": "structure_invalid",
        "annotated_at_utc": "2026-07-30T01:00:00+00:00",
    }
    legacy_path = tmp_path / "legacy-commercial.csv"
    _write_csv(
        legacy_path,
        [
            {
                **common,
                "structure_label": "invalid",
                "tourism_label": "not_applicable",
                "commercial_label": "uncertain",
            }
        ],
    )
    with pytest.raises(AnnotationRepositoryError) as legacy_error:
        import_post_annotations(
            derived,
            csv_path=legacy_path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="b" * 64,
        )
    assert (
        legacy_error.value.reason_code
        == "commercial_label_not_in_cleaning_contract"
    )

    conflict_path = tmp_path / "invalid-with-tourism-judgment.csv"
    _write_csv(
        conflict_path,
        [
            {
                **common,
                "structure_label": "invalid",
                "tourism_label": "uncertain",
            }
        ],
    )
    with pytest.raises(AnnotationRepositoryError) as conflict_error:
        import_post_annotations(
            derived,
            csv_path=conflict_path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="b" * 64,
        )
    assert conflict_error.value.reason_code == "tourism_applicability_conflict"

    valid_path = tmp_path / "invalid-not-applicable.csv"
    _write_csv(
        valid_path,
        [
            {
                "annotation_id": "invalid-na",
                **common,
                "structure_label": "invalid",
                "tourism_label": "not_applicable",
            }
        ],
    )
    result = import_post_annotations(
        derived,
        csv_path=valid_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="b" * 64,
    )
    assert result.row_count == 1
    with connect_derived(derived) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(text_post_annotations)")
        }
        assert "commercial_label" not in columns
        base = list(
            connection.execute(
                "SELECT * FROM text_post_annotations WHERE annotation_id = 'invalid-na'"
            ).fetchone()
        )
        base[0] = "invalid-cross-axis-copy"
        base[2] = None
        base[5] = "c" * 64
        base[6] = None
        base[7] = "usable"
        base[11] = "2026-07-30T01:01:00+00:00"
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "INSERT INTO text_post_annotations VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(base),
            )


def test_delayed_recheck_accepts_the_same_reviewer(tmp_path: Path) -> None:
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
                "review_round": slot,
                "structure_label": "usable",
                "tourism_label": "related",
                "reason_codes": "test",
                "annotated_at_utc": (
                    "2026-07-30T01:00:00+00:00"
                    if slot == 1
                    else "2026-08-14T01:00:00+00:00"
                ),
            }
            for slot in (1, 2)
        ],
    )

    result = import_post_annotations(
        derived,
        csv_path=path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="b" * 64,
    )
    assert result.row_count == 2
    with sqlite3.connect(derived) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0] == 2


def test_recheck_before_frozen_interval_is_rejected(tmp_path: Path) -> None:
    """复核身份可以相同，但时间间隔是不可绕过的科研质量门。"""

    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(
        derived, candidate_build_id=build_id, config=config
    )
    path = tmp_path / "early-recheck.csv"
    _write_csv(
        path,
        [
            {
                "annotation_id": "early-1",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "annotator_hash": "a" * 64,
                "review_round": 1,
                "structure_label": "usable",
                "tourism_label": "related",
                "reason_codes": "test",
                "annotated_at_utc": "2026-07-30T01:00:00+00:00",
            },
            {
                "annotation_id": "early-2",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "annotator_hash": "a" * 64,
                "review_round": 2,
                "structure_label": "usable",
                "tourism_label": "related",
                "reason_codes": "test",
                "annotated_at_utc": "2026-08-01T01:00:00+00:00",
            },
        ],
    )

    with pytest.raises(AnnotationRepositoryError) as error:
        import_post_annotations(
            derived,
            csv_path=path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="b" * 64,
        )
    assert error.value.reason_code == "recheck_interval_not_met"
    with sqlite3.connect(derived) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM text_post_annotations"
        ).fetchone()[0] == 0


def test_sqlite_rejects_duplicate_review_round_but_allows_reused_reviewer(
    tmp_path: Path,
) -> None:
    """数据库拒绝重复轮次，但不把审核者身份误当作人数约束。"""

    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(derived, candidate_build_id=build_id, config=config)
    path = tmp_path / "one-annotation.csv"
    _write_csv(
        path,
        [
            {
                "annotation_id": "slot-one",
                "sample_run_id": sample.sample_run_id,
                "source_post_id": 1,
                "source_version": 1,
                "annotator_hash": "a" * 64,
                "review_round": 1,
                "structure_label": "usable",
                "tourism_label": "related",
                "reason_codes": "test",
                "annotated_at_utc": "2026-07-30T01:00:00+00:00",
            }
        ],
    )
    imported = import_post_annotations(
        derived,
        csv_path=path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="b" * 64,
    )
    with connect_derived(derived) as connection:
        base = connection.execute(
            "SELECT * FROM text_post_annotations WHERE annotation_id = 'slot-one'"
        ).fetchone()
        values = tuple(base)
        duplicate_slot = list(values)
        duplicate_slot[0] = "slot-copy"
        duplicate_slot[5] = "c" * 64
        with pytest.raises(sqlite3.IntegrityError, match="slot already filled"):
            connection.execute(
                "INSERT INTO text_post_annotations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(duplicate_slot),
            )
        reused_annotator = list(values)
        reused_annotator[0] = "slot-two-same-annotator"
        reused_annotator[6] = 2
        connection.execute(
            "INSERT INTO text_post_annotations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(reused_annotator),
        )
    assert imported.row_count == 1


def test_stability_waits_for_full_plan_then_freezes_supplement(tmp_path: Path) -> None:
    """不完整复核不能通过，稳定性偏低会创建固定且去重的补充复核轮次。"""

    derived, base_config, _, build_id = _candidate_fixture(tmp_path)
    config = _small_annotation_config(base_config)
    sample = create_initial_sampling_run(derived, candidate_build_id=build_id, config=config)
    with connect_derived(derived) as connection:
        double_post = connection.execute(
            """
            SELECT source_post_id, source_version FROM text_sample_members
            WHERE sample_run_id = ? AND requires_double_label = 1
            """,
            (sample.sample_run_id,),
        ).fetchone()

    common = {
        "sample_run_id": sample.sample_run_id,
        "source_post_id": int(double_post[0]),
        "source_version": int(double_post[1]),
        "structure_label": "usable",
        "reason_codes": "test",
    }
    slot_one = tmp_path / "agreement-slot-one.csv"
    _write_csv(
        slot_one,
        [
            {
                "annotation_id": "agreement-a1",
                **common,
                "annotator_hash": "1" * 64,
                "review_round": 1,
                "tourism_label": "related",
                "annotated_at_utc": "2026-07-30T01:00:00+00:00",
            }
        ],
    )
    import_post_annotations(
        derived,
        csv_path=slot_one,
        guide_version=config.text_label_guide_version,
        imported_by_hash="3" * 64,
        minimum_recheck_interval_days=1,
    )
    incomplete = evaluate_agreement_workflow(
        derived, sample_run_id=sample.sample_run_id, config=config
    )
    assert incomplete.status == "incomplete"
    assert incomplete.complete_pair_count == 0
    assert incomplete.metrics is None
    assert incomplete.supplement_run_id is None

    slot_two = tmp_path / "agreement-slot-two.csv"
    _write_csv(
        slot_two,
        [
            {
                "annotation_id": "agreement-a2",
                **common,
                "annotator_hash": "2" * 64,
                "review_round": 2,
                "tourism_label": "unrelated",
                "annotated_at_utc": "2026-08-01T01:01:00+00:00",
            }
        ],
    )
    import_post_annotations(
        derived,
        csv_path=slot_two,
        guide_version=config.text_label_guide_version,
        imported_by_hash="4" * 64,
        minimum_recheck_interval_days=1,
    )
    created = evaluate_agreement_workflow(
        derived, sample_run_id=sample.sample_run_id, config=config
    )
    awaiting_supplement = evaluate_agreement_workflow(
        derived, sample_run_id=sample.sample_run_id, config=config
    )
    first_export = tmp_path / "supplement-one.csv"
    second_export = tmp_path / "supplement-two.csv"

    assert created.status == "supplement_created"
    assert created.complete_pair_count == created.planned_pair_count == 1
    assert created.additional_recheck_required == 2
    assert created.supplement_selected_count == 2
    assert awaiting_supplement.status == "incomplete"
    assert awaiting_supplement.planned_pair_count == 3
    assert awaiting_supplement.complete_pair_count == 1
    assert awaiting_supplement.supplement_run_id is None
    with connect_derived(derived) as connection:
        supplement = connection.execute(
            """
            SELECT seal_status, member_manifest_sha256
            FROM text_double_label_supplements WHERE supplement_run_id = ?
            """,
            (created.supplement_run_id,),
        ).fetchone()
        assert supplement["seal_status"] == "finalized"
        assert len(supplement["member_manifest_sha256"]) == 64
    with connect_derived(derived) as second_connection:
        member = second_connection.execute(
            """
            SELECT * FROM text_double_label_supplement_members
            WHERE supplement_run_id = ? ORDER BY selection_rank LIMIT 1
            """,
            (created.supplement_run_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            second_connection.execute(
                """
                INSERT INTO text_double_label_supplement_members(
                    supplement_run_id, sample_run_id, source_post_id,
                    source_version, selection_rank
                ) VALUES (?, ?, ?, ?, 999)
                """,
                (
                    created.supplement_run_id,
                    sample.sample_run_id,
                    int(double_post[0]),
                    int(double_post[1]),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            second_connection.execute(
                """
                UPDATE text_double_label_supplement_members
                SET selection_rank = 999
                WHERE supplement_run_id = ? AND source_post_id = ?
                """,
                (created.supplement_run_id, member["source_post_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            second_connection.execute(
                """
                DELETE FROM text_double_label_supplement_members
                WHERE supplement_run_id = ? AND source_post_id = ?
                """,
                (created.supplement_run_id, member["source_post_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            second_connection.execute(
                """
                UPDATE text_double_label_supplements
                SET member_manifest_sha256 = ? WHERE supplement_run_id = ?
                """,
                ("0" * 64, created.supplement_run_id),
            )
        second_connection.execute("SAVEPOINT mismatched_supplement_parent")
        second_connection.execute(
            """
            INSERT INTO text_double_label_supplements(
                supplement_run_id, sample_run_id, sequence_number,
                trigger_evaluation_sha256, requested_count, selected_count,
                member_manifest_sha256, created_at_utc, seal_status
            )
            SELECT 'mismatched-supplement-parent', sample_run_id, 999,
                   ?, 1, 1, ?, created_at_utc, 'building'
            FROM text_double_label_supplements WHERE supplement_run_id = ?
            """,
            ("1" * 64, "2" * 64, created.supplement_run_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="parent mismatch"):
            second_connection.execute(
                """
                INSERT INTO text_double_label_supplement_members(
                    supplement_run_id, sample_run_id, source_post_id,
                    source_version, selection_rank
                ) VALUES ('mismatched-supplement-parent',
                          'missing-sample-parent', ?, ?, 1)
                """,
                (member["source_post_id"], member["source_version"]),
            )
        second_connection.execute("ROLLBACK TO mismatched_supplement_parent")
        second_connection.execute("RELEASE mismatched_supplement_parent")
    assert export_supplement_annotation_tasks(
        derived,
        supplement_run_id=str(created.supplement_run_id),
        review_round=1,
        output_path=first_export,
    ) == 2
    assert export_supplement_annotation_tasks(
        derived,
        supplement_run_id=str(created.supplement_run_id),
        review_round=2,
        output_path=second_export,
    ) == 2
    with first_export.open("r", encoding="utf-8", newline="") as first_stream:
        first_ids = {row["source_post_id"] for row in csv.DictReader(first_stream)}
    with second_export.open("r", encoding="utf-8", newline="") as second_stream:
        second_ids = {row["source_post_id"] for row in csv.DictReader(second_stream)}
    assert first_ids == second_ids
    assert str(double_post[0]) not in first_ids


def test_duplicate_candidate_needs_separate_final_review(tmp_path: Path) -> None:
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
    final_review_path = tmp_path / "pair-final-review.csv"
    _write_csv(
        final_review_path,
        [
            {
                "final_review_id": "pair-final",
                **common,
                "reviewer_hash": "1" * 64,
                "evidence_review_ids": "pair-a1|pair-a2",
                "reviewed_at_utc": "2026-07-30T04:00:00+00:00",
            }
        ],
    )
    import_duplicate_final_reviews(
        derived,
        csv_path=final_review_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="5" * 64,
    )
    confirmed = create_leakage_build(
        derived,
        candidate_build_id=build_id,
        duplicate_adjudication_ids=("pair-final",),
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
        assert connection.execute(
            "SELECT seal_status FROM text_leakage_builds WHERE leakage_build_id = ?",
            (confirmed.leakage_build_id,),
        ).fetchone()[0] == "finalized"
        member = connection.execute(
            "SELECT * FROM text_leakage_members WHERE leakage_build_id = ? LIMIT 1",
            (confirmed.leakage_build_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="rows are sealed"):
            connection.execute(
                """
                INSERT INTO text_leakage_members(
                    leakage_build_id, component_id, source_post_id, source_version,
                    author_edge_used, exact_edge_used, confirmed_near_edge_used
                ) VALUES (?, 'late-component', ?, ?, 0, 0, 0)
                """,
                (confirmed.leakage_build_id, member[2], member[3]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM text_leakage_members WHERE leakage_build_id = ?",
                (confirmed.leakage_build_id,),
            )
        connection.execute(
            """
            INSERT INTO text_leakage_builds(
                leakage_build_id, candidate_build_id,
                adjudication_manifest_sha256, input_post_count,
                component_count, output_sha256, created_at_utc, seal_status
            ) VALUES ('bad-leakage-seal', ?, ?, 1, 1, ?,
                      '2026-07-30T00:00:00+00:00', 'building')
            """,
            (build_id, "e" * 64, "f" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError, match="seal validation failed"):
            connection.execute(
                """
                UPDATE text_leakage_builds SET seal_status = 'finalized'
                WHERE leakage_build_id = 'bad-leakage-seal'
                """
            )


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


def test_stability_threshold_requests_fixed_additional_rechecks() -> None:
    config = annotation_config(
        # 复用正式配置可同时验证 0.80/0.70/100 的公开契约。
        load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v3.1.yaml")
    )
    records = [
        {
            "source_post_id": post_id,
            "source_version": 1,
            "assignment_slot": slot,
            "structure_label": "usable",
            "tourism_label": tourism,
        }
        for post_id, pair in ((1, ("related", "related")), (2, ("related", "unrelated")))
        for slot, tourism in enumerate(pair, 1)
    ]

    report = calculate_agreement(records, config=config)

    assert report.structure.raw_agreement == 1.0
    assert report.structure.cohen_kappa == 1.0
    assert report.tourism.raw_agreement == 0.5
    assert report.additional_recheck_required == 100


def test_agreement_excludes_tourism_when_structure_is_not_applicable() -> None:
    """结构无效配对不制造虚假的旅游一致率或追加双标需求。"""

    config = annotation_config(
        load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v3.1.yaml")
    )
    records = [
        {
            "source_post_id": 1,
            "source_version": 1,
            "assignment_slot": slot,
            "structure_label": "invalid",
            "tourism_label": "not_applicable",
        }
        for slot in (1, 2)
    ]

    report = calculate_agreement(records, config=config)

    assert report.structure.meets_threshold is True
    assert report.tourism.paired_count == 0
    assert report.tourism.raw_agreement is None
    assert report.tourism.cohen_kappa is None
    assert report.tourism.meets_threshold is None
    assert report.additional_recheck_required == 0
