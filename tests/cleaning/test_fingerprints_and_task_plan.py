from __future__ import annotations

from tourism_ugc_study.cleaning.fingerprints import image_fingerprints, post_fingerprints
from tourism_ugc_study.cleaning.task_plan import (
    algorithm_affected_stages,
    image_affected_stages,
    post_affected_stages,
)


class MappingRow(dict[str, object]):
    """提供 sqlite3.Row 最小接口的纯内存测试替身。"""


def test_engagement_change_only_changes_analysis_fingerprint() -> None:
    original = MappingRow(
        platform_key="xhs",
        source_type="search",
        status="captured",
        title="标题",
        content_text="正文",
        author_platform_id="author",
        captured_at="2026-07-30",
        post_likes_count=1,
    )
    changed = MappingRow(original, post_likes_count=2)

    original_text, original_author, original_analysis = post_fingerprints(original)
    changed_text, changed_author, changed_analysis = post_fingerprints(changed)
    assert original_text == changed_text
    assert original_author == changed_author
    assert original_analysis != changed_analysis
    assert post_affected_stages({"analysis"}) == set()


def test_image_file_change_does_not_require_role_processing() -> None:
    original = MappingRow(
        web_post_id=1,
        image_index=0,
        image_url="https://invalid/image",
        image_role="content",
        local_path=None,
        width=None,
        height=None,
        mime_type=None,
        sha256=None,
    )
    changed = MappingRow(original, local_path="images/1.jpg", sha256="a" * 64)

    original_relation, original_file = image_fingerprints(original)
    changed_relation, changed_file = image_fingerprints(changed)
    assert original_relation == changed_relation
    assert original_file != changed_file
    assert image_affected_stages({"file"}) == {
        "image_fingerprint",
        "image_noise",
        "finalize",
    }


def test_upstream_algorithm_change_propagates_to_downstream_stages() -> None:
    assert algorithm_affected_stages("post", {"text_deterministic"}) == {
        "text_deterministic",
        "text_relevance",
        "finalize",
    }
    assert algorithm_affected_stages("image", {"image_fingerprint"}) == {
        "image_fingerprint",
        "image_noise",
        "finalize",
    }
