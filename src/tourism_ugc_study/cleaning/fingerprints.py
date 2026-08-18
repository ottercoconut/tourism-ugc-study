"""与持久化无关的源字段投影和分轴指纹计算。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Protocol, Sequence


class RowLike(Protocol):
    """指纹计算所需的最小行接口，兼容 sqlite3.Row 和测试替身。"""

    def keys(self) -> Sequence[str]: ...

    def __getitem__(self, key: str) -> Any: ...


def canonical_sha256(value: Mapping[str, Any]) -> str:
    """对字段名稳定排序后计算 SHA-256，避免列顺序影响结果。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _project(row: RowLike, fields: Sequence[str]) -> dict[str, Any]:
    """只投影当前源版本实际存在的字段，兼容受支持的旧采集库。"""

    available = set(row.keys())
    return {field: row[field] if field in available else None for field in fields}


def post_fingerprints(row: RowLike) -> tuple[str, str, str]:
    """分别计算文本、作者关系和分析侧指纹，避免互动量触发清洗。"""

    text = _project(
        row,
        ("platform_key", "source_type", "status", "title", "content_text"),
    )
    author = _project(row, ("platform_key", "author_platform_id"))
    analysis = _project(
        row,
        (
            "platform_post_id",
            "source_url",
            "canonical_url",
            "published_at",
            "captured_at",
            "keyword",
            "post_likes_count",
            "post_favorites_count",
            "post_comments_count",
            "post_shares_count",
            "post_reposts_count",
            "post_views_count",
        ),
    )
    return canonical_sha256(text), canonical_sha256(author), canonical_sha256(analysis)


def post_author_identity_present(row: RowLike) -> bool:
    """判断帖子是否具备可用于泄漏分组的作者平台标识。

    这里只返回存在性，不保存或暴露作者原值。`NULL`、空串和全空白均视为
    缺失；缺失作者必须在后续切分中按帖子自身建立独立分组。
    """

    if "author_platform_id" not in set(row.keys()):
        return False
    value = row["author_platform_id"]
    return value is not None and bool(str(value).strip())
