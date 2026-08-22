"""数据清洗流水线的版本化配置加载与公开参数校验。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

import yaml


class ConfigurationError(ValueError):
    """配置违反公开契约时抛出的无敏感信息异常。

    消息只描述字段和约束，不回显 YAML 原值、机器路径或疑似凭据；加载失败
    必须发生在数据库或网络 I/O 之前，调用方不得忽略后继续运行。
    """


_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|credential|api[_-]?key)", re.IGNORECASE)


@dataclass(frozen=True)
class ScopeMigration:
    """用于证明上游数据仅包含青岛范围的迁移记录。

    ``version`` 是已执行上游迁移的正整数版本，``name`` 是公开稳定名称。该
    对象只作为运行级输入契约证据，不参与逐条城市判断或数据清洗标签。
    """

    version: int
    name: str


@dataclass(frozen=True)
class InputContractConfig:
    """运行级城市范围契约；该契约不构成逐条清洗标签。

    ``expected_city`` 固定研究范围，``accepted_legacy_city_values`` 仅用于兼容
    上游历史元数据，``upstream_scope_migration`` 证明范围已在清洗前收敛。任何
    字段都不得被落成逐条 ``is_qingdao`` 清洗结论。
    """

    expected_city: str
    accepted_legacy_city_values: tuple[str, ...]
    upstream_scope_migration: ScopeMigration


@dataclass(frozen=True)
class IncrementalConfig:
    """由协议配置冻结、与具体处理算法解耦的增量调度默认值。

    批量、领取、重试和过期字段控制任务状态机；``post_order`` 固定发现次序，
    ``changed_source_action`` 固定源版本变化策略。配置只调度帖子文本任务，
    不改变领域判定，所有正整数和枚举在加载阶段校验。
    """

    max_posts_per_batch: int
    claim_size: int
    max_attempts: int
    stale_after_minutes: int
    post_order: tuple[str, ...]
    changed_source_action: str


@dataclass(frozen=True)
class CleaningConfig:
    """校验后的 v3.2 文本清洗配置及其规范化摘要。

    ``input_contract`` 与 ``incremental`` 分别承载上游范围和增量调度参数；
    ``raw`` 是公开 YAML 的只读投影，``sha256`` 是其规范化科研身份。对象不
    包含数据库路径或凭据，仓储必须同时校验 ``protocol_version`` 与摘要后
    才能复用旧运行。
    """

    protocol_version: str
    text_label_guide_version: str
    random_seed: int
    input_contract: InputContractConfig
    incremental: IncrementalConfig
    algorithm_versions: Mapping[str, str | int]
    raw: Mapping[str, Any]
    sha256: str


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping")
    return value


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{field} must be a positive integer")
    return value


def _is_absolute_local_path(value: str) -> bool:
    return Path(value).expanduser().is_absolute() or PureWindowsPath(value).is_absolute()


def _validate_public_values(value: Any, field: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text):
                raise ConfigurationError(f"sensitive field is not allowed: {field}.{key_text}")
            _validate_public_values(child, f"{field}.{key_text}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_public_values(child, f"{field}[{index}]")
        return
    if isinstance(value, str) and _is_absolute_local_path(value):
        raise ConfigurationError(f"absolute local path is not allowed: {field}")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ConfigurationError(f"unsupported YAML value at {field}")


def _canonical_sha256(raw: Mapping[str, Any]) -> str:
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_config(path: str | Path) -> CleaningConfig:
    """加载并校验不含运行时路径和敏感字段的版本化 YAML 配置。

    ``path`` 只用于本次读取，不进入返回配置或摘要。成功返回经类型、公开值、
    协议版本和固定算法门槛校验的 :class:`CleaningConfig`；文件不可读、
    YAML 非法、字段缺失、绝对路径或疑似凭据均统一抛出
    :class:`ConfigurationError`，且不会写数据库或创建目录。
    """

    config_path = Path(path)
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError("configuration could not be read") from exc

    raw = _require_mapping(parsed, "config")
    _validate_public_values(raw)

    protocol_version = _require_nonempty_string(raw.get("protocol_version"), "protocol_version")
    if protocol_version != "3.2":
        raise ConfigurationError("protocol_version must be 3.2")

    input_raw = _require_mapping(raw.get("input_contract"), "input_contract")
    migration_raw = _require_mapping(
        input_raw.get("upstream_scope_migration"),
        "input_contract.upstream_scope_migration",
    )
    accepted_values = input_raw.get("accepted_legacy_city_values")
    if not isinstance(accepted_values, list) or not accepted_values:
        raise ConfigurationError("accepted_legacy_city_values must be a non-empty list")
    accepted_cities = tuple(
        _require_nonempty_string(item, "accepted_legacy_city_values item") for item in accepted_values
    )
    if len(set(accepted_cities)) != len(accepted_cities):
        raise ConfigurationError("accepted_legacy_city_values must not contain duplicates")

    expected_city = _require_nonempty_string(input_raw.get("expected_city"), "expected_city")
    if expected_city not in accepted_cities:
        raise ConfigurationError("expected_city must be accepted by the legacy city contract")

    incremental_raw = _require_mapping(raw.get("incremental"), "incremental")
    post_order = incremental_raw.get("post_order")
    if not isinstance(post_order, list) or not post_order:
        raise ConfigurationError("incremental.post_order must be a non-empty list")

    algorithm_versions = _require_mapping(raw.get("algorithm_versions"), "algorithm_versions")
    required_algorithms = {
        "input_contract",
        "source_snapshot",
        "object_manifest",
        "derived_schema",
        "inventory",
        "scheduler",
        "text_deterministic",
        "text_normalization",
        "text_runtime",
        "annotation_sampling",
        "text_relevance",
        "finalize",
    }
    missing_algorithms = sorted(required_algorithms - set(algorithm_versions))
    if missing_algorithms:
        raise ConfigurationError(
            "algorithm_versions is missing: " + ", ".join(missing_algorithms)
        )
    for key, value in algorithm_versions.items():
        if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
            raise ConfigurationError(f"algorithm_versions.{key} must be a string or integer")

    random_seed = _require_positive_int(raw.get("random_seed"), "random_seed")
    config = CleaningConfig(
        protocol_version=protocol_version,
        text_label_guide_version=_require_nonempty_string(
            raw.get("text_label_guide_version"), "text_label_guide_version"
        ),
        random_seed=random_seed,
        input_contract=InputContractConfig(
            expected_city=expected_city,
            accepted_legacy_city_values=accepted_cities,
            upstream_scope_migration=ScopeMigration(
                version=_require_positive_int(migration_raw.get("version"), "scope migration version"),
                name=_require_nonempty_string(migration_raw.get("name"), "scope migration name"),
            ),
        ),
        incremental=IncrementalConfig(
            max_posts_per_batch=_require_positive_int(
                incremental_raw.get("max_posts_per_batch"), "max_posts_per_batch"
            ),
            claim_size=_require_positive_int(incremental_raw.get("claim_size"), "claim_size"),
            max_attempts=_require_positive_int(
                incremental_raw.get("max_attempts"), "max_attempts"
            ),
            stale_after_minutes=_require_positive_int(
                incremental_raw.get("stale_after_minutes"), "stale_after_minutes"
            ),
            post_order=tuple(
                _require_nonempty_string(item, "post_order item") for item in post_order
            ),
            changed_source_action=_require_nonempty_string(
                incremental_raw.get("changed_source_action"), "changed_source_action"
            ),
        ),
        algorithm_versions=dict(algorithm_versions),
        raw=dict(raw),
        sha256=_canonical_sha256(raw),
    )
    return config


def matches_frozen_run(
    config: CleaningConfig,
    config_sha256: str,
    protocol_version: str,
) -> bool:
    """判断运行时配置是否与创建运行时冻结的配置和协议完全一致。

    只有协议版本和规范化配置摘要同时相等才返回真；函数不尝试迁移旧配置、
    不比较对象身份，也不访问数据库。仓储应在复用运行前调用并把假值视为
    硬阻断，避免相同 ID 下混入不同科研参数。
    """

    return config.sha256 == config_sha256 and config.protocol_version == protocol_version
