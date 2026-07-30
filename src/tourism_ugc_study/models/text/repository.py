"""仲裁金标读取、模型产物落盘和候选预测审计的集成边界。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import joblib

from tourism_ugc_study.cleaning.config import CleaningConfig
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived

from .config import relevance_config
from .relevance import GoldDocument, fit_relevance_model, unrelated_margins
from .split import SplitDocument, build_split_plan
from .thresholds import PredictionInput, route_predictions


class ModelRepositoryError(RuntimeError):
    """显式金标、泄漏清单、模型产物或审计写入不满足契约时抛出。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("text model repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class TrainingOptions:
    """训练执行模式；小样本覆盖只允许用于明确标记的 smoke。"""

    smoke_only: bool = False
    temporal_test_min_per_platform_override: int | None = None
    smoke_max_gold_documents: int = 100


@dataclass(frozen=True)
class ModelRunResult:
    """模型运行身份、三集合规模、阈值和候选队列计数。"""

    model_run_id: str
    status: str
    train_count: int
    validation_count: int
    test_count: int
    chosen_c: float
    high_risk_threshold: float | None
    low_risk_threshold: float | None
    low_risk_enabled: bool
    prediction_count: int
    review_required_count: int
    artifact_sha256: str


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _explicit_gold_documents(
    connection,
    *,
    candidate_build_id: str,
    leakage_build_id: str,
    adjudication_ids: Sequence[str],
    guide_version: str,
) -> tuple[tuple[GoldDocument, ...], str]:
    """只读取显式列出的仲裁二分类金标，拒绝“最新标签”推断。"""

    if not adjudication_ids or len(adjudication_ids) != len(set(adjudication_ids)):
        raise ModelRepositoryError("unique_gold_adjudication_ids_required")
    placeholders = ",".join("?" for _ in adjudication_ids)
    rows = connection.execute(
        f"""
        SELECT a.adjudication_id, a.source_post_id, a.source_version,
               a.structure_label, a.tourism_label, a.guide_version,
               a.decision_context, i.platform_key, i.captured_at_sort,
               r.normalized_model_text, l.component_id
        FROM text_post_adjudications AS a
        JOIN source_post_inventory AS i ON i.source_post_id = a.source_post_id
        JOIN text_candidate_corpus_members AS c
          ON c.build_id = ? AND c.source_post_id = a.source_post_id
         AND c.source_version = a.source_version
        JOIN text_deterministic_results AS r ON r.task_id = c.task_id
        JOIN text_leakage_members AS l
          ON l.leakage_build_id = ? AND l.source_post_id = a.source_post_id
         AND l.source_version = a.source_version
        WHERE a.adjudication_id IN ({placeholders})
        """,
        (candidate_build_id, leakage_build_id, *adjudication_ids),
    ).fetchall()
    if len(rows) != len(adjudication_ids):
        raise ModelRepositoryError("gold_adjudication_not_in_model_corpus")
    seen_posts: set[tuple[int, int]] = set()
    documents: list[GoldDocument] = []
    for row in rows:
        identity = (int(row["source_post_id"]), int(row["source_version"]))
        if identity in seen_posts:
            raise ModelRepositoryError("multiple_gold_adjudications_for_post")
        seen_posts.add(identity)
        if (
            row["decision_context"] != "gold"
            or row["guide_version"] != guide_version
            or row["structure_label"] != "usable"
            or row["tourism_label"] not in {"related", "unrelated"}
        ):
            raise ModelRepositoryError("inadmissible_gold_adjudication")
        documents.append(
            GoldDocument(
                source_post_id=identity[0],
                source_version=identity[1],
                platform_key=str(row["platform_key"]),
                captured_at_sort=str(row["captured_at_sort"] or ""),
                normalized_model_text=str(row["normalized_model_text"]),
                tourism_label=str(row["tourism_label"]),
                component_id=str(row["component_id"]),
                adjudication_id=str(row["adjudication_id"]),
            )
        )
    ordered = tuple(sorted(documents, key=lambda item: (item.source_post_id, item.source_version)))
    manifest = _sha256(
        [
            [
                item.adjudication_id,
                item.source_post_id,
                item.source_version,
                item.tourism_label,
                item.component_id,
                _sha256(item.normalized_model_text),
            ]
            for item in ordered
        ]
    )
    return ordered, manifest


def _candidate_prediction_inputs(connection, build_id: str) -> tuple[tuple[PredictionInput, str], ...]:
    """读取完整可用规范化语料；不从正式源库或人工标签表猜测输入。"""

    return tuple(
        (
            PredictionInput(
                int(row["source_post_id"]),
                int(row["source_version"]),
                str(row["platform_key"]),
                0.0,
            ),
            str(row["normalized_model_text"]),
        )
        for row in connection.execute(
            """
            SELECT c.source_post_id, c.source_version, c.platform_key,
                   r.normalized_model_text
            FROM text_candidate_corpus_members AS c
            JOIN text_deterministic_results AS r ON r.task_id = c.task_id
            WHERE c.build_id = ? AND c.structure_status = 'usable'
            ORDER BY c.source_post_id, c.source_version
            """,
            (build_id,),
        )
    )


def _write_artifact(path: Path, payload: object) -> str:
    """在目标目录原子替换模型文件，失败时不留下半写产物。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".text-model-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        joblib.dump(payload, temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return _file_sha256(path)


def _stored_result(connection, model_run_id: str) -> ModelRunResult | None:
    row = connection.execute(
        "SELECT * FROM text_model_runs WHERE model_run_id = ?", (model_run_id,)
    ).fetchone()
    if row is None:
        return None
    prediction = connection.execute(
        """
        SELECT COUNT(*) AS count, SUM(requires_human_review) AS review_count
        FROM text_model_predictions WHERE model_run_id = ?
        """,
        (model_run_id,),
    ).fetchone()
    return ModelRunResult(
        model_run_id,
        str(row["status"]),
        int(row["train_count"]),
        int(row["validation_count"]),
        int(row["test_count"]),
        float(row["chosen_c"]),
        float(row["high_risk_threshold"]) if row["high_risk_threshold"] is not None else None,
        float(row["low_risk_threshold"]) if row["low_risk_threshold"] is not None else None,
        bool(row["low_risk_enabled"]),
        int(prediction["count"]),
        int(prediction["review_count"] or 0),
        str(row["model_artifact_sha256"]),
    )


def train_relevance_from_adjudications(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    leakage_build_id: str,
    gold_adjudication_ids: Sequence[str],
    artifact_directory: str | Path,
    config: CleaningConfig,
    options: TrainingOptions = TrainingOptions(),
) -> ModelRunResult:
    """训练并持久化相关性模型；模型输出只形成候选与人工复核队列。

    正式模式使用配置中的每平台至少 20 条时序测试约束。只有
    `smoke_only=True` 时才允许降低该数量，且金标数超过上限会拒绝运行，防止
    smoke 入口被误用于全量训练。
    """

    if options.temporal_test_min_per_platform_override is not None and not options.smoke_only:
        raise ModelRepositoryError("split_override_requires_smoke_mode")
    model_config = relevance_config(config)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        build = connection.execute(
            "SELECT run_id, status, is_complete_corpus FROM text_candidate_builds WHERE build_id = ?",
            (candidate_build_id,),
        ).fetchone()
        leakage = connection.execute(
            "SELECT candidate_build_id FROM text_leakage_builds WHERE leakage_build_id = ?",
            (leakage_build_id,),
        ).fetchone()
        if (
            build is None
            or build["status"] != "finalized"
            or not int(build["is_complete_corpus"])
            or leakage is None
            or leakage["candidate_build_id"] != candidate_build_id
        ):
            raise ModelRepositoryError("model_input_build_mismatch")
        documents, gold_manifest = _explicit_gold_documents(
            connection,
            candidate_build_id=candidate_build_id,
            leakage_build_id=leakage_build_id,
            adjudication_ids=gold_adjudication_ids,
            guide_version=config.text_label_guide_version,
        )
        if options.smoke_only and len(documents) > options.smoke_max_gold_documents:
            raise ModelRepositoryError("smoke_gold_limit_exceeded")
        split_plan = build_split_plan(
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
            random_seed=config.random_seed,
            temporal_test_fraction=model_config.temporal_test_fraction,
            temporal_test_min_per_platform=(
                options.temporal_test_min_per_platform_override
                if options.temporal_test_min_per_platform_override is not None
                else model_config.temporal_test_min_per_platform
            ),
            validation_fraction=model_config.validation_fraction,
        )
        training = fit_relevance_model(
            documents,
            split_plan=split_plan,
            config=model_config,
            random_seed=config.random_seed,
            smoke_only=options.smoke_only,
        )
        threshold_payload = training.thresholds.__dict__
        model_run_id = _sha256(
            [
                candidate_build_id,
                leakage_build_id,
                config.algorithm_versions["text_relevance"],
                config.sha256,
                gold_manifest,
                split_plan.manifest_sha256,
                training.chosen_c,
                threshold_payload,
                "smoke" if options.smoke_only else "completed",
            ]
        )[:32]
        existing = _stored_result(connection, model_run_id)
        if existing is not None:
            return existing
        candidates = _candidate_prediction_inputs(connection, candidate_build_id)
        margins = unrelated_margins(training.pipeline, [text for _, text in candidates])
        prediction_inputs = tuple(
            PredictionInput(
                item.source_post_id,
                item.source_version,
                item.platform_key,
                float(margin),
            )
            for (item, _), margin in zip(candidates, margins, strict=True)
        )
        routed = route_predictions(
            prediction_inputs,
            thresholds=training.thresholds,
            random_seed=config.random_seed,
            model_run_id=model_run_id,
            low_risk_audit_fraction=model_config.low_risk_audit_fraction,
            low_risk_audit_min_per_platform=model_config.low_risk_audit_min_per_platform,
        )

    artifact_path = Path(artifact_directory) / f"text-relevance-{model_run_id}.joblib"
    artifact_hash = _write_artifact(
        artifact_path,
        {
            "pipeline": training.pipeline,
            "metadata": {
                "model_run_id": model_run_id,
                "smoke_only": options.smoke_only,
                "gold_manifest_sha256": gold_manifest,
                "split_manifest_sha256": split_plan.manifest_sha256,
                "chosen_c": training.chosen_c,
                "thresholds": threshold_payload,
            },
        },
    )
    split_counts = Counter(item.split_name for item in split_plan.assignments)
    metrics = {
        **training.metrics,
        "gold_manifest_sha256": gold_manifest,
        "split_manifest_sha256": split_plan.manifest_sha256,
        "thresholds": threshold_payload,
        "prediction_count": len(routed),
        "review_required_count": sum(item.requires_human_review for item in routed),
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        with connection:
            connection.execute(
                """
                INSERT INTO text_model_runs(
                    model_run_id, run_id, candidate_build_id, leakage_build_id,
                    guide_version, algorithm_version, config_sha256,
                    gold_manifest_sha256, split_manifest_sha256,
                    train_count, validation_count, test_count, chosen_c,
                    high_risk_threshold, low_risk_threshold, low_risk_enabled,
                    metrics_json, model_artifact_path, model_artifact_sha256,
                    status, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_run_id,
                    str(build["run_id"]),
                    candidate_build_id,
                    leakage_build_id,
                    config.text_label_guide_version,
                    str(config.algorithm_versions["text_relevance"]),
                    config.sha256,
                    gold_manifest,
                    split_plan.manifest_sha256,
                    split_counts["train"],
                    split_counts["validation"],
                    split_counts["test"],
                    training.chosen_c,
                    training.thresholds.high_risk_threshold,
                    training.thresholds.low_risk_threshold,
                    int(training.thresholds.low_risk_enabled),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    str(artifact_path),
                    artifact_hash,
                    "smoke" if options.smoke_only else "completed",
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO text_dataset_splits(
                    model_run_id, source_post_id, source_version,
                    component_id, split_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        model_run_id,
                        item.source_post_id,
                        item.source_version,
                        item.component_id,
                        item.split_name,
                    )
                    for item in split_plan.assignments
                ],
            )
            connection.executemany(
                """
                INSERT INTO text_model_predictions(
                    model_run_id, source_post_id, source_version, margin,
                    suggested_action, requires_human_review,
                    low_risk_audit_selected, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        model_run_id,
                        item.source_post_id,
                        item.source_version,
                        item.margin,
                        item.suggested_action,
                        int(item.requires_human_review),
                        int(item.low_risk_audit_selected),
                        now,
                    )
                    for item in routed
                ],
            )
    return ModelRunResult(
        model_run_id,
        "smoke" if options.smoke_only else "completed",
        split_counts["train"],
        split_counts["validation"],
        split_counts["test"],
        training.chosen_c,
        training.thresholds.high_risk_threshold,
        training.thresholds.low_risk_threshold,
        training.thresholds.low_risk_enabled,
        len(routed),
        sum(item.requires_human_review for item in routed),
        artifact_hash,
    )
