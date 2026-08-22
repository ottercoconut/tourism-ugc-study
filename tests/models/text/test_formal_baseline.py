"""正式清洗 baseline 的全局切分、折外校准和运行包契约测试。"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest

import tourism_ugc_study.models.text.formal_training as formal_training
from tourism_ugc_study.cleaning.config import load_stable_config
from tourism_ugc_study.cleaning.reference_evidence import ReferenceValidationResult
from tourism_ugc_study.models.text.formal_baseline import (
    BaselineDocument,
    FormalBaselineError,
    build_global_split_plan,
    component_split_names,
    fit_formal_baseline,
    split_counts,
)
from tourism_ugc_study.models.text.formal_training import (
    BaselineEvidenceBundle,
    FormalTrainingError,
    load_baseline_evidence,
    load_frozen_baseline_model,
    train_formal_baseline_package,
)


ROOT = Path(__file__).resolve().parents[3]
HASH = "a" * 64
OTHER_HASH = "b" * 64


def _synthetic_documents() -> tuple[BaselineDocument, ...]:
    """构造不含平台且足以支持五折校准的平衡参考集。"""

    documents: list[BaselineDocument] = []
    for index in range(120):
        unrelated = index % 2 == 1
        documents.append(
            BaselineDocument(
                source_post_id=index + 1,
                source_version=1,
                captured_at_sort=f"2026-07-{index + 1:03d}",
                normalized_model_text=(
                    f"[TITLE]\n招聘广告{index}\n[BODY]\n房产产品促销工作岗位{index}"
                    if unrelated
                    else f"[TITLE]\n青岛旅行{index}\n[BODY]\n海边景点美食路线体验{index}"
                ),
                tourism_label="unrelated" if unrelated else "related",
                component_id=f"component-{index}",
                task_id=f"task-{index}",
            )
        )
    return tuple(documents)


def _config():
    """加载仓库冻结的正式清洗配置。"""

    return load_stable_config(ROOT / "configs/cleaning.yaml")


def test_baseline_document_and_split_contract_have_no_platform_field() -> None:
    assert "platform_key" not in {field.name for field in fields(BaselineDocument)}
    documents = _synthetic_documents()
    plan = build_global_split_plan(
        documents,
        random_seed=_config().random_seed,
        temporal_test_fraction=0.20,
        validation_fraction=0.20,
    )

    counts = split_counts(plan)
    assert plan.test_candidate_count == 24
    assert counts["test"] == 24
    assert all(len(names) == 1 for names in component_split_names(plan).values())
    assert all(
        next(
            assignment.split_name
            for assignment in plan.assignments
            if assignment.source_post_id == post_id
        )
        == "test"
        for post_id in range(97, 121)
    )


def test_global_temporal_candidates_expand_complete_leakage_component() -> None:
    documents = list(_synthetic_documents())
    # 最新候选 120 与早期记录 1 同属一个分量；扩展后两者必须都进测试集。
    documents[0] = BaselineDocument(
        **{
            **documents[0].__dict__,
            "component_id": documents[-1].component_id,
        }
    )
    plan = build_global_split_plan(
        documents,
        random_seed=_config().random_seed,
        temporal_test_fraction=0.20,
        validation_fraction=0.20,
    )
    assignments = {item.source_post_id: item.split_name for item in plan.assignments}

    assert assignments[1] == "test"
    assert assignments[120] == "test"
    assert split_counts(plan)["test"] == 25


def test_formal_baseline_uses_train_oof_and_does_not_fit_test_vocabulary() -> None:
    documents = _synthetic_documents()
    config = _config()
    plan = build_global_split_plan(
        documents,
        random_seed=config.random_seed,
        temporal_test_fraction=float(config.split["temporal_test_fraction"]),
        validation_fraction=float(config.split["validation_fraction"]),
    )
    split_by_id = {item.source_post_id: item.split_name for item in plan.assignments}
    poisoned = tuple(
        BaselineDocument(
            **{
                **item.__dict__,
                "normalized_model_text": "测试集唯一词绝不可进入词表"
                if split_by_id[item.source_post_id] == "test"
                else item.normalized_model_text,
                "tourism_label": "LOCKED"
                if split_by_id[item.source_post_id] == "test"
                else item.tourism_label,
            }
        )
        for item in documents
    )
    result = fit_formal_baseline(poisoned, split_plan=plan, config=config)

    vocabulary = result.model.pipeline.named_steps["vectorizer"].vocabulary_
    assert "测试集唯一" not in vocabulary
    assert len(result.train_oof_probabilities) == split_counts(plan)["train"]
    assert len(result.validation_probabilities) == split_counts(plan)["validation"]
    assert result.calibration_fold_count == 5
    assert all(
        0.0 <= row.p_unrelated <= 1.0
        for row in (*result.train_oof_probabilities, *result.validation_probabilities)
    )
    assert result.validation_metrics["diagnostic_cutoff_is_routing_threshold"] is False
    assert set(result.validation_metrics) >= {
        "precision_unrelated",
        "recall_unrelated",
        "f1_unrelated",
        "pr_auc_unrelated",
        "brier_score",
        "log_loss",
        "confusion",
    }


def test_baseline_rejects_component_leakage_or_split_identity_mismatch() -> None:
    documents = _synthetic_documents()
    config = _config()
    plan = build_global_split_plan(
        documents,
        random_seed=config.random_seed,
        temporal_test_fraction=0.20,
        validation_fraction=0.20,
    )
    with pytest.raises(FormalBaselineError) as error:
        fit_formal_baseline(documents[:-1], split_plan=plan, config=config)
    assert error.value.reason_code == "baseline_split_reference_mismatch"


def _evidence_bundle() -> BaselineEvidenceBundle:
    """构造不依赖真实数据库的已校验证据摘要。"""

    return BaselineEvidenceBundle(
        documents=_synthetic_documents(),
        reference=ReferenceValidationResult(
            csv_sha256=HASH,
            manifest_sha256=OTHER_HASH,
            row_count=120,
            label_counts={"related": 60, "unrelated": 60},
            frame_counts={"probability": 100, "targeted": 20},
            member_manifest_sha256=HASH,
            candidate_build_id="candidate-1",
            probability_estimation_status="valid",
            database_annotation_count=0,
        ),
        leakage_build_id="leakage-1",
        leakage_manifest_sha256=HASH,
        candidate_build_id="candidate-1",
        uncertain_count=0,
    )


def test_training_package_is_immutable_reusable_and_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        formal_training,
        "load_baseline_evidence",
        lambda *_args, **_kwargs: _evidence_bundle(),
    )
    first = train_formal_baseline_package(
        "reference.csv",
        "reference.json",
        "derived.sqlite",
        tmp_path / "models",
        leakage_build_id="leakage-1",
        config=_config(),
        code_version="c" * 40,
    )
    package = tmp_path / "models" / first.model_id
    manifest = json.loads((package / "training-manifest.json").read_text(encoding="utf-8"))
    model = load_frozen_baseline_model(package)
    probabilities = model.predict_p_unrelated(["[TITLE]\n青岛海边旅游攻略"])

    assert first.status == "frozen"
    assert first.reused is False
    assert first.test_status == "locked_not_opened"
    assert first.threshold_status == "UNSET"
    assert manifest["platform_used"] is False
    assert manifest["fit_scope"] == "train_only"
    assert manifest["calibration_scope"] == "train_grouped_out_of_fold_only"
    assert manifest["test_status"] == "locked_not_opened"
    assert manifest["threshold_status"] == "UNSET"
    assert np.isfinite(probabilities).all()

    def _unexpected_fit(*_args: object, **_kwargs: object) -> None:
        """保证相同请求在拟合前复用。"""

        raise AssertionError("相同内容寻址请求不得再次拟合")

    monkeypatch.setattr(formal_training, "fit_formal_baseline", _unexpected_fit)
    repeated = train_formal_baseline_package(
        "reference.csv",
        "reference.json",
        "derived.sqlite",
        tmp_path / "models",
        leakage_build_id="leakage-1",
        config=_config(),
        code_version="c" * 40,
    )
    assert repeated.model_id == first.model_id
    assert repeated.reused is True


def test_training_loader_reports_missing_leakage_schema_with_stable_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_path = tmp_path / "reference.csv"
    csv_path.write_bytes(b"")
    csv_hash = hashlib.sha256(b"").hexdigest()
    database = tmp_path / "derived.sqlite"
    sqlite3.connect(database).close()
    reference = ReferenceValidationResult(
        csv_sha256=csv_hash,
        manifest_sha256=OTHER_HASH,
        row_count=700,
        label_counts={"related": 350, "unrelated": 350},
        frame_counts={"probability": 500, "targeted": 200},
        member_manifest_sha256=HASH,
        candidate_build_id="candidate-1",
        probability_estimation_status="valid",
        database_annotation_count=0,
    )
    monkeypatch.setattr(
        formal_training,
        "validate_reference_evidence",
        lambda *_args, **_kwargs: reference,
    )
    monkeypatch.setattr(formal_training, "_reference_labels", lambda _path: ())

    with pytest.raises(FormalTrainingError) as error:
        load_baseline_evidence(
            csv_path,
            tmp_path / "reference.json",
            database,
            leakage_build_id="missing-leakage",
            config=_config(),
        )

    assert error.value.reason_code == "training_leakage_database_contract_invalid"


def test_training_package_refuses_tampered_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        formal_training,
        "load_baseline_evidence",
        lambda *_args, **_kwargs: _evidence_bundle(),
    )
    first = train_formal_baseline_package(
        "reference.csv",
        "reference.json",
        "derived.sqlite",
        tmp_path / "models",
        leakage_build_id="leakage-1",
        config=_config(),
        code_version="c" * 40,
    )
    package = tmp_path / "models" / first.model_id
    (package / "model.joblib").write_bytes(b"tampered")

    with pytest.raises(FormalTrainingError) as error:
        train_formal_baseline_package(
            "reference.csv",
            "reference.json",
            "derived.sqlite",
            tmp_path / "models",
            leakage_build_id="leakage-1",
            config=_config(),
            code_version="c" * 40,
        )
    assert error.value.reason_code == "baseline_package_artifact_hash_mismatch"
