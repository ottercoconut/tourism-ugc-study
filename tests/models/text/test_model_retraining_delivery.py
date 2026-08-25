"""研究者阈值冻结后的纯路由、人工表与最终交付测试。"""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.models.text.model_retraining_delivery import (
    ModelRetrainingDeliveryError,
    build_delivery_decisions,
    build_manual_review_members,
    reroute_scored_members,
    route_probability,
)
from tourism_ugc_study.models.text.model_retraining_delivery_artifacts import (
    _manual_csv_bytes,
)
from tourism_ugc_study.models.text.model_retraining_delivery_config import (
    ModelRetrainingDeliveryConfigError,
    load_model_retraining_delivery_plan,
)
from tourism_ugc_study.models.text.model_retraining_inference import (
    ScoredRoutingMember,
)
from tourism_ugc_study.models.text.model_retraining_snapshot import (
    RetrainingDocument,
)


ROOT = Path(__file__).resolve().parents[3]
DELIVERY_CONFIG = ROOT / "configs" / "cleaning-model-retraining-delivery.yaml"


def _scored(index: int, probability: float, action: str) -> ScoredRoutingMember:
    """构造不含真实UGC的确定性评分记录。"""

    import hashlib

    text = f"synthetic-{index}"
    return ScoredRoutingMember(
        member_key=f"member-{index}",
        source_post_id=1000 + index,
        source_version=1,
        component_id=f"component-{index}",
        normalized_model_text=text,
        normalized_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        p_unrelated=probability,
        provisional_action=action,
    )


def _document(index: int, label: str) -> RetrainingDocument:
    """构造不含真实UGC的人工训练记录。"""

    import hashlib

    text = f"training-{index}"
    return RetrainingDocument(
        member_key=f"training-member-{index}",
        source_post_id=2000 + index,
        source_version=1,
        component_id=f"training-component-{index}",
        normalized_model_text=text,
        normalized_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        tourism_label=label,
        evidence_origin="final_reference",
        inclusion_probability=None,
        analysis_weight=None,
        historical_test_consumed=False,
    )


def test_delivery_config_freezes_researcher_selection_and_counts() -> None:
    """公开配置必须固定0.31/0.96、旧审计边界和交付计数。"""

    plan = load_model_retraining_delivery_plan(DELIVERY_CONFIG)
    assert plan.selection.T_keep == 0.31
    assert plan.selection.T_exclude == 0.96
    assert plan.selection.acceptance_basis == "researcher_accepted_post_hoc_risk"
    assert plan.selection.independent_release_evidence is False
    assert plan.selection.historical_audit_status == "ONE_TAIL_RELEASED"
    assert plan.expected_manual_task_count == 2286
    assert plan.expected_action_counts == {
        "keep": 6835,
        "exclude": 4737,
        "manual_review": 2286,
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("selection", "T_keep"), 0.32),
        (("selection", "historical_audit_outcome_preserved"), False),
        (("guards", "predict_call_count"), 1),
    ],
)
def test_delivery_config_rejects_protocol_drift(
    tmp_path: Path, path: tuple[str, str], value: object
) -> None:
    """阈值、历史结论和纯重分流保护不得静默放宽。"""

    payload = yaml.safe_load(DELIVERY_CONFIG.read_text(encoding="utf-8"))
    payload[path[0]][path[1]] = value
    changed = tmp_path / "changed.yaml"
    changed.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ModelRetrainingDeliveryConfigError):
        load_model_retraining_delivery_plan(changed)


def test_route_probability_uses_closed_automatic_tails() -> None:
    """两个阈值点本身应分别进入自动保留和自动排除。"""

    assert route_probability(0.31, T_keep=0.31, T_exclude=0.96) == "auto_keep"
    assert route_probability(0.310001, T_keep=0.31, T_exclude=0.96) == "manual_review"
    assert route_probability(0.959999, T_keep=0.31, T_exclude=0.96) == "manual_review"
    assert route_probability(0.96, T_keep=0.31, T_exclude=0.96) == "auto_exclude"
    with pytest.raises(ModelRetrainingDeliveryError):
        route_probability(float("nan"), T_keep=0.31, T_exclude=0.96)


def test_reroute_preserves_probability_and_identity() -> None:
    """重分流只能修改动作，不能改动模型输出或私有身份。"""

    original = (
        _scored(1, 0.10, "manual_review"),
        _scored(2, 0.50, "auto_keep"),
        _scored(3, 0.99, "manual_review"),
    )
    rerouted = reroute_scored_members(original, T_keep=0.31, T_exclude=0.96)
    assert [item.provisional_action for item in rerouted] == [
        "auto_keep",
        "manual_review",
        "auto_exclude",
    ]
    for before, after in zip(original, rerouted, strict=True):
        assert before.identity == after.identity
        assert before.member_key == after.member_key
        assert before.p_unrelated == after.p_unrelated
        assert before.normalized_sha256 == after.normalized_sha256


def test_manual_review_table_is_stable_blind_four_column_csv() -> None:
    """人工表排除已有标签，仅暴露固定四列且使用UTF-8 BOM。"""

    records = (
        _scored(1, 0.50, "manual_review"),
        _scored(2, 0.60, "manual_review"),
        _scored(3, 0.10, "auto_keep"),
    )
    run_id = "a" * 32
    tasks = build_manual_review_members(
        records,
        excluded_human_member_keys={"member-2"},
        sample_run_id=run_id,
    )
    assert len(tasks) == 1
    rows = [
        {
            "task_id": tasks[0].task_id,
            "sample_run_id": tasks[0].sample_run_id,
            "normalized_model_text": tasks[0].member.normalized_model_text,
            "tourism_label": "",
        }
    ]
    payload = _manual_csv_bytes(rows)
    assert payload.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    assert reader.fieldnames == [
        "task_id",
        "sample_run_id",
        "normalized_model_text",
        "tourism_label",
    ]
    assert list(reader)[0]["tourism_label"] == ""
    assert _manual_csv_bytes(rows) == payload


def test_final_delivery_uses_human_override_and_both_tails() -> None:
    """训练与审计人工标签优先，其余两个自动尾部均按冻结策略交付。"""

    base_plan = load_model_retraining_delivery_plan(DELIVERY_CONFIG)
    plan = replace(
        base_plan,
        expected_total_decision_count=6,
        expected_action_counts={"keep": 2, "exclude": 3, "manual_review": 1},
        expected_source_counts={
            "human_training_label": 2,
            "human_audit_label": 1,
            "model_released_auto_keep": 1,
            "model_released_auto_exclude": 1,
            "model_middle_band": 1,
        },
    )
    documents = (_document(1, "related"), _document(2, "unrelated"))
    records = (
        _scored(1, 0.10, "auto_keep"),
        _scored(2, 0.99, "auto_exclude"),
        _scored(3, 0.50, "manual_review"),
        _scored(4, 0.20, "auto_keep"),
    )
    audit = [
        {
            "member_key": "member-1",
            "source_post_id": 1001,
            "source_version": 1,
            "tourism_label": "unrelated",
        }
    ]
    decisions = build_delivery_decisions(
        documents,
        records,
        audit,
        policy_id="b" * 32,
        delivery_plan=plan,
    )
    by_identity = {item.identity: item for item in decisions}
    assert by_identity[(1001, 1)].final_action == "exclude"
    assert by_identity[(1001, 1)].decision_source == "human_audit_label"
    assert by_identity[(1002, 1)].decision_source == "model_released_auto_exclude"
    assert by_identity[(1004, 1)].decision_source == "model_released_auto_keep"
    assert by_identity[(1003, 1)].final_action == "manual_review"
