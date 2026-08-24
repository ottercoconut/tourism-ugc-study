"""最终判读与双尾审计计划不可变封存测试。"""

from pathlib import Path

import tourism_ugc_study.models.text.model_deployment_acceptance_artifacts as artifacts


CONFIG = Path("configs/cleaning-model-deployment-acceptance.yaml")


def test_freezes_and_strictly_reuses_acceptance_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """封存不应读取测试或生成决定，同身份只允许严格复用。"""

    validated = {
        "policy_manifest_sha256": "a" * 64,
        "wave_b_evaluation_manifest_sha256": "b" * 64,
        "wave_b_completed_csv_sha256": "c" * 64,
        "qwen_training_manifest_sha256": "d" * 64,
        "qwen_model_artifact_sha256": "e" * 64,
        "split_artifact_sha256": "f" * 64,
        "test_manifest_sha256": "0" * 64,
    }
    monkeypatch.setattr(
        artifacts, "_validate_inputs", lambda *args, **kwargs: validated
    )

    first = artifacts.freeze_deployment_acceptance_package(
        CONFIG,
        tmp_path / "policy",
        tmp_path / "wave-b",
        tmp_path / "completed.csv",
        tmp_path / "qwen",
        tmp_path / "split",
        tmp_path / "root",
        code_version="1" * 40,
    )
    package = tmp_path / "root" / first.freeze_id
    assert first.status == "ACCEPTANCE_AND_AUDIT_FROZEN"
    assert first.test_status == "locked_not_opened"
    assert first.audit_status == "FROZEN_NOT_RUN"
    assert first.deployment_status == "NOT_AUTHORIZED"
    assert first.fit_call_count == 0
    assert first.prediction_call_count == 0
    assert first.auto_cleaning_decisions_present is False
    assert package.joinpath("acceptance-manifest.json").is_file()

    reused = artifacts.freeze_deployment_acceptance_package(
        CONFIG,
        tmp_path / "policy",
        tmp_path / "wave-b",
        tmp_path / "completed.csv",
        tmp_path / "qwen",
        tmp_path / "split",
        tmp_path / "root",
        code_version="1" * 40,
        expected_existing_manifest_sha256=first.package_manifest_sha256,
    )
    assert reused.reused is True
    assert reused.freeze_id == first.freeze_id
    assert reused.package_manifest_sha256 == first.package_manifest_sha256
