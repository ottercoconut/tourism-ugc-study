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


@dataclass(frozen=True)
class AgreementCompletion:
    """计划双标配对的完成状态；未完成时不产生一致性通过结论。"""

    planned_pair_count: int
    complete_pair_count: int
    is_complete: bool
    report: AgreementReport | None


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


def evaluate_planned_agreement(
    records: Iterable[Mapping[str, object]],
    *,
    planned_identities: Iterable[tuple[int, int]],
    config: AnnotationConfig,
) -> AgreementCompletion:
    """核对全部计划 pair 后才计算一致性。

    每个计划对象必须恰好存在 slot 1/2 各一条，且两条记录来自不同标注者。
    任一 pair 未完成时返回 `report=None`，调用方不得据部分记录判定通过。
    """

    planned = tuple(sorted(set(planned_identities)))
    if not planned:
        raise ValueError("double-label plan is empty")
    planned_set = set(planned)
    grouped: dict[tuple[int, int], dict[int, Mapping[str, object]]] = defaultdict(dict)
    for record in records:
        identity = (int(record["source_post_id"]), int(record["source_version"]))
        if identity not in planned_set:
            continue
        slot = int(record["assignment_slot"])
        if slot not in (1, 2):
            continue
        if slot in grouped[identity]:
            raise ValueError("duplicate annotation assignment slot")
        grouped[identity][slot] = record
    complete_records: list[Mapping[str, object]] = []
    complete_count = 0
    for identity in planned:
        slots = grouped.get(identity, {})
        if set(slots) != {1, 2}:
            continue
        if str(slots[1]["annotator_hash"]) == str(slots[2]["annotator_hash"]):
            raise ValueError("double-label annotators must differ")
        complete_count += 1
        complete_records.extend((slots[1], slots[2]))
    if complete_count != len(planned):
        return AgreementCompletion(len(planned), complete_count, False, None)
    return AgreementCompletion(
        len(planned),
        complete_count,
        True,
        calculate_agreement(complete_records, config=config),
    )


def agreement_report(
    derived_db: str,
    *,
    sample_run_id: str,
    config: AnnotationConfig,
) -> AgreementReport:
    """从追加式原始标注生成完整报告；计划 pair 缺失时明确失败。

    此兼容入口不创建补充轮次；正式工作流应使用 repository 中的
    `evaluate_agreement_workflow`，由其保存状态并在低门槛时冻结补充样本。
    """

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        planned = {
            (int(row[0]), int(row[1]))
            for row in connection.execute(
                """
                SELECT source_post_id, source_version FROM text_sample_members
                WHERE sample_run_id = ? AND requires_double_label = 1
                UNION
                SELECT m.source_post_id, m.source_version
                FROM text_double_label_supplement_members AS m
                JOIN text_double_label_supplements AS s
                  ON s.supplement_run_id = m.supplement_run_id
                 AND s.seal_status = 'finalized'
                WHERE m.sample_run_id = ?
                """,
                (sample_run_id, sample_run_id),
            )
        }
        rows = connection.execute(
            """
            SELECT source_post_id, source_version, assignment_slot,
                   annotator_hash, structure_label, tourism_label, commercial_label
            FROM text_post_annotations
            WHERE sample_run_id = ? AND assignment_slot IN (1, 2)
            ORDER BY source_post_id, source_version, assignment_slot
            """,
            (sample_run_id,),
        ).fetchall()
    completion = evaluate_planned_agreement(
        rows, planned_identities=planned, config=config
    )
    if not completion.is_complete or completion.report is None:
        raise ValueError(
            f"double-label plan incomplete: {completion.complete_pair_count}/"
            f"{completion.planned_pair_count}"
        )
    return completion.report
