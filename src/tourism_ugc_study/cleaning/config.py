"""数据清洗流水线的版本化配置加载与公开参数校验。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

import yaml


class ConfigurationError(ValueError):
    """配置违反公开契约时抛出的无敏感信息异常。

    消息只描述字段和约束，不回显 YAML 原值、机器路径或疑似凭据；加载失败
    必须发生在数据库或网络 I/O 之前，调用方不得忽略后继续运行。
    """


@dataclass(frozen=True)
class StableCleaningConfig:
    """正式清洗框架的稳定配置投影。

    Attributes:
        status: 框架状态；阈值阶段完成前固定为待实现状态。
        label_guide_version: 人工旅游相关性标签手册身份。
        random_seed: 全局可复现随机种子。
        text: 深度不可变的字符文本模型参数。
        leakage: 深度不可变的泄漏分组开关。
        split: 深度不可变的全局切分参数。
        routing: 深度不可变的三段式路由占位；本阶段必须为 ``UNSET``。
        audit: 深度不可变的保留集审计占位；本阶段必须为 ``UNSET``。
        artifacts: 规范化规则的相对路径与内容版本锁。
        raw: 完整配置的深度不可变投影。
        sha256: 加载前规范化配置内容的 SHA-256。
    """

    status: str
    label_guide_version: str
    random_seed: int
    text: Mapping[str, Any]
    leakage: Mapping[str, Any]
    split: Mapping[str, Any]
    routing: Mapping[str, Any]
    audit: Mapping[str, Any]
    artifacts: Mapping[str, Any]
    raw: Mapping[str, Any]
    sha256: str


_STABLE_ROOT_FIELDS = frozenset(
    {
        "status",
        "label_guide_version",
        "random_seed",
        "text",
        "leakage",
        "split",
        "routing",
        "audit",
        "artifacts",
    }
)
_STABLE_TEXT_FIELDS = frozenset(
    {
        "analyzer",
        "ngram_range",
        "min_df",
        "max_df",
        "sublinear_tf",
        "classifier",
        "svm_c",
        "class_weight",
        "calibration",
        "calibration_folds",
    }
)
_STABLE_LEAKAGE_FIELDS = frozenset(
    {"author", "exact_duplicate", "confirmed_near_duplicate"}
)
_STABLE_SPLIT_FIELDS = frozenset(
    {"temporal_test_fraction", "validation_fraction", "scope"}
)
_STABLE_ROUTING_FIELDS = frozenset({"T_keep", "T_exclude", "unset_behavior"})
_STABLE_AUDIT_FIELDS = frozenset(
    {
        "sample_size",
        "max_event_rate",
        "upper_confidence_limit",
        "recovery_gate",
        "population",
    }
)
_STABLE_ARTIFACT_FIELDS = frozenset(
    {"normalization_config", "normalization_version_lock"}
)


def _stable_mapping(value: Any, field: str) -> Mapping[str, Any]:
    """要求一个稳定配置节点是映射。

    Args:
        value: 待检查的 YAML 节点。
        field: 用于失败原因的公开字段路径。

    Returns:
        通过类型校验的映射。

    Raises:
        ConfigurationError: 节点不是映射。
    """

    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping")
    return value


def _stable_ratio(value: Any, field: str) -> float:
    """解析严格位于零和一之间的比例。

    Args:
        value: 待解析数值。
        field: 用于失败原因的公开字段路径。

    Returns:
        浮点比例。

    Raises:
        ConfigurationError: 值不是有限的开区间比例。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{field} must be numeric")
    result = float(value)
    if not 0.0 < result < 1.0:
        raise ConfigurationError(f"{field} must be in (0, 1)")
    return result


def _stable_positive_int(value: Any, field: str) -> int:
    """解析稳定配置中的正整数。

    Args:
        value: 待解析值。
        field: 用于失败原因的公开字段路径。

    Returns:
        通过校验的正整数。

    Raises:
        ConfigurationError: 值不是正整数。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{field} must be a positive integer")
    return value


def _stable_unset(value: Any, field: str) -> str:
    """要求尚未冻结的策略字段保持 ``UNSET``。

    Args:
        value: 待检查的策略值。
        field: 用于失败原因的公开字段路径。

    Returns:
        固定字符串 ``UNSET``。

    Raises:
        ConfigurationError: 字段被提前设置为数值或其他值。
    """

    if value != "UNSET":
        raise ConfigurationError(f"{field} must remain UNSET before formalization")
    return "UNSET"


def _require_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], field: str
) -> None:
    """拒绝缺失或未知的稳定配置字段。

    Args:
        value: 待检查映射。
        expected: 该层唯一允许的键集合。
        field: 用于失败原因的公开字段路径。

    Raises:
        ConfigurationError: 存在缺失键或未知键。
    """

    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise ConfigurationError(f"{field} is missing: " + ", ".join(missing))
    if unknown:
        raise ConfigurationError(f"{field} has unknown fields: " + ", ".join(unknown))


def _deep_freeze(value: Any) -> Any:
    """递归转换配置容器，防止摘要生成后发生原地修改。

    Args:
        value: 已通过公开值校验的配置值。

    Returns:
        映射转为只读映射、列表转为元组后的深度不可变值。
    """

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(child) for child in value)
    return value


def _normalization_path(config_path: Path, relative_value: str) -> Path:
    """解析仓库相对的规范化规则路径。

    Args:
        config_path: 当前稳定配置文件路径。
        relative_value: 配置中声明的相对路径。

    Returns:
        已解析且存在的规则文件路径。

    Raises:
        ConfigurationError: 路径为绝对路径或无法在仓库根/当前目录定位。
    """

    relative = Path(relative_value)
    if relative.is_absolute():
        raise ConfigurationError("artifacts.normalization_config must be relative")
    roots = [config_path.parent.parent, Path.cwd()]
    for root in roots:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    raise ConfigurationError("normalization configuration could not be located")


def load_stable_config(path: str | Path) -> StableCleaningConfig:
    """加载并完整校验 ``configs/cleaning.yaml``。

    Args:
        path: 稳定配置文件路径。配置中的规则文件仍使用仓库相对路径。

    Returns:
        内容哈希已冻结、所有容器深度不可变的配置对象。

    Raises:
        ConfigurationError: YAML 不可读、字段未知、值违反框架、规则文件找不到，
            或规范化规则内容与版本锁不一致。
    """

    config_path = Path(path)
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        raise ConfigurationError("stable cleaning configuration could not be read") from exc
    raw = _stable_mapping(parsed, "config")
    _validate_public_values(raw)
    _require_exact_keys(raw, _STABLE_ROOT_FIELDS, "config")
    status = _require_nonempty_string(raw.get("status"), "status")
    if status != "FRAMEWORK_FROZEN / THRESHOLD_PENDING / IMPLEMENTATION_PENDING":
        raise ConfigurationError("status must describe the frozen framework")
    label_guide_version = _require_nonempty_string(
        raw.get("label_guide_version"), "label_guide_version"
    )
    if label_guide_version != "text-cleaning-v1.5":
        raise ConfigurationError("label_guide_version must be text-cleaning-v1.5")
    random_seed = _stable_positive_int(raw.get("random_seed"), "random_seed")
    if random_seed != 20260728:
        raise ConfigurationError("random_seed must remain 20260728")
    text = _stable_mapping(raw["text"], "text")
    _require_exact_keys(text, _STABLE_TEXT_FIELDS, "text")
    if (
        text.get("analyzer") != "char"
        or text.get("classifier") != "linear_svm"
        or text.get("calibration") != "sigmoid"
    ):
        raise ConfigurationError(
            "text must use char features, linear SVM, and sigmoid calibration"
        )
    svm_c = text.get("svm_c")
    if isinstance(svm_c, bool) or not isinstance(svm_c, (int, float)):
        raise ConfigurationError("text.svm_c must be numeric")
    if float(svm_c) != 1.0:
        raise ConfigurationError("text.svm_c must remain 1.0")
    if _stable_positive_int(text.get("calibration_folds"), "text.calibration_folds") != 5:
        raise ConfigurationError("text.calibration_folds must remain 5")
    if text.get("class_weight") != "balanced":
        raise ConfigurationError("text.class_weight must be balanced")
    if text.get("sublinear_tf") is not True:
        raise ConfigurationError("text.sublinear_tf must remain true")
    ngram = text.get("ngram_range")
    if ngram != [2, 5]:
        raise ConfigurationError("text.ngram_range must remain [2, 5]")
    if _stable_positive_int(text.get("min_df"), "text.min_df") != 2:
        raise ConfigurationError("text.min_df must remain 2")
    max_df = text.get("max_df")
    if isinstance(max_df, bool) or not isinstance(max_df, (int, float)):
        raise ConfigurationError("text.max_df must be numeric")
    if float(max_df) != 0.995:
        raise ConfigurationError("text.max_df must remain 0.995")
    split = _stable_mapping(raw["split"], "split")
    _require_exact_keys(split, _STABLE_SPLIT_FIELDS, "split")
    if _stable_ratio(split.get("temporal_test_fraction"), "split.temporal_test_fraction") != 0.20:
        raise ConfigurationError("split.temporal_test_fraction must be 0.20")
    if _stable_ratio(split.get("validation_fraction"), "split.validation_fraction") != 0.20:
        raise ConfigurationError("split.validation_fraction must be 0.20")
    if split.get("scope") != "global":
        raise ConfigurationError("split.scope must be global")
    leakage = _stable_mapping(raw["leakage"], "leakage")
    _require_exact_keys(leakage, _STABLE_LEAKAGE_FIELDS, "leakage")
    for key in ("author", "exact_duplicate", "confirmed_near_duplicate"):
        if leakage.get(key) is not True:
            raise ConfigurationError(f"leakage.{key} must be true")
    routing = _stable_mapping(raw["routing"], "routing")
    _require_exact_keys(routing, _STABLE_ROUTING_FIELDS, "routing")
    for key in ("T_keep", "T_exclude"):
        _stable_unset(routing.get(key), f"routing.{key}")
    if routing.get("unset_behavior") != "manual_review":
        raise ConfigurationError("routing.unset_behavior must be manual_review")
    audit = _stable_mapping(raw["audit"], "audit")
    _require_exact_keys(audit, _STABLE_AUDIT_FIELDS, "audit")
    for key in ("sample_size", "max_event_rate", "upper_confidence_limit", "recovery_gate"):
        _stable_unset(audit.get(key), f"audit.{key}")
    if audit.get("population") != "retained_global":
        raise ConfigurationError("audit.population must be retained_global")
    artifacts = _stable_mapping(raw["artifacts"], "artifacts")
    _require_exact_keys(artifacts, _STABLE_ARTIFACT_FIELDS, "artifacts")
    normalization_value = _require_nonempty_string(
        artifacts.get("normalization_config"), "artifacts.normalization_config"
    )
    if normalization_value != "configs/cleaning-text-normalization-v1.yaml":
        raise ConfigurationError("artifacts.normalization_config must use the frozen rules")
    normalization_lock = _require_nonempty_string(
        artifacts.get("normalization_version_lock"),
        "artifacts.normalization_version_lock",
    )
    normalization_path = _normalization_path(config_path, normalization_value)
    # 延迟导入避免 text_config 在定义 ConfigurationError 时形成模块循环依赖。
    from .text_config import load_text_config

    load_text_config(normalization_path, expected_version_lock=normalization_lock)
    sha256 = _canonical_sha256(raw)
    frozen_raw = _deep_freeze(raw)
    return StableCleaningConfig(
        status=status,
        label_guide_version=label_guide_version,
        random_seed=random_seed,
        text=frozen_raw["text"],
        leakage=frozen_raw["leakage"],
        split=frozen_raw["split"],
        routing=frozen_raw["routing"],
        audit=frozen_raw["audit"],
        artifacts=frozen_raw["artifacts"],
        raw=frozen_raw,
        sha256=sha256,
    )


_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|credential|api[_-]?key)", re.IGNORECASE)


@dataclass(frozen=True)
class ScopeMigration:
    """用于证明上游数据仅包含青岛范围的迁移记录。

    Attributes:
        version: 已执行上游迁移的正整数版本。
        name: 不含路径或敏感信息的公开稳定名称。
    """

    version: int
    name: str


@dataclass(frozen=True)
class InputContractConfig:
    """运行级城市范围契约；该契约不构成逐条清洗标签。

    Attributes:
        expected_city: 研究输入的固定城市范围。
        accepted_legacy_city_values: 仅用于兼容上游历史元数据的城市值。
        upstream_scope_migration: 证明范围已在清洗前收敛的迁移身份。
    """

    expected_city: str
    accepted_legacy_city_values: tuple[str, ...]
    upstream_scope_migration: ScopeMigration


@dataclass(frozen=True)
class IncrementalConfig:
    """由协议配置冻结、与具体处理算法解耦的增量调度默认值。

    Attributes:
        max_posts_per_batch: 单批允许处理的帖子上限。
        claim_size: 每次领取的任务数。
        max_attempts: 单任务最大尝试次数。
        stale_after_minutes: 运行被判定为过期的分钟数。
        post_order: 冻结的任务发现排序字段。
        changed_source_action: 源版本变化后的处理动作。
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

    Attributes:
        protocol_version: 历史可执行协议版本。
        text_label_guide_version: 历史人工标签手册身份。
        random_seed: 全局可复现随机种子。
        input_contract: 上游城市范围契约。
        incremental: 增量调度参数。
        algorithm_versions: 各流水线步骤的算法身份。
        raw: 完整历史配置投影。
        sha256: 规范化配置内容的 SHA-256。
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
    """要求历史配置节点是映射。

    Args:
        value: 待检查的 YAML 节点。
        field: 用于失败信息的字段路径。

    Returns:
        通过校验的映射。

    Raises:
        ConfigurationError: 节点不是映射。
    """

    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping")
    return value


def _require_nonempty_string(value: Any, field: str) -> str:
    """解析历史配置中的非空字符串。

    Args:
        value: 待检查值。
        field: 用于失败信息的字段路径。

    Returns:
        去除首尾空白后的字符串。

    Raises:
        ConfigurationError: 值不是非空字符串。
    """

    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _require_positive_int(value: Any, field: str) -> int:
    """解析历史配置中的正整数。

    Args:
        value: 待检查值。
        field: 用于失败信息的字段路径。

    Returns:
        通过校验的正整数。

    Raises:
        ConfigurationError: 值不是正整数。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{field} must be a positive integer")
    return value


def _is_absolute_local_path(value: str) -> bool:
    """判断字符串是否为 POSIX 或 Windows 本地绝对路径。

    Args:
        value: 待检查字符串。

    Returns:
        任一平台路径语法将其判为绝对路径时返回 ``True``。
    """

    return Path(value).expanduser().is_absolute() or PureWindowsPath(value).is_absolute()


def _validate_public_values(value: Any, field: str = "config") -> None:
    """递归拒绝敏感键、绝对路径和 YAML 非标量值。

    Args:
        value: 当前配置节点。
        field: 当前节点的公开字段路径。

    Raises:
        ConfigurationError: 节点包含敏感键、绝对路径或不支持的值类型。
    """

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
    """计算配置规范 JSON 的 SHA-256。

    Args:
        raw: 已完成公开值校验的配置映射。

    Returns:
        小写十六进制 SHA-256。
    """

    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_config(path: str | Path) -> CleaningConfig:
    """加载并校验不含运行时路径和敏感字段的版本化 YAML 配置。

    Args:
        path: 历史 v3.2 配置文件路径；路径本身不进入配置摘要。

    Returns:
        经类型、公开值、协议版本和算法门槛校验的历史配置。

    Raises:
        ConfigurationError: 文件不可读、YAML 非法、字段缺失，或出现绝对路径、
            疑似凭据及不符合历史协议的值。
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

    Args:
        config: 当前已校验的历史配置。
        config_sha256: 创建既有运行时记录的配置摘要。
        protocol_version: 创建既有运行时记录的协议版本。

    Returns:
        协议版本和规范化配置摘要同时相等时返回 ``True``。
    """

    return config.sha256 == config_sha256 and config.protocol_version == protocol_version
