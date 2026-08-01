"""图片技术噪声人工标签、CSV 契约与一致性指标的纯领域逻辑。

本模块只接受唯一人工轴 ``technical_noise_label``，不复用文本标注中的商业、
结构或旅游相关性字段。函数不访问图片、数据库或网络；持久化与任务调度由
仓储/工作流模块负责。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence


TECHNICAL_NOISE_LABELS = frozenset(
    {
        "valid_content",
        "site_background",
        "site_ui",
        "placeholder_or_error",
        "tracking_or_qr_only",
        "uncertain",
    }
)
EXCLUSION_LABELS = frozenset(
    {"site_background", "site_ui", "placeholder_or_error", "tracking_or_qr_only"}
)
IMAGE_ANNOTATION_COLUMNS = (
    "task_id",
    "review_run_id",
    "fingerprint_id",
    "assignment_slot",
    "technical_noise_label",
    "reason_codes",
    "technical_flags",
    "annotator_hash",
    "guide_version",
    "annotated_at_utc",
)
IMAGE_ADJUDICATION_COLUMNS = (
    "review_run_id",
    "fingerprint_id",
    "left_annotation_id",
    "right_annotation_id",
    "technical_noise_label",
    "reason_codes",
    "adjudicator_hash",
    "guide_version",
    "adjudicated_at_utc",
)


@dataclass(frozen=True)
class AnnotationPair:
    """同一图片两个独立槽位的原始技术噪声标签。

    ``fingerprint_id`` 是冻结复核成员身份；``left_label/right_label`` 分别对应
    slot 1/2，不表示先后优先级。对象只承载原始枚举，不含仲裁或最终动作；
    标签合法性与图片身份唯一性由 :func:`calculate_image_agreement` 统一检查。
    """

    fingerprint_id: str
    left_label: str
    right_label: str


@dataclass(frozen=True)
class ImageAgreement:
    """完整双标计划的一致性结果。

    ``raw_agreement/cohen_kappa`` 在计划未完成时均为空。双标只出现一个类别时
    κ 不可估而不是伪填 0；原始一致率仍然可报告并决定是否通过 0.80 门槛。
    ``planned_pair_count/complete_pair_count/agreement_count`` 记录计划、完成和一致
    数；``label_disagreements`` 保存有向标签组合计数，便于定位手册边界。
    ``evaluation_status`` 仅为 incomplete/passed/supplement_required，不能直接
    作为图片排除标签。
    """

    planned_pair_count: int
    complete_pair_count: int
    agreement_count: int
    raw_agreement: float | None
    cohen_kappa: float | None
    kappa_status: str
    evaluation_status: str
    label_disagreements: tuple[tuple[str, str, int], ...]


def validate_safe_csv_cell(value: str) -> str:
    """拒绝电子表格公式前缀并返回原字符串。

    检查忽略左侧空白，拒绝 ``= + - @``。这些字符在人工表格打开时可能触发
    公式或外部数据访问；调用方应在任何数据库写入前检查每个单元格。空值合法，
    因为标签模板的待填字段在导出时本来为空。
    """

    if value.lstrip().startswith(("=", "+", "-", "@")):
        raise ValueError("spreadsheet formula cells are not allowed")
    return value


def split_codes(value: str) -> tuple[str, ...]:
    """规范化分号分隔的理由或技术旗标，并拒绝空白/重复之外的歧义。

    空字符串返回空元组；非空代码去除首尾空白、去重并排序。代码只允许小写
    ASCII 字母、数字、下划线和连字符，以免自由文本或图片内容误入派生库。
    """

    values = {item.strip() for item in value.split(";") if item.strip()}
    for item in values:
        if not all(character.islower() or character.isdigit() or character in "_-" for character in item):
            raise ValueError("reason and flag codes must use lowercase safe tokens")
    return tuple(sorted(values))


def calculate_image_agreement(
    pairs: Sequence[AnnotationPair],
    *,
    planned_pair_count: int,
    minimum_raw_agreement: float,
) -> ImageAgreement:
    """按完整计划计算原始一致率和 Cohen's κ。

    ``pairs`` 必须对象唯一、标签合法，且数量不得超过计划。计划未完成时返回
    ``incomplete``，不会用部分 pair 宣称通过。完整计划只有一个观测类别或期望
    一致率为 1 时 κ 状态为 ``undefined_single_category``；这本身不触发补样。
    原始一致率低于门槛时才返回 ``supplement_required``。非法阈值、重复图片、
    未知标签或完成数超过计划时抛出 ``ValueError``；函数不修改输入或持久化
    评估结果。
    """

    if planned_pair_count < 0 or not 0 < minimum_raw_agreement < 1:
        raise ValueError("agreement plan or threshold is invalid")
    seen: set[str] = set()
    for pair in pairs:
        if pair.fingerprint_id in seen:
            raise ValueError("agreement pairs must have unique fingerprints")
        seen.add(pair.fingerprint_id)
        if pair.left_label not in TECHNICAL_NOISE_LABELS or pair.right_label not in TECHNICAL_NOISE_LABELS:
            raise ValueError("unknown technical noise label")
    if len(pairs) > planned_pair_count:
        raise ValueError("complete pairs exceed planned pairs")

    disagreements = Counter(
        (pair.left_label, pair.right_label)
        for pair in pairs
        if pair.left_label != pair.right_label
    )
    if len(pairs) != planned_pair_count:
        return ImageAgreement(
            planned_pair_count,
            len(pairs),
            sum(pair.left_label == pair.right_label for pair in pairs),
            None,
            None,
            "incomplete",
            "incomplete",
            tuple((left, right, count) for (left, right), count in sorted(disagreements.items())),
        )

    agreement_count = sum(pair.left_label == pair.right_label for pair in pairs)
    raw = agreement_count / planned_pair_count if planned_pair_count else 1.0
    categories = sorted(
        {label for pair in pairs for label in (pair.left_label, pair.right_label)}
    )
    if len(categories) < 2 or not pairs:
        kappa = None
        kappa_status = "undefined_single_category"
    else:
        left_counts = Counter(pair.left_label for pair in pairs)
        right_counts = Counter(pair.right_label for pair in pairs)
        expected = sum(
            (left_counts[category] / planned_pair_count)
            * (right_counts[category] / planned_pair_count)
            for category in categories
        )
        if expected == 1:
            kappa = None
            kappa_status = "undefined_single_category"
        else:
            kappa = (raw - expected) / (1 - expected)
            kappa_status = "estimated"
    status = "passed" if raw >= minimum_raw_agreement else "supplement_required"
    return ImageAgreement(
        planned_pair_count,
        len(pairs),
        agreement_count,
        raw,
        kappa,
        kappa_status,
        status,
        tuple((left, right, count) for (left, right), count in sorted(disagreements.items())),
    )
