"""批次调度边界测试；全部使用合成回执，不加载模型。"""

import copy
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.research_round_artifacts import write_json
from tourism_ugc_study.cleaning.research_round_execution import batch_command, verify_batch_output
from tourism_ugc_study.cleaning.source_snapshot import file_sha256


def _package(tmp_path):
    """构造可核验的最小原生批次包。"""
    package = tmp_path / "batch-id"
    package.mkdir()
    write_json(package / "input-receipt.json", {"input_file_sha256": "c" * 64, "count": 2})
    manifest = {"status": "NEW_BATCH_SCORED", "code_version": "a" * 40, "count": 2,
                "policy_manifest_sha256": "b" * 64, "snapshot_manifest_sha256": "d" * 64,
                "fit_call_count": 0, "source_database_write_count": 0, "training_member_overlap_count": 0,
                "action_counts": {"auto_keep": 2}, "embedding_cache_hit_count": 1, "embedding_cache_miss_count": 1,
                "artifacts": {"input_receipt": {"filename": "input-receipt.json",
                                              "sha256": file_sha256(package / "input-receipt.json")}}}
    write_json(package / "incremental-inference-manifest.json", manifest)
    return package, manifest, {"count": 2, "sha256": "c" * 64}, {"policy_manifest_sha256": "b" * 64,
                                                               "training_manifest_sha256": "d" * 64}


def test_completed_package_verification_binds_input_and_code(tmp_path):
    package, manifest, batch, cfg = _package(tmp_path)
    result = verify_batch_output(tmp_path, batch, cfg, "a" * 40)
    assert result["count"] == 2
    assert result["manifest_sha256"] == file_sha256(package / "incremental-inference-manifest.json")
    with pytest.raises(ValueError, match="binding_mismatch"):
        verify_batch_output(tmp_path, batch, cfg, "e" * 40)
    with pytest.raises(ValueError, match="input_mismatch"):
        verify_batch_output(tmp_path, dict(batch, sha256="f" * 64), cfg, "a" * 40)


@pytest.mark.parametrize("field,value", [("fit_call_count", 1), ("source_database_write_count", 1),
                                        ("training_member_overlap_count", 1), ("count", 3)])
def test_completed_package_rejects_forbidden_execution(tmp_path, field, value):
    package, manifest, batch, cfg = _package(tmp_path)
    modified = copy.deepcopy(manifest)
    modified[field] = value
    write_json(package / "incremental-inference-manifest.json", modified)
    with pytest.raises(ValueError, match="binding_mismatch"):
        verify_batch_output(tmp_path, batch, cfg, "a" * 40)


def test_completed_package_rejects_changed_artifact(tmp_path):
    package, manifest, batch, cfg = _package(tmp_path)
    write_json(package / "input-receipt.json", {"count": 8})
    with pytest.raises(ValueError, match="hash_mismatch"):
        verify_batch_output(tmp_path, batch, cfg, "a" * 40)


def test_command_only_uses_production_predict_entry_and_absolute_private_paths():
    cfg = {"training_package": "results/train", "training_manifest_sha256": "a" * 64,
           "policy_package": "results/policy", "policy_manifest_sha256": "b" * 64,
           "model_directory": "../models/model"}
    command = batch_command(Path("/fixed/code"), Path("/private/study"), Path("/private/round"),
                            {"filename": "inference-batches/batch-001.csv"}, cfg)
    assert command[2] == "/fixed/code/scripts/cleaning_predict_tourism_relevance.py"
    assert "--execute-prediction" in command
    assert "--train" not in command
    assert command[command.index("--input-csv") + 1] == "/private/round/inference-batches/batch-001.csv"
