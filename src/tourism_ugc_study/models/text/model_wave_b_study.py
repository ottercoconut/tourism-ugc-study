"""冻结双阈值策略下 Wave B 的人口排除、交叉分层与概率抽样。"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence

from .model_reliability_study import ScoredEvaluationMember
from .model_routing_policy_config import ModelRoutingPolicyPlan, RoutingStrategy


class ModelWaveBStudyError(RuntimeError):
    """Wave B 人口或抽样违反冻结策略时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、身份、概率或私有路径的稳定失败码。"""

        super().__init__("formal model wave b study failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class WaveBSampleMember:
    """Wave B 私有映射中的一条概率样本。"""

    task_id: str
    source_post_id: int
    source_version: int
    component_id: str
    normalized_sha256: str
    stratum: str
    selected_action: str
    comparator_action: str
    stratum_population_count: int
    stratum_sample_count: int
    inclusion_probability: float
    analysis_weight: float
    sparse_p_unrelated: float
    qwen_p_unrelated: float


def routing_action(probability: float, strategy: RoutingStrategy) -> str:
    """按冻结边界等号把概率映射为三段动作。

    Args:
        probability: 已封存的无关概率。
        strategy: 模型与两个冻结阈值。

    Returns:
        ``auto_keep``、``manual_review`` 或 ``auto_exclude``。

    Raises:
        ModelWaveBStudyError: 概率不是有限的闭区间数值。
    """

    if (
        not isinstance(probability, (int, float))
        or not math.isfinite(float(probability))
        or not 0.0 <= probability <= 1.0
    ):
        raise ModelWaveBStudyError("model_wave_b_probability_invalid")
    if probability <= strategy.t_keep:
        return "auto_keep"
    if probability >= strategy.t_exclude:
        return "auto_exclude"
    return "manual_review"


def wave_b_stratum(
    member: ScoredEvaluationMember, *, plan: ModelRoutingPolicyPlan
) -> str:
    """返回 Qwen selected 与 sparse comparator 的唯一动作交叉层。"""

    selected_action = routing_action(member.qwen_p_unrelated, plan.selected)
    comparator_action = routing_action(
        member.sparse_p_unrelated, plan.comparator
    )
    return f"qwen_{selected_action}__sparse_{comparator_action}"


def eligible_wave_b_population(
    scored: Sequence[ScoredEvaluationMember],
    *,
    excluded_wave_a_components: frozenset[str],
    plan: ModelRoutingPolicyPlan,
) -> tuple[ScoredEvaluationMember, ...]:
    """整体排除 Wave A 分量并核对冻结九层容量。

    Wave A 的240条可能只涉及227个分量；这里删除这些分量在10,103条
    评分人口中的全部693条成员，避免策略选择证据与独立评价证据相关。
    """

    if (
        len(scored) != plan.selected.population_count
        or len({item.identity for item in scored}) != len(scored)
        or not excluded_wave_a_components
    ):
        raise ModelWaveBStudyError("model_wave_b_scored_population_invalid")
    eligible = tuple(
        item for item in scored if item.component_id not in excluded_wave_a_components
    )
    counts = Counter(wave_b_stratum(item, plan=plan) for item in eligible)
    expected = {item.name: item.population_count for item in plan.wave_b_strata}
    if (
        len(eligible) != plan.wave_b_eligible_population_count
        or len({item.component_id for item in eligible})
        != plan.wave_b_eligible_component_count
        or dict(counts) != expected
    ):
        raise ModelWaveBStudyError("model_wave_b_population_binding_mismatch")
    return eligible


def _random_key(
    seed: int, policy_id: str, member: ScoredEvaluationMember
) -> str:
    """生成不依赖外部随机库版本的稳定无放回抽样键。"""

    payload = (
        f"{seed}|{policy_id}|wave-b|{member.source_post_id}|"
        f"{member.source_version}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sample_wave_b(
    scored: Sequence[ScoredEvaluationMember],
    *,
    excluded_wave_a_components: frozenset[str],
    plan: ModelRoutingPolicyPlan,
) -> tuple[WaveBSampleMember, ...]:
    """按冻结九层分配抽取360条并保存设计权重。

    Args:
        scored: 已封存的10,103条双模型概率人口。
        excluded_wave_a_components: Wave A 私有映射中的全部分量。
        plan: 研究者确认的模型、阈值、容量与样本分配。

    Returns:
        任务 ID 排序的360条私有抽样映射。

    Raises:
        ModelWaveBStudyError: 人口、容量、样本唯一性或设计权重漂移。
    """

    eligible = eligible_wave_b_population(
        scored,
        excluded_wave_a_components=excluded_wave_a_components,
        plan=plan,
    )
    grouped: dict[str, list[ScoredEvaluationMember]] = defaultdict(list)
    for member in eligible:
        grouped[wave_b_stratum(member, plan=plan)].append(member)
    sampled: list[WaveBSampleMember] = []
    for stratum_plan in plan.wave_b_strata:
        ordered = sorted(
            grouped[stratum_plan.name],
            key=lambda member: (
                _random_key(plan.random_seed, plan.policy_id, member),
                member.identity,
            ),
        )
        inclusion_probability = (
            stratum_plan.sample_count / stratum_plan.population_count
        )
        for member in ordered[: stratum_plan.sample_count]:
            selected_action = routing_action(
                member.qwen_p_unrelated, plan.selected
            )
            comparator_action = routing_action(
                member.sparse_p_unrelated, plan.comparator
            )
            task_id = hashlib.sha256(
                (
                    f"{plan.policy_id}|wave-b|{member.source_post_id}|"
                    f"{member.source_version}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            sampled.append(
                WaveBSampleMember(
                    task_id=task_id,
                    source_post_id=member.source_post_id,
                    source_version=member.source_version,
                    component_id=member.component_id,
                    normalized_sha256=member.normalized_sha256,
                    stratum=stratum_plan.name,
                    selected_action=selected_action,
                    comparator_action=comparator_action,
                    stratum_population_count=stratum_plan.population_count,
                    stratum_sample_count=stratum_plan.sample_count,
                    inclusion_probability=inclusion_probability,
                    analysis_weight=1.0 / inclusion_probability,
                    sparse_p_unrelated=member.sparse_p_unrelated,
                    qwen_p_unrelated=member.qwen_p_unrelated,
                )
            )
    sampled.sort(key=lambda item: item.task_id)
    if (
        len(sampled) != plan.wave_b_target_sample_count
        or len({item.task_id for item in sampled}) != len(sampled)
        or len(
            {(item.source_post_id, item.source_version) for item in sampled}
        )
        != len(sampled)
        or any(
            item.component_id in excluded_wave_a_components
            or not 0.0 < item.inclusion_probability <= 1.0
            or item.analysis_weight != 1.0 / item.inclusion_probability
            for item in sampled
        )
    ):
        raise ModelWaveBStudyError("model_wave_b_sample_invalid")
    return tuple(sampled)
