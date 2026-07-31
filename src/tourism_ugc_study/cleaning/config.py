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
    """配置违反公开契约时抛出的无敏感信息异常。"""


_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|credential|api[_-]?key)", re.IGNORECASE)


@dataclass(frozen=True)
class ScopeMigration:
    """用于证明上游数据仅包含青岛范围的迁移记录。"""

    version: int
    name: str


@dataclass(frozen=True)
class InputContractConfig:
    """运行级城市范围契约；该契约不构成逐条清洗标签。"""

    expected_city: str
    accepted_legacy_city_values: tuple[str, ...]
    upstream_scope_migration: ScopeMigration


@dataclass(frozen=True)
class IncrementalConfig:
    """由协议配置冻结、与具体处理算法解耦的增量调度默认值。"""

    max_posts_per_batch: int
    claim_size: int
    max_attempts: int
    stale_after_minutes: int
    post_order: tuple[str, ...]
    changed_source_action: str


@dataclass(frozen=True)
class ImageConfig:
    """图片清洗框架的依赖锁与确定性候选参数。

    这些字段只决定技术校验和候选生成，不表达最终图片标签。版本号与参数必须
    通过配置摘要进入运行谱系，以便真实图片到位后可以复现同一套指纹结果。
    """

    pillow_version: str
    imagehash_version: str
    phash_hash_size: int
    phash_highfreq_factor: int
    candidate_hamming_max: int
    tiny_side_px: int
    tiny_file_bytes: int
    extreme_aspect_ratio: float
    repeated_post_min: int
    repeated_author_min: int


def validate_image_algorithm_contract(image: ImageConfig) -> None:
    """校验 v2.4 图片指纹与近同候选的固定算法边界。

    输入是已完成基础类型解析的 :class:`ImageConfig`；函数无返回值、无 I/O 和
    状态变更，可在配置加载及任何仓储写入前重复调用。pHash 必须固定为 64 位
    DCT 配置 `hash_size=8/highfreq_factor=4`，候选汉明距离必须在 1..10；违反
    契约抛出仅含字段语义、不含路径或配置原文的 :class:`ConfigurationError`。
    """

    if image.phash_hash_size != 8:
        raise ConfigurationError("image.phash_hash_size must be 8")
    if image.phash_highfreq_factor != 4:
        raise ConfigurationError("image.phash_highfreq_factor must be 4")
    if not 1 <= image.candidate_hamming_max <= 10:
        raise ConfigurationError("image.candidate_hamming_max must be between 1 and 10")


@dataclass(frozen=True)
class CleaningConfig:
    """校验后的 v2.4 配置及其规范化摘要。"""

    protocol_version: str
    text_label_guide_version: str
    image_label_guide_version: str
    random_seed: int
    input_contract: InputContractConfig
    incremental: IncrementalConfig
    image: ImageConfig
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


def _require_positive_float(value: Any, field: str) -> float:
    """读取严格为正的数值，同时拒绝 YAML 中会伪装为整数的布尔值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigurationError(f"{field} must be a positive number")
    return float(value)


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
    """加载并校验不含运行时路径和敏感字段的版本化 YAML 配置。"""

    config_path = Path(path)
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError("configuration could not be read") from exc

    raw = _require_mapping(parsed, "config")
    _validate_public_values(raw)

    protocol_version = _require_nonempty_string(raw.get("protocol_version"), "protocol_version")
    if protocol_version != "2.4":
        raise ConfigurationError("protocol_version must be 2.4")

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

    image_raw = _require_mapping(raw.get("image"), "image")

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
        "text_relevance",
        "image_role",
        "image_fingerprint",
        "image_noise",
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
    image_config = ImageConfig(
        pillow_version=_require_nonempty_string(
            image_raw.get("pillow_version"), "image.pillow_version"
        ),
        imagehash_version=_require_nonempty_string(
            image_raw.get("imagehash_version"), "image.imagehash_version"
        ),
        phash_hash_size=_require_positive_int(
            image_raw.get("phash_hash_size"), "image.phash_hash_size"
        ),
        phash_highfreq_factor=_require_positive_int(
            image_raw.get("phash_highfreq_factor"), "image.phash_highfreq_factor"
        ),
        candidate_hamming_max=_require_positive_int(
            image_raw.get("candidate_hamming_max"), "image.candidate_hamming_max"
        ),
        tiny_side_px=_require_positive_int(
            image_raw.get("tiny_side_px"), "image.tiny_side_px"
        ),
        tiny_file_bytes=_require_positive_int(
            image_raw.get("tiny_file_bytes"), "image.tiny_file_bytes"
        ),
        extreme_aspect_ratio=_require_positive_float(
            image_raw.get("extreme_aspect_ratio"), "image.extreme_aspect_ratio"
        ),
        repeated_post_min=_require_positive_int(
            image_raw.get("repeated_post_min"), "image.repeated_post_min"
        ),
        repeated_author_min=_require_positive_int(
            image_raw.get("repeated_author_min"), "image.repeated_author_min"
        ),
    )
    validate_image_algorithm_contract(image_config)
    config = CleaningConfig(
        protocol_version=protocol_version,
        text_label_guide_version=_require_nonempty_string(
            raw.get("text_label_guide_version"), "text_label_guide_version"
        ),
        image_label_guide_version=_require_nonempty_string(
            raw.get("image_label_guide_version"), "image_label_guide_version"
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
        image=image_config,
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
    """判断运行时配置是否与创建运行时冻结的配置和协议完全一致。"""

    return config.sha256 == config_sha256 and config.protocol_version == protocol_version
