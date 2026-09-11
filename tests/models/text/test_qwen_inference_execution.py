"""CUDA迁移只改运行设备，冻结算法与缓存隔离的合成验证。"""

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tourism_ugc_study.cleaning.source_snapshot import file_sha256
from tourism_ugc_study.models.text.model_retraining_config import load_model_retraining_plan
from tourism_ugc_study.models.text.qwen_complete_chunk_runtime import LocalQwenCompleteChunkEncoder
from tourism_ugc_study.models.text.qwen_embedding_cache import QwenEmbeddingCache
from tourism_ugc_study.models.text.qwen_embedding_config import load_qwen_embedding_plan
from tourism_ugc_study.models.text.qwen_embedding_runtime import QwenModelSnapshot
from tourism_ugc_study.models.text.qwen_inference_execution import (
    QwenInferenceExecutionError, load_inference_execution,
)


ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "configs/cleaning-inference-cuda.yaml"


def test_explicit_profile_is_hash_bound_and_rejects_algorithm_overrides(tmp_path):
    """运行文件须有外部摘要；禁止阈值、窗口或dtype变更。"""
    profile = load_inference_execution(CONFIG, file_sha256(CONFIG))
    assert profile.device == "cuda:0" and profile.batch_size == 8
    with pytest.raises(QwenInferenceExecutionError, match="hash_mismatch"):
        load_inference_execution(CONFIG, "a" * 64)
    for extra in ({"T_keep": 0.5}, {"max_length": 512}, {"parameter_dtype": "float16"},
                  {"batch_size": True}, {"batch_size": 0}, {"device": "cpu"}):
        data = yaml.safe_load(CONFIG.read_text()) | extra
        path = tmp_path / "invalid.yaml"
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(QwenInferenceExecutionError, match="config_invalid"):
            load_inference_execution(path, file_sha256(path))


def test_runtime_override_preserves_frozen_plans_and_model_parameters(monkeypatch):
    """迁移不改训练/编码计划身份、指令、窗口或聚合配置。"""
    plan = load_model_retraining_plan(ROOT / "configs/cleaning-model-retraining.yaml")
    base = load_qwen_embedding_plan(ROOT / "configs/cleaning-qwen-embedding-baseline.yaml")
    before = asdict(plan), asdict(base)
    monkeypatch.setattr("tourism_ugc_study.models.text.qwen_embedding_runtime.validate_qwen_model_directory",
                        lambda *args, **kwargs: None)
    profile = load_inference_execution(CONFIG, file_sha256(CONFIG))
    encoder = LocalQwenCompleteChunkEncoder("/synthetic/model", base_plan=base,
                                           retraining_plan=plan, inference_execution=profile)
    assert (asdict(plan), asdict(base)) == before
    assert encoder._requested_device == "cuda:0" and encoder._batch_size == 8
    assert encoder._plan.encoder.instruction == plan.qwen_instruction
    assert encoder._plan.encoder.max_length == 2048
    assert encoder._plan.execution.parameter_dtype == "bfloat16"
    assert encoder.inference_execution_identity["configuration_sha256"] == file_sha256(CONFIG)


def test_runtime_device_or_software_change_cannot_reuse_old_cache(tmp_path):
    """显式运行配置及依赖版本都改变命名空间，不能读取历史MPS向量。"""
    plan = load_model_retraining_plan(ROOT / "configs/cleaning-model-retraining.yaml")
    snapshot = QwenModelSnapshot(plan.qwen_repository, plan.qwen_revision,
                                "a" * 64, "b" * 64, 2560, 2048, True)
    profile = load_inference_execution(CONFIG, file_sha256(CONFIG))
    identities = [None, profile.identity(), replace(profile, device="mps").identity(),
                  profile.identity() | {"software_versions": {"torch": "different"}}]
    ids = {QwenEmbeddingCache(tmp_path, plan=plan, model_snapshot=snapshot,
            encoder=SimpleNamespace(inference_execution_identity=identity)).namespace_id
           for identity in identities}
    assert len(ids) == len(identities)
