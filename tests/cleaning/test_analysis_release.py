"""分析发布纯领域投影的集合、守恒和去敏边界测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tourism_ugc_study.cleaning.analysis_dedup import (
    AnalysisDedupMember,
    build_analysis_dedup,
)
from tourism_ugc_study.cleaning.analysis_release import (
    FinalizedImageTechnicalDecision,
    ImageSourceRelation,
    ReleaseRequest,
    build_analysis_release,
)
from tourism_ugc_study.cleaning.post_decision import PostDecision


def _post(post_id: int, decision: str) -> PostDecision:
    """生成不含文本的帖子决定夹具。"""

    reasons = {
        "keep": ("human_confirmed_usable_related",),
        "review": ("required_evidence_missing",),
        "exclude": ("structure_invalid",),
    }
    return PostDecision(
        source_post_id=post_id,
        source_version=1,
        decision=decision,  # type: ignore[arg-type]
        reason_codes=reasons[decision],
        evidence_ids=(),
        rule_version="post-decision-v1",
        decision_sha256=f"{post_id:x}".rjust(64, "0"),
    )


def _dedup(
    post_ids: tuple[int, ...],
    *,
    shared_hash_ids: tuple[int, ...] = (),
):
    """按指定精确同一成员生成真实分析去重构建夹具。"""

    members = []
    for post_id in post_ids:
        exact = "a" * 64 if post_id in shared_hash_ids else f"{post_id:x}".rjust(64, "b")
        members.append(AnalysisDedupMember(post_id, 1, exact))
    return build_analysis_dedup(members, (), rule_version="analysis-dedup-v1")


def _relation(
    image_id: int,
    post_id: int,
    role: str,
) -> ImageSourceRelation:
    """生成冻结图片关系；运行期校验仍会检查错误枚举。"""

    return ImageSourceRelation(image_id, 1, post_id, 1, role)  # type: ignore[arg-type]


def _image_decision(image_id: int, action: str) -> FinalizedImageTechnicalDecision:
    """生成 #10 finalized 技术决定的最小去敏投影。"""

    return FinalizedImageTechnicalDecision(
        image_id,
        1,
        action,  # type: ignore[arg-type]
        "image-decision-build-1",
        f"{image_id:x}".rjust(64, "0"),
    )


def _full_projection():
    """构建覆盖三个角色和四种内容状态的标准发布投影。"""

    posts = (_post(1, "keep"), _post(2, "keep"), _post(3, "exclude"), _post(4, "review"))
    relations = (
        _relation(16, 1, "content"),  # 缺技术决定，必须保持 blocked。
        _relation(10, 1, "author_avatar"),
        _relation(12, 1, "content"),
        _relation(11, 3, "page"),
        _relation(13, 3, "content"),
        _relation(14, 1, "content"),
        _relation(15, 1, "content"),
    )
    image_decisions = (
        _image_decision(15, "exclude"),
        _image_decision(12, "keep"),
        _image_decision(14, "review"),
        _image_decision(13, "keep"),
    )
    return build_analysis_release(
        ReleaseRequest(
            "release-001",
            "run-001",
            "formal",
            {"schema_version": 24, "config_sha256": "c" * 64},
        ),
        posts,
        _dedup((1, 2, 3, 4), shared_hash_ids=(1, 2)),
        relations,
        image_decisions,
    )


def test_four_release_sets_apply_parent_filter_without_mutating_image_action() -> None:
    """四类集合只按规定交集生成，父帖过滤仍保留图片技术 keep。"""

    result = _full_projection()

    assert [member.identity for member in result.analysis_posts_eligible] == [(1, 1), (2, 1)]
    assert [member.identity for member in result.analysis_posts_deduplicated] == [(1, 1)]
    assert [member.image_identity for member in result.analysis_images_eligible] == [(12, 1)]
    assert [member.image_identity for member in result.analysis_images_evidence_only] == [(11, 1)]

    projections = {item.source_image_id: item for item in result.image_projections}
    assert projections[13].content_status == "keep"
    assert projections[13].technical_decision_action == "keep"
    assert projections[13].output_collection is None
    assert projections[13].projection_reason_codes == ("parent_post_not_eligible",)
    assert projections[10].output_collection is None
    assert projections[10].technical_decision_action is None
    assert projections[11].output_collection == "analysis_images_evidence_only"


def test_role_and_content_partitions_conserve_all_image_relations() -> None:
    """三个来源角色及内容图四状态分别构成无遗漏分区。"""

    report = _full_projection().quality_report

    assert dict(report.image_role_counts) == {
        "author_avatar": 1,
        "content": 5,
        "page": 1,
    }
    assert dict(report.content_technical_counts) == {
        "blocked": 1,
        "exclude": 1,
        "keep": 2,
        "review": 1,
    }
    assert dict(report.post_decision_counts) == {"exclude": 1, "keep": 2, "review": 1}
    assert report.image_projection_reason_counts["parent_post_not_eligible"] == 1
    assert sum(report.image_role_counts.values()) == 7
    assert sum(report.content_technical_counts.values()) == 5


def test_each_eligible_cluster_has_one_representative_and_falls_back_safely() -> None:
    """上游代表非 keep 时，只在同簇合格子集中稳定选择一个代表。"""

    result = build_analysis_release(
        ReleaseRequest("release-fallback", "run-fallback", "smoke"),
        (_post(1, "exclude"), _post(2, "keep"), _post(3, "keep")),
        _dedup((1, 2, 3), shared_hash_ids=(1, 2)),
        (),
        (),
    )

    assert [member.identity for member in result.analysis_posts_eligible] == [(2, 1), (3, 1)]
    assert [member.identity for member in result.analysis_posts_deduplicated] == [(2, 1), (3, 1)]
    fallback = next(
        member for member in result.analysis_posts_deduplicated if member.identity == (2, 1)
    )
    assert fallback.member_reason_code == "analysis_dedup_eligible_representative_fallback"
    assert result.quality_report.dedup_counts["eligible_nonrepresentative_count"] == 0


def test_input_order_does_not_change_members_report_or_manifest() -> None:
    """相同显式输入的排列不影响成员、计数与两个摘要。"""

    first = _full_projection()
    posts = (_post(4, "review"), _post(3, "exclude"), _post(2, "keep"), _post(1, "keep"))
    relations = tuple(
        reversed(
            (
                _relation(16, 1, "content"),
                _relation(10, 1, "author_avatar"),
                _relation(12, 1, "content"),
                _relation(11, 3, "page"),
                _relation(13, 3, "content"),
                _relation(14, 1, "content"),
                _relation(15, 1, "content"),
            )
        )
    )
    second = build_analysis_release(
        ReleaseRequest(
            "release-001",
            "run-001",
            "formal",
            {"config_sha256": "c" * 64, "schema_version": 24},
        ),
        posts,
        _dedup((4, 3, 2, 1), shared_hash_ids=(2, 1)),
        relations,
        tuple(
            reversed(
                (
                    _image_decision(15, "exclude"),
                    _image_decision(12, "keep"),
                    _image_decision(14, "review"),
                    _image_decision(13, "keep"),
                )
            )
        ),
    )

    assert second.analysis_posts_eligible == first.analysis_posts_eligible
    assert second.analysis_posts_deduplicated == first.analysis_posts_deduplicated
    assert second.image_projections == first.image_projections
    assert second.quality_report.report_sha256 == first.quality_report.report_sha256
    assert second.manifest_sha256 == first.manifest_sha256


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_path": "/private/tmp/source.sqlite"},
        {"artifact": "https://example.invalid/image.jpg"},
        {"raw_text": "原始游记"},
        {"author_id": "creator-1"},
        {"nested": {"access_token": "secret-1"}},
    ],
)
def test_recursive_metadata_guard_rejects_sensitive_values(metadata: dict[str, object]) -> None:
    """路径、URL、原始内容、作者身份和令牌字段均在递归边界拒绝。"""

    with pytest.raises(ValueError):
        build_analysis_release(
            ReleaseRequest("release-safe", "run-safe", "formal", metadata),
            (_post(1, "keep"),),
            _dedup((1,)),
            (),
            (),
        )


@pytest.mark.parametrize("field", ["release_id", "run_id"])
def test_release_and_run_ids_are_explicit_and_never_latest(field: str) -> None:
    """发布 API 不接受缺省或可变 latest 身份。"""

    request = ReleaseRequest("release-safe", "run-safe", "formal")
    with pytest.raises(ValueError):
        build_analysis_release(
            replace(request, **{field: "latest"}),
            (_post(1, "keep"),),
            _dedup((1,)),
            (),
            (),
        )


def test_formal_and_smoke_are_projection_only_and_require_repository_gate() -> None:
    """纯领域层明确运行模式，但两种模式都不能自行 accepted。"""

    for mode in ("formal", "smoke"):
        result = build_analysis_release(
            ReleaseRequest(f"release-{mode}", f"run-{mode}", mode),  # type: ignore[arg-type]
            (_post(1, "keep"),),
            _dedup((1,)),
            (),
            (),
        )
        assert result.mode == mode
        assert result.acceptance_state == "projection_only"
        assert result.requires_repository_acceptance is True


def test_only_finalized_technical_actions_are_accepted_without_visual_topic_axis() -> None:
    """接口拒绝非 finalized 与错误轴动作，不提供商业/视觉不相关分支。"""

    request = ReleaseRequest("release-image", "run-image", "formal")
    posts = (_post(1, "keep"),)
    dedup = _dedup((1,))
    relations = (_relation(1, 1, "content"),)

    with pytest.raises(ValueError):
        build_analysis_release(
            request,
            posts,
            dedup,
            relations,
            (replace(_image_decision(1, "keep"), seal_status="building"),),
        )
    with pytest.raises(ValueError):
        build_analysis_release(
            request,
            posts,
            dedup,
            relations,
            (_image_decision(1, "topic_irrelevant"),),
        )

    # 游戏截图或推荐计划图没有进入发布技术接口的主题字段；上游只要给出
    # finalized keep，它们就按普通内容图进入同一个集合。
    result = build_analysis_release(
        request,
        posts,
        dedup,
        relations,
        (_image_decision(1, "keep"),),
    )
    assert [member.image_identity for member in result.analysis_images_eligible] == [(1, 1)]


def test_dedup_and_image_inputs_must_match_the_explicit_post_snapshot() -> None:
    """缺簇、重复图片关系和非内容图片技术决定均不能生成部分发布。"""

    request = ReleaseRequest("release-match", "run-match", "formal")
    posts = (_post(1, "keep"), _post(2, "keep"))
    with pytest.raises(ValueError, match="partition"):
        build_analysis_release(request, posts, _dedup((1,)), (), ())

    relation = _relation(1, 1, "page")
    with pytest.raises(ValueError, match="content relations only"):
        build_analysis_release(
            request,
            (_post(1, "keep"),),
            _dedup((1,)),
            (relation,),
            (_image_decision(1, "keep"),),
        )
    with pytest.raises(ValueError, match="duplicate image"):
        build_analysis_release(
            request,
            (_post(1, "keep"),),
            _dedup((1,)),
            (_relation(1, 1, "content"), _relation(1, 1, "content")),
            (),
        )


def test_quality_report_contains_only_deidentified_structures() -> None:
    """报告对象只暴露 ID、模式、计数和 SHA，不保存请求元数据原值。"""

    report = _full_projection().quality_report
    assert report.release_id == "release-001"
    assert report.run_id == "run-001"
    assert len(report.request_metadata_sha256) == 64
    assert len(report.report_sha256) == 64
    assert not hasattr(report, "metadata")
    assert not hasattr(report, "source_path")
    assert not hasattr(report, "url")
    assert not hasattr(report, "raw_text")
    assert not hasattr(report, "author_id")
    assert not hasattr(report, "token")
