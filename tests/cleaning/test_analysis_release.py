"""纯文本分析发布的成员、哈希和输入边界测试。"""

from __future__ import annotations

import pytest

from tourism_ugc_study.cleaning.analysis_dedup import (
    AnalysisDedupMember,
    build_analysis_dedup,
)
from tourism_ugc_study.cleaning.analysis_release import ReleaseRequest, build_analysis_release
from tourism_ugc_study.cleaning.post_decision import PostDecision


def _post(post_id: int, action: str) -> PostDecision:
    reasons = {
        "keep": ("human_confirmed_usable_related",),
        "review": ("required_evidence_missing",),
        "exclude": ("structure_invalid",),
    }
    return PostDecision(
        source_post_id=post_id,
        source_version=1,
        decision=action,  # type: ignore[arg-type]
        reason_codes=reasons[action],
        evidence_ids=(),
        rule_version="post-decision-v1",
        decision_sha256=f"{post_id:x}".rjust(64, "0"),
    )


def _dedup(post_ids: tuple[int, ...], shared: tuple[int, ...] = ()):
    members = [
        AnalysisDedupMember(
            post_id,
            1,
            "a" * 64 if post_id in shared else f"{post_id:x}".rjust(64, "b"),
        )
        for post_id in post_ids
    ]
    return build_analysis_dedup(members, (), rule_version="analysis-dedup-v1")


def test_release_contains_only_keep_posts_and_one_member_per_cluster() -> None:
    result = build_analysis_release(
        ReleaseRequest("release-1", "run-1", "formal"),
        (_post(1, "keep"), _post(2, "keep"), _post(3, "exclude")),
        _dedup((1, 2, 3), shared=(1, 2)),
    )

    assert [item.identity for item in result.analysis_posts_eligible] == [(1, 1), (2, 1)]
    assert [item.identity for item in result.analysis_posts_deduplicated] == [(1, 1)]
    assert dict(result.quality_report.output_member_counts) == {
        "analysis_posts_deduplicated": 1,
        "analysis_posts_eligible": 2,
    }


def test_input_order_does_not_change_release_hashes() -> None:
    first = build_analysis_release(
        ReleaseRequest("release-1", "run-1", "smoke"),
        (_post(1, "keep"), _post(2, "exclude")),
        _dedup((1, 2)),
    )
    second = build_analysis_release(
        ReleaseRequest("release-1", "run-1", "smoke"),
        (_post(2, "exclude"), _post(1, "keep")),
        _dedup((2, 1)),
    )

    assert second.manifest_sha256 == first.manifest_sha256
    assert second.quality_report.report_sha256 == first.quality_report.report_sha256


def test_release_rejects_mutable_alias_and_incomplete_dedup_partition() -> None:
    with pytest.raises(ValueError, match="latest"):
        build_analysis_release(
            ReleaseRequest("latest", "run-1", "smoke"),
            (_post(1, "keep"),),
            _dedup((1,)),
        )

    with pytest.raises(ValueError, match="partition"):
        build_analysis_release(
            ReleaseRequest("release-1", "run-1", "smoke"),
            (_post(1, "keep"), _post(2, "keep")),
            _dedup((1,)),
        )
