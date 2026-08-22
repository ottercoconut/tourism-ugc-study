"""当前正式计划使用的数据清洗公开接口。

验证器依赖参考 artifact 读取器，因此按需导入，避免标注层读取清洗投影时形成
循环依赖。
"""

from __future__ import annotations

from typing import Any

from .config import ConfigurationError, StableCleaningConfig, load_stable_config
from .formal_schema import migrate_formal_schema
from .text_config import TextCleaningConfig, load_text_config
from .text_normalize import NormalizedText, normalize_post_text

__all__ = [
    "StableCleaningConfig",
    "ConfigurationError",
    "ReferenceEvidenceError",
    "ReferenceValidationResult",
    "TextCleaningConfig",
    "NormalizedText",
    "load_stable_config",
    "load_text_config",
    "normalize_post_text",
    "migrate_formal_schema",
    "validate_reference_evidence",
]


def __getattr__(name: str) -> Any:
    """按需加载最终参考证据验证接口。

    Args:
        name: 调用方请求的公开属性名。

    Returns:
        参考证据模块中的公开对象。

    Raises:
        AttributeError: 名称不属于本包公开接口。
    """

    if name in {
        "ReferenceEvidenceError",
        "ReferenceValidationResult",
        "validate_reference_evidence",
    }:
        from . import reference_evidence

        return getattr(reference_evidence, name)
    raise AttributeError(name)
