"""1,300条重训快照与不可变包测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tourism_ugc_study.models.text.model_retraining_config import (
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_snapshot import (
    RetrainingDocument,
    RetrainingSnapshot,
)
from tourism_ugc_study.models.text.model_retraining_snapshot_artifacts import (
    ModelRetrainingSnapshotArtifactError,
    freeze_retraining_snapshot_package,
)


def _snapshot() -> RetrainingSnapshot:
    """构造不含真实UGC的完整规模合成快照。"""

    documents: list[RetrainingDocument] = []
    for index in range(1300):
        label = "related" if index < 585 else "unrelated"
        origin = (
            "final_reference"
            if index < 700
            else "wave_a" if index < 940 else "wave_b"
        )
        text = f"合成测试文本{index}"
        documents.append(
            RetrainingDocument(
                member_key=f"member-{index:04d}",
                source_post_id=index + 1,
                source_version=1,
                component_id=f"component-{index:04d}",
                normalized_model_text=text,
                normalized_sha256=hashlib.sha256(text.encode()).hexdigest(),
                tourism_label=label,
                evidence_origin=origin,
                inclusion_probability=(None if index < 700 else 0.5),
                analysis_weight=(None if index < 700 else 2.0),
                historical_test_consumed=index < 148,
            )
        )
    return RetrainingSnapshot(
        documents=tuple(documents),
        count=1300,
        related_count=585,
        unrelated_count=715,
        component_count=1300,
        origin_counts={"final_reference": 700, "wave_a": 240, "wave_b": 360},
        historical_test_consumed_count=148,
        leakage_output_sha256="a" * 64,
        member_binding_sha256="b" * 64,
    )


def test_snapshot_package_is_content_addressed_and_strictly_reused(
    tmp_path: Path,
) -> None:
    """相同快照必须依赖外部manifest哈希才能复用。"""

    plan = load_model_retraining_plan("configs/cleaning-model-retraining.yaml")
    first = freeze_retraining_snapshot_package(
        _snapshot(), tmp_path, plan=plan, code_version="c" * 40
    )
    with pytest.raises(ModelRetrainingSnapshotArtifactError) as caught:
        freeze_retraining_snapshot_package(
            _snapshot(), tmp_path, plan=plan, code_version="c" * 40
        )
    assert (
        caught.value.reason_code
        == "model_retraining_snapshot_existing_requires_manifest_hash"
    )
    reused = freeze_retraining_snapshot_package(
        _snapshot(),
        tmp_path,
        plan=plan,
        code_version="c" * 40,
        expected_existing_manifest_sha256=first.manifest_sha256,
    )
    assert reused.reused is True
    assert reused.snapshot_id == first.snapshot_id
    assert reused.count == 1300
    assert reused.historical_test_consumed_count == 148


def test_snapshot_package_rejects_invalid_training_identity(tmp_path: Path) -> None:
    """标签计数漂移不得生成看似有效的新训练包。"""

    plan = load_model_retraining_plan("configs/cleaning-model-retraining.yaml")
    snapshot = _snapshot()
    invalid = RetrainingSnapshot(
        documents=snapshot.documents,
        count=1300,
        related_count=584,
        unrelated_count=716,
        component_count=1300,
        origin_counts=snapshot.origin_counts,
        historical_test_consumed_count=148,
        leakage_output_sha256="a" * 64,
        member_binding_sha256="b" * 64,
    )
    with pytest.raises(ModelRetrainingSnapshotArtifactError) as caught:
        freeze_retraining_snapshot_package(
            invalid, tmp_path, plan=plan, code_version="c" * 40
        )
    assert (
        caught.value.reason_code
        == "model_retraining_snapshot_package_input_invalid"
    )
