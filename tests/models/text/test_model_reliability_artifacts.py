"""双模型人口框与 Wave A 私有 artifact 边界测试。"""

import csv
import codecs
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import tourism_ugc_study.models.text.model_reliability_artifacts as artifacts
from tourism_ugc_study.models.text.model_reliability_config import (
    load_model_reliability_plan,
)
from tourism_ugc_study.models.text.model_reliability_study import (
    EligibleEvaluationMember,
    EligiblePopulation,
    ScoredEvaluationMember,
)
from tourism_ugc_study.models.text.qwen_head_tail_artifacts import (
    QwenHeadTailArtifactError,
    load_frozen_qwen_head_tail_model,
)
from tourism_ugc_study.models.text.qwen_head_tail_config import (
    load_qwen_head_tail_plan,
)


PLAN = load_model_reliability_plan(
    Path("configs/cleaning-model-reliability-study.yaml")
)


def _scored(index: int) -> ScoredEvaluationMember:
    """生成六层均衡的合成双模型概率。"""

    patterns = [
        (0.95, 0.95),
        (0.20, 0.95),
        (0.95, 0.20),
        (0.80, 0.20),
        (0.80, 0.70),
        (0.20, 0.30),
    ]
    sparse, qwen = patterns[index % len(patterns)]
    return ScoredEvaluationMember(
        source_post_id=index + 1,
        source_version=1,
        component_id=f"component-{index // 2}",
        normalized_sha256=f"{index:064x}",
        sparse_p_unrelated=sparse,
        qwen_p_unrelated=qwen,
    )


def test_qwen_loader_rejects_wrong_joblib_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """公开加载入口不得接受任意 joblib 对象。"""

    monkeypatch.setattr(
        artifacts, "load_frozen_qwen_head_tail_model", load_frozen_qwen_head_tail_model
    )
    import tourism_ugc_study.models.text.qwen_head_tail_artifacts as qwen_artifacts

    monkeypatch.setattr(qwen_artifacts, "_validate_existing_package", lambda *args, **kwargs: None)
    monkeypatch.setattr(qwen_artifacts.joblib, "load", lambda _path: object())
    plan = load_qwen_head_tail_plan(
        Path("configs/cleaning-qwen-english-head-tail.yaml")
    )

    with pytest.raises(QwenHeadTailArtifactError) as error:
        load_frozen_qwen_head_tail_model(
            tmp_path,
            expected_run_id="bdf73219d584edfcbeea02772716a90a",
            expected_manifest_sha256="e" * 64,
            plan=plan,
        )

    assert error.value.reason_code == "qwen_head_tail_model_type_invalid"


def test_prepare_wave_a_hides_model_answers_and_preserves_private_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """公开任务只含文本和空标签，概率、分层与权重仅在私有映射。"""

    scored = tuple(
        replace(
            _scored(index),
            normalized_sha256=hashlib.sha256(
                f"合成文本 {index}".encode("utf-8")
            ).hexdigest(),
        )
        for index in range(600)
    )
    plan = replace(
        PLAN,
        expected_eligible_count=600,
        expected_eligible_component_count=300,
    )
    members = tuple(
        EligibleEvaluationMember(
            source_post_id=item.source_post_id,
            source_version=item.source_version,
            component_id=item.component_id,
            platform_key="synthetic",
            normalized_model_text=f"合成文本 {index}",
            normalized_sha256=item.normalized_sha256,
        )
        for index, item in enumerate(scored)
    )
    population = EligiblePopulation(
        members=members,
        candidate_count=600,
        candidate_component_count=300,
        reference_count=0,
        reference_component_count=0,
        excluded_count=0,
        eligible_component_count=300,
        projection_member_sha256="a" * 64,
        leakage_output_sha256="b" * 64,
    )
    scored_package = tmp_path / "scored"
    scored_package.mkdir()
    manifest_path = scored_package / "frame-manifest.json"
    manifest_path.write_text(json.dumps({"frame_id": "frame"}), encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    monkeypatch.setattr(artifacts, "load_model_reliability_plan", lambda _path: plan)
    monkeypatch.setattr(artifacts, "_load_scored_members", lambda *args, **kwargs: scored)
    monkeypatch.setattr(
        artifacts,
        "load_eligible_evaluation_population",
        lambda *args, **kwargs: population,
    )

    result = artifacts.prepare_wave_a_package(
        tmp_path / "reference.csv",
        tmp_path / "derived.sqlite",
        scored_package,
        Path("configs/cleaning-model-reliability-study.yaml"),
        tmp_path / "wave-root",
        normalization_config=object(),  # 依赖已被合成人口替代。
        expected_scored_manifest_sha256=manifest_sha256,
    )

    package = tmp_path / "wave-root" / result.wave_id
    with (package / "wave-a-tourism-relevance-annotation.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    private = json.loads((package / "private-map.json").read_text(encoding="utf-8"))

    assert reader.fieldnames == [
        "task_id",
        "sample_run_id",
        "normalized_model_text",
        "tourism_label",
    ]
    assert (package / "wave-a-tourism-relevance-annotation.csv").read_bytes().startswith(
        codecs.BOM_UTF8
    )
    assert len(rows) == 240
    assert all(row["tourism_label"] == "" for row in rows)
    assert {row["sample_run_id"] for row in rows} == {result.wave_id}
    assert all("probability" not in key for key in reader.fieldnames)
    assert len(private["records"]) == 240
    assert private["labels_entered_fit"] is False
    assert all(record["analysis_weight"] > 0 for record in private["records"])
    assert all(record["platform_key"] == "synthetic" for record in private["records"])
    assert set(private["hidden_from_annotation"]) == {
        "source_post_id",
        "source_version",
        "platform_key",
        "model_name",
        "model_probability",
        "sampling_stratum",
        "selection_reason",
        "inclusion_probability",
        "analysis_weight",
    }
    assert result.labels_entered_fit is False

    mixed_batch = tmp_path / "mixed-batch.csv"
    with mixed_batch.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=reader.fieldnames)
        writer.writeheader()
        for index, row in enumerate(rows):
            row["tourism_label"] = "related"
            if index == 0:
                row["sample_run_id"] = "wrong-wave"
            writer.writerow(row)
    with pytest.raises(artifacts.ModelReliabilityArtifactError) as error:
        artifacts._load_completed_wave_a_labels(mixed_batch, private)
    assert error.value.reason_code == "model_reliability_completed_label_invalid"


def test_evaluate_wave_a_seals_labels_without_model_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """初标评价只能进入探索完成态，不能直接选择模型或冻结阈值。"""

    scored = tuple(
        replace(
            _scored(index),
            normalized_sha256=hashlib.sha256(
                f"合成文本 {index}".encode("utf-8")
            ).hexdigest(),
        )
        for index in range(600)
    )
    plan = replace(
        PLAN,
        expected_eligible_count=600,
        expected_eligible_component_count=300,
    )
    members = tuple(
        EligibleEvaluationMember(
            source_post_id=item.source_post_id,
            source_version=item.source_version,
            component_id=item.component_id,
            platform_key="synthetic",
            normalized_model_text=f"合成文本 {index}",
            normalized_sha256=item.normalized_sha256,
        )
        for index, item in enumerate(scored)
    )
    population = EligiblePopulation(
        members=members,
        candidate_count=600,
        candidate_component_count=300,
        reference_count=0,
        reference_component_count=0,
        excluded_count=0,
        eligible_component_count=300,
        projection_member_sha256="a" * 64,
        leakage_output_sha256="b" * 64,
    )
    scored_package = tmp_path / "scored"
    scored_package.mkdir()
    scored_manifest = scored_package / "frame-manifest.json"
    scored_manifest.write_text(json.dumps({"frame_id": "frame"}), encoding="utf-8")
    scored_manifest_sha256 = hashlib.sha256(scored_manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(artifacts, "load_model_reliability_plan", lambda _path: plan)
    monkeypatch.setattr(artifacts, "_load_scored_members", lambda *args, **kwargs: scored)
    monkeypatch.setattr(
        artifacts,
        "load_eligible_evaluation_population",
        lambda *args, **kwargs: population,
    )
    wave = artifacts.prepare_wave_a_package(
        tmp_path / "reference.csv",
        tmp_path / "derived.sqlite",
        scored_package,
        Path("configs/cleaning-model-reliability-study.yaml"),
        tmp_path / "wave-root",
        normalization_config=object(),
        expected_scored_manifest_sha256=scored_manifest_sha256,
    )
    wave_package = tmp_path / "wave-root" / wave.wave_id
    completed = tmp_path / "completed.csv"
    with (wave_package / "wave-a-tourism-relevance-annotation.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source, completed.open("w", encoding="utf-8-sig", newline="") as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
        writer.writeheader()
        for index, row in enumerate(reader):
            row["tourism_label"] = "related" if index % 2 == 0 else "unrelated"
            writer.writerow(row)

    result = artifacts.evaluate_wave_a_package(
        completed,
        wave_package,
        scored_package,
        Path("configs/cleaning-model-reliability-study.yaml"),
        tmp_path / "evaluation-root",
        expected_wave_manifest_sha256=wave.package_manifest_sha256,
        expected_scored_manifest_sha256=scored_manifest_sha256,
    )

    package = tmp_path / "evaluation-root" / result.evaluation_id
    report = json.loads((package / "evaluation-report.json").read_text(encoding="utf-8"))
    assert result.status == "WAVE_A_EXPLORATORY_COMPLETE"
    assert result.labels_entered_fit is False
    assert result.may_select_model is False
    assert report["may_freeze_threshold"] is False
    assert report["test_status"] == "locked_not_opened"
