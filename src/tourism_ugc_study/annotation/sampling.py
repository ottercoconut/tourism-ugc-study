"""与数据库无关的可复现概率、定向和周期抽样。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from .config import AnnotationConfig


@dataclass(frozen=True)
class SamplingPost:
    """抽样算法所需的去标识化帖子投影。"""

    source_post_id: int
    source_version: int
    platform_key: str
    normalized_length: int
    near_candidate_count: int
    cross_platform_near_count: int
    near_component_id: str = ""


@dataclass(frozen=True)
class SampleMember:
    """一条样本成员及其抽样框和纳入概率。"""

    source_post_id: int
    source_version: int
    platform_key: str
    sample_frame: str
    selection_reason_code: str
    selection_rank: int
    inclusion_probability_ppm: int | None
    analysis_weight: float | None


@dataclass(frozen=True)
class SamplePlan:
    """完全由输入语料、配置和种子决定的不可变抽样计划。"""

    members: tuple[SampleMember, ...]
    population_manifest_sha256: str
    output_sha256: str


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rank(seed: int, namespace: str, post: SamplingPost) -> str:
    """生成与输入顺序无关的稳定伪随机排序键。"""

    return _sha256([seed, namespace, post.source_post_id, post.source_version])


def _population_manifest(posts: Sequence[SamplingPost]) -> str:
    return _sha256(
        [
            [post.source_post_id, post.source_version, post.platform_key]
            for post in sorted(posts, key=lambda item: (item.source_post_id, item.source_version))
        ]
    )


def _target_reason(post: SamplingPost) -> tuple[int, str]:
    """按可复核的非语义信号排序定向困难样本，模型标签不参与抽样。"""

    if post.cross_platform_near_count:
        return 0, "cross_platform_near_candidate"
    if post.near_candidate_count:
        return 1, "near_candidate"
    if post.normalized_length <= 80:
        return 2, "short_usable_text"
    return 3, "deterministic_fill"


def _target_component_key(post: SamplingPost) -> str:
    """返回候选分量身份；旧测试投影缺少分量时退化为帖子单例。"""

    return post.near_component_id or f"singleton:{post.source_post_id}:{post.source_version}"


def _target_component_choice(
    members: Sequence[SamplingPost],
    *,
    random_seed: int,
) -> tuple[tuple[int, str], str, SamplingPost]:
    """为一个候选分量选择唯一代表，并生成稳定的分量排序键。

    排序继续沿用原定向帖子哈希，使修订前已经选中的每个分量优先保留其
    原有首项。选择只依赖冻结身份和非语义候选信号，不读取人工标签。
    """

    reason = min((_target_reason(post) for post in members), key=lambda item: item[0])
    ranked = sorted(members, key=lambda post: _rank(random_seed, "targeted", post))
    return reason, _rank(random_seed, "targeted", ranked[0]), ranked[0]


def _probability_allocation(
    posts: Sequence[SamplingPost],
    *,
    total_size: int,
) -> dict[str, int]:
    """按平台人口占比，以最大余数法分配固定概率样本配额。

    配额从零开始按平台人口占比分配，不设置最低样本数。先取各平台
    理想配额的向下整数，再按小数余数从大到小补齐；余数相同时以平台键
    稳定决胜。总人口不足目标量时执行全查。
    """

    population = Counter(post.platform_key for post in posts)
    target = min(total_size, len(posts))
    if target == 0:
        return {}
    if target == len(posts):
        return dict(population)
    ideals = {
        platform: target * count / len(posts)
        for platform, count in population.items()
    }
    quotas = {platform: math.floor(ideal) for platform, ideal in ideals.items()}
    remaining = target - sum(quotas.values())
    remainder_order = sorted(
        population,
        key=lambda platform: (-(ideals[platform] - quotas[platform]), platform),
    )
    for platform in remainder_order[:remaining]:
        quotas[platform] += 1
    return quotas


def build_initial_sample_plan(
    posts: Sequence[SamplingPost],
    *,
    config: AnnotationConfig,
    random_seed: int,
) -> SamplePlan:
    """生成首轮概率样本和定向边界样本。

    概率样本从零开始按平台人口占比分配，平台内等概率无放回，保存逐平台
    纳入概率和 Horvitz-Thompson 权重；定向样本只用于困难案例覆盖，不生成
    总体权重。
    定向样本与概率样本在候选分量层面互斥，并且每个候选分量最多选择
    一个帖子代表，以最大化有限人工名额覆盖的文本家族多样性。
    """

    identities = [(post.source_post_id, post.source_version) for post in posts]
    if len(identities) != len(set(identities)):
        raise ValueError("sampling population contains duplicate identities")
    ordered_population = tuple(
        sorted(posts, key=lambda item: (item.source_post_id, item.source_version))
    )
    quotas = _probability_allocation(
        posts,
        total_size=config.initial_probability_size,
    )
    by_platform: dict[str, list[SamplingPost]] = defaultdict(list)
    for post in posts:
        by_platform[post.platform_key].append(post)
    probability = [
        post
        for platform in sorted(by_platform)
        for post in sorted(
            by_platform[platform],
            key=lambda item: _rank(random_seed, f"probability:{platform}", item),
        )[: quotas.get(platform, 0)]
    ]

    # 定向样本的观察单位仍是帖子，但选择单位改为近重复候选分量。概率样本
    # 已覆盖的分量不再进入定向框，避免同一内容家族重复消耗人工相关性名额。
    # 候选分量只约束抽样多样性，不会被写成最终重复真值。
    probability_identities = {
        (post.source_post_id, post.source_version) for post in probability
    }
    probability_components = {_target_component_key(post) for post in probability}
    by_component: dict[str, list[SamplingPost]] = defaultdict(list)
    for post in posts:
        by_component[_target_component_key(post)].append(post)
    component_choices = [
        _target_component_choice(members, random_seed=random_seed)
        for component_id, members in sorted(by_component.items())
        if component_id not in probability_components
        and not any(
            (post.source_post_id, post.source_version) in probability_identities
            for post in members
        )
    ]
    targeted_size = min(config.initial_targeted_size, len(component_choices))
    targeted_choices = sorted(
        component_choices,
        key=lambda item: (item[0][0], item[1]),
    )[:targeted_size]
    members: list[SampleMember] = []
    for rank, post in enumerate(probability, 1):
        platform_population = len(by_platform[post.platform_key])
        platform_sample = quotas[post.platform_key]
        inclusion_ppm = min(
            1_000_000,
            round(platform_sample / platform_population * 1_000_000),
        )
        analysis_weight = platform_population / platform_sample
        members.append(
            SampleMember(
                post.source_post_id,
                post.source_version,
                post.platform_key,
                "probability",
                "equal_probability",
                rank,
                inclusion_ppm,
                analysis_weight,
            )
        )
    for rank, (reason, _, post) in enumerate(targeted_choices, 1):
        members.append(
            SampleMember(
                post.source_post_id,
                post.source_version,
                post.platform_key,
                "targeted",
                reason[1],
                rank,
                None,
                None,
            )
        )
    payload = [member.__dict__ for member in members]
    # 引用该变量可让空语料与有序语料都进入 manifest，且避免调用方输入顺序改变结果。
    population_manifest = _population_manifest(ordered_population)
    return SamplePlan(tuple(members), population_manifest, _sha256(payload))


def build_periodic_sample_plan(
    posts: Sequence[SamplingPost],
    *,
    already_sampled_ids: Iterable[int],
    sample_size: int,
    random_seed: int,
    round_number: int,
) -> SamplePlan:
    """从新增且未复核的帖子中抽取一个周期概率复核样本。"""

    if round_number <= 0:
        raise ValueError("round_number must be positive")
    excluded = set(already_sampled_ids)
    eligible = [post for post in posts if post.source_post_id not in excluded]
    selected = sorted(
        eligible,
        key=lambda item: _rank(random_seed, f"periodic-{round_number}", item),
    )[: min(sample_size, len(eligible))]
    inclusion_ppm = (
        min(1_000_000, round(len(selected) / len(eligible) * 1_000_000))
        if eligible and selected
        else None
    )
    weight = len(eligible) / len(selected) if selected else None
    members = tuple(
        SampleMember(
            post.source_post_id,
            post.source_version,
            post.platform_key,
            "periodic_probability",
            f"new_posts_round_{round_number}",
            rank,
            inclusion_ppm,
            weight,
        )
        for rank, post in enumerate(selected, 1)
    )
    return SamplePlan(members, _population_manifest(posts), _sha256([m.__dict__ for m in members]))


def freeze_periodic_source_id_window(
    first_seen_source_post_ids: Iterable[int],
    *,
    round_number: int,
    increment_posts: int,
) -> tuple[int, ...]:
    """按首次出现顺序冻结一个不重叠的新增帖子窗口。

    输入必须来自 inventory 的 `first_seen_snapshot_id`，而不是当前库行数之差；
    因此旧帖删失不会抵消新增量，同一帖产生新 source_version 也不会重复计数。
    调用方只有在返回完整 `increment_posts` 个 ID 时才能创建该轮次。
    """

    if round_number <= 0 or increment_posts <= 0:
        raise ValueError("round_number and increment_posts must be positive")
    ordered = tuple(first_seen_source_post_ids)
    if len(ordered) != len(set(ordered)):
        raise ValueError("first-seen source post IDs must be unique")
    start = (round_number - 1) * increment_posts
    return ordered[start : start + increment_posts]
