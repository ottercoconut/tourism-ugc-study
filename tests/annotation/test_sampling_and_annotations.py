from __future__ import annotations

import csv
import hashlib
import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from tourism_ugc_study.annotation.config import annotation_config
from tourism_ugc_study.annotation.leakage_groups import create_leakage_build
from tourism_ugc_study.annotation.repository import (
    AnnotationRepositoryError,
    POST_ANNOTATION_TASK_FIELDS,
    create_initial_sampling_run,
    create_periodic_sampling_run,
    export_near_duplicate_candidates,
    export_post_annotation_tasks,
    export_post_annotation_tasks_reusing_labels,
    finalize_post_annotation_tasks,
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
        "initial_targeted_size": 3,
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


def test_initial_sampling_is_reproducible_and_exports_single_axis(tmp_path: Path) -> None:
    derived, config, _, build_id = _candidate_fixture(tmp_path)

    first = create_initial_sampling_run(
        derived, candidate_build_id=build_id, config=config
    )
    repeated = create_initial_sampling_run(
        derived, candidate_build_id=build_id, config=config
    )
    output = tmp_path / "tourism-relevance.csv"

    assert first == repeated
    assert first.population_count == 3
    assert first.probability_count == 3
    assert first.targeted_count == 0
    assert export_post_annotation_tasks(
        derived,
        sample_run_id=first.sample_run_id,
        output_path=output,
    ) == 3
    with output.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert tuple(rows[0]) == POST_ANNOTATION_TASK_FIELDS
    assert all(not row["tourism_label"] for row in rows)
    assert "structure_label" not in rows[0]
    assert "review_round" not in rows[0]

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
        member_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(text_sample_members)")
        }
        run_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(text_sampling_runs)")
        }
        assert "requires_double_label" not in member_columns
        assert "double_label_count" not in run_columns



def test_resampled_export_reuses_labels_by_frozen_identity_and_keeps_history(
    tmp_path: Path,
) -> None:
    """重抽样只能按帖子版本复用标签，历史证据文件不得被覆盖。"""

    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(
        derived,
        candidate_build_id=build_id,
        config=config,
    )
    historical = tmp_path / "historical-completed.csv"
    export_post_annotation_tasks(
        derived,
        sample_run_id=sample.sample_run_id,
        output_path=historical,
    )
    with historical.open("r", encoding="utf-8", newline="") as stream:
        historical_rows = list(csv.DictReader(stream))
    for row, label in zip(
        historical_rows,
        ("related", "unrelated", "uncertain"),
        strict=True,
    ):
        row["tourism_label"] = label
    _write_csv(historical, historical_rows)
    historical_sha256 = hashlib.sha256(historical.read_bytes()).hexdigest()

    optimized = tmp_path / "optimized.csv"
    pending = tmp_path / "pending.csv"
    result = export_post_annotation_tasks_reusing_labels(
        derived,
        sample_run_id=sample.sample_run_id,
        previous_completed_path=historical,
        output_path=optimized,
        pending_output_path=pending,
    )

    assert result.optimized_row_count == result.reused_label_count == 3
    assert result.pending_label_count == 0
    assert hashlib.sha256(historical.read_bytes()).hexdigest() == historical_sha256
    with optimized.open("r", encoding="utf-8", newline="") as stream:
        optimized_rows = list(csv.DictReader(stream))
    with pending.open("r", encoding="utf-8", newline="") as stream:
        pending_rows = list(csv.DictReader(stream))
    assert {row["tourism_label"] for row in optimized_rows} == {
        "related",
        "unrelated",
        "uncertain",
    }
    assert pending_rows == []


def test_finalize_post_tasks_emits_one_complete_current_sample(tmp_path: Path) -> None:
    """待标表必须完整覆盖 base 空标签，成功后只输出一张全标签完成表。"""

    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(
        derived,
        candidate_build_id=build_id,
        config=config,
    )
    base = tmp_path / "optimized.csv"
    export_post_annotation_tasks(
        derived,
        sample_run_id=sample.sample_run_id,
        output_path=base,
    )
    with base.open("r", encoding="utf-8", newline="") as stream:
        pending_rows = list(csv.DictReader(stream))
    for index, row in enumerate(pending_rows):
        row["tourism_label"] = "related" if index == 0 else "unrelated"
    pending = tmp_path / "pending.csv"
    _write_csv(pending, pending_rows)

    completed = tmp_path / "completed.csv"
    result = finalize_post_annotation_tasks(
        base_path=base,
        pending_path=pending,
        output_path=completed,
    )

    assert result.row_count == 3
    assert result.related_count == 1
    assert result.unrelated_count == 2
    assert result.uncertain_count == 0
    with completed.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        completed_rows = list(reader)
        assert reader.fieldnames == list(POST_ANNOTATION_TASK_FIELDS)
    assert all(row["tourism_label"] for row in completed_rows)


def test_finalize_post_tasks_rejects_incomplete_pending_coverage(tmp_path: Path) -> None:
    """待标表遗漏 base 的任一空标签身份时不得生成伪完成表。"""

    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(
        derived,
        candidate_build_id=build_id,
        config=config,
    )
    base = tmp_path / "optimized.csv"
    export_post_annotation_tasks(
        derived,
        sample_run_id=sample.sample_run_id,
        output_path=base,
    )
    with base.open("r", encoding="utf-8", newline="") as stream:
        pending_rows = list(csv.DictReader(stream))
    pending_rows = pending_rows[:-1]
    for row in pending_rows:
        row["tourism_label"] = "related"
    pending = tmp_path / "pending.csv"
    _write_csv(pending, pending_rows)

    with pytest.raises(AnnotationRepositoryError) as exc_info:
        finalize_post_annotation_tasks(
            base_path=base,
            pending_path=pending,
            output_path=tmp_path / "completed.csv",
        )

    assert exc_info.value.reason_code == "finalize_pending_identity_mismatch"


def test_probability_sample_records_platform_inclusion_weights() -> None:
    config = load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v3.2.yaml")
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


def test_probability_sample_allocates_all_quotas_proportionally() -> None:
    """平台配额从零按人口占比计算，不再预留每平台最低数量。"""

    config = load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v3.2.yaml")
    platform_counts = {
        "bilibili": 400,
        "douyin": 300,
        "weibo": 200,
        "xhs": 75,
        "zhihu": 25,
    }
    posts = tuple(
        SamplingPost(
            source_post_id=platform_index * 1_000 + index + 1,
            source_version=1,
            platform_key=platform,
            normalized_length=100,
            near_candidate_count=0,
            cross_platform_near_count=0,
        )
        for platform_index, (platform, count) in enumerate(platform_counts.items())
        for index in range(count)
    )

    plan = build_initial_sample_plan(
        posts,
        config=annotation_config(config),
        random_seed=config.random_seed,
    )
    probability = [item for item in plan.members if item.sample_frame == "probability"]
    actual = Counter(item.platform_key for item in probability)

    # 37.5 与 12.5 的余数相同，按平台键稳定决胜，额外名额给 xhs。
    assert actual == {
        "bilibili": 200,
        "douyin": 150,
        "weibo": 100,
        "xhs": 38,
        "zhihu": 12,
    }


def test_targeted_sample_uses_one_post_per_component_and_excludes_probability_components() -> None:
    """定向相关性任务不得让同一高相似文本家族重复消耗名额。"""

    config = load_config(Path(__file__).resolve().parents[2] / "configs" / "cleaning-v3.2.yaml")
    small = replace(
        config,
        raw={
            **config.raw,
            "annotation": {
                **config.raw["annotation"],
                "initial_probability_size": 2,
                "initial_targeted_size": 3,
            },
        },
    )
    posts = tuple(
        SamplingPost(
            source_post_id=index,
            source_version=1,
            platform_key="xhs",
            normalized_length=60 + index,
            near_candidate_count=int(component != "singleton"),
            cross_platform_near_count=0,
            near_component_id=(component if component != "singleton" else f"single-{index}"),
        )
        for index, component in enumerate(
            ("campaign-a", "campaign-a", "campaign-a", "campaign-b", "campaign-b", "singleton", "singleton", "singleton"),
            1,
        )
    )

    plan = build_initial_sample_plan(
        posts,
        config=annotation_config(small),
        random_seed=small.random_seed,
    )
    probability = [item for item in plan.members if item.sample_frame == "probability"]
    targeted = [item for item in plan.members if item.sample_frame == "targeted"]
    component_by_id = {post.source_post_id: post.near_component_id for post in posts}
    probability_components = {component_by_id[item.source_post_id] for item in probability}
    targeted_components = [component_by_id[item.source_post_id] for item in targeted]

    assert len(targeted_components) == len(set(targeted_components))
    assert not probability_components.intersection(targeted_components)
    assert len(targeted) == 3


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
        [{
            "annotation_id": "post-a1",
            "sample_run_id": sample.sample_run_id,
            "source_post_id": 1,
            "source_version": 1,
            "tourism_label": "related",
        }],
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
        [{
            "final_review_id": "post-final-1",
            "sample_run_id": sample.sample_run_id,
            "source_post_id": 1,
            "source_version": 1,
            "tourism_label": "related",
            "evidence_review_ids": "post-a1",
            "decision_context": "reference",
        }],
    )
    import_post_final_reviews(
        derived,
        csv_path=final_review_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="e" * 64,
    )

    obsolete_final_review_path = tmp_path / "obsolete-final-review.csv"
    _write_csv(
        obsolete_final_review_path,
        [{
            "final_review_id": "post-final-obsolete",
            "sample_run_id": sample.sample_run_id,
            "source_post_id": 1,
            "source_version": 1,
            "reviewer_hash": "a" * 64,
            "tourism_label": "related",
            "evidence_review_ids": "post-a1",
            "decision_context": "reference",
            "reviewed_at_utc": "2026-07-30T02:00:00+00:00",
        }],
    )
    with pytest.raises(AnnotationRepositoryError) as obsolete_metadata:
        import_post_final_reviews(
            derived,
            csv_path=obsolete_final_review_path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="e" * 64,
        )
    assert obsolete_metadata.value.reason_code == "row_review_metadata_not_in_contract"

    assert imported.reused is False
    assert reused.reused is True
    with connect_derived(derived) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM text_post_adjudications").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE text_post_annotations SET tourism_label = 'unrelated'")



def test_post_annotation_contract_rejects_obsolete_axes_and_allows_uncertain(
    tmp_path: Path,
) -> None:
    """旧版结构/轮次/商业字段必须失败；uncertain 可合法为零或极少出现。"""

    derived, config, _, build_id = _candidate_fixture(tmp_path)
    sample = create_initial_sampling_run(
        derived, candidate_build_id=build_id, config=config
    )
    common = {
        "sample_run_id": sample.sample_run_id,
        "source_post_id": 1,
        "source_version": 1,
    }
    legacy_path = tmp_path / "legacy.csv"
    _write_csv(
        legacy_path,
        [{
            **common,
            "review_round": 1,
            "structure_label": "usable",
            "tourism_label": "related",
            "commercial_label": "uncertain",
        }],
    )
    with pytest.raises(AnnotationRepositoryError) as legacy_error:
        import_post_annotations(
            derived,
            csv_path=legacy_path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="b" * 64,
        )
    assert legacy_error.value.reason_code == "commercial_label_not_in_cleaning_contract"

    uncertain_path = tmp_path / "uncertain.csv"
    _write_csv(
        uncertain_path,
        [{"annotation_id": "uncertain-1", **common, "tourism_label": "uncertain"}],
    )
    result = import_post_annotations(
        derived,
        csv_path=uncertain_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="b" * 64,
    )
    assert result.row_count == 1

    removed_reason_path = tmp_path / "removed-reason.csv"
    _write_csv(
        removed_reason_path,
        [{
            **common,
            "annotation_id": "removed-reason-1",
            "tourism_label": "related",
            "reason_codes": "travel_main_subject",
        }],
    )
    with pytest.raises(AnnotationRepositoryError) as reason_error:
        import_post_annotations(
            derived,
            csv_path=removed_reason_path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="b" * 64,
        )
    assert reason_error.value.reason_code == "reason_codes_not_in_cleaning_contract"

    obsolete_metadata_path = tmp_path / "obsolete-metadata.csv"
    _write_csv(
        obsolete_metadata_path,
        [{
            **common,
            "annotation_id": "obsolete-metadata-1",
            "tourism_label": "related",
            "annotator_hash": "a" * 64,
            "annotated_at_utc": "2026-07-30T01:00:00+00:00",
        }],
    )
    with pytest.raises(AnnotationRepositoryError) as metadata_error:
        import_post_annotations(
            derived,
            csv_path=obsolete_metadata_path,
            guide_version=config.text_label_guide_version,
            imported_by_hash="b" * 64,
        )
    assert metadata_error.value.reason_code == "row_annotation_metadata_not_in_contract"
    with connect_derived(derived) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(text_post_annotations)")
        }
        assert "commercial_label" not in columns
        assert "structure_label" not in columns
        assert "assignment_slot" not in columns











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
            },
            {
                "annotation_id": "pair-a2",
                **common,
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
                "evidence_review_ids": "pair-a1|pair-a2",
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
