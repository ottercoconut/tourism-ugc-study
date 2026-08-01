"""旅游相关性基线、切分、阈值和抽审的版本化配置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tourism_ugc_study.cleaning.config import CleaningConfig, ConfigurationError


@dataclass(frozen=True)
class RelevanceConfig:
    """经校验的文本基线参数；所有阈值目标必须在训练前冻结。"""

    analyzer: str
    ngram_range: tuple[int, int]
    min_df: int
    max_df: float
    sublinear_tf: bool
    class_weight: str
    c_grid: tuple[float, ...]
    validation_fraction: float
    temporal_test_fraction: float
    temporal_test_min_per_platform: int
    platform_stable_negative_min: int
    high_risk_unrelated_precision_min: float
    high_risk_unrelated_recall_min: float
    low_risk_related_precision_min: float
    low_risk_related_recall_min: float
    low_risk_audit_fraction: float
    low_risk_audit_min_per_platform: int


def _number(raw: Mapping[str, object], key: str, *, unit: bool = False) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"text.{key} must be numeric")
    result = float(value)
    if unit and not 0.0 < result <= 1.0:
        raise ConfigurationError(f"text.{key} must be in (0, 1]")
    return result


def _positive_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"text.{key} must be a positive integer")
    return value


def relevance_config(config: CleaningConfig) -> RelevanceConfig:
    """从主配置解析相关性模型规则，拒绝缺字段和隐式默认值。"""

    raw = config.raw.get("text")
    if not isinstance(raw, Mapping):
        raise ConfigurationError("text must be a mapping")
    ngram = raw.get("ngram_range")
    c_grid = raw.get("c_grid")
    if (
        not isinstance(ngram, list)
        or len(ngram) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in ngram)
        or int(ngram[0]) <= 0
        or int(ngram[0]) > int(ngram[1])
    ):
        raise ConfigurationError("text.ngram_range must contain two increasing integers")
    if not isinstance(c_grid, list) or not c_grid:
        raise ConfigurationError("text.c_grid must be a non-empty list")
    c_values = tuple(float(value) for value in c_grid)
    if any(value <= 0 for value in c_values) or len(c_values) != len(set(c_values)):
        raise ConfigurationError("text.c_grid values must be unique and positive")
    if raw.get("analyzer") != "char":
        raise ConfigurationError("text.analyzer must be char")
    if raw.get("class_weight") != "balanced":
        raise ConfigurationError("text.class_weight must be balanced")
    if not isinstance(raw.get("sublinear_tf"), bool):
        raise ConfigurationError("text.sublinear_tf must be boolean")
    return RelevanceConfig(
        analyzer="char",
        ngram_range=(int(ngram[0]), int(ngram[1])),
        min_df=_positive_int(raw, "min_df"),
        max_df=_number(raw, "max_df", unit=True),
        sublinear_tf=bool(raw["sublinear_tf"]),
        class_weight="balanced",
        c_grid=tuple(sorted(c_values)),
        validation_fraction=_number(raw, "validation_fraction", unit=True),
        temporal_test_fraction=_number(raw, "temporal_test_fraction", unit=True),
        temporal_test_min_per_platform=_positive_int(raw, "temporal_test_min_per_platform"),
        platform_stable_negative_min=_positive_int(raw, "platform_stable_negative_min"),
        high_risk_unrelated_precision_min=_number(
            raw, "high_risk_unrelated_precision_min", unit=True
        ),
        high_risk_unrelated_recall_min=_number(
            raw, "high_risk_unrelated_recall_min", unit=True
        ),
        low_risk_related_precision_min=_number(
            raw, "low_risk_related_precision_min", unit=True
        ),
        low_risk_related_recall_min=_number(
            raw, "low_risk_related_recall_min", unit=True
        ),
        low_risk_audit_fraction=_number(raw, "low_risk_audit_fraction", unit=True),
        low_risk_audit_min_per_platform=_positive_int(
            raw, "low_risk_audit_min_per_platform"
        ),
    )
