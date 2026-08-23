"""sparse challenger 不可变运行包与可读输出测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import tourism_ugc_study.models.text.sparse_challenger_artifacts as artifacts
from tourism_ugc_study.models.text.model_acceptance import (
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.sparse_challenger import ChallengerDocument
from tourism_ugc_study.models.text.sparse_challenger_artifacts import (
    SparseChallengerArtifactError,
    SparseChallengerEvidenceBundle,
    render_sparse_challenger_result,
    train_sparse_challenger_package,
)
from tourism_ugc_study.models.text.sparse_challenger_config import (
    load_sparse_challenger_plan,
)


ROOT = Path(__file__).resolve().parents[3]


def _documents() -> tuple[ChallengerDocument, ...]:
    """构造足够完成三折外层比较的训练侧证据。"""

    return tuple(
        ChallengerDocument(
            member_key=f"member-{index:03d}",
            component_id=f"component-{index:03d}",
            normalized_model_text=(
                f"招聘推广商家套餐联系方式{index}"
                if index % 2
                else f"游客青岛海边景点路线体验{index}"
            ),
            tourism_label="unrelated" if index % 2 else "related",
        )
        for index in range(60)
    )


def _small_plan():
    """保留各模型族一个候选，避免持久化测试重复完整网格。"""

    plan = load_sparse_challenger_plan(
        ROOT / "configs/cleaning-text-challenger.yaml"
    )
    candidates = tuple(
        next(candidate for candidate in plan.candidates if candidate.family == family)
        for family in (
            "tfidf_linear_svc",
            "nbsvm",
            "tfidf_logistic_regression",
        )
    )
    return replace(plan, outer_folds=3, inner_folds=2, candidates=candidates)


def test_training_package_is_immutable_reusable_and_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _small_plan()
    policy = replace(
        load_model_acceptance_policy(
            ROOT / "configs/cleaning-model-acceptance.yaml"
        ),
        bootstrap_repetitions=100,
    )
    evidence = SparseChallengerEvidenceBundle(
        documents=_documents(),
        reference_manifest_sha256="b" * 64,
        baseline_split_manifest_sha256="c" * 64,
        baseline_package_manifest_sha256="d" * 64,
        train_count=60,
        validation_count=10,
        test_count=10,
    )
    monkeypatch.setattr(artifacts, "load_sparse_challenger_plan", lambda _path: plan)
    monkeypatch.setattr(
        artifacts, "load_model_acceptance_policy", lambda _path: policy
    )
    monkeypatch.setattr(
        artifacts,
        "load_sparse_challenger_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    arguments = (
        "reference.csv",
        "reference.json",
        "derived.sqlite",
        "baseline-package",
        "plan.yaml",
        "policy.yaml",
        tmp_path / "challengers",
    )
    first = train_sparse_challenger_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="a" * 40,
    )
    second = train_sparse_challenger_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="a" * 40,
    )

    assert first.status == "frozen"
    assert first.reused is False
    assert second.reused is True
    assert second.run_id == first.run_id
    assert second.package_manifest_sha256 == first.package_manifest_sha256
    assert first.train_count == 60
    assert first.test_status == "locked_not_opened"
    assert first.threshold_status == "UNSET"
    assert first.validation_status in {"pending_directional_check", "not_allowed"}
    package = tmp_path / "challengers" / first.run_id
    assert (package / "selected-model.joblib").is_file()
    assert (package / "paired-outer-oof.json").is_file()
    assert (package / "full-training-scores.json").is_file()
    assert (package / "acceptance-report.json").is_file()

    human = render_sparse_challenger_result(first)
    assert "UGC 安全优先验收" in human
    assert "related→unrelated" in human
    assert "锁定测试：locked_not_opened" in human
    assert "自动清洗决定：未生成" in human


def test_existing_package_rejects_artifact_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _small_plan()
    policy = replace(
        load_model_acceptance_policy(
            ROOT / "configs/cleaning-model-acceptance.yaml"
        ),
        bootstrap_repetitions=100,
    )
    evidence = SparseChallengerEvidenceBundle(
        documents=_documents(),
        reference_manifest_sha256="b" * 64,
        baseline_split_manifest_sha256="c" * 64,
        baseline_package_manifest_sha256="d" * 64,
        train_count=60,
        validation_count=10,
        test_count=10,
    )
    monkeypatch.setattr(artifacts, "load_sparse_challenger_plan", lambda _path: plan)
    monkeypatch.setattr(
        artifacts, "load_model_acceptance_policy", lambda _path: policy
    )
    monkeypatch.setattr(
        artifacts,
        "load_sparse_challenger_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    arguments = (
        "reference.csv",
        "reference.json",
        "derived.sqlite",
        "baseline-package",
        "plan.yaml",
        "policy.yaml",
        tmp_path / "challengers",
    )
    result = train_sparse_challenger_package(
        *arguments,
        config=object(),
        normalization_config=object(),
        code_version="a" * 40,
    )
    model_path = (
        tmp_path / "challengers" / result.run_id / "selected-model.joblib"
    )
    model_path.write_bytes(model_path.read_bytes() + b"tampered")

    with pytest.raises(SparseChallengerArtifactError) as error:
        train_sparse_challenger_package(
            *arguments,
            config=object(),
            normalization_config=object(),
            code_version="a" * 40,
        )

    assert (
        error.value.reason_code
        == "sparse_challenger_package_artifact_hash_mismatch"
    )

