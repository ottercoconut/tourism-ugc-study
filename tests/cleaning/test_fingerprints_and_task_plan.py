"""帖子分轴指纹和纯文本任务影响规则测试。"""

from tourism_ugc_study.cleaning.fingerprints import post_fingerprints
from tourism_ugc_study.cleaning.task_plan import (
    POST_STAGES,
    algorithm_affected_stages,
    post_affected_stages,
    stage_required,
)


class Row(dict):
    """提供 sqlite3.Row 兼容键接口的测试行。"""

    def keys(self):  # type: ignore[override]
        return super().keys()


def test_post_fingerprints_separate_text_and_analysis_fields() -> None:
    original = Row(
        platform_key="xhs",
        source_type="search",
        status="active",
        title="标题",
        content_text="正文",
        author_platform_id="author-1",
        post_likes_count=1,
    )
    changed = Row(original, post_likes_count=9)

    original_text, original_author, original_analysis = post_fingerprints(original)
    changed_text, changed_author, changed_analysis = post_fingerprints(changed)
    assert original_text == changed_text
    assert original_author == changed_author
    assert original_analysis != changed_analysis


def test_task_plan_has_only_required_post_stages() -> None:
    assert POST_STAGES == ("text_deterministic", "text_relevance", "finalize")
    assert post_affected_stages({"text"}) == set(POST_STAGES)
    assert algorithm_affected_stages("post", {"text_relevance"}) == {
        "text_relevance",
        "finalize",
    }
    assert all(stage_required("post", stage) == 1 for stage in POST_STAGES)
