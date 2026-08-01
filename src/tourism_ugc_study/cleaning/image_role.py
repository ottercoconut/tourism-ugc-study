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
    """来源角色不在协议枚举中时抛出的去敏异常。

    `reason_code` 可进入日志和调度状态，不携带 URL、路径或图片内容。该异常
    表示上游关系证据不满足协议，调用方不得猜测或降级成 `content`；它不是
    图片解码失败，也不应按可重试的文件阻塞处理。
    """

    def __init__(self, reason_code: str = "invalid_relation_role") -> None:
        super().__init__("image relation role is invalid")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ImageRoleDecision:
    """一个来源角色对应的固定处理动作与审计理由。

    `relation_role` 是冻结源快照中的权威关系；`handling_action` 只决定是否进入
    内容图片技术检查；`reason_code` 用于审计。对象不含图片内容或最终清洗标签，
    相同角色始终返回相同不可变对象。
    """

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
    """把一个权威来源角色映射为确定性处理动作。

    输入必须是 `author_avatar`、`page` 或 `content`；返回值不读取 URL、尺寸或
    图像字节，也不产生删除结论。未知值抛出 :class:`ImageRoleError`，防止来源
    不明图片被静默当作内容图。函数无状态、无 I/O，可安全重复调用。
    """

    try:
        return _ROLE_DECISIONS[value]
    except (KeyError, TypeError) as exc:
        raise ImageRoleError() from exc
