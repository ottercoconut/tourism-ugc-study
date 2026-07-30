"""与数据库无关的可复现概率、定向和双标抽样。"""

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


@dataclass(frozen=True)
class SampleMember:
    """一条样本成员及其抽样框、纳入概率和双标要求。"""

    source_post_id: int
    source_version: int
    platform_key: str
    sample_frame: str
    selection_reason_code: str
    selection_rank: int
    inclusion_probability_ppm: int | None
    analysis_weight: float | None
    requires_double_label: bool


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


def _probability_allocation(
    posts: Sequence[SamplingPost],
    *,
    total_size: int,
    minimum_per_platform: int,
) -> dict[str, int]:
    """按平台容量确定固定配额，并以最大余数法分配剩余名额。"""

    population = Counter(post.platform_key for post in posts)
    target = min(total_size, len(posts))
    if target == len(posts):
        return dict(population)
    base = {
        platform: min(minimum_per_platform, count)
        for platform, count in population.items()
    }
    if sum(base.values()) > target:
        # 平台数异常增多导致最低配额总和超过总量时，退化为按平台规模的
        # 概率分配，仍保持每个平台纳入概率可计算。
        base = {platform: 0 for platform in population}
    remaining = target - sum(base.values())
    capacity = {platform: population[platform] - base[platform] for platform in population}
    while remaining > 0:
        total_capacity = sum(capacity.values())
        if total_capacity <= 0:
            break
        ideals = {
            platform: remaining * capacity[platform] / total_capacity
            for platform in population
        }
        increments = {
            platform: min(capacity[platform], math.floor(ideals[platform]))
            for platform in population
        }
        if sum(increments.values()) == 0:
            platform = max(
                (name for name in population if capacity[name] > 0),
                key=lambda name: (ideals[name], name),
            )
            increments[platform] = 1
        for platform, increment in increments.items():
            base[platform] += increment
            capacity[platform] -= increment
            remaining -= increment
    return base


def build_initial_sample_plan(
    posts: Sequence[SamplingPost],
    *,
    config: AnnotationConfig,
    random_seed: int,
) -> SamplePlan:
    """生成首轮概率样本、定向样本和双人盲标子样本。

    概率样本按平台固定最低配额并按剩余容量分配，平台内等概率无放回，
    保存逐平台纳入概率和 Horvitz-Thompson 权重；定向样本只用于困难案例
    覆盖，不生成总体权重。
    两个抽样框允许重合，并以独立成员行保留各自用途。
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
        minimum_per_platform=config.probability_min_per_platform,
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

    targeted_size = min(config.initial_targeted_size, len(posts))
    targeted = sorted(
        posts,
        key=lambda item: (
            _target_reason(item)[0],
            _rank(random_seed, "targeted", item),
        ),
    )[:targeted_size]
    unique_sample_posts = {
        (post.source_post_id, post.source_version): post for post in (*probability, *targeted)
    }
    double_size = min(config.initial_double_label_size, len(unique_sample_posts))
    double_ids = {
        (post.source_post_id, post.source_version)
        for post in sorted(
            unique_sample_posts.values(),
            key=lambda item: _rank(random_seed, "double-label", item),
        )[:double_size]
    }
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
                (post.source_post_id, post.source_version) in double_ids,
            )
        )
    for rank, post in enumerate(targeted, 1):
        members.append(
            SampleMember(
                post.source_post_id,
                post.source_version,
                post.platform_key,
                "targeted",
                _target_reason(post)[1],
                rank,
                None,
                None,
                (post.source_post_id, post.source_version) in double_ids,
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
            False,
        )
        for rank, post in enumerate(selected, 1)
    )
    return SamplePlan(members, _population_manifest(posts), _sha256([m.__dict__ for m in members]))
