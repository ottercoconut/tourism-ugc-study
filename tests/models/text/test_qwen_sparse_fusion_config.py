"""Qwen＋sparse 无泄漏融合冻结配置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_acceptance import (
    load_model_acceptance_policy,
)
from tourism_ugc_study.models.text.qwen_sparse_fusion_config import (
    QwenSparseFusionConfigError,
    load_qwen_sparse_fusion_plan,
)


ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "configs/cleaning-qwen-sparse-fusion.yaml"


def test_fusion_plan_and_acceptance_share_paired_nested_contract() -> None:
    plan = load_qwen_sparse_fusion_plan(PLAN_PATH)
    policy = load_model_acceptance_policy(
        ROOT / "configs/cleaning-qwen-fusion-model-acceptance.yaml"
    )

    assert [item.qwen_weight for item in plan.weights] == [
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
    ]
    assert len({item.weight_id for item in plan.weights}) == 5
    assert plan.outer_folds == 5
    assert plan.inner_folds == 4
    assert policy.policy_sha256 == plan.acceptance_policy_sha256
    assert policy.evidence_scope == "train_nested_group_oof"
    assert policy.paired_outer_folds is True


def test_fusion_plan_rejects_unregistered_weight(tmp_path: Path) -> None:
    raw = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    raw["fusion"]["qwen_weights"].append(0.9)
    changed = tmp_path / "changed.yaml"
    changed.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(QwenSparseFusionConfigError) as error:
        load_qwen_sparse_fusion_plan(changed)

    assert error.value.reason_code == "qwen_sparse_fusion_plan_invalid"
