"""图片来源关系到处理动作的确定性分流。

来源角色描述图片如何被采集，不是技术噪声标签或视觉内容判断。本模块只做
固定映射，禁止根据 URL、尺寸或画面内容猜测角色。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RelationRole = Literal["author_avatar", "page", "content"]
HandlingAction = Literal["exclude_from_content", "evidence_only", "inspect_content"]


class ImageRoleError(ValueError):
    """来源角色不在协议枚举中时抛出的去敏异常。"""

    def __init__(self, reason_code: str = "invalid_relation_role") -> None:
        super().__init__("image relation role is invalid")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ImageRoleDecision:
    """一个来源角色对应的固定处理动作与审计理由。"""

    relation_role: RelationRole
    handling_action: HandlingAction
    reason_code: str


_ROLE_DECISIONS: dict[str, ImageRoleDecision] = {
    "author_avatar": ImageRoleDecision(
        relation_role="author_avatar",
        handling_action="exclude_from_content",
        reason_code="author_avatar_not_content",
    ),
    "page": ImageRoleDecision(
        relation_role="page",
        handling_action="evidence_only",
        reason_code="page_evidence_only",
    ),
    "content": ImageRoleDecision(
        relation_role="content",
        handling_action="inspect_content",
        reason_code="content_requires_technical_check",
    ),
}


def decide_image_role(value: str) -> ImageRoleDecision:
    """返回严格角色映射；未知值失败，避免把来源不明图片静默当作内容图。"""

    try:
        return _ROLE_DECISIONS[value]
    except (KeyError, TypeError) as exc:
        raise ImageRoleError() from exc
