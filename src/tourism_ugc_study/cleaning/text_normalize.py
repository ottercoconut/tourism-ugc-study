"""纯函数形式的文本规范化、结构检查与精确重复键计算。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

import regex

from .text_config import TextCleaningConfig
from .text_runtime import text_runtime_sha256


_URL_PATTERN = regex.compile(
    r"(?i)(?:https?://|www\.)[^\s<>\[\]{}（）()，。！？；：、、“”‘’,!;@]+"
)
_TOPIC_PATTERN = regex.compile(r"#([^#\n]{1,100})#")
_MENTION_PATTERN = regex.compile(r"(?<![\p{L}\p{N}_.+-])@[\p{L}\p{N}_·.-]+")
_GRAPHEME_PATTERN = regex.compile(r"\X")
_PICTOGRAPH_PATTERN = regex.compile(r"\p{Extended_Pictographic}")
_HORIZONTAL_WHITESPACE = regex.compile(r"[^\S\n]+")
_EXCESS_NEWLINES = re.compile(r"\n{3,}")
_ONLY_REPLACEMENT_TOKENS = re.compile(
    r"(?:\[(?:URL|MENTION|EMOJI)\]|\s)+",
    re.DOTALL,
)


@dataclass(frozen=True)
class NormalizedText:
    """单条帖子的确定性文本结果，不承载科研标签或最终保留决策。"""

    normalized_title: str
    normalized_body: str
    model_text: str
    normalized_sha256: str
    exact_canonical_sha256: str | None
    structure_status: str
    structure_reason_code: str
    evidence: Mapping[str, object]
    output_sha256: str


@dataclass(frozen=True)
class StructuredTextProjection:
    """一个源字段经结构识别后的纯文本投影及去敏证据。

    Attributes:
        text: 供后续通用规范化使用的纯文本或原始普通值。
        format_id: 识别出的结构格式；普通字段为 ``plain_text``。
        status: ``usable`` 或失败关闭的 ``invalid``。
        reason_code: 稳定结构判断原因，不包含正文。
        text_insert_count: 被顺序拼接的文本操作数量。
        ignored_embed_count: 已声明且被忽略的非文本嵌入数量。
    """

    text: object
    format_id: str
    status: str
    reason_code: str
    text_insert_count: int
    ignored_embed_count: int


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_newlines(value: str) -> str:
    return (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x85", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )


def project_structured_text(
    value: object,
    config: TextCleaningConfig,
) -> StructuredTextProjection:
    """把受支持的 Delta JSON 字段确定性投影为纯文本。

    Args:
        value: 源标题或正文；普通值保持原样。
        config: 冻结文本规则，声明结构键和可忽略嵌入类型。

    Returns:
        纯文本或原始普通值，以及不含正文的结构证据。

    Notes:
        仅当对象声明 ``ops`` 时才视为结构化正文。文本 ``insert`` 按原顺序
        拼接；图片和截断节点不提供文本信息，因此只计数而不生成模型 token。
        疑似 Delta 但无法解析、操作结构非法或包含未知嵌入时失败关闭。
    """

    if not isinstance(value, str):
        return StructuredTextProjection(value, "plain_text", "usable", "plain_text", 0, 0)
    # 采集字段可能在 JSON 前带 BOM 或零宽格式符；这些字符不是正文，也不能
    # 让结构文档降级为普通文本。这里只移动探测起点，不改写 JSON 内部字符。
    structured_start = 0
    while structured_start < len(value):
        character = value[structured_start]
        if character.isspace() or unicodedata.category(character) in {"Cf", "Cc"}:
            structured_start += 1
            continue
        break
    stripped = value[structured_start:].strip()
    operations_key = config.structured_text.operations_key
    marker_pattern = re.compile(rf'["\']{re.escape(operations_key)}["\']\s*:')
    if not stripped.startswith("{"):
        return StructuredTextProjection(value, "plain_text", "usable", "plain_text", 0, 0)
    try:
        parsed: Any = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        if marker_pattern.search(stripped):
            return StructuredTextProjection(
                "", config.structured_text.format_id, "invalid",
                "structured_text_malformed", 0, 0,
            )
        return StructuredTextProjection(value, "plain_text", "usable", "plain_text", 0, 0)
    if not isinstance(parsed, Mapping) or operations_key not in parsed:
        return StructuredTextProjection(value, "plain_text", "usable", "plain_text", 0, 0)
    operations = parsed.get(operations_key)
    if not isinstance(operations, list):
        return StructuredTextProjection(
            "", config.structured_text.format_id, "invalid",
            "structured_text_operations_invalid", 0, 0,
        )
    pieces: list[str] = []
    text_count = 0
    embed_count = 0
    for operation in operations:
        if (
            not isinstance(operation, Mapping)
            or config.structured_text.insert_key not in operation
            or set(operation) - {config.structured_text.insert_key, "attributes"}
            or (
                "attributes" in operation
                and not isinstance(operation["attributes"], Mapping)
            )
        ):
            return StructuredTextProjection(
                "", config.structured_text.format_id, "invalid",
                "structured_text_operation_invalid", text_count, embed_count,
            )
        insert = operation[config.structured_text.insert_key]
        if isinstance(insert, str):
            pieces.append(insert)
            text_count += 1
            continue
        if isinstance(insert, Mapping) and len(insert) == 1:
            embed_type = next(iter(insert))
            if str(embed_type) in config.structured_text.ignored_embed_types:
                embed_count += 1
                continue
        return StructuredTextProjection(
            "", config.structured_text.format_id, "invalid",
            "structured_text_embed_unsupported", text_count, embed_count,
        )
    projected_text = "".join(pieces)
    substantive_text, _ = _strip_controls(projected_text)
    if not substantive_text.strip():
        return StructuredTextProjection(
            "", config.structured_text.format_id, "invalid",
            "structured_text_no_text", text_count, embed_count,
        )
    return StructuredTextProjection(
        projected_text, config.structured_text.format_id, "usable",
        "structured_text_extracted", text_count, embed_count,
    )


def _strip_controls(value: str) -> tuple[str, int]:
    """移除剩余格式控制符和孤立变体选择符，但保留规范换行。"""

    kept: list[str] = []
    removed = 0
    for character in value:
        category = unicodedata.category(character)
        if character == "\n":
            kept.append(character)
        elif character.isspace():
            # 制表符等水平空白需进入后续折叠步骤，不能与零宽控制符同样删除。
            kept.append(" ")
        elif category in {"Cf", "Cc"} or character in {"\ufe0e", "\ufe0f"}:
            removed += 1
        else:
            kept.append(character)
    return "".join(kept), removed


def _replace_emoji(value: str, token: str) -> tuple[str, int]:
    parts: list[str] = []
    count = 0
    for grapheme in _GRAPHEME_PATTERN.findall(value):
        if _PICTOGRAPH_PATTERN.search(grapheme):
            parts.append(token)
            count += 1
        else:
            parts.append(grapheme)
    return "".join(parts), count


def _normalize_field(value: object, config: TextCleaningConfig) -> tuple[str, dict[str, int]]:
    """按固定顺序规范化一个字段，并返回不含正文的替换计数。"""

    rules = config.normalization
    text = "" if value is None else str(value)
    text = unicodedata.normalize(rules.unicode_form, _normalize_newlines(text))
    counts = {"url": 0, "mention": 0, "topic": 0, "emoji": 0, "control": 0}
    def replace_url(match: regex.Match[str]) -> str:
        matched = match.group(0)
        suffix = matched[len(matched.rstrip(".,!;:")) :]
        return rules.url_token + suffix

    text, counts["url"] = _URL_PATTERN.subn(replace_url, text)

    def replace_topic(match: regex.Match[str]) -> str:
        topic = _HORIZONTAL_WHITESPACE.sub(" ", match.group(1).strip())
        return f"{rules.topic_open_token}{topic}{rules.topic_close_token}"

    text, counts["topic"] = _TOPIC_PATTERN.subn(replace_topic, text)
    def replace_mention(match: regex.Match[str]) -> str:
        matched = match.group(0)
        suffix = matched[len(matched.rstrip(".-")) :]
        return rules.mention_token + suffix

    text, counts["mention"] = _MENTION_PATTERN.subn(replace_mention, text)
    text, counts["emoji"] = _replace_emoji(text, rules.emoji_token)
    text, counts["control"] = _strip_controls(text)
    text = _HORIZONTAL_WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _EXCESS_NEWLINES.sub("\n\n", text).strip()
    return text, counts


def _exact_field(value: object, config: TextCleaningConfig) -> str:
    """形成不做语义替换的精确重复字段，避免 URL 或作者被错误合并。"""

    rules = config.exact_duplicate
    text = "" if value is None else str(value)
    text = unicodedata.normalize(rules.unicode_form, _normalize_newlines(text))
    text, _ = _strip_controls(text)
    if rules.casefold:
        text = text.casefold()
    if rules.remove_all_whitespace:
        text = "".join(text.split())
    return text


def _structure_status(
    title: str,
    body: str,
    source_status: object,
    config: TextCleaningConfig,
) -> tuple[str, str]:
    """依据结构事实三分状态；短文本和无标题本身都不构成不可用。"""

    status = "" if source_status is None else str(source_status).strip().casefold()
    if not title and not body:
        return "invalid", "empty_title_and_body"
    if status in config.structure.invalid_source_statuses:
        return "invalid", "invalid_source_status"
    combined = "\n".join(part for part in (title, body) if part)
    for rule in config.structure.invalid_fullmatch_rules:
        if re.fullmatch(rule.pattern, combined, flags=re.IGNORECASE):
            return "invalid", rule.rule_id
    if status in config.structure.uncertain_source_statuses:
        return "uncertain", "uncertain_source_status"
    if _ONLY_REPLACEMENT_TOKENS.fullmatch(combined):
        return "uncertain", "replacement_tokens_only"
    return "usable", "structure_usable"


def normalize_post_text(
    title: object,
    body: object,
    *,
    source_status: object = None,
    config: TextCleaningConfig,
) -> NormalizedText:
    """投影并规范化一条帖子，生成稳定模型文本与结构证据。

    Args:
        title: 源标题；可为普通文本、Quill Delta JSON 或空值。
        body: 源正文；可为普通文本、Quill Delta JSON 或空值。
        source_status: 上游结构状态；仅按冻结状态集合参与结构判定。
        config: 已校验的冻结文本清洗配置。

    Returns:
        包含规范化标题、正文、模型文本、精确重复哈希、三分结构状态、
        去敏计数证据和内容寻址输出哈希的不可变结果。

    Notes:
        未知或损坏的结构化正文不会回退为原始 JSON 字符串，而是返回
        ``structure_status="invalid"`` 及稳定 reason code；空标题或短文本
        本身不构成失败。函数不读取平台字段，也不产生自动清洗决定。
    """

    title_projection = project_structured_text(title, config)
    body_projection = project_structured_text(body, config)
    normalized_title, title_counts = _normalize_field(title_projection.text, config)
    normalized_body, body_counts = _normalize_field(body_projection.text, config)
    model_text = (
        f"{config.normalization.title_marker}\n{normalized_title}\n"
        f"{config.normalization.body_marker}\n{normalized_body}"
    )
    failed_projection = next(
        (
            projection
            for projection in (title_projection, body_projection)
            if projection.status == "invalid"
        ),
        None,
    )
    if failed_projection is None:
        status, reason_code = _structure_status(
            normalized_title,
            normalized_body,
            source_status,
            config,
        )
    else:
        status, reason_code = "invalid", failed_projection.reason_code
    exact_title = _exact_field(title_projection.text, config)
    exact_body = _exact_field(body_projection.text, config)
    exact_canonical = f"T:{exact_title}\x1fB:{exact_body}"
    exact_sha256 = _sha256(exact_canonical) if status == "usable" else None
    replacement_counts = {
        name: title_counts[name] + body_counts[name]
        for name in sorted(title_counts)
    }
    evidence: dict[str, object] = {
        "title_present": bool(normalized_title),
        "body_present": bool(normalized_body),
        "normalized_title_length": len(normalized_title),
        "normalized_body_length": len(normalized_body),
        "replacement_counts": replacement_counts,
        "structure_rule_id": reason_code,
        "structured_text": {
            "title": {
                "format_id": title_projection.format_id,
                "status": title_projection.status,
                "reason_code": title_projection.reason_code,
                "text_insert_count": title_projection.text_insert_count,
                "ignored_embed_count": title_projection.ignored_embed_count,
            },
            "body": {
                "format_id": body_projection.format_id,
                "status": body_projection.status,
                "reason_code": body_projection.reason_code,
                "text_insert_count": body_projection.text_insert_count,
                "ignored_embed_count": body_projection.ignored_embed_count,
            },
        },
    }
    normalized_sha256 = _sha256(model_text)
    output_payload = {
        "config_sha256": config.sha256,
        "exact_canonical_sha256": exact_sha256,
        "evidence": evidence,
        "normalized_sha256": normalized_sha256,
        "runtime_sha256": text_runtime_sha256(),
        "structure_reason_code": reason_code,
        "structure_status": status,
    }
    output_sha256 = _sha256(
        json.dumps(output_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return NormalizedText(
        normalized_title=normalized_title,
        normalized_body=normalized_body,
        model_text=model_text,
        normalized_sha256=normalized_sha256,
        exact_canonical_sha256=exact_sha256,
        structure_status=status,
        structure_reason_code=reason_code,
        evidence=evidence,
        output_sha256=output_sha256,
    )
