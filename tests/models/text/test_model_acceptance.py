"""UGC 安全优先模型验收策略、配对统计与 artifact 边界测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_acceptance import (
    ModelAcceptanceError,
    PairedOofObservation,
    evaluate_model_acceptance,
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.model_acceptance_artifacts import (
    evaluate_model_acceptance_artifact,
)


ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "configs/cleaning-model-acceptance.yaml"


def _observations(*, unsafe_candidate: bool = False) -> list[PairedOofObservation]:
    """生成双类、逐 component 配对且结果稳定的模拟 OOF。"""

    records: list[PairedOofObservation] = []
    for index in range(100):
        related = index < 50
        candidate_probability = 0.10 if related else 0.90
        if unsafe_candidate and related and index < 10:
            candidate_probability = 0.80
        records.append(
            PairedOofObservation(
                member_key=f"member-{index}",
                component_id=f"component-{index}",
                tourism_label="related" if related else "unrelated",
                baseline_p_unrelated=0.40 if related else 0.60,
                candidate_p_unrelated=candidate_probability,
            )
        )
    return records


def _fast_policy():
    """保留正式门但降低单元测试 bootstrap 重复次数。"""

    return replace(
        load_model_acceptance_policy(POLICY_PATH),
        bootstrap_repetitions=300,
    )


def _evidence(records: list[PairedOofObservation]) -> dict[str, object]:
    """构造符合公开 artifact 契约的配对证据。"""

    policy = load_model_acceptance_policy(POLICY_PATH)
    return {
        "artifact_kind": "formal-cleaning-paired-nested-oof-comparison",
        "baseline_model_id": policy.baseline_model_id,
        "candidate_model_id": "challenger-1",
        "reference_csv_sha256": policy.reference_csv_sha256,
        "train_manifest_sha256": policy.train_manifest_sha256,
        "scope": "train_nested_group_oof",
        "paired_outer_folds": True,
        "test_members_read": False,
        "test_probabilities_present": False,
        "platform_used": False,
        "records": [
            {
                "member_key": record.member_key,
                "component_id": record.component_id,
                "tourism_label": record.tourism_label,
                "baseline_p_unrelated": record.baseline_p_unrelated,
                "candidate_p_unrelated": record.candidate_p_unrelated,
            }
            for record in records
        ],
    }


def test_frozen_policy_binds_current_baseline_and_separates_thresholds() -> None:
    policy = load_model_acceptance_policy(POLICY_PATH)

    assert policy.baseline_model_id == "9cd30922aabf7fb2e2ba42e5a0396cfd"
    assert policy.confidence_level == 0.90
    assert policy.bootstrap_repetitions == 5000
    assert policy.safety_maximum_point_delta == 0.0
    assert policy.safety_maximum_upper_delta == 0.02


def test_policy_rejects_relaxed_safety_or_unknown_fields(tmp_path: Path) -> None:
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    raw["gates"]["safety"]["maximum_point_delta"] = 0.01
    raw["routing_threshold"] = 0.80
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ModelAcceptanceError) as error:
        load_model_acceptance_policy(path)
    assert error.value.reason_code == "model_acceptance_policy_invalid"


def test_safe_candidate_passes_all_development_gates() -> None:
    report = evaluate_model_acceptance(
        _observations(),
        _fast_policy(),
        candidate_model_id="challenger-1",
    )

    assert report["status"] == "passed"
    assert report["all_gates_passed"] is True
    assert all(report["gates"].values())
    assert report["test_members_read"] is False
    assert report["diagnostic_cutoff_is_routing_threshold"] is False


def test_accuracy_like_improvement_cannot_override_ugc_safety_failure() -> None:
    report = evaluate_model_acceptance(
        _observations(unsafe_candidate=True),
        _fast_policy(),
        candidate_model_id="challenger-unsafe",
    )

    assert report["status"] == "failed_retain_baseline"
    assert report["gates"]["safety_point_noninferiority"] is False
    assert report["all_gates_passed"] is False
    assert report["failure_behavior"] == "retain_baseline"


def test_duplicate_members_and_incomplete_classes_fail_closed() -> None:
    records = _observations()[:2]
    duplicate = [records[0], replace(records[1], member_key=records[0].member_key)]
    with pytest.raises(ModelAcceptanceError) as duplicate_error:
        evaluate_model_acceptance(
            duplicate,
            _fast_policy(),
            candidate_model_id="challenger-1",
        )
    assert duplicate_error.value.reason_code == "model_acceptance_observation_invalid"

    related_only = _observations()[:20]
    with pytest.raises(ModelAcceptanceError) as class_error:
        evaluate_model_acceptance(
            related_only,
            _fast_policy(),
            candidate_model_id="challenger-1",
        )
    assert class_error.value.reason_code == "model_acceptance_classes_incomplete"


def test_artifact_rejects_test_access_and_writes_aggregate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence(_observations())
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    output = tmp_path / "report.json"

    import tourism_ugc_study.models.text.model_acceptance_artifacts as artifacts

    original_loader = artifacts.load_model_acceptance_policy
    monkeypatch.setattr(
        artifacts,
        "load_model_acceptance_policy",
        lambda path: replace(original_loader(path), bootstrap_repetitions=300),
    )
    report = evaluate_model_acceptance_artifact(POLICY_PATH, evidence_path, output)

    assert output.is_file()
    assert report["observation_count"] == 100
    assert "records" not in report

    evidence["test_probabilities_present"] = True
    leaked_path = tmp_path / "leaked.json"
    leaked_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ModelAcceptanceError) as error:
        evaluate_model_acceptance_artifact(
            POLICY_PATH,
            leaked_path,
            tmp_path / "leaked-report.json",
        )
    assert error.value.reason_code == "model_acceptance_evidence_lineage_invalid"
