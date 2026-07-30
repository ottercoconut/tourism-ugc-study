from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tourism_ugc_study.annotation.leakage_groups import create_leakage_build
from tourism_ugc_study.annotation.repository import (
    import_post_adjudications,
    import_post_annotations,
)
from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.scheduler import create_batch
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tourism_ugc_study.cleaning.state_machine import claim_tasks
from tourism_ugc_study.cleaning.text_config import load_text_config
from tourism_ugc_study.cleaning.text_repository import (
    build_text_candidates,
    process_text_tasks,
)
from tourism_ugc_study.models.text.config import relevance_config
from tourism_ugc_study.models.text.relevance import GoldDocument, fit_relevance_model
from tourism_ugc_study.models.text.repository import (
    ModelRepositoryError,
    TrainingOptions,
    train_relevance_from_adjudications,
)
from tourism_ugc_study.models.text.split import SplitDocument, build_split_plan
from tourism_ugc_study.models.text.thresholds import (
    PredictionInput,
    ThresholdPlan,
    route_predictions,
    select_thresholds,
)
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _synthetic_gold() -> tuple[GoldDocument, ...]:
    documents: list[GoldDocument] = []
    for platform_index, platform in enumerate(("xhs", "douyin")):
        for index in range(18):
            post_id = platform_index * 18 + index + 1
            unrelated = index % 2 == 1
            text = (
                f"[TITLE]\n青岛企业招聘广告{post_id}\n[BODY]\n产品推广房产资讯{post_id}"
                if unrelated
                else f"[TITLE]\n青岛旅行攻略{post_id}\n[BODY]\n海边景点美食路线体验{post_id}"
            )
            documents.append(
                GoldDocument(
                    source_post_id=post_id,
                    source_version=1,
                    platform_key=platform,
                    captured_at_sort=f"2026-07-{index + 1:02d}T00:00:00",
                    normalized_model_text=text,
                    tourism_label="unrelated" if unrelated else "related",
                    component_id=f"component-{post_id}",
                    adjudication_id=f"gold-{post_id}",
                )
            )
    return tuple(documents)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _integrated_smoke_inputs(tmp_path: Path):
    """构造 36 条合成帖子并走完快照、规范化、候选和人工金标链。"""

    source = tmp_path / "synthetic-source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    _build_source(source)
    gold = _synthetic_gold()
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO source_platforms VALUES ('douyin')")
        connection.execute("DELETE FROM web_post_images")
        connection.execute("DELETE FROM web_posts")
        connection.executemany(
            """
            INSERT INTO web_posts(
                id, platform_key, platform_post_id, source_type, source_url,
                title, author_platform_id, captured_at, content_text,
                post_images_count, status
            ) VALUES (?, ?, ?, 'search', ?, ?, ?, ?, ?, 0, 'captured')
            """,
            [
                (
                    item.source_post_id,
                    item.platform_key,
                    f"post-{item.source_post_id}",
                    f"https://invalid/{item.source_post_id}",
                    item.normalized_model_text.split("\n")[1],
                    f"author-{item.source_post_id}",
                    item.captured_at_sort,
                    item.normalized_model_text.split("\n")[-1],
                )
                for item in gold
            ],
        )
    config = load_config(PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml")
    text_config = load_text_config(
        PROJECT_ROOT / "configs" / "cleaning-text-normalization-v1.yaml",
        expected_version_lock=str(config.algorithm_versions["text_normalization"]),
    )
    snapshot = snapshot_source(source, derived, config, "synthetic-smoke")
    discover_increment(derived, snapshot.snapshot_id, config)
    batch = create_batch(derived, "synthetic-smoke", config)
    claims = claim_tasks(derived, batch.batch_id, config, stage_name="text_deterministic")
    processed = process_text_tasks(
        derived, claims, config=config, text_config=text_config
    )
    assert len(processed.succeeded) == 36
    build = build_text_candidates(
        derived,
        run_id="synthetic-smoke",
        snapshot_id=snapshot.snapshot_id,
        config=config,
        text_config=text_config,
    )

    annotation_path = tmp_path / "synthetic-annotations.csv"
    _write_csv(
        annotation_path,
        [
            {
                "annotation_id": f"raw-{item.source_post_id}",
                "sample_run_id": "",
                "source_post_id": item.source_post_id,
                "source_version": 1,
                "annotator_hash": "a" * 64,
                "assignment_slot": "",
                "structure_label": "usable",
                "tourism_label": item.tourism_label,
                # promotion 只作为独立标签出现，不改变相关性金标或模型动作。
                "commercial_label": (
                    "promotion" if item.source_post_id % 3 == 0 else "organic"
                ),
                "reason_codes": "synthetic_smoke",
                "annotated_at_utc": f"2026-07-30T01:{item.source_post_id:02d}:00+00:00",
            }
            for item in gold
        ],
    )
    import_post_annotations(
        derived,
        csv_path=annotation_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="b" * 64,
    )
    adjudication_path = tmp_path / "synthetic-adjudications.csv"
    _write_csv(
        adjudication_path,
        [
            {
                "adjudication_id": item.adjudication_id,
                "sample_run_id": "",
                "source_post_id": item.source_post_id,
                "source_version": 1,
                "adjudicator_hash": "c" * 64,
                "structure_label": "usable",
                "tourism_label": item.tourism_label,
                "commercial_label": (
                    "promotion" if item.source_post_id % 3 == 0 else "organic"
                ),
                "reason_codes": "synthetic_smoke",
                "evidence_annotation_ids": f"raw-{item.source_post_id}",
                "decision_context": "gold",
                "adjudicated_at_utc": f"2026-07-30T02:{item.source_post_id:02d}:00+00:00",
            }
            for item in gold
        ],
    )
    import_post_adjudications(
        derived,
        csv_path=adjudication_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="d" * 64,
    )
    leakage = create_leakage_build(
        derived,
        candidate_build_id=build.build_id,
        duplicate_adjudication_ids=(),
    )
    return derived, config, build.build_id, leakage.leakage_build_id, gold


def test_small_synthetic_training_is_leakage_safe_and_marked_smoke() -> None:
    documents = _synthetic_gold()
    config = relevance_config(load_config(PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"))
    split = build_split_plan(
        [
            SplitDocument(
                item.source_post_id,
                item.source_version,
                item.platform_key,
                item.captured_at_sort,
                item.tourism_label,
                item.component_id,
            )
            for item in documents
        ],
        random_seed=20260728,
        temporal_test_fraction=config.temporal_test_fraction,
        temporal_test_min_per_platform=2,
        validation_fraction=config.validation_fraction,
    )

    trained = fit_relevance_model(
        documents,
        split_plan=split,
        config=config,
        random_seed=20260728,
        smoke_only=True,
    )

    split_by_component: dict[str, set[str]] = {}
    for item in split.assignments:
        split_by_component.setdefault(item.component_id, set()).add(item.split_name)
    assert all(len(names) == 1 for names in split_by_component.values())
    assert trained.metrics["smoke_only"] is True
    assert trained.metrics["run_mode"] == "smoke"
    assert trained.metrics["positive_class"] == "unrelated"
    assert set(trained.metrics["split_counts"]) == {"train", "validation", "test"}
    for platform in ("xhs", "douyin"):
        platform_report = trained.metrics["test_slices"]["platform"][platform]
        assert platform_report["unrelated_count"] < 30
        assert platform_report["stable_conclusion_allowed"] is False
        assert platform_report["suppression_reason"] == (
            "platform_unrelated_count_below_minimum"
        )


def test_temporal_candidates_are_frozen_before_cross_platform_component_expansion() -> None:
    """另一平台被带入的旧成员不能替代该平台最新 20 条候选。"""

    documents: list[SplitDocument] = []
    for platform_index, platform in enumerate(("xhs", "douyin")):
        for index in range(100):
            post_id = platform_index * 100 + index + 1
            # xhs 最新 20 条所在分量还带有 douyin 最旧 20 条。旧实现会先把
            # 这 20 条计入 douyin 配额，从而不再冻结 douyin 自己的最新帖子。
            component = (
                f"bridge-{index - 80}"
                if platform == "xhs" and index >= 80
                else f"bridge-{index}"
                if platform == "douyin" and index < 20
                else f"component-{post_id}"
            )
            documents.append(
                SplitDocument(
                    source_post_id=post_id,
                    source_version=1,
                    platform_key=platform,
                    captured_at_sort=f"2026-07-{index + 1:03d}",
                    tourism_label="unrelated" if index % 2 else "related",
                    component_id=component,
                )
            )

    plan = build_split_plan(
        documents,
        random_seed=20260728,
        temporal_test_fraction=0.2,
        temporal_test_min_per_platform=20,
        validation_fraction=0.2,
    )
    split_by_id = {item.source_post_id: item.split_name for item in plan.assignments}

    assert set(range(81, 101)).issubset(split_by_id)
    assert set(range(181, 201)).issubset(split_by_id)
    assert all(split_by_id[post_id] == "test" for post_id in range(81, 101))
    assert all(split_by_id[post_id] == "test" for post_id in range(181, 201))
    assert set(plan.test_candidate_identities) == {
        *((post_id, 1) for post_id in range(81, 101)),
        *((post_id, 1) for post_id in range(181, 201)),
    }
    assert len({
        plan.train_manifest_sha256,
        plan.validation_manifest_sha256,
        plan.test_manifest_sha256,
        plan.manifest_sha256,
    }) == 4


def test_validation_thresholds_do_not_need_test_labels() -> None:
    plan = select_thresholds(
        ["related", "related", "unrelated", "unrelated"],
        [-2.0, -1.0, 1.0, 2.0],
        high_risk_precision_min=0.9,
        high_risk_recall_min=0.5,
        low_risk_related_precision_min=0.9,
        low_risk_related_recall_min=0.5,
    )

    assert plan.high_risk_threshold == 1.0
    assert plan.low_risk_threshold == -1.0
    assert plan.low_risk_enabled is True


def test_low_risk_audit_uses_platform_formula_and_never_excludes() -> None:
    thresholds = ThresholdPlan(1.0, -1.0, True, 1.0, 1.0, 1.0, 1.0)
    predictions = tuple(
        PredictionInput(index, 1, "xhs", -2.0) for index in range(1, 61)
    ) + (
        PredictionInput(100, 1, "xhs", 2.0),
        PredictionInput(101, 1, "xhs", 0.0),
    )

    routed = route_predictions(
        predictions,
        thresholds=thresholds,
        random_seed=20260728,
        model_run_id="smoke-model",
        low_risk_audit_fraction=0.05,
        low_risk_audit_min_per_platform=50,
    )

    assert sum(item.low_risk_audit_selected for item in routed) == 50
    assert sum(item.requires_human_review for item in routed) == 52
    assert {item.suggested_action for item in routed} == {
        "low_risk_keep_candidate",
        "high_risk_review",
        "manual_review",
    }
    assert all("exclude" not in item.suggested_action for item in routed)


def test_integrated_smoke_persists_model_manifest_without_human_override(
    tmp_path: Path,
) -> None:
    derived, config, candidate_build_id, leakage_build_id, gold = _integrated_smoke_inputs(
        tmp_path
    )
    artifacts = tmp_path / "artifacts"
    gold_manifest = tmp_path / "gold-ids.txt"
    gold_manifest.write_text(
        "\n".join(item.adjudication_id for item in gold) + "\n", encoding="utf-8"
    )
    candidate_manifest = tmp_path / "candidate-post-ids.txt"
    candidate_manifest.write_text(
        "\n".join(str(item.source_post_id) for item in gold) + "\n", encoding="utf-8"
    )

    process = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "text_train_relevance.py"),
            "--derived-db",
            str(derived),
            "--candidate-build-id",
            candidate_build_id,
            "--leakage-build-id",
            leakage_build_id,
            "--gold-adjudication-ids",
            str(gold_manifest),
            "--artifact-directory",
            str(artifacts),
            "--config",
            str(PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"),
            "smoke",
            "--test-min-per-platform",
            "2",
            "--candidate-post-ids",
            str(candidate_manifest),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(process.stdout)
    repeated = train_relevance_from_adjudications(
        derived,
        candidate_build_id=candidate_build_id,
        leakage_build_id=leakage_build_id,
        gold_adjudication_ids=[item.adjudication_id for item in gold],
        artifact_directory=artifacts,
        config=config,
        options=TrainingOptions(
            run_mode="smoke",
            temporal_test_min_per_platform_override=2,
            smoke_candidate_post_ids=tuple(item.source_post_id for item in gold),
        ),
    )

    assert payload["model_run_id"] == repeated.model_run_id
    assert payload["status"] == "smoke"
    assert payload["train_count"] + payload["validation_count"] + payload["test_count"] == 36
    assert payload["prediction_count"] == 36
    assert (artifacts / f"text-relevance-{repeated.model_run_id}.joblib").is_file()
    review_path = tmp_path / "model-review.csv"
    _write_csv(
        review_path,
        [
            {
                "adjudication_id": "model-review-1",
                "sample_run_id": "",
                "source_post_id": 1,
                "source_version": 1,
                "adjudicator_hash": "e" * 64,
                "structure_label": "usable",
                "tourism_label": "unrelated",
                "commercial_label": "promotion",
                "reason_codes": "human_confirmed_after_model_review",
                "evidence_annotation_ids": "raw-1",
                "decision_context": "model_review",
                "model_run_id": repeated.model_run_id,
                "adjudicated_at_utc": "2026-07-30T03:00:00+00:00",
            }
        ],
    )
    import_post_adjudications(
        derived,
        csv_path=review_path,
        guide_version=config.text_label_guide_version,
        imported_by_hash="f" * 64,
    )
    with sqlite3.connect(derived) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT metrics_json, status FROM text_model_runs WHERE model_run_id = ?",
            (repeated.model_run_id,),
        ).fetchone()
        metrics = json.loads(run["metrics_json"])
        assert run["status"] == "smoke"
        assert metrics["smoke_only"] is True
        assert metrics["run_mode"] == "smoke"
        assert connection.execute(
            "SELECT COUNT(*) FROM text_model_predictions WHERE suggested_action LIKE '%exclude%'"
        ).fetchone()[0] == 0
        # promotion 金标不会在模型表中生成排除字段或最终决定。
        prediction_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(text_model_predictions)")
        }
        assert "commercial_label" not in prediction_columns
        assert "cleaning_decision" not in prediction_columns
        assert connection.execute(
            """
            SELECT model_run_id FROM text_post_adjudications
            WHERE adjudication_id = 'model-review-1'
            """
        ).fetchone()[0] == repeated.model_run_id


def test_formal_training_cli_requires_explicit_execution_gate(tmp_path: Path) -> None:
    process = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "text_train_relevance.py"),
            "--derived-db",
            str(tmp_path / "must-not-create.sqlite"),
            "--candidate-build-id",
            "candidate",
            "--leakage-build-id",
            "leakage",
            "--gold-adjudication-ids",
            str(tmp_path / "missing.txt"),
            "--artifact-directory",
            str(tmp_path / "artifacts"),
            "formal",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 2
    assert "--execute-formal-training" in process.stderr
    assert not (tmp_path / "must-not-create.sqlite").exists()


def test_core_formal_training_gate_rejects_before_sqlite_connect(tmp_path: Path) -> None:
    """直接调用核心 API 也不能绕过正式训练确认或创建空数据库。"""

    database = tmp_path / "must-not-create-from-api.sqlite"
    config = load_config(PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml")

    with pytest.raises(ModelRepositoryError) as error:
        train_relevance_from_adjudications(
            database,
            candidate_build_id="candidate",
            leakage_build_id="leakage",
            gold_adjudication_ids=("gold",),
            artifact_directory=tmp_path / "artifacts",
            config=config,
            options=TrainingOptions(run_mode="formal"),
        )

    assert error.value.reason_code == "formal_execution_confirmation_required"
    assert not database.exists()


def test_smoke_rejects_36_gold_and_120_candidates_before_sqlite_connect(
    tmp_path: Path,
) -> None:
    """gold 很小时也不能借 smoke 对更大的候选语料执行预测或落库。"""

    database = tmp_path / "must-not-create-for-large-smoke.sqlite"
    config = load_config(PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml")

    with pytest.raises(ModelRepositoryError) as error:
        train_relevance_from_adjudications(
            database,
            candidate_build_id="candidate",
            leakage_build_id="leakage",
            gold_adjudication_ids=tuple(f"gold-{index}" for index in range(36)),
            artifact_directory=tmp_path / "artifacts",
            config=config,
            options=TrainingOptions(
                run_mode="smoke",
                temporal_test_min_per_platform_override=2,
                smoke_candidate_post_ids=tuple(range(1, 121)),
            ),
        )

    assert error.value.reason_code == "smoke_candidate_limit_exceeded"
    assert not database.exists()
