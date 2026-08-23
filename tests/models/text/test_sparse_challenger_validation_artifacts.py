"""一次性 challenger 验证 artifact 与复用边界测试。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import tourism_ugc_study.models.text.sparse_challenger_validation_artifacts as artifacts
from tourism_ugc_study.models.text.model_acceptance import (
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.sparse_challenger_config import (
    load_sparse_challenger_plan,
)
from tourism_ugc_study.models.text.sparse_challenger_validation import (
    ChallengerValidationDocument,
)
from tourism_ugc_study.models.text.sparse_challenger_validation_artifacts import (
    SparseChallengerValidationArtifactError,
    SparseChallengerValidationEvidence,
    evaluate_sparse_challenger_validation_package,
    render_sparse_challenger_validation_result,
)


ROOT = Path(__file__).resolve().parents[3]


class _PredictOnce:
    """记录验证包是否重复调用预测的冻结候选替身。"""

    def __init__(self) -> None:
        self.calls = 0

    def predict_p_unrelated(self, texts):
        self.calls += 1
        return np.asarray(
            [0.10 if index < len(texts) / 2 else 0.90 for index in range(len(texts))]
        )


def _documents() -> tuple[ChallengerValidationDocument, ...]:
    """构造20条平衡验证输入及 baseline 既有概率。"""

    return tuple(
        ChallengerValidationDocument(
            member_key=f"member-{index:02d}",
            component_id=f"component-{index:02d}",
            normalized_model_text=f"冻结验证文本{index}",
            tourism_label="related" if index < 10 else "unrelated",
            baseline_p_unrelated=(
                0.60 if index == 0 else (0.20 if index < 10 else 0.80)
            ),
        )
        for index in range(20)
    )


def test_validation_package_predicts_once_then_reuses_without_prediction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = load_sparse_challenger_plan(
        ROOT / "configs/cleaning-text-challenger.yaml"
    )
    policy = load_model_acceptance_policy(
        ROOT / "configs/cleaning-model-acceptance.yaml"
    )
    predictor = _PredictOnce()
    evidence = SparseChallengerValidationEvidence(
        documents=_documents(),
        frozen_candidate=predictor,
        candidate_model_id="candidate-1",
        challenger_run_id="run-1",
        challenger_manifest_sha256="a" * 64,
        baseline_manifest_sha256="b" * 64,
        reference_manifest_sha256="c" * 64,
    )
    monkeypatch.setattr(artifacts, "load_sparse_challenger_plan", lambda _path: plan)
    monkeypatch.setattr(
        artifacts, "load_model_acceptance_policy", lambda _path: policy
    )
    monkeypatch.setattr(
        artifacts,
        "load_sparse_challenger_validation_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    arguments = (
        "reference.csv",
        "reference.json",
        "derived.sqlite",
        "baseline-package",
        "challenger-package",
        "plan.yaml",
        "policy.yaml",
        tmp_path / "validation",
    )
    first = evaluate_sparse_challenger_validation_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
    )
    second = evaluate_sparse_challenger_validation_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
    )

    assert predictor.calls == 1
    assert first.reused is False
    assert second.reused is True
    assert second.validation_id == first.validation_id
    assert second.manifest_sha256 == first.manifest_sha256
    assert first.direction_status == "directionally_consistent"
    assert first.test_status == "locked_not_opened"
    assert first.threshold_status == "UNSET"
    package = tmp_path / "validation" / first.validation_id
    assert (package / "validation-manifest.json").is_file()
    assert (package / "validation-probabilities.json").is_file()
    assert (package / "validation-report.json").is_file()

    human = render_sparse_challenger_validation_result(first)
    assert "directional_check_only" in human
    assert "related→unrelated" in human
    assert "未对20条另设显著性或验收门" in human
    assert "locked_not_opened" in human


def test_validation_package_rejects_tampered_probability_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = load_sparse_challenger_plan(
        ROOT / "configs/cleaning-text-challenger.yaml"
    )
    policy = load_model_acceptance_policy(
        ROOT / "configs/cleaning-model-acceptance.yaml"
    )
    evidence = SparseChallengerValidationEvidence(
        documents=_documents(),
        frozen_candidate=_PredictOnce(),
        candidate_model_id="candidate-1",
        challenger_run_id="run-1",
        challenger_manifest_sha256="a" * 64,
        baseline_manifest_sha256="b" * 64,
        reference_manifest_sha256="c" * 64,
    )
    monkeypatch.setattr(artifacts, "load_sparse_challenger_plan", lambda _path: plan)
    monkeypatch.setattr(
        artifacts, "load_model_acceptance_policy", lambda _path: policy
    )
    monkeypatch.setattr(
        artifacts,
        "load_sparse_challenger_validation_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    arguments = (
        "reference.csv",
        "reference.json",
        "derived.sqlite",
        "baseline-package",
        "challenger-package",
        "plan.yaml",
        "policy.yaml",
        tmp_path / "validation",
    )
    result = evaluate_sparse_challenger_validation_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="d" * 40,
    )
    path = (
        tmp_path
        / "validation"
        / result.validation_id
        / "validation-probabilities.json"
    )
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(SparseChallengerValidationArtifactError) as error:
        evaluate_sparse_challenger_validation_package(
            *arguments,
            config=object(),
            normalization_config=object(),
            code_version="d" * 40,
        )

    assert error.value.reason_code == "sparse_validation_artifact_hash_mismatch"
