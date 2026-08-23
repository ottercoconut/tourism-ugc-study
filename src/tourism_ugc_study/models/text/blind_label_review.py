"""隐藏模型答案的开发标签一致性复核领域计算。

本模块只负责选择强矛盾目标、匹配随机预测正确对照，以及在解除盲化后汇总
人工回填。文件读取、哈希校验和 artifact 持久化由相邻模块负责，避免把私有
正文或本机路径带入领域错误。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


ALLOWED_REVIEW_LABELS = frozenset({"related", "unrelated", "uncertain"})
SELECTION_GROUP_TARGET = "model_conflict_target"
SELECTION_GROUP_CONTROL = "matched_correct_control"
SELECTION_REASON_HIGH_CONFIDENCE = "high_confidence_conflict"
SELECTION_REASON_SHORT_TEXT = "short_text_error"
SELECTION_REASON_CONTROL = "matched_correct_control"


class BlindLabelReviewError(RuntimeError):
    """盲化标签复核契约失败时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、作者、源身份或私有路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化领域失败。

        Args:
            reason_code: 供 CLI、测试与运行记录使用的稳定失败码。
        """

        super().__init__("blind label consistency review failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class DevelopmentReviewRecord:
    """单条仅限本地使用的开发证据。

    Attributes:
        source_post_id: 最终参考成员的源帖子正整数身份。
        source_version: 对应源版本正整数身份。
        split_name: 只允许 ``train_oof`` 或 ``validation``。
        original_label: 当前最终人工标签。
        p_unrelated: baseline 对 ``unrelated`` 的开发概率。
        review_text: 供复核者判断的规范化文本；不得写入 Git。
        length_band: 冻结文本长度层。
    """

    source_post_id: int
    source_version: int
    split_name: str
    original_label: str
    p_unrelated: float
    review_text: str
    length_band: str


@dataclass(frozen=True)
class BlindReviewSelection:
    """盲化任务与私有解除盲化映射的内存结果。

    Attributes:
        task_rows: 只含复核键、文本和空白人工填写列的打乱任务。
        mapping_records: 含选择组和源身份的私有映射，不得提交。
        target_count: 模型矛盾目标数。
        control_count: 匹配随机正确对照数。
        exact_match_control_count: 同标签、切分和长度层精确匹配的对照数。
        relaxed_match_control_count: 同标签与切分内按最近长度放宽的对照数。
    """

    task_rows: tuple[Mapping[str, str], ...]
    mapping_records: tuple[Mapping[str, Any], ...]
    target_count: int
    control_count: int
    exact_match_control_count: int
    relaxed_match_control_count: int


def _identity(record: DevelopmentReviewRecord) -> tuple[int, int]:
    """返回可稳定排序的源身份元组。"""

    return record.source_post_id, record.source_version


def _predicted_label(record: DevelopmentReviewRecord) -> str:
    """按固定0.5诊断分界生成预测类别。"""

    return "unrelated" if record.p_unrelated >= 0.5 else "related"


def _is_high_confidence_conflict(
    record: DevelopmentReviewRecord,
    probability_cutoff: float,
) -> bool:
    """判断模型概率是否与当前人工标签强烈矛盾。"""

    return (
        record.original_label == "related"
        and record.p_unrelated >= probability_cutoff
    ) or (
        record.original_label == "unrelated"
        and record.p_unrelated <= 1.0 - probability_cutoff
    )


def _review_key(
    model_id: str,
    random_seed: int,
    record: DevelopmentReviewRecord,
) -> str:
    """生成绑定模型、种子和成员且不直接暴露身份的复核键。"""

    payload = (
        f"blind-label-consistency:{model_id}:{random_seed}:"
        f"{record.source_post_id}:{record.source_version}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _pair_key(
    model_id: str,
    random_seed: int,
    target: DevelopmentReviewRecord,
) -> str:
    """生成目标及其匹配对照共享的不可逆配对键。"""

    payload = (
        f"blind-label-pair:{model_id}:{random_seed}:"
        f"{target.source_post_id}:{target.source_version}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _validate_records(records: Sequence[DevelopmentReviewRecord]) -> None:
    """失败关闭地校验开发记录，特别拒绝测试或集合重叠。"""

    if not records:
        raise BlindLabelReviewError("blind_review_development_records_empty")
    seen: set[tuple[int, int]] = set()
    for record in records:
        identity = _identity(record)
        if (
            record.source_post_id <= 0
            or record.source_version <= 0
            or identity in seen
            or record.split_name not in {"train_oof", "validation"}
            or record.original_label not in {"related", "unrelated"}
            or not math.isfinite(record.p_unrelated)
            or not 0.0 <= record.p_unrelated <= 1.0
            or not record.review_text.strip()
            or not record.length_band
        ):
            raise BlindLabelReviewError("blind_review_development_record_invalid")
        seen.add(identity)


def select_blind_label_review(
    records: Sequence[DevelopmentReviewRecord],
    *,
    model_id: str,
    random_seed: int,
    probability_cutoff: float = 0.90,
    short_text_max: int = 600,
    control_ratio: int = 1,
) -> BlindReviewSelection:
    """选择矛盾目标并按标签、切分和长度层匹配随机正确对照。

    Args:
        records: 训练 OOF 与验证开发记录；不得包含测试成员或测试概率。
        model_id: 冻结 baseline 的内容寻址身份。
        random_seed: 控制匹配抽样与任务行打乱的固定种子。
        probability_cutoff: 强矛盾概率门，必须严格大于0.5且小于1。
        short_text_max: 低于此字符数的0.5诊断误差进入复核目标。
        control_ratio: 每条目标匹配的随机预测正确对照数，必须为正整数。

    Returns:
        不显示模型答案的任务行和仅限本地的解除盲化映射。

    Raises:
        BlindLabelReviewError: 配置、开发记录或精确匹配对照不足。

    Notes:
        平台、作者、样本框和路由阈值均不参与选择。0.5仅用于识别开发
        诊断误差，不成为正式清洗阈值。
    """

    _validate_records(records)
    if (
        not model_id.strip()
        or isinstance(random_seed, bool)
        or not isinstance(random_seed, int)
        or not 0.5 < probability_cutoff < 1.0
        or isinstance(short_text_max, bool)
        or not isinstance(short_text_max, int)
        or short_text_max <= 0
        or isinstance(control_ratio, bool)
        or not isinstance(control_ratio, int)
        or control_ratio <= 0
    ):
        raise BlindLabelReviewError("blind_review_selection_config_invalid")

    targets: list[tuple[DevelopmentReviewRecord, str]] = []
    available_controls: list[DevelopmentReviewRecord] = []
    for record in records:
        is_error = _predicted_label(record) != record.original_label
        if is_error and _is_high_confidence_conflict(record, probability_cutoff):
            targets.append((record, SELECTION_REASON_HIGH_CONFIDENCE))
        elif is_error and len(record.review_text) < short_text_max:
            targets.append((record, SELECTION_REASON_SHORT_TEXT))
        elif not is_error:
            available_controls.append(record)

    if not targets:
        raise BlindLabelReviewError("blind_review_no_targets")
    targets.sort(key=lambda item: (_identity(item[0]), item[1]))
    rng = random.Random(random_seed)
    available_controls.sort(key=_identity)

    mapping: list[dict[str, Any]] = []
    selected_identities: set[tuple[int, int]] = set()
    exact_match_control_count = 0
    relaxed_match_control_count = 0
    for target, reason in targets:
        eligible = [
            record
            for record in available_controls
            if record.original_label == target.original_label
            and record.split_name == target.split_name
            and _identity(record) not in selected_identities
        ]
        if len(eligible) < control_ratio:
            raise BlindLabelReviewError("blind_review_control_pool_insufficient")
        pair_key = _pair_key(model_id, random_seed, target)
        selected_controls: list[tuple[DevelopmentReviewRecord, str]] = []
        for _ in range(control_ratio):
            remaining = [
                record
                for record in eligible
                if _identity(record) not in {
                    _identity(item[0]) for item in selected_controls
                }
            ]
            exact = [
                record
                for record in remaining
                if record.length_band == target.length_band
            ]
            if exact:
                control = exact[rng.randrange(len(exact))]
                matching_level = "exact_label_split_length_band"
                exact_match_control_count += 1
            else:
                nearest_distance = min(
                    abs(len(record.review_text) - len(target.review_text))
                    for record in remaining
                )
                nearest = [
                    record
                    for record in remaining
                    if abs(len(record.review_text) - len(target.review_text))
                    == nearest_distance
                ]
                control = nearest[rng.randrange(len(nearest))]
                matching_level = "nearest_length_same_label_split"
                relaxed_match_control_count += 1
            selected_controls.append((control, matching_level))
        selected = [(target, "not_applicable"), *selected_controls]
        for index, (record, matching_level) in enumerate(selected):
            identity = _identity(record)
            if identity in selected_identities:
                raise BlindLabelReviewError("blind_review_member_reused")
            selected_identities.add(identity)
            is_target = index == 0
            mapping.append(
                {
                    "review_key": _review_key(model_id, random_seed, record),
                    "pair_key": pair_key,
                    "selection_group": (
                        SELECTION_GROUP_TARGET
                        if is_target
                        else SELECTION_GROUP_CONTROL
                    ),
                    "selection_reason": reason if is_target else SELECTION_REASON_CONTROL,
                    "matching_level": matching_level,
                    "source_post_id": record.source_post_id,
                    "source_version": record.source_version,
                    "split_name": record.split_name,
                    "original_label": record.original_label,
                    "model_label": _predicted_label(record),
                    "p_unrelated": record.p_unrelated,
                    "length_band": record.length_band,
                    "review_text_sha256": hashlib.sha256(
                        record.review_text.encode("utf-8")
                    ).hexdigest(),
                    "review_text": record.review_text,
                }
            )

    task_rows = [
        {
            "review_key": str(record["review_key"]),
            "review_text": str(record["review_text"]),
            "review_label": "",
            "review_note": "",
        }
        for record in mapping
    ]
    random.Random(random_seed + 1).shuffle(task_rows)
    private_mapping = tuple(
        {key: value for key, value in record.items() if key != "review_text"}
        for record in sorted(mapping, key=lambda item: str(item["review_key"]))
    )
    return BlindReviewSelection(
        task_rows=tuple(task_rows),
        mapping_records=private_mapping,
        target_count=len(targets),
        control_count=len(targets) * control_ratio,
        exact_match_control_count=exact_match_control_count,
        relaxed_match_control_count=relaxed_match_control_count,
    )


def summarize_blind_label_review(
    mapping_records: Sequence[Mapping[str, Any]],
    completed_rows: Sequence[Mapping[str, str]],
    *,
    model_id: str,
) -> Mapping[str, Any]:
    """解除选择组盲化并分别汇总目标与随机正确对照。

    Args:
        mapping_records: 已通过 artifact 摘要校验的私有映射记录。
        completed_rows: 固定列未变且人工标签完整的任务行。
        model_id: 任务绑定的冻结 baseline 身份。

    Returns:
        不含正文、帖子/作者身份、概率或私有路径的分组汇总。

    Raises:
        BlindLabelReviewError: 成员不一一对应、回填非法或映射契约损坏。

    Notes:
        定向矛盾组和随机正确对照分开报告；任何一组都不自动改写最终 CSV。
    """

    if not model_id.strip() or not mapping_records:
        raise BlindLabelReviewError("blind_review_summary_mapping_invalid")
    mapping_by_key: dict[str, Mapping[str, Any]] = {}
    for record in mapping_records:
        try:
            review_key = str(record["review_key"])
            group = str(record["selection_group"])
            original_label = str(record["original_label"])
        except (KeyError, TypeError) as exc:
            raise BlindLabelReviewError("blind_review_summary_mapping_invalid") from exc
        if (
            not review_key
            or review_key in mapping_by_key
            or group not in {SELECTION_GROUP_TARGET, SELECTION_GROUP_CONTROL}
            or original_label not in {"related", "unrelated"}
        ):
            raise BlindLabelReviewError("blind_review_summary_mapping_invalid")
        mapping_by_key[review_key] = record

    completed_by_key: dict[str, Mapping[str, str]] = {}
    for row in completed_rows:
        review_key = str(row.get("review_key", "")).strip()
        review_label = str(row.get("review_label", "")).strip()
        if (
            not review_key
            or review_key in completed_by_key
            or review_label not in ALLOWED_REVIEW_LABELS
        ):
            raise BlindLabelReviewError("blind_review_completed_row_invalid")
        completed_by_key[review_key] = row
    if set(completed_by_key) != set(mapping_by_key):
        raise BlindLabelReviewError("blind_review_completed_members_mismatch")

    counters = {
        SELECTION_GROUP_TARGET: Counter(),
        SELECTION_GROUP_CONTROL: Counter(),
    }
    records: list[dict[str, Any]] = []
    for review_key in sorted(mapping_by_key):
        mapping = mapping_by_key[review_key]
        completed = completed_by_key[review_key]
        original_label = str(mapping["original_label"])
        reviewed_label = str(completed["review_label"]).strip()
        if reviewed_label == "uncertain":
            decision = "uncertain"
        elif reviewed_label == original_label:
            decision = "maintained"
        else:
            decision = "changed"
        group = str(mapping["selection_group"])
        counters[group][decision] += 1
        records.append(
            {
                "review_key": review_key,
                "selection_group": group,
                "selection_reason": str(mapping["selection_reason"]),
                "original_label": original_label,
                "reviewed_label": reviewed_label,
                "decision": decision,
                "review_note_present": bool(str(completed.get("review_note", "")).strip()),
            }
        )

    summaries: dict[str, Mapping[str, Any]] = {}
    for group, counts in counters.items():
        determinate = counts["maintained"] + counts["changed"]
        summaries[group] = {
            "count": sum(counts.values()),
            "maintained_count": counts["maintained"],
            "changed_count": counts["changed"],
            "uncertain_count": counts["uncertain"],
            "change_rate_among_determinate": (
                counts["changed"] / determinate if determinate else None
            ),
        }
    return {
        "artifact_kind": "formal-cleaning-blind-label-review-summary",
        "status": "completed_not_applied",
        "model_id": model_id,
        "test_status": "locked_not_opened",
        "test_members_read": False,
        "test_probabilities_present": False,
        "platform_used": False,
        "automatic_reference_update": False,
        "inference_boundary": (
            "model_conflict_target_is_model_selected_and_cannot_estimate_"
            "overall_label_error_rate"
        ),
        "group_summaries": summaries,
        "records": records,
    }
