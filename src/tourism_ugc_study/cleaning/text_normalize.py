"""纯函数形式的文本规范化、结构检查与精确重复键计算。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

import regex

from .text_config import TextCleaningConfig


_URL_PATTERN = regex.compile(
    r"(?i)(?:https?://|www\.)[^\s<>\[\]{}（）()，。！？；：、、“”‘’,!;:@]+"
)
_TOPIC_PATTERN = regex.compile(r"#([^#\n]{1,100})#")
_MENTION_PATTERN = regex.compile(r"(?<![\p{L}\p{N}_.+-])@[\p{L}\p{N}_·.-]+")
_GRAPHEME_PATTERN = regex.compile(r"\X")
_PICTOGRAPH_PATTERN = regex.compile(r"\p{Extended_Pictographic}")
_HORIZONTAL_WHITESPACE = regex.compile(r"[^\S\n]+")
_EXCESS_NEWLINES = re.compile(r"\n{3,}")
_ONLY_REPLACEMENT_TOKENS = re.compile(
    r"(?:\[(?:URL|MENTION|EMOJI)\]|\[TOPIC\].*?\[/TOPIC\]|\s)+",
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
    text, counts["url"] = _URL_PATTERN.subn(rules.url_token, text)

    def replace_topic(match: regex.Match[str]) -> str:
        topic = _HORIZONTAL_WHITESPACE.sub(" ", match.group(1).strip())
        return f"{rules.topic_open_token}{topic}{rules.topic_close_token}"

    text, counts["topic"] = _TOPIC_PATTERN.subn(replace_topic, text)
    text, counts["mention"] = _MENTION_PATTERN.subn(rules.mention_token, text)
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
    """规范化一条帖子，并生成稳定哈希、三分结构状态和去敏证据。"""

    normalized_title, title_counts = _normalize_field(title, config)
    normalized_body, body_counts = _normalize_field(body, config)
    model_text = (
        f"{config.normalization.title_marker}\n{normalized_title}\n"
        f"{config.normalization.body_marker}\n{normalized_body}"
    )
    status, reason_code = _structure_status(
        normalized_title,
        normalized_body,
        source_status,
        config,
    )
    exact_title = _exact_field(title, config)
    exact_body = _exact_field(body, config)
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
    }
    normalized_sha256 = _sha256(model_text)
    output_payload = {
        "config_sha256": config.sha256,
        "exact_canonical_sha256": exact_sha256,
        "evidence": evidence,
        "normalized_sha256": normalized_sha256,
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
