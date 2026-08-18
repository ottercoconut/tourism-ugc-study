"""文本旅游相关性人工审核的抽样配置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tourism_ugc_study.cleaning.config import CleaningConfig, ConfigurationError


@dataclass(frozen=True)
class AnnotationConfig:
    """经校验的文本标注参数。

    数量是科研抽样量，不得用工程批次大小替代。人工任务只有旅游相关性
    一个判断轴；结构可用性已由候选构建前的全量确定性规则保证。
    """

    initial_probability_size: int
    probability_min_per_platform: int
    initial_targeted_size: int
    periodic_increment_posts: int
    periodic_probability_size: int


def _positive_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"annotation.{key} must be a positive integer")
    return value


def annotation_config(config: CleaningConfig) -> AnnotationConfig:
    """从主配置解析标注参数；缺字段时拒绝运行而不采用隐式默认值。"""

    raw = config.raw.get("annotation")
    if not isinstance(raw, Mapping):
        raise ConfigurationError("annotation must be a mapping")
    parsed = AnnotationConfig(
        initial_probability_size=_positive_int(raw, "initial_probability_size"),
        probability_min_per_platform=_positive_int(raw, "probability_min_per_platform"),
        initial_targeted_size=_positive_int(raw, "initial_targeted_size"),
        periodic_increment_posts=_positive_int(raw, "periodic_increment_posts"),
        periodic_probability_size=_positive_int(raw, "periodic_probability_size"),
    )
    return parsed
