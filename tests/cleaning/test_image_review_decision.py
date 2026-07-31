"""图片决定规则与默认保留语义测试。"""

from __future__ import annotations

import pytest

from tourism_ugc_study.cleaning.image_review_decision import (
    DecisionEvidence,
    resolve_image_decision,
)


def _annotation(identity: str, slot: int, label: str) -> DecisionEvidence:
    return DecisionEvidence(identity, "annotation", label, slot)


def test_default_keep_has_no_human_label() -> None:
    decision = resolve_image_decision((), is_candidate=False)
    assert decision.decision_action == "keep"
    assert decision.technical_noise_label is None
    assert decision.provenance == "default_keep_no_candidate"


def test_candidate_and_proposed_exclusion_require_complete_human_chain() -> None:
    with pytest.raises(ValueError):
        resolve_image_decision((), is_candidate=True)
    with pytest.raises(ValueError):
        resolve_image_decision((_annotation("a", 1, "site_ui"),), is_candidate=True)
    with pytest.raises(ValueError):
        resolve_image_decision(
            (_annotation("a", 1, "site_ui"), _annotation("b", 2, "site_background")),
            is_candidate=True,
        )


def test_double_agreement_can_exclude_but_uncertain_requires_adjudication() -> None:
    excluded = resolve_image_decision(
        (_annotation("a", 1, "site_ui"), _annotation("b", 2, "site_ui")),
        is_candidate=True,
    )
    assert excluded.decision_action == "exclude"
    assert excluded.provenance == "double_agreement"
    with pytest.raises(ValueError):
        resolve_image_decision(
            (_annotation("c", 1, "uncertain"), _annotation("d", 2, "uncertain")),
            is_candidate=True,
        )


def test_adjudicated_uncertain_stays_review() -> None:
    decision = resolve_image_decision(
        (
            _annotation("a", 1, "uncertain"),
            _annotation("b", 2, "valid_content"),
            DecisionEvidence("c", "adjudication", "uncertain", None),
        ),
        is_candidate=True,
    )
    assert decision.decision_action == "review"
    assert decision.technical_noise_label == "uncertain"
    assert decision.provenance == "adjudication"
