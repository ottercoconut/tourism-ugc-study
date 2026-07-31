"""图片保留集两层概率审计与单侧 Wilson 上限的纯统计逻辑。

主样本从全部可审计保留关系中等概率稳定抽取，只有主样本用于总体非加权点
估计和单侧 95% Wilson。平台补充仅补足小平台观察数，不进入总体区间；任一
补充技术噪声仍使轮次失败。本模块不访问数据库、图片或网络。
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import NormalDist
from typing import Iterable, Sequence

from .image_review_annotation import EXCLUSION_LABELS


@dataclass(frozen=True)
class AuditPopulationItem:
    """一个保留关系/图片的审计人口成员。"""

    fingerprint_id: str
    platform_key: str


@dataclass(frozen=True)
class AuditSampleMember:
    """两层审计中的一个稳定成员及其设计权重。

    ``sampling_layer`` 为 primary 或 platform_supplement。补充层概率是在主样本
    已冻结条件下的平台剩余人口纳入概率；该权重只供审计透明度，不进入总体
    Wilson 计算。
    """

    fingerprint_id: str
    platform_key: str
    sampling_layer: str
    stable_rank: int
    inclusion_probability: float
    sampling_weight: float


@dataclass(frozen=True)
class AuditSamplePlan:
    """主样本、平台补充及区间方法的确定性计划。"""

    population_count: int
    primary_members: tuple[AuditSampleMember, ...]
    supplement_members: tuple[AuditSampleMember, ...]
    interval_method: str


@dataclass(frozen=True)
class AuditObservation:
    """审计成员的人工技术噪声标签。"""

    fingerprint_id: str
    sampling_layer: str
    technical_noise_label: str


@dataclass(frozen=True)
class AuditEvaluation:
    """完整审计的事件数、总体估计、上限和验收状态。"""

    completed_count: int
    primary_event_count: int
    supplement_event_count: int
    primary_point_estimate: float | None
    one_sided_upper: float | None
    evaluation_status: str
    reason_code: str


def _rank(seed: int, namespace: str, identity: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{seed}:{namespace}:{identity}".encode("utf-8")).hexdigest()
    return digest, identity


def build_keep_audit_sample(
    population: Sequence[AuditPopulationItem],
    *,
    seed: int,
    primary_size: int,
    platform_supplement_min: int,
    excluded_fingerprint_ids: Iterable[str] = (),
) -> AuditSamplePlan:
    """创建等概率主样本和小平台定向补充，且排除旧轮成员。

    主样本从未在旧轮出现的全部人口中按稳定哈希等概率取 ``min(200,N)``；若
    完整人口不超过 200 且没有旧轮排除则为 census。随后每个平台补至
    ``min(30,Np)``，补充不挤占主样本。人口身份必须唯一，旧轮已耗尽人口时
    显式失败，不能通过复抽相同图片凑够轮次。
    """

    if seed <= 0 or primary_size <= 0 or platform_supplement_min <= 0:
        raise ValueError("audit sampling parameters must be positive")
    by_id: dict[str, AuditPopulationItem] = {}
    for item in population:
        if item.fingerprint_id in by_id:
            raise ValueError("audit population fingerprint ids must be unique")
        by_id[item.fingerprint_id] = item
    excluded = set(excluded_fingerprint_ids)
    eligible = [item for item in by_id.values() if item.fingerprint_id not in excluded]
    if by_id and not eligible:
        raise ValueError("non-overlapping audit population is exhausted")
    eligible.sort(key=lambda item: _rank(seed, "image-audit-primary", item.fingerprint_id))
    primary_count = min(primary_size, len(eligible))
    primary_raw = eligible[:primary_count]
    primary_probability = primary_count / len(eligible) if eligible else 1.0
    primary = tuple(
        AuditSampleMember(
            item.fingerprint_id,
            item.platform_key,
            "primary",
            rank,
            primary_probability,
            1 / primary_probability,
        )
        for rank, item in enumerate(primary_raw, start=1)
    )

    population_by_platform: dict[str, list[AuditPopulationItem]] = defaultdict(list)
    eligible_by_platform: dict[str, list[AuditPopulationItem]] = defaultdict(list)
    for item in by_id.values():
        population_by_platform[item.platform_key].append(item)
    primary_ids = {item.fingerprint_id for item in primary}
    for item in eligible:
        if item.fingerprint_id not in primary_ids:
            eligible_by_platform[item.platform_key].append(item)
    primary_platform_counts = Counter(item.platform_key for item in primary)
    supplement: list[AuditSampleMember] = []
    for platform_key in sorted(population_by_platform):
        target = min(platform_supplement_min, len(population_by_platform[platform_key]))
        deficit = max(0, target - primary_platform_counts[platform_key])
        pool = sorted(
            eligible_by_platform[platform_key],
            key=lambda item: _rank(seed, f"image-audit-supplement:{platform_key}", item.fingerprint_id),
        )
        chosen = pool[:deficit]
        probability = len(chosen) / len(pool) if pool else 1.0
        for item in chosen:
            supplement.append(
                AuditSampleMember(
                    item.fingerprint_id,
                    item.platform_key,
                    "platform_supplement",
                    len(supplement) + 1,
                    probability,
                    1 / probability,
                )
            )
    interval_method = (
        "census"
        if len(by_id) <= primary_size and not excluded and len(primary) == len(by_id)
        else "wilson_one_sided_95"
    )
    return AuditSamplePlan(len(by_id), primary, tuple(supplement), interval_method)


def wilson_one_sided_upper(events: int, total: int, confidence_level: float = 0.95) -> float:
    """计算二项比例单侧 Wilson 上限，不使用正态近似的 ``p±zSE``。

    ``events`` 必须在 0..total，``total`` 必须为正；置信水平位于 (0,1)。返回
    0..1。200 个主样本零事件约为 0.013347，一事件约为 0.022097。
    """

    if total <= 0 or events < 0 or events > total or not 0 < confidence_level < 1:
        raise ValueError("Wilson inputs are invalid")
    z = NormalDist().inv_cdf(confidence_level)
    proportion = events / total
    denominator = 1 + z * z / total
    centre = proportion + z * z / (2 * total)
    radius = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    )
    return min(1.0, (centre + radius) / denominator)


def evaluate_keep_audit(
    plan: AuditSamplePlan,
    observations: Sequence[AuditObservation],
    *,
    residual_noise_rate_max: float,
    confidence_level: float,
) -> AuditEvaluation:
    """按两层计划完整度和技术噪声事件评估一轮审计。

    缺标、重复标、unknown 成员均返回/抛出明确失败。``uncertain`` 视为未解决
    事件，使轮次失败；补充层事件不进入总体点估计/Wilson，但同样阻止通过。
    census 使用已全查人口的观测比例作为上限；抽样轮使用单侧 Wilson。
    """

    expected = {
        item.fingerprint_id: item
        for item in (*plan.primary_members, *plan.supplement_members)
    }
    by_id: dict[str, AuditObservation] = {}
    for observation in observations:
        if observation.fingerprint_id not in expected:
            raise ValueError("audit observation is outside frozen sample")
        if observation.fingerprint_id in by_id:
            raise ValueError("duplicate audit observation")
        if observation.sampling_layer != expected[observation.fingerprint_id].sampling_layer:
            raise ValueError("audit observation layer mismatch")
        by_id[observation.fingerprint_id] = observation
    if len(by_id) != len(expected):
        return AuditEvaluation(len(by_id), 0, 0, None, None, "incomplete", "audit_annotations_incomplete")

    def is_event(observation: AuditObservation) -> bool:
        return observation.technical_noise_label in EXCLUSION_LABELS or observation.technical_noise_label == "uncertain"

    primary_observations = [by_id[item.fingerprint_id] for item in plan.primary_members]
    supplement_observations = [by_id[item.fingerprint_id] for item in plan.supplement_members]
    primary_events = sum(is_event(item) for item in primary_observations)
    supplement_events = sum(is_event(item) for item in supplement_observations)
    if not primary_observations:
        return AuditEvaluation(len(by_id), 0, supplement_events, None, None, "incomplete", "audit_primary_empty")
    point = primary_events / len(primary_observations)
    upper = (
        point
        if plan.interval_method == "census"
        else wilson_one_sided_upper(primary_events, len(primary_observations), confidence_level)
    )
    # census 已观察完整人口，允许真实事件率在冻结阈值内；抽样轮仍会由 Wilson
    # 上限控制不确定性。只有平台补充层采用“任一事件即失败”的绝对保护门。
    passed = (
        supplement_events == 0
        and point <= residual_noise_rate_max
        and upper <= residual_noise_rate_max
    )
    return AuditEvaluation(
        len(by_id),
        primary_events,
        supplement_events,
        point,
        upper,
        "passed" if passed else "failed",
        "audit_quality_gate_passed" if passed else "residual_technical_noise_detected",
    )
