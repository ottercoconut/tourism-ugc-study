"""V0作者身份共同校准和独立盲试标的汇总与信度报告。

共同校准模式只报告逐字段分歧、UNK和人工角色支持数，不把讨论样本包装成
正式信度。盲试标模式才对新作者样本计算名义Krippendorff's alpha、作者级
bootstrap 95%置信区间、类别支持和混淆矩阵。
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from tourism_ugc_study.annotation.role_pilot import (
    ACTOR_SCOPES,
    CE_CODES,
    CODEBOOK_VERSION,
    CONTENT_VERTICALS,
    EA_CODES,
    ROLE_RULE_VERSION,
    RAW_ROLE_VALUES,
    RolePilotError,
)


COMPONENT_VALUES = frozenset({"0", "1", "UNK", "NA"})
EVIDENCE_STATUS_VALUES = frozenset({"SUFFICIENT", "INSUFFICIENT", "OUT_OF_SCOPE"})


@dataclass(frozen=True)
class ReliabilityResult:
    """一个名义尺度字段的信度、区间与支持信息。"""

    field: str
    alpha: float | None
    ci95_low: float | None
    ci95_high: float | None
    author_count: int
    support_by_coder: Mapping[str, Mapping[str, int]]
    confusion_matrix: Mapping[str, Mapping[str, int]]
    gate_status: str


def _read_csv(path: Path) -> tuple[dict[str, str], ...]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return tuple(dict(row) for row in csv.DictReader(stream))


def _parse_codes(value: str) -> tuple[str, ...]:
    return tuple(sorted({item.strip() for item in value.split("|") if item.strip()}))


def _confidence(row: Mapping[str, str], field: str) -> int:
    try:
        value = int(row.get(field, ""))
    except ValueError as error:
        raise RolePilotError(f"{field}必须为1—5整数") from error
    if value not in {1, 2, 3, 4, 5}:
        raise RolePilotError(f"{field}必须为1—5整数")
    return value


def validate_completed_role_rows(
    rows: Sequence[Mapping[str, str]], *, expected_annotator: str | None = None
) -> None:
    """校验一位编码员的V0表已完成且仍遵守封闭值域。"""

    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        author_id = row.get("author_snapshot_id", "").strip()
        if not author_id or author_id in seen:
            raise RolePilotError(f"第{row_number}行作者ID缺失或重复")
        seen.add(author_id)
        if expected_annotator is not None and row.get("annotator_id") != expected_annotator:
            raise RolePilotError(f"第{row_number}行annotator_id不匹配")
        if row.get("actor_scope") not in ACTOR_SCOPES:
            raise RolePilotError(f"第{row_number}行actor_scope未完成或越界")
        if row.get("content_vertical") not in CONTENT_VERTICALS:
            raise RolePilotError(f"第{row_number}行content_vertical未完成或越界")
        ea_codes = set(_parse_codes(row.get("expert_authority_criterion_codes", "")))
        ce_codes = set(_parse_codes(row.get("consumer_experience_criterion_codes", "")))
        if not ea_codes or not ea_codes <= EA_CODES:
            raise RolePilotError(f"第{row_number}行EA代码未完成或越界")
        if not ce_codes or not ce_codes <= CE_CODES:
            raise RolePilotError(f"第{row_number}行CE代码未完成或越界")
        for field in (
            "ev_expert_authority",
            "ev_consumer_experience",
            "ev_sustained_creation",
        ):
            if row.get(field) not in COMPONENT_VALUES:
                raise RolePilotError(f"第{row_number}行{field}未完成或越界")
        if row.get("evidence_status") not in EVIDENCE_STATUS_VALUES:
            raise RolePilotError(f"第{row_number}行evidence_status未完成或越界")
        if row.get("creator_role_manual") not in RAW_ROLE_VALUES:
            raise RolePilotError(f"第{row_number}行人工角色未完成或越界")
        if row.get("community_relation_status") != "UNAVAILABLE":
            raise RolePilotError(f"第{row_number}行CI必须固定为UNAVAILABLE")
        for confidence_field in (
            "actor_scope_confidence",
            "content_vertical_confidence",
            "expert_authority_confidence",
            "ev_expert_authority_confidence",
            "consumer_experience_confidence",
            "ev_consumer_experience_confidence",
            "ev_sustained_creation_confidence",
            "evidence_status_confidence",
            "creator_role_manual_confidence",
        ):
            confidence = _confidence(row, confidence_field)
            if confidence <= 2 and not row.get("low_confidence_note", "").strip():
                raise RolePilotError(f"第{row_number}行低置信判断缺少备注")
        if row.get("role_rule_version") != ROLE_RULE_VERSION:
            raise RolePilotError(f"第{row_number}行角色规则版本不匹配")
        if row.get("codebook_version") != CODEBOOK_VERSION:
            raise RolePilotError(f"第{row_number}行编码表版本不匹配")


def _nominal_alpha(pairs: Sequence[tuple[str, str]]) -> float | None:
    """计算两名编码员、完整配对数据的名义Krippendorff's alpha。"""

    if not pairs:
        return None
    observed = sum(left != right for left, right in pairs) / len(pairs)
    pooled = Counter(value for pair in pairs for value in pair)
    total = sum(pooled.values())
    if total < 2:
        return None
    agreement_chance = sum(count * (count - 1) for count in pooled.values()) / (
        total * (total - 1)
    )
    expected = 1 - agreement_chance
    if expected == 0:
        return 1.0 if observed == 0 else None
    return 1 - observed / expected


def reliability_for_pairs(
    field: str,
    pairs: Sequence[tuple[str, str]],
    *,
    coder_ids: tuple[str, str],
    bootstrap_samples: int = 2000,
    seed: int = 20260824,
) -> ReliabilityResult:
    """计算alpha、作者级bootstrap区间、支持数和混淆矩阵。"""

    alpha = _nominal_alpha(pairs)
    randomizer = random.Random(seed)
    bootstrap: list[float] = []
    if pairs:
        for _ in range(bootstrap_samples):
            sample = tuple(pairs[randomizer.randrange(len(pairs))] for _ in pairs)
            value = _nominal_alpha(sample)
            if value is not None:
                bootstrap.append(value)
    bootstrap.sort()
    if bootstrap:
        low = bootstrap[math_index(len(bootstrap), 0.025)]
        high = bootstrap[math_index(len(bootstrap), 0.975)]
    else:
        low = high = None
    support = {
        coder_ids[0]: dict(sorted(Counter(left for left, _ in pairs).items())),
        coder_ids[1]: dict(sorted(Counter(right for _, right in pairs).items())),
    }
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for left, right in pairs:
        confusion[left][right] += 1
    if alpha is None:
        status = "INSUFFICIENT_VARIATION"
    elif alpha < 0.80:
        status = "RETURN_TO_CALIBRATION"
    else:
        status = "PASS_ALPHA_GATE"
    return ReliabilityResult(
        field=field,
        alpha=alpha,
        ci95_low=low,
        ci95_high=high,
        author_count=len(pairs),
        support_by_coder=support,
        confusion_matrix={key: dict(sorted(value.items())) for key, value in sorted(confusion.items())},
        gate_status=status,
    )


def math_index(length: int, quantile: float) -> int:
    """返回保守的经验分位数索引并限制在有效范围。"""

    return min(max(int(round((length - 1) * quantile)), 0), length - 1)


def summarize_role_round(
    coder_a_path: Path,
    coder_b_path: Path,
    coverage_path: Path,
    *,
    mode: str,
    bootstrap_samples: int = 2000,
    seed: int = 20260824,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """汇总共同校准或独立盲试标，不修改两份原始编码。"""

    if mode not in {"CALIBRATION", "BLIND_PILOT"}:
        raise RolePilotError("mode必须为CALIBRATION或BLIND_PILOT")
    rows_a = _read_csv(coder_a_path)
    rows_b = _read_csv(coder_b_path)
    if not rows_a or not rows_b:
        raise RolePilotError("两份角色编码均须完成后再汇总")
    coder_a = rows_a[0].get("annotator_id", "")
    coder_b = rows_b[0].get("annotator_id", "")
    if not coder_a or not coder_b or coder_a == coder_b:
        raise RolePilotError("两份原始编码必须来自不同匿名编码员")
    validate_completed_role_rows(rows_a, expected_annotator=coder_a)
    validate_completed_role_rows(rows_b, expected_annotator=coder_b)
    by_a = {row["author_snapshot_id"]: row for row in rows_a}
    by_b = {row["author_snapshot_id"]: row for row in rows_b}
    if by_a.keys() != by_b.keys():
        raise RolePilotError("两位编码员的作者成员清单不一致")
    coverage = {
        row["author_snapshot_id"]: row for row in _read_csv(coverage_path)
    }
    if by_a.keys() != coverage.keys():
        raise RolePilotError("coverage-derived与编码成员清单不一致")

    discrepancies: list[dict[str, object]] = []
    manual_role_support: dict[str, Counter[str]] = {
        coder_a: Counter(),
        coder_b: Counter(),
    }
    unk_counts: dict[str, Counter[str]] = {coder_a: Counter(), coder_b: Counter()}
    for author_id in sorted(by_a):
        rows = {coder_a: by_a[author_id], coder_b: by_b[author_id]}
        for coder, row in rows.items():
            manual_role_support[coder][row["creator_role_manual"]] += 1
            for component, value in (
                ("EA", row["ev_expert_authority"]),
                ("CE", row["ev_consumer_experience"]),
                ("SC", row["ev_sustained_creation"]),
                ("EVIDENCE", row["evidence_status"]),
                ("ROLE", row["creator_role_manual"]),
            ):
                if value in {"UNK", "INSUFFICIENT"}:
                    unk_counts[coder][component] += 1
        for field in (
            "actor_scope",
            "content_vertical",
            "expert_authority_criterion_codes",
            "ev_expert_authority",
            "consumer_experience_criterion_codes",
            "ev_consumer_experience",
            "ev_sustained_creation",
            "evidence_status",
            "creator_role_manual",
        ):
            left = by_a[author_id][field]
            right = by_b[author_id][field]
            if field.endswith("criterion_codes"):
                left = "|".join(_parse_codes(left))
                right = "|".join(_parse_codes(right))
            if left != right:
                discrepancies.append(
                    {
                        "author_snapshot_id": author_id,
                        "field": field,
                        "coder_a": coder_a,
                        "coder_a_value": left,
                        "coder_b": coder_b,
                        "coder_b_value": right,
                        "status": "OPEN",
                    }
                )

    reliability: list[ReliabilityResult] = []
    if mode == "BLIND_PILOT":
        pairs_by_field: dict[str, list[tuple[str, str]]] = {
            "actor_scope": [],
            "content_vertical": [],
            "ev_expert_authority": [],
            "ev_consumer_experience": [],
            "ev_sustained_creation": [],
            "evidence_status": [],
            "creator_role_manual": [],
        }
        for code in sorted(EA_CODES):
            pairs_by_field[f"EA::{code}"] = []
        for code in sorted(CE_CODES):
            pairs_by_field[f"CE::{code}"] = []
        for author_id in sorted(by_a):
            pairs_by_field["actor_scope"].append(
                (by_a[author_id]["actor_scope"], by_b[author_id]["actor_scope"])
            )
            pairs_by_field["content_vertical"].append(
                (
                    by_a[author_id]["content_vertical"],
                    by_b[author_id]["content_vertical"],
                )
            )
            for field in (
                "ev_expert_authority",
                "ev_consumer_experience",
                "ev_sustained_creation",
                "evidence_status",
                "creator_role_manual",
            ):
                pairs_by_field[field].append(
                    (by_a[author_id][field], by_b[author_id][field])
                )
            ea_a = set(_parse_codes(by_a[author_id]["expert_authority_criterion_codes"]))
            ea_b = set(_parse_codes(by_b[author_id]["expert_authority_criterion_codes"]))
            ce_a = set(_parse_codes(by_a[author_id]["consumer_experience_criterion_codes"]))
            ce_b = set(_parse_codes(by_b[author_id]["consumer_experience_criterion_codes"]))
            for code in sorted(EA_CODES):
                pairs_by_field[f"EA::{code}"].append(
                    (str(int(code in ea_a)), str(int(code in ea_b)))
                )
            for code in sorted(CE_CODES):
                pairs_by_field[f"CE::{code}"].append(
                    (str(int(code in ce_a)), str(int(code in ce_b)))
                )
        reliability = [
            reliability_for_pairs(
                field,
                pairs,
                coder_ids=(coder_a, coder_b),
                bootstrap_samples=bootstrap_samples,
                seed=seed + index,
            )
            for index, (field, pairs) in enumerate(sorted(pairs_by_field.items()))
        ]

    summary: dict[str, object] = {
        "mode": mode,
        "formal_reliability_claim_allowed": mode == "BLIND_PILOT",
        "author_count": len(by_a),
        "coder_ids": [coder_a, coder_b],
        "disagreement_count": len(discrepancies),
        "unk_counts": {
            coder: dict(sorted(counts.items())) for coder, counts in unk_counts.items()
        },
        "manual_role_support": {
            coder: dict(sorted(counts.items()))
            for coder, counts in manual_role_support.items()
        },
        "reliability": [asdict(item) for item in reliability],
        "gate": (
            "CALIBRATION_DISCUSSION_ONLY"
            if mode == "CALIBRATION"
            else (
                "RETURN_TO_CALIBRATION"
                if any(item.gate_status != "PASS_ALPHA_GATE" for item in reliability)
                else "PASS_ALPHA_GATE"
            )
        ),
    }
    return summary, tuple(discrepancies)


def write_role_summary(
    summary_path: Path,
    discrepancies_path: Path,
    summary: Mapping[str, object],
    discrepancies: Iterable[Mapping[str, object]],
) -> None:
    """新增汇总与分歧文件；既有输出存在时拒绝覆盖。"""

    if summary_path.exists() or discrepancies_path.exists():
        raise RolePilotError("汇总输出已存在；为保护审计轨迹，拒绝覆盖")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = tuple(discrepancies)
    fields = (
        "author_snapshot_id",
        "field",
        "coder_a",
        "coder_a_value",
        "coder_b",
        "coder_b_value",
        "status",
    )
    with discrepancies_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
