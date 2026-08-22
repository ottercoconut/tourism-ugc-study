"""确定性文本清洗规则的独立加载、类型化与版本锁校验。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .config import ConfigurationError


@dataclass(frozen=True)
class FullmatchRule:
    """只在全文完全匹配时生效的结构不可用规则。"""

    rule_id: str
    pattern: str


@dataclass(frozen=True)
class NormalizationRules:
    """文本规范化的固定标记和 Unicode 形式。"""

    unicode_form: str
    title_marker: str
    body_marker: str
    url_token: str
    mention_token: str
    topic_open_token: str
    topic_close_token: str
    emoji_token: str


@dataclass(frozen=True)
class StructuredTextRules:
    """结构化正文到纯文本投影的冻结规则。

    Attributes:
        format_id: 可审计的结构格式身份。
        operations_key: 文档中保存顺序操作数组的键。
        insert_key: 单个操作中保存文本或嵌入的键。
        ignored_embed_types: 已确认不提供文本语义、只计数的嵌入类型。
        malformed_policy: 结构损坏时的固定失败策略。
    """

    format_id: str
    operations_key: str
    insert_key: str
    ignored_embed_types: frozenset[str]
    malformed_policy: str


@dataclass(frozen=True)
class StructureRules:
    """结构状态规则；不包含旅游相关性或内容价值判断。"""

    invalid_source_statuses: frozenset[str]
    uncertain_source_statuses: frozenset[str]
    invalid_fullmatch_rules: tuple[FullmatchRule, ...]


@dataclass(frozen=True)
class ExactDuplicateRules:
    """精确重复规范串的形成规则。"""

    unicode_form: str
    casefold: bool
    remove_all_whitespace: bool
    preserve_title_body_boundary: bool


@dataclass(frozen=True)
class NearDuplicateRules:
    """近似候选生成参数；阈值只筛候选，不形成最终重复结论。"""

    analyzer: str
    ngram_range: tuple[int, int]
    min_df: int
    max_df_ratio: float
    sublinear_tf: bool
    smooth_idf: bool
    min_chars: int
    length_ratio_min_ppm: int
    blocking_keys_per_document: int
    blocking_df_ratio_ppm: int
    blocking_df_cap: int
    candidate_threshold_ppm: int
    final_threshold: None


@dataclass(frozen=True)
class TextCleaningConfig:
    """经校验的文本规则，以及可写入结果谱系的文件哈希。"""

    version: str
    normalization: NormalizationRules
    structured_text: StructuredTextRules
    structure: StructureRules
    exact_duplicate: ExactDuplicateRules
    near_duplicate: NearDuplicateRules
    sha256: str

    @property
    def version_lock(self) -> str:
        """返回同时锁定人工版本与原始配置字节的算法标识。"""

        return f"{self.version}+sha256:{self.sha256}"


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{field} must be a boolean")
    return value


def _int(value: Any, field: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError(f"{field} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{field} must be <= {maximum}")
    return value


def _ratio(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{field} must be numeric")
    result = float(value)
    if not 0.0 < result <= 1.0:
        raise ConfigurationError(f"{field} must be in (0, 1]")
    return result


def _string_set(value: Any, field: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{field} must be a non-empty list")
    values = frozenset(_string(item, f"{field} item").casefold() for item in value)
    if len(values) != len(value):
        raise ConfigurationError(f"{field} must not contain duplicates")
    return values


def _fullmatch_rules(value: Any) -> tuple[FullmatchRule, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError("structure.invalid_fullmatch_rules must be a non-empty list")
    rules: list[FullmatchRule] = []
    ids: set[str] = set()
    for index, item in enumerate(value):
        raw = _mapping(item, f"invalid_fullmatch_rules[{index}]")
        rule_id = _string(raw.get("rule_id"), "rule_id")
        pattern = _string(raw.get("pattern"), "pattern")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rule_id) or rule_id in ids:
            raise ConfigurationError("fullmatch rule ids must be unique machine identifiers")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigurationError("invalid fullmatch rule pattern") from exc
        ids.add(rule_id)
        rules.append(FullmatchRule(rule_id, pattern))
    return tuple(rules)


def load_text_config(
    path: str | Path,
    *,
    expected_version_lock: str | None = None,
) -> TextCleaningConfig:
    """加载独立规则文件，并可核对主配置中冻结的版本锁。"""

    config_path = Path(path)
    try:
        payload = config_path.read_bytes()
        parsed = yaml.safe_load(payload.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError("text configuration could not be read") from exc
    root = _mapping(parsed, "text config")
    normalization = _mapping(root.get("normalization"), "normalization")
    structured = _mapping(root.get("structured_text"), "structured_text")
    structure = _mapping(root.get("structure"), "structure")
    exact = _mapping(root.get("exact_duplicate"), "exact_duplicate")
    near = _mapping(root.get("near_duplicate"), "near_duplicate")
    ngram_range = near.get("ngram_range")
    if (
        not isinstance(ngram_range, list)
        or len(ngram_range) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in ngram_range)
        or ngram_range[0] < 1
        or ngram_range[0] > ngram_range[1]
    ):
        raise ConfigurationError("near_duplicate.ngram_range must contain two ordered integers")

    structured_rules = StructuredTextRules(
        format_id=_string(structured.get("format_id"), "structured_text.format_id"),
        operations_key=_string(
            structured.get("operations_key"), "structured_text.operations_key"
        ),
        insert_key=_string(
            structured.get("insert_key"), "structured_text.insert_key"
        ),
        ignored_embed_types=_string_set(
            structured.get("ignored_embed_types"),
            "structured_text.ignored_embed_types",
        ),
        malformed_policy=_string(
            structured.get("malformed_policy"),
            "structured_text.malformed_policy",
        ),
    )
    if structured_rules != StructuredTextRules(
        format_id="quill_delta_json",
        operations_key="ops",
        insert_key="insert",
        ignored_embed_types=frozenset(
            {"article-card", "cut-off", "image", "native-image", "video-card"}
        ),
        malformed_policy="invalid",
    ):
        raise ConfigurationError("structured_text must match the frozen projection contract")
    config = TextCleaningConfig(
        version=_string(root.get("version"), "version"),
        normalization=NormalizationRules(
            unicode_form=_string(normalization.get("unicode_form"), "unicode_form"),
            title_marker=_string(normalization.get("title_marker"), "title_marker"),
            body_marker=_string(normalization.get("body_marker"), "body_marker"),
            url_token=_string(normalization.get("url_token"), "url_token"),
            mention_token=_string(normalization.get("mention_token"), "mention_token"),
            topic_open_token=_string(
                normalization.get("topic_open_token"), "topic_open_token"
            ),
            topic_close_token=_string(
                normalization.get("topic_close_token"), "topic_close_token"
            ),
            emoji_token=_string(normalization.get("emoji_token"), "emoji_token"),
        ),
        structured_text=structured_rules,
        structure=StructureRules(
            invalid_source_statuses=_string_set(
                structure.get("invalid_source_statuses"), "invalid_source_statuses"
            ),
            uncertain_source_statuses=_string_set(
                structure.get("uncertain_source_statuses"), "uncertain_source_statuses"
            ),
            invalid_fullmatch_rules=_fullmatch_rules(
                structure.get("invalid_fullmatch_rules")
            ),
        ),
        exact_duplicate=ExactDuplicateRules(
            unicode_form=_string(exact.get("unicode_form"), "exact_duplicate.unicode_form"),
            casefold=_bool(exact.get("casefold"), "exact_duplicate.casefold"),
            remove_all_whitespace=_bool(
                exact.get("remove_all_whitespace"), "exact_duplicate.remove_all_whitespace"
            ),
            preserve_title_body_boundary=_bool(
                exact.get("preserve_title_body_boundary"),
                "exact_duplicate.preserve_title_body_boundary",
            ),
        ),
        near_duplicate=NearDuplicateRules(
            analyzer=_string(near.get("analyzer"), "near_duplicate.analyzer"),
            ngram_range=(int(ngram_range[0]), int(ngram_range[1])),
            min_df=_int(near.get("min_df"), "near_duplicate.min_df"),
            max_df_ratio=_ratio(near.get("max_df_ratio"), "near_duplicate.max_df_ratio"),
            sublinear_tf=_bool(near.get("sublinear_tf"), "near_duplicate.sublinear_tf"),
            smooth_idf=_bool(near.get("smooth_idf"), "near_duplicate.smooth_idf"),
            min_chars=_int(near.get("min_chars"), "near_duplicate.min_chars"),
            length_ratio_min_ppm=_int(
                near.get("length_ratio_min_ppm"),
                "near_duplicate.length_ratio_min_ppm",
                maximum=1_000_000,
            ),
            blocking_keys_per_document=_int(
                near.get("blocking_keys_per_document"), "blocking_keys_per_document"
            ),
            blocking_df_ratio_ppm=_int(
                near.get("blocking_df_ratio_ppm"),
                "blocking_df_ratio_ppm",
                maximum=1_000_000,
            ),
            blocking_df_cap=_int(near.get("blocking_df_cap"), "blocking_df_cap"),
            candidate_threshold_ppm=_int(
                near.get("candidate_threshold_ppm"),
                "candidate_threshold_ppm",
                maximum=1_000_000,
            ),
            final_threshold=None,
        ),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    if config.normalization.unicode_form != "NFKC" or config.exact_duplicate.unicode_form != "NFKC":
        raise ConfigurationError("text Unicode forms must be NFKC")
    if config.near_duplicate.analyzer != "char":
        raise ConfigurationError("near_duplicate.analyzer must be char")
    if near.get("final_threshold") is not None:
        raise ConfigurationError("near_duplicate.final_threshold must remain null")
    if expected_version_lock is not None and config.version_lock != expected_version_lock:
        raise ConfigurationError("text configuration version lock mismatch")
    return config
