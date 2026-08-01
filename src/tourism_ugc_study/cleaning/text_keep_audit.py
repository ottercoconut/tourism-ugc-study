"""文本保留集平台比例抽样与质量门禁的纯计算逻辑。

本模块只接收调用方明确冻结的 ``keep`` 帖子人口，不查询数据库，也不推断最终
帖子决定。抽样采用平台比例分层：先用系统随机化舍入确定各平台配额，再在平台
内稳定无放回抽取。该设计使每个可抽成员的一阶纳入概率均为 ``n/N``，总体既
可输出 Horvitz--Thompson 点估计，也可在项目协议约定下使用普通二项 Wilson
上限；平台切片单独报告，不代替总体门禁。

同一人口 manifest 不允许重新抽样。调用方还可传入旧轮成员，使人口变化后的
新轮不复抽旧成员；由于本模块没有旧轮原始标注，含这类历史排除的单轮结果会
明确阻止通过，防止把剩余人口的结果误称为完整保留集审计。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from statistics import NormalDist
from typing import Iterable, Sequence


STRUCTURE_LABELS = frozenset({"usable", "invalid", "uncertain"})
TOURISM_LABELS = frozenset({"related", "unrelated", "uncertain", "not_applicable"})


@dataclass(frozen=True)
class TextKeepPopulationItem:
    """一个明确传入的文本保留人口成员。

    ``source_post_id`` 与 ``source_version`` 共同标识被审计的帖子版本；只使用
    帖子 ID 会把后续文本修订误当成同一证据。``platform_key`` 是冻结分层键，
    必须为非空字符串。对象不携带文本、作者标识、模型分数或人工标签。
    """

    source_post_id: int
    source_version: int
    platform_key: str

    @property
    def member_key(self) -> str:
        """返回不含内容的稳定帖子版本键。"""

        return f"{self.source_post_id}:{self.source_version}"


@dataclass(frozen=True)
class TextKeepSampleMember:
    """冻结样本中的一个成员及其设计信息。

    ``stable_rank`` 是总体样本中的一基序号；``platform_rank`` 是平台内一基
    序号。系统随机化配额与平台内简单随机抽样组合后，每个成员的边际
    ``inclusion_probability`` 相同，``sampling_weight`` 为其倒数。平台人口与
    配额随成员一并保存，使 manifest 和后续审计能重算抽样设计。
    """

    source_post_id: int
    source_version: int
    platform_key: str
    stable_rank: int
    platform_rank: int
    platform_population_count: int
    platform_sample_count: int
    inclusion_probability: float
    sampling_weight: float

    @property
    def member_key(self) -> str:
        """返回与人口成员一致的稳定帖子版本键。"""

        return f"{self.source_post_id}:{self.source_version}"


@dataclass(frozen=True)
class TextKeepAuditStratum:
    """冻结计划中的一个平台分层及其人口、样本配额。

    分层即使得到零配额也必须保留，保证平台切片不会因未抽中而从报告消失。
    ``population_count`` 为排除旧轮成员后的可抽人数，``sample_count`` 为本轮
    配额；两者均由比例分配算法确定，不含观察结果。
    """

    platform_key: str
    population_count: int
    sample_count: int


@dataclass(frozen=True)
class TextKeepAuditPlan:
    """一次不可变的文本保留集审计抽样计划。

    ``source_population_count`` 是调用方传入的人口规模，``audit_population_count``
    是排除旧轮成员后本轮可抽的人口规模。两者不相等时，当前纯计算模块不能将
    单轮证据与历史证据合并，因此 ``interval_method`` 为 ``not_applicable``，
    后续评估必须失败。正常抽样使用 ``wilson_one_sided_95``，不足目标量且无
    历史排除时全查并使用 ``census``。
    """

    seed: int
    target_sample_size: int
    source_population_count: int
    audit_population_count: int
    historical_excluded_count: int
    population_manifest_sha256: str
    sample_manifest_sha256: str
    interval_method: str
    strata: tuple[TextKeepAuditStratum, ...]
    members: tuple[TextKeepSampleMember, ...]


@dataclass(frozen=True)
class TextKeepObservation:
    """一个冻结成员的文本清洗双轴人工观察。

    结构轴只允许 ``usable/invalid/uncertain``，旅游轴只允许
    ``related/unrelated/uncertain/not_applicable``。``invalid`` 必须且只能搭配
    ``not_applicable``；构造对象本身不验证，评估函数统一拒绝非法证据。
    """

    source_post_id: int
    source_version: int
    structure_label: str
    tourism_label: str

    @property
    def member_key(self) -> str:
        """返回观察所引用的稳定帖子版本键。"""

        return f"{self.source_post_id}:{self.source_version}"


@dataclass(frozen=True)
class TextKeepAuditSlice:
    """总体或单个平台的独立审计切片统计。

    平台切片的 ``point_estimate`` 是该平台冻结样本内的描述性事件比例；平台
    没有入样成员时为 ``None``。总体切片另由评估结果给出 HT 点估计和置信
    上限。切片不单独产生验收决定，避免小平台样本被误当作总体二项样本。
    """

    platform_key: str
    population_count: int
    sample_count: int
    completed_count: int
    event_count: int
    point_estimate: float | None


@dataclass(frozen=True)
class TextKeepAuditEvaluation:
    """文本保留集审计的完整度、总体门禁和平台切片结果。

    完整且设计可用时，``ht_point_estimate`` 为按纳入概率计算的总体事件率；
    自加权设计下它与样本事件率相同。census 的 ``one_sided_upper`` 等于真实
    点估计，抽样轮为单侧 Wilson 95% 上限。门禁要求点估计不高于 3%、上限
    不高于 5%；``failure_reason_codes`` 按固定顺序保存所有失败原因。
    """

    completed_count: int
    event_count: int
    ht_point_estimate: float | None
    one_sided_upper: float | None
    interval_method: str
    evaluation_status: str
    failure_reason_codes: tuple[str, ...]
    platform_slices: tuple[TextKeepAuditSlice, ...]


def _stable_rank(seed: int, namespace: str, identity: str) -> tuple[str, str]:
    """返回跨进程稳定的伪随机排序键，不依赖 Python 哈希随机化。"""

    digest = hashlib.sha256(f"{seed}:{namespace}:{identity}".encode("utf-8")).hexdigest()
    return digest, identity


def _manifest_sha256(payload: object) -> str:
    """以规范 JSON 计算 manifest 摘要，输入顺序差异不会影响调用方已排序结构。"""

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validated_population(
    population: Sequence[TextKeepPopulationItem],
) -> tuple[TextKeepPopulationItem, ...]:
    """验证人口身份和平台键，并返回按身份规范排序的不可变人口。"""

    by_key: dict[str, TextKeepPopulationItem] = {}
    for item in population:
        if item.source_post_id <= 0 or item.source_version <= 0:
            raise ValueError("text keep population identity must be positive")
        if not item.platform_key or item.platform_key.strip() != item.platform_key:
            raise ValueError("text keep population platform key is invalid")
        if item.member_key in by_key:
            raise ValueError("text keep population member keys must be unique")
        by_key[item.member_key] = item
    if not by_key:
        raise ValueError("text keep audit population must not be empty")
    return tuple(sorted(by_key.values(), key=lambda item: item.member_key))


def population_manifest_sha256(population: Sequence[TextKeepPopulationItem]) -> str:
    """返回完整输入人口的稳定 manifest SHA-256。

    manifest 只包含帖子版本身份和平台分层键，不包含文本或作者信息。输入顺序
    不影响结果；非法、空或重复人口与正式抽样一样抛出 ``ValueError``。
    """

    normalized = _validated_population(population)
    return _manifest_sha256(
        {
            "schema": "text-keep-audit-population-v1",
            "members": [
                {
                    "source_post_id": item.source_post_id,
                    "source_version": item.source_version,
                    "platform_key": item.platform_key,
                }
                for item in normalized
            ],
        }
    )


def _systematic_proportional_quotas(
    platform_counts: dict[str, int],
    *,
    sample_size: int,
    population_count: int,
    seed: int,
) -> dict[str, int]:
    """用系统随机化舍入生成整数平台配额并保留相等边际纳入概率。

    每个平台先取精确比例配额的整数部分，剩余名额按小数部分构成的连续区间
    分配。随机起点由固定 seed 的 SHA-256 唯一确定；在重复随机化意义下，每个
    平台获得额外名额的概率恰为其小数部分，因此每个成员的一阶纳入概率都是
    ``sample_size/population_count``。函数只计算配额，不选择具体成员。
    """

    exact = {
        platform: Fraction(sample_size * count, population_count)
        for platform, count in platform_counts.items()
    }
    quotas = {platform: int(value) for platform, value in exact.items()}
    fractions = {platform: value - int(value) for platform, value in exact.items()}
    remainder = sample_size - sum(quotas.values())
    if remainder == 0:
        return quotas

    # 用完整 256 位摘要产生 [0,1) 精确有理起点，避免浮点累计改变配额。
    digest = hashlib.sha256(f"{seed}:text-keep-platform-quota".encode("utf-8")).digest()
    start = Fraction(int.from_bytes(digest, "big"), 1 << (8 * len(digest)))
    points = [start + offset for offset in range(remainder)]
    cumulative = Fraction(0, 1)
    point_index = 0
    for platform in sorted(platform_counts):
        cumulative += fractions[platform]
        while point_index < len(points) and points[point_index] < cumulative:
            quotas[platform] += 1
            point_index += 1
    if point_index != remainder or sum(quotas.values()) != sample_size:
        raise RuntimeError("proportional quota rounding invariant failed")
    return quotas


def build_text_keep_audit_sample(
    population: Sequence[TextKeepPopulationItem],
    *,
    seed: int,
    target_sample_size: int = 300,
    previous_population_manifest_sha256s: Iterable[str] = (),
    previous_member_keys: Iterable[str] = (),
) -> TextKeepAuditPlan:
    """从明确的 keep 人口冻结平台比例样本与两份稳定 manifest。

    ``target_sample_size`` 至少为 300；人口不超过目标量时全查。人口较大时按
    平台规模比例分配配额，再在各平台按固定 seed 稳定无放回抽样。系统随机化
    配额保证总体自加权，成员保存纳入概率及其倒数。若完整人口 manifest 已在
    ``previous_population_manifest_sha256s`` 中出现则直接拒绝，不能换 seed 重抽
    同一人口挑选通过结果。``previous_member_keys`` 会从变化后人口中排除旧轮
    成员；这类计划因缺少历史标签合并而标记为区间不适用，不能单轮通过。

    返回纯 dataclass，不写数据库或文件。身份/平台非法、人口为空、目标量不足
    300、重复人口 manifest 或旧成员耗尽当前人口时抛出 ``ValueError``。
    """

    if not isinstance(seed, int):
        raise ValueError("text keep audit seed must be an integer")
    if target_sample_size < 300:
        raise ValueError("text keep audit target sample size must be at least 300")
    normalized = _validated_population(population)
    population_manifest = population_manifest_sha256(normalized)
    previous_manifests = set(previous_population_manifest_sha256s)
    if population_manifest in previous_manifests:
        raise ValueError("text keep audit population manifest was already audited")

    excluded = set(previous_member_keys)
    eligible = tuple(item for item in normalized if item.member_key not in excluded)
    historical_excluded_count = len(normalized) - len(eligible)
    if not eligible:
        raise ValueError("non-overlapping text keep audit population is exhausted")

    sample_size = min(target_sample_size, len(eligible))
    platform_counts = Counter(item.platform_key for item in eligible)
    quotas = _systematic_proportional_quotas(
        dict(platform_counts),
        sample_size=sample_size,
        population_count=len(eligible),
        seed=seed,
    )
    probability = sample_size / len(eligible)
    chosen: list[tuple[TextKeepPopulationItem, int]] = []
    for platform in sorted(platform_counts):
        platform_population = [item for item in eligible if item.platform_key == platform]
        ranked = sorted(
            platform_population,
            key=lambda item: _stable_rank(
                seed,
                f"text-keep-platform:{platform}",
                item.member_key,
            ),
        )
        for platform_rank, item in enumerate(ranked[: quotas[platform]], start=1):
            chosen.append((item, platform_rank))

    # 最终 manifest 顺序与平台遍历或字典实现无关；稳定哈希仅决定展示/任务顺序，
    # 不改变已经由平台配额确定的一阶概率。
    chosen.sort(
        key=lambda pair: _stable_rank(seed, "text-keep-audit-output", pair[0].member_key)
    )
    members = tuple(
        TextKeepSampleMember(
            source_post_id=item.source_post_id,
            source_version=item.source_version,
            platform_key=item.platform_key,
            stable_rank=rank,
            platform_rank=platform_rank,
            platform_population_count=platform_counts[item.platform_key],
            platform_sample_count=quotas[item.platform_key],
            inclusion_probability=probability,
            sampling_weight=1 / probability,
        )
        for rank, (item, platform_rank) in enumerate(chosen, start=1)
    )
    if len(members) != sample_size:
        raise RuntimeError("text keep audit sample size invariant failed")
    strata = tuple(
        TextKeepAuditStratum(
            platform_key=platform,
            population_count=platform_counts[platform],
            sample_count=quotas[platform],
        )
        for platform in sorted(platform_counts)
    )

    if historical_excluded_count:
        interval_method = "not_applicable_historical_members_excluded"
    elif sample_size == len(normalized):
        interval_method = "census"
    else:
        interval_method = "wilson_one_sided_95"
    sample_manifest = _manifest_sha256(
        {
            "schema": "text-keep-audit-sample-v1",
            "population_manifest_sha256": population_manifest,
            "seed": seed,
            "target_sample_size": target_sample_size,
            "source_population_count": len(normalized),
            "audit_population_count": len(eligible),
            "historical_excluded_count": historical_excluded_count,
            "interval_method": interval_method,
            # 概率以整数分子/分母封存，避免不同 JSON 浮点格式影响身份。
            "inclusion_probability": {
                "numerator": sample_size,
                "denominator": len(eligible),
            },
            "strata": [
                {
                    "platform_key": stratum.platform_key,
                    "population_count": stratum.population_count,
                    "sample_count": stratum.sample_count,
                }
                for stratum in strata
            ],
            "members": [
                {
                    "source_post_id": member.source_post_id,
                    "source_version": member.source_version,
                    "platform_key": member.platform_key,
                    "stable_rank": member.stable_rank,
                    "platform_rank": member.platform_rank,
                    "platform_population_count": member.platform_population_count,
                    "platform_sample_count": member.platform_sample_count,
                }
                for member in members
            ],
        }
    )
    return TextKeepAuditPlan(
        seed=seed,
        target_sample_size=target_sample_size,
        source_population_count=len(normalized),
        audit_population_count=len(eligible),
        historical_excluded_count=historical_excluded_count,
        population_manifest_sha256=population_manifest,
        sample_manifest_sha256=sample_manifest,
        interval_method=interval_method,
        strata=strata,
        members=members,
    )


def wilson_one_sided_upper(
    events: int,
    total: int,
    confidence_level: float = 0.95,
) -> float:
    """计算二项比例的单侧 Wilson 上限。

    ``events`` 必须位于 ``0..total``，``total`` 为正，置信水平位于 (0,1)。
    本函数只负责数学计算；调用方必须先确认样本为自加权设计，不能把不等概率
    样本机械代入普通二项 Wilson。
    """

    if total <= 0 or events < 0 or events > total or not 0 < confidence_level < 1:
        raise ValueError("text keep audit Wilson inputs are invalid")
    z = NormalDist().inv_cdf(confidence_level)
    proportion = events / total
    denominator = 1 + z * z / total
    centre = proportion + z * z / (2 * total)
    radius = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    )
    return min(1.0, (centre + radius) / denominator)


def _validate_observation(observation: TextKeepObservation) -> None:
    """验证双轴标签集合与 invalid/not_applicable 双向适用性。"""

    if observation.structure_label not in STRUCTURE_LABELS:
        raise ValueError("text keep audit structure label is invalid")
    if observation.tourism_label not in TOURISM_LABELS:
        raise ValueError("text keep audit tourism label is invalid")
    invalid_pair = observation.structure_label == "invalid"
    not_applicable_pair = observation.tourism_label == "not_applicable"
    if invalid_pair != not_applicable_pair:
        raise ValueError("text keep audit dual-axis applicability is invalid")


def _is_keep_error_event(observation: TextKeepObservation) -> bool:
    """返回观察是否为保留错误或未解决事件。

    结构 ``invalid``、结构 ``uncertain``、旅游 ``unrelated`` 或旅游
    ``uncertain`` 均计为事件。只有结构可用且旅游相关的观察不计事件；函数不
    把事件自动写成帖子排除决定。
    """

    return (
        observation.structure_label in {"invalid", "uncertain"}
        or observation.tourism_label in {"unrelated", "uncertain"}
    )


def _platform_slices(
    plan: TextKeepAuditPlan,
    observations: dict[str, TextKeepObservation],
) -> tuple[TextKeepAuditSlice, ...]:
    """按平台分别计算描述性完成度和事件率，包含零入样平台。"""

    slices: list[TextKeepAuditSlice] = []
    for stratum in plan.strata:
        platform_members = [
            member for member in plan.members if member.platform_key == stratum.platform_key
        ]
        platform_observations = [
            observations[member.member_key]
            for member in platform_members
            if member.member_key in observations
        ]
        events = sum(_is_keep_error_event(item) for item in platform_observations)
        slices.append(
            TextKeepAuditSlice(
                platform_key=stratum.platform_key,
                population_count=stratum.population_count,
                sample_count=stratum.sample_count,
                completed_count=len(platform_observations),
                event_count=events,
                point_estimate=(events / len(platform_observations) if platform_observations else None),
            )
        )
    return tuple(slices)


def evaluate_text_keep_audit(
    plan: TextKeepAuditPlan,
    observations: Sequence[TextKeepObservation],
    *,
    point_estimate_max: float = 0.03,
    one_sided_upper_max: float = 0.05,
    confidence_level: float = 0.95,
) -> TextKeepAuditEvaluation:
    """评估完整文本保留集样本并执行 3%/5% 双门禁。

    观察必须与冻结样本一一对应；样本外、重复帖子版本或双轴不适用组合立即
    抛出 ``ValueError``。缺标返回 ``incomplete``，不计算总体结果。完整样本
    使用 Horvitz--Thompson 总量估计除以本轮人口得到点估计；自加权抽样使用
    单侧 Wilson 95% 上限，census 则令上限等于真实点估计。点估计严格大于
    3% 或上限严格大于 5% 时失败，恰等于阈值不失败。历史成员已排除的计划
    因本模块没有旧证据合并而明确失败，绝不误套普通 Wilson。
    """

    if (
        not 0 <= point_estimate_max <= 1
        or not 0 <= one_sided_upper_max <= 1
        or not 0 < confidence_level < 1
    ):
        raise ValueError("text keep audit evaluation thresholds are invalid")
    expected = {member.member_key: member for member in plan.members}
    if not expected:
        raise ValueError("text keep audit plan has no members")
    by_key: dict[str, TextKeepObservation] = {}
    for observation in observations:
        if observation.member_key not in expected:
            raise ValueError("text keep audit observation is outside frozen sample")
        if observation.member_key in by_key:
            raise ValueError("duplicate text keep audit observation")
        _validate_observation(observation)
        by_key[observation.member_key] = observation
    slices = _platform_slices(plan, by_key)
    if len(by_key) != len(expected):
        return TextKeepAuditEvaluation(
            completed_count=len(by_key),
            event_count=sum(_is_keep_error_event(item) for item in by_key.values()),
            ht_point_estimate=None,
            one_sided_upper=None,
            interval_method=plan.interval_method,
            evaluation_status="incomplete",
            failure_reason_codes=("text_keep_audit_annotations_incomplete",),
            platform_slices=slices,
        )

    event_count = sum(_is_keep_error_event(item) for item in by_key.values())
    estimated_event_total = sum(
        expected[key].sampling_weight
        for key, observation in by_key.items()
        if _is_keep_error_event(observation)
    )
    point = estimated_event_total / plan.audit_population_count
    reasons: list[str] = []
    if plan.interval_method == "census":
        upper = point
    elif plan.interval_method == "wilson_one_sided_95":
        probabilities = {round(member.inclusion_probability, 15) for member in plan.members}
        if len(probabilities) != 1:
            upper = None
            reasons.append("text_keep_audit_wilson_not_applicable_non_self_weighting")
        else:
            upper = wilson_one_sided_upper(event_count, len(plan.members), confidence_level)
    else:
        upper = None
        reasons.append("text_keep_audit_historical_evidence_not_combined")

    if point > point_estimate_max:
        reasons.append("text_keep_audit_point_estimate_above_maximum")
    if upper is None:
        reasons.append("text_keep_audit_confidence_upper_not_available")
    elif upper > one_sided_upper_max:
        reasons.append("text_keep_audit_confidence_upper_above_maximum")
    return TextKeepAuditEvaluation(
        completed_count=len(by_key),
        event_count=event_count,
        ht_point_estimate=point,
        one_sided_upper=upper,
        interval_method=plan.interval_method,
        evaluation_status="passed" if not reasons else "failed",
        failure_reason_codes=tuple(reasons),
        platform_slices=slices,
    )
