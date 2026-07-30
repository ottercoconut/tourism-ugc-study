"""双人盲标的原始一致率、Cohen's kappa 与追加双标门槛。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived

from .config import AnnotationConfig


@dataclass(frozen=True)
class AxisAgreement:
    """单个判断轴的成对样本量、一致率和机会校正一致性。"""

    paired_count: int
    raw_agreement: float
    cohen_kappa: float
    meets_threshold: bool


@dataclass(frozen=True)
class AgreementReport:
    """三个独立判断轴的一致性和自动追加双标结论。"""

    structure: AxisAgreement
    tourism: AxisAgreement
    commercial: AxisAgreement
    additional_double_label_required: int


def _kappa(left: list[str], right: list[str]) -> tuple[float, float]:
    """计算多类别 Cohen's kappa，并定义常量完全一致时 κ=1。"""

    if not left or len(left) != len(right):
        raise ValueError("paired labels are required")
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    left_counts, right_counts = Counter(left), Counter(right)
    labels = set(left_counts) | set(right_counts)
    expected = sum(
        left_counts[label] / len(left) * right_counts[label] / len(right)
        for label in labels
    )
    if expected == 1.0:
        return observed, 1.0 if observed == 1.0 else 0.0
    return observed, (observed - expected) / (1.0 - expected)


def calculate_agreement(
    records: Iterable[Mapping[str, object]],
    *,
    config: AnnotationConfig,
) -> AgreementReport:
    """只使用 assignment slot 1/2 的完整配对，分别计算三个标签轴。

    同一帖子同一槽位出现多条记录意味着盲标输入不唯一，函数会拒绝而不是
    通过“最新一条”静默覆盖。任一轴未过门槛即建议追加固定 100 条双标。
    """

    grouped: dict[tuple[int, int], dict[int, Mapping[str, object]]] = defaultdict(dict)
    for record in records:
        slot = int(record["assignment_slot"])
        if slot not in (1, 2):
            continue
        identity = (int(record["source_post_id"]), int(record["source_version"]))
        if slot in grouped[identity]:
            raise ValueError("duplicate annotation assignment slot")
        grouped[identity][slot] = record
    paired = [slots for slots in grouped.values() if set(slots) == {1, 2}]
    if not paired:
        raise ValueError("no complete double-label pairs")

    def axis(field: str) -> AxisAgreement:
        left = [str(slots[1][field]) for slots in paired]
        right = [str(slots[2][field]) for slots in paired]
        raw, kappa = _kappa(left, right)
        return AxisAgreement(
            paired_count=len(paired),
            raw_agreement=raw,
            cohen_kappa=kappa,
            meets_threshold=(
                raw >= config.minimum_raw_agreement
                and kappa >= config.minimum_cohen_kappa
            ),
        )

    structure = axis("structure_label")
    tourism = axis("tourism_label")
    commercial = axis("commercial_label")
    additional = (
        0
        if all(item.meets_threshold for item in (structure, tourism, commercial))
        else config.additional_double_label_size
    )
    return AgreementReport(structure, tourism, commercial, additional)


def agreement_report(
    derived_db: str,
    *,
    sample_run_id: str,
    config: AnnotationConfig,
) -> AgreementReport:
    """从追加式原始标注生成报告；仲裁标签不参与标注者一致性。"""

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        rows = connection.execute(
            """
            SELECT source_post_id, source_version, assignment_slot,
                   structure_label, tourism_label, commercial_label
            FROM text_post_annotations
            WHERE sample_run_id = ? AND assignment_slot IN (1, 2)
            ORDER BY source_post_id, source_version, assignment_slot
            """,
            (sample_run_id,),
        ).fetchall()
    return calculate_agreement(rows, config=config)
