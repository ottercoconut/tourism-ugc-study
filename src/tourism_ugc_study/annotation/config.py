"""文本人工审核的抽样、间隔复核与稳定性门槛配置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tourism_ugc_study.cleaning.config import CleaningConfig, ConfigurationError


@dataclass(frozen=True)
class AnnotationConfig:
    """经校验的文本标注参数。

    数量是科研抽样量，不得用工程批次大小替代；复核阈值只作用于
    结构可用性和适用的旅游相关性两个清洗判断轴。配置不对参与人数
    作任何推断，只描述实际执行的审核轮次和证据要求。
    """

    initial_probability_size: int
    probability_min_per_platform: int
    initial_targeted_size: int
    initial_recheck_size: int
    minimum_recheck_interval_days: int
    minimum_recheck_raw_agreement: float
    minimum_recheck_cohen_kappa: float
    additional_recheck_size: int
    periodic_increment_posts: int
    periodic_probability_size: int


def _positive_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"annotation.{key} must be a positive integer")
    return value


def _unit_interval(raw: Mapping[str, object], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"annotation.{key} must be a number")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ConfigurationError(f"annotation.{key} must be between 0 and 1")
    return result


def annotation_config(config: CleaningConfig) -> AnnotationConfig:
    """从主配置解析标注参数；缺字段时拒绝运行而不采用隐式默认值。"""

    raw = config.raw.get("annotation")
    if not isinstance(raw, Mapping):
        raise ConfigurationError("annotation must be a mapping")
    parsed = AnnotationConfig(
        initial_probability_size=_positive_int(raw, "initial_probability_size"),
        probability_min_per_platform=_positive_int(raw, "probability_min_per_platform"),
        initial_targeted_size=_positive_int(raw, "initial_targeted_size"),
        initial_recheck_size=_positive_int(raw, "initial_recheck_size"),
        minimum_recheck_interval_days=_positive_int(
            raw, "minimum_recheck_interval_days"
        ),
        minimum_recheck_raw_agreement=_unit_interval(
            raw, "minimum_recheck_raw_agreement"
        ),
        minimum_recheck_cohen_kappa=_unit_interval(
            raw, "minimum_recheck_cohen_kappa"
        ),
        additional_recheck_size=_positive_int(raw, "additional_recheck_size"),
        periodic_increment_posts=_positive_int(raw, "periodic_increment_posts"),
        periodic_probability_size=_positive_int(raw, "periodic_probability_size"),
    )
    if parsed.initial_recheck_size > (
        parsed.initial_probability_size + parsed.initial_targeted_size
    ):
        raise ConfigurationError("initial_recheck_size exceeds initial sample capacity")
    return parsed
