"""Qwen3-Embedding 语义 baseline 预登记配置的契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_acceptance import (
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingConfigError,
    load_qwen_embedding_plan,
)


ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
POLICY_PATH = ROOT / "configs/cleaning-qwen-model-acceptance.yaml"


def _changed_plan(tmp_path: Path, change) -> Path:
    """复制冻结计划并施加单一漂移。"""

    raw = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "changed.yaml"
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_plan_freezes_one_local_encoder_and_one_linear_head() -> None:
    plan = load_qwen_embedding_plan(PLAN_PATH)
    policy = load_model_acceptance_policy(POLICY_PATH)

    assert plan.plan_sha256 == (
        "ee1bbff554e1abcdfa6797a1b446515833bd8a7c0ff1db5d8143d7a2ac5f6097"
    )
    assert plan.encoder.repository == "Qwen/Qwen3-Embedding-0.6B"
    assert len(plan.encoder.revision) == 40
    assert plan.encoder.embedding_dimension == 1024
    assert plan.encoder.max_length == 2048
    assert plan.encoder.snapshot_sha256 == (
        "302e3ceebabd93cebf4f9b0a4bb42765c4504ff9aa3087720ec23497c3afc8bb"
    )
    assert plan.execution.device == "mps"
    assert plan.classifier.family == "logistic_regression"
    assert plan.classifier.C == 1.0
    assert plan.comparator_model_id == policy.baseline_model_id
    assert plan.acceptance_policy_sha256 == policy.policy_sha256
    assert plan.test_manifest_sha256 == policy.test_manifest_sha256


@pytest.mark.parametrize(
    ("change", "reason_code"),
    [
        (
            lambda raw: raw.update({"platform": "feature"}),
            "qwen_embedding_config_invalid",
        ),
        (
            lambda raw: raw["encoder"].update({"fine_tuning": True}),
            "qwen_embedding_plan_invalid",
        ),
        (
            lambda raw: raw["encoder"].update({"title_body_channels": True}),
            "qwen_embedding_plan_invalid",
        ),
        (
            lambda raw: raw["classifier"].update({"C": 3.0}),
            "qwen_embedding_plan_invalid",
        ),
        (
            lambda raw: raw["evaluation"].update(
                {"diagnostic_cutoff_is_routing_threshold": True}
            ),
            "qwen_embedding_plan_invalid",
        ),
    ],
)
def test_plan_rejects_unregistered_changes(
    tmp_path: Path, change, reason_code: str
) -> None:
    path = _changed_plan(tmp_path, change)

    with pytest.raises(QwenEmbeddingConfigError) as error:
        load_qwen_embedding_plan(path)

    assert error.value.reason_code == reason_code
