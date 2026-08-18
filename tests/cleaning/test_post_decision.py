"""帖子最终决定领域规则的单元测试。"""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from tourism_ugc_study.cleaning.post_decision import (
    HumanTextEvidence,
    ModelDecisionEvidence,
    PostDecisionRequest,
    build_post_decisions,
    decide_post,
)


VERSION_SHA = "a" * 64


def _human(
    evidence_id: str,
    tourism: str,
) -> HumanTextEvidence:
    """构造测试用人工旅游相关性证据。"""

    return HumanTextEvidence(evidence_id, tourism)  # type: ignore[arg-type]


def _model(
    action: str,
    **overrides: object,
) -> ModelDecisionEvidence:
    """构造默认通过正式门槛的模型候选。"""

    values: dict[str, object] = {
        "evidence_id": f"prediction-{action}",
        "model_run_id": "formal-model-1",
        "suggested_action": action,
        "run_mode": "formal",
        "seal_status": "finalized",
        "required_version_manifest_sha256": VERSION_SHA,
        "model_version_manifest_sha256": VERSION_SHA,
        "test_set_isolation_passed": True,
        "low_risk_threshold_enabled": True,
        "platform_audit_complete": True,
        "text_keep_audit_passed": True,
    }
    values.update(overrides)
    return ModelDecisionEvidence(**values)  # type: ignore[arg-type]


def _request(
    *,
    human: tuple[HumanTextEvidence, ...] = (),
    model: ModelDecisionEvidence | None = None,
    post_id: int = 1,
    build_kind: str = "final",
) -> PostDecisionRequest:
    """构造固定规则版本的单帖决定请求。"""

    return PostDecisionRequest(
        source_post_id=post_id,
        source_version=2,
        rule_version="post-decision-v1",
        build_kind=build_kind,  # type: ignore[arg-type]
        human_evidence=human,
        model_evidence=model,
    )


@pytest.mark.parametrize(
    ("tourism", "expected", "reason"),
    [
        ("unrelated", "exclude", "human_confirmed_tourism_unrelated"),
        ("related", "keep", "human_confirmed_tourism_related"),
        ("uncertain", "review", "human_evidence_uncertain"),
    ],
)
def test_human_tourism_rules(
    tourism: str,
    expected: str,
    reason: str,
) -> None:
    """人工相关性三个值均按约定映射。"""

    decision = decide_post(
        _request(human=(_human("human-1", tourism),))
    )

    assert decision.decision == expected
    assert decision.reason_codes == (reason,)


def test_human_evidence_has_priority_and_conflict_never_uses_latest() -> None:
    """人工证据覆盖模型，而多条人工冲突只能进入复核。"""

    human_keep = _human("human-keep", "related")
    prioritized = decide_post(
        _request(human=(human_keep,), model=_model("high_risk_review"))
    )
    conflict = decide_post(
        _request(
            human=(
                human_keep,
                _human("human-exclude", "unrelated"),
            )
        )
    )

    assert prioritized.decision == "keep"
    assert prioritized.reason_codes == ("human_confirmed_tourism_related",)
    assert set(prioritized.evidence_ids) == {
        "human-keep",
        "prediction-high_risk_review",
    }
    assert conflict.decision == "review"
    assert conflict.reason_codes == ("human_evidence_conflict",)


def test_missing_evidence_and_review_model_actions_remain_review() -> None:
    """缺证、高风险及中间候选均不得变成模型最终判断。"""

    missing = decide_post(_request())
    high = decide_post(_request(model=_model("high_risk_review")))
    manual = decide_post(_request(model=_model("manual_review")))

    assert missing.decision == "review"
    assert missing.reason_codes == ("required_evidence_missing",)
    assert high.decision == manual.decision == "review"
    assert high.reason_codes == ("high_risk_review",)
    assert manual.reason_codes == ("manual_review",)


def test_low_risk_candidate_requires_every_formal_gate() -> None:
    """只有全门通过的正式低风险候选可保留，任一缺口都回到复核。"""

    passing = decide_post(
        _request(model=_model("low_risk_keep_candidate"))
    )
    assert passing.decision == "keep"
    assert passing.reason_codes == ("formal_low_risk_candidate_gates_passed",)

    failing_cases = [
        ("model_not_formal", {"run_mode": "smoke"}),
        ("model_not_finalized", {"seal_status": "building"}),
        (
            "model_version_mismatch",
            {"model_version_manifest_sha256": "b" * 64},
        ),
        ("model_test_isolation_failed", {"test_set_isolation_passed": False}),
        ("low_risk_threshold_disabled", {"low_risk_threshold_enabled": False}),
        ("platform_audit_incomplete", {"platform_audit_complete": False}),
        ("text_keep_audit_not_passed", {"text_keep_audit_passed": False}),
    ]
    for reason, overrides in failing_cases:
        decision = decide_post(
            _request(model=_model("low_risk_keep_candidate", **overrides))
        )
        assert decision.decision == "review"
        assert reason in decision.reason_codes


def test_candidate_build_freezes_low_risk_population_before_final_audit() -> None:
    """候选构建可冻结待审计低风险项，最终构建仍拒绝未通过审计的项。"""

    model = _model("low_risk_keep_candidate", text_keep_audit_passed=False)
    candidate = decide_post(_request(model=model, build_kind="candidate"))
    final = decide_post(_request(model=model, build_kind="final"))

    assert candidate.decision == "keep"
    assert candidate.reason_codes == (
        "low_risk_keep_candidate_pending_text_audit",
    )
    assert final.decision == "review"
    assert "text_keep_audit_not_passed" in final.reason_codes


def test_decision_hash_is_order_independent_and_covers_rule_and_evidence() -> None:
    """相同证据集合稳定复算，规则版本或证据身份变化会改变哈希。"""

    first_evidence = _human("annotation-1", "related")
    second_evidence = _human("annotation-2", "related")
    forward_request = _request(human=(first_evidence, second_evidence))
    reverse_request = replace(
        forward_request,
        human_evidence=(second_evidence, first_evidence),
    )

    forward = decide_post(forward_request)
    reverse = decide_post(reverse_request)
    changed_rule = decide_post(replace(forward_request, rule_version="post-decision-v2"))
    changed_evidence = decide_post(
        replace(
            forward_request,
            human_evidence=(
                first_evidence,
                replace(second_evidence, evidence_id="annotation-3"),
            ),
        )
    )

    assert forward == reverse
    assert forward.decision_sha256 != changed_rule.decision_sha256
    assert forward.decision_sha256 != changed_evidence.decision_sha256


def test_batch_is_sorted_and_commercial_attribute_does_not_exist() -> None:
    """批量结果确定排序，领域契约中不引入商业清洗属性。"""

    decisions = build_post_decisions(
        [
            _request(post_id=3),
            _request(post_id=1),
            _request(post_id=2),
        ]
    )

    assert [decision.source_post_id for decision in decisions] == [1, 2, 3]
    assert "commercial_label" not in {field.name for field in fields(PostDecisionRequest)}
    with pytest.raises(ValueError, match="duplicate post decision identity"):
        build_post_decisions([_request(post_id=1), _request(post_id=1)])
