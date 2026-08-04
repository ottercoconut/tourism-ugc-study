from __future__ import annotations

import pytest

from tourism_ugc_study.cleaning.image_role import ImageRoleError, decide_image_role


def test_source_roles_have_fixed_non_semantic_actions() -> None:
    assert decide_image_role("author_avatar").handling_action == "exclude_from_content"
    assert decide_image_role("page").handling_action == "evidence_only"
    assert decide_image_role("content").handling_action == "inspect_content"


def test_role_mapping_does_not_guess_from_url_or_content_format() -> None:
    """路线图等内容格式仍由来源 role 决定是否进入技术检查。"""

    assert decide_image_role("content").reason_code == "content_requires_technical_check"
    with pytest.raises(ImageRoleError):
        decide_image_role("route_plan")
