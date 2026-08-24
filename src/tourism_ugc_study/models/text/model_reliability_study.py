"""新标签评价的人口排除、双模型分层与概率抽样核心。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from tourism_ugc_study.cleaning.reference_projection import (
    ReferenceProjectionError,
    load_reference_text_projection,
)
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .model_reliability_config import ModelReliabilityPlan


class ModelReliabilityStudyError(RuntimeError):
    """评价人口、概率或抽样违反契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("formal model reliability study failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class EligibleEvaluationMember:
    """排除最终参考泄漏分量后可供评价的一条候选帖子。"""

    source_post_id: int
    source_version: int
    component_id: str
    platform_key: str
    normalized_model_text: str
    normalized_sha256: str

    @property
    def identity(self) -> tuple[int, int]:
        """返回与派生库共享的稳定源身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class ScoredEvaluationMember:
    """两个冻结模型对同一合格成员给出的无关概率。"""

    source_post_id: int
    source_version: int
    component_id: str
    normalized_sha256: str
    sparse_p_unrelated: float
    qwen_p_unrelated: float

    @property
    def identity(self) -> tuple[int, int]:
        """返回稳定源身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class WaveSampleMember:
    """Wave A 的隐藏分层映射与设计权重。"""

    task_id: str
    source_post_id: int
    source_version: int
    component_id: str
    normalized_sha256: str
    stratum: str
    stratum_population_count: int
    stratum_sample_count: int
    inclusion_probability: float
    analysis_weight: float
    sparse_p_unrelated: float
    qwen_p_unrelated: float


@dataclass(frozen=True)
class EligiblePopulation:
    """合格评价人口及排除谱系的去敏摘要。"""

    members: tuple[EligibleEvaluationMember, ...]
    candidate_count: int
    candidate_component_count: int
    reference_count: int
    reference_component_count: int
    excluded_count: int
    eligible_component_count: int
    projection_member_sha256: str
    leakage_output_sha256: str


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256 并统一失败码。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelReliabilityStudyError(
            "model_reliability_input_unreadable"
        ) from exc
    return digest.hexdigest()


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    """以只读和 query-only 模式打开派生 SQLite。"""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise ModelReliabilityStudyError(
            "model_reliability_database_readonly_open_failed"
        ) from exc


def _load_reference_identities(
    csv_path: str | Path, *, expected_sha256: str
) -> set[tuple[int, int]]:
    """读取最终参考 CSV 的身份列，不读取标签或旧概率字段。"""

    path = Path(csv_path)
    if _file_sha256(path) != expected_sha256:
        raise ModelReliabilityStudyError(
            "model_reliability_reference_hash_mismatch"
        )
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or not {
                "source_post_id",
                "source_version",
            }.issubset(reader.fieldnames):
                raise ValueError
            identities = {
                (int(row["source_post_id"]), int(row["source_version"]))
                for row in reader
            }
    except (OSError, UnicodeError, csv.Error, TypeError, ValueError) as exc:
        raise ModelReliabilityStudyError(
            "model_reliability_reference_identity_invalid"
        ) from exc
    if any(post_id <= 0 or version <= 0 for post_id, version in identities):
        raise ModelReliabilityStudyError(
            "model_reliability_reference_identity_invalid"
        )
    return identities


def load_eligible_evaluation_population(
    reference_csv: str | Path,
    derived_db: str | Path,
    *,
    plan: ModelReliabilityPlan,
    normalization_config: TextCleaningConfig,
) -> EligiblePopulation:
    """构造不与最终参考700条共享任何 leakage component 的人口。

    Args:
        reference_csv: 当前最终参考 CSV，仅用于身份排除和文件哈希验证。
        derived_db: 已封存候选构建和 finalized leakage build 的派生库。
        plan: 冻结模型评价计划。
        normalization_config: 与既有模型相同的规范化文本规则。

    Returns:
        10,103 条预期合格帖子、7,500 个分量和完整去敏谱系。

    Raises:
        ModelReliabilityStudyError: 构建、成员、哈希、计数或只读边界漂移。
    """

    reference = _load_reference_identities(
        reference_csv, expected_sha256=plan.reference_csv_sha256
    )
    connection = _readonly_connection(derived_db)
    try:
        build = connection.execute(
            """
            SELECT candidate_build_id, input_post_count, component_count,
                   output_sha256, seal_status
            FROM text_leakage_builds WHERE leakage_build_id = ?
            """,
            (plan.leakage_build_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT l.component_id, l.source_post_id, l.source_version,
                   c.platform_key
            FROM text_leakage_members AS l
            JOIN text_candidate_corpus_members AS c
              ON c.build_id = ?
             AND c.source_post_id = l.source_post_id
             AND c.source_version = l.source_version
            WHERE l.leakage_build_id = ?
            ORDER BY l.source_post_id, l.source_version
            """,
            (plan.candidate_build_id, plan.leakage_build_id),
        ).fetchall()
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    except sqlite3.Error as exc:
        raise ModelReliabilityStudyError(
            "model_reliability_leakage_contract_invalid"
        ) from exc
    finally:
        connection.close()
    if (
        build is None
        or str(build["candidate_build_id"]) != plan.candidate_build_id
        or str(build["seal_status"]) != "finalized"
        or int(build["input_post_count"]) != plan.expected_candidate_count
        or int(build["component_count"])
        != plan.expected_candidate_component_count
        or query_only != 1
    ):
        raise ModelReliabilityStudyError(
            "model_reliability_leakage_contract_invalid"
        )
    try:
        binding_by_identity = {
            (int(row["source_post_id"]), int(row["source_version"])): (
                str(row["component_id"]),
                str(row["platform_key"]),
            )
            for row in rows
        }
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelReliabilityStudyError(
            "model_reliability_leakage_member_invalid"
        ) from exc
    if (
        len(rows) != plan.expected_candidate_count
        or len(binding_by_identity) != len(rows)
        or any(not platform.strip() for _component, platform in binding_by_identity.values())
        or len({component for component, _platform in binding_by_identity.values()})
        != plan.expected_candidate_component_count
        or not reference.issubset(binding_by_identity)
        or len(reference) != plan.expected_reference_count
    ):
        raise ModelReliabilityStudyError(
            "model_reliability_leakage_member_invalid"
        )
    reference_components = {binding_by_identity[item][0] for item in reference}
    if len(reference_components) != plan.expected_reference_component_count:
        raise ModelReliabilityStudyError(
            "model_reliability_reference_component_count_mismatch"
        )
    try:
        projection = load_reference_text_projection(
            derived_db,
            candidate_build_id=plan.candidate_build_id,
            config=normalization_config,
        )
    except ReferenceProjectionError as exc:
        raise ModelReliabilityStudyError(exc.reason_code) from exc
    if set(projection.by_identity) != set(binding_by_identity):
        raise ModelReliabilityStudyError(
            "model_reliability_projection_membership_mismatch"
        )
    eligible: list[EligibleEvaluationMember] = []
    for identity, (component_id, platform_key) in binding_by_identity.items():
        if component_id in reference_components:
            continue
        projected = projection.by_identity[identity]
        if projected.structure_status != "usable":
            raise ModelReliabilityStudyError(
                "model_reliability_eligible_structure_invalid"
            )
        eligible.append(
            EligibleEvaluationMember(
                source_post_id=identity[0],
                source_version=identity[1],
                component_id=component_id,
                platform_key=platform_key,
                normalized_model_text=projected.normalized_model_text,
                normalized_sha256=projected.normalized_sha256,
            )
        )
    eligible.sort(key=lambda item: item.identity)
    eligible_components = {item.component_id for item in eligible}
    if (
        len(eligible) != plan.expected_eligible_count
        or len(eligible_components) != plan.expected_eligible_component_count
    ):
        raise ModelReliabilityStudyError(
            "model_reliability_eligible_population_count_mismatch"
        )
    return EligiblePopulation(
        members=tuple(eligible),
        candidate_count=len(rows),
        candidate_component_count=len(
            {component for component, _platform in binding_by_identity.values()}
        ),
        reference_count=len(reference),
        reference_component_count=len(reference_components),
        excluded_count=len(rows) - len(eligible),
        eligible_component_count=len(eligible_components),
        projection_member_sha256=projection.member_sha256,
        leakage_output_sha256=str(build["output_sha256"]),
    )


def pair_model_scores(
    members: Sequence[EligibleEvaluationMember],
    sparse_probabilities: Sequence[float],
    qwen_probabilities: Sequence[float],
) -> tuple[ScoredEvaluationMember, ...]:
    """把两个模型的纯预测按同一成员顺序配对并校验概率。"""

    sparse = np.asarray(sparse_probabilities, dtype=float)
    qwen = np.asarray(qwen_probabilities, dtype=float)
    if (
        not members
        or sparse.shape != (len(members),)
        or qwen.shape != (len(members),)
        or not np.isfinite(sparse).all()
        or not np.isfinite(qwen).all()
        or np.any((sparse < 0.0) | (sparse > 1.0))
        or np.any((qwen < 0.0) | (qwen > 1.0))
    ):
        raise ModelReliabilityStudyError(
            "model_reliability_prediction_invalid"
        )
    return tuple(
        ScoredEvaluationMember(
            source_post_id=member.source_post_id,
            source_version=member.source_version,
            component_id=member.component_id,
            normalized_sha256=member.normalized_sha256,
            sparse_p_unrelated=float(sparse_probability),
            qwen_p_unrelated=float(qwen_probability),
        )
        for member, sparse_probability, qwen_probability in zip(
            members, sparse, qwen, strict=True
        )
    )


def wave_a_stratum(member: ScoredEvaluationMember) -> str:
    """按冻结优先级把每条成员分配到恰好一个 Wave A 分层。"""

    sparse = member.sparse_p_unrelated
    qwen = member.qwen_p_unrelated
    if sparse >= 0.90 and qwen >= 0.90:
        return "both_p_ge_0_90"
    if qwen >= 0.90:
        return "qwen_only_p_ge_0_90"
    if sparse >= 0.90:
        return "sparse_only_p_ge_0_90"
    if (sparse >= 0.50) != (qwen >= 0.50):
        return "action_disagreement_at_0_50"
    if sparse >= 0.50 and qwen >= 0.50:
        return "both_p_ge_0_50_remainder"
    return "both_p_lt_0_50"


def _redistributed_allocations(
    population_counts: Mapping[str, int], targets: Mapping[str, int]
) -> dict[str, int]:
    """小层全取后按其余各层剩余容量比例确定性补足样本。"""

    if set(population_counts) != set(targets):
        raise ModelReliabilityStudyError(
            "model_reliability_wave_a_strata_invalid"
        )
    allocation = {
        stratum: min(int(targets[stratum]), int(population_counts[stratum]))
        for stratum in targets
    }
    required = sum(targets.values())
    shortage = required - sum(allocation.values())
    while shortage > 0:
        capacities = {
            stratum: population_counts[stratum] - allocation[stratum]
            for stratum in targets
            if population_counts[stratum] > allocation[stratum]
        }
        total_capacity = sum(capacities.values())
        if total_capacity < shortage or total_capacity <= 0:
            raise ModelReliabilityStudyError(
                "model_reliability_wave_a_population_too_small"
            )
        exact = {
            stratum: shortage * capacity / total_capacity
            for stratum, capacity in capacities.items()
        }
        added = 0
        for stratum in targets:
            if stratum not in capacities:
                continue
            increment = min(capacities[stratum], math.floor(exact[stratum]))
            allocation[stratum] += increment
            added += increment
        remainder = shortage - added
        ranking = sorted(
            capacities,
            key=lambda stratum: (
                -(exact[stratum] - math.floor(exact[stratum])),
                list(targets).index(stratum),
            ),
        )
        for stratum in ranking:
            if remainder == 0:
                break
            if allocation[stratum] < population_counts[stratum]:
                allocation[stratum] += 1
                remainder -= 1
        new_shortage = required - sum(allocation.values())
        if new_shortage >= shortage:
            raise ModelReliabilityStudyError(
                "model_reliability_wave_a_redistribution_failed"
            )
        shortage = new_shortage
    return allocation


def _random_key(seed: int, wave: str, member: ScoredEvaluationMember) -> str:
    """生成不依赖 NumPy 版本的稳定抽样随机键。"""

    payload = (
        f"{seed}|{wave}|{member.source_post_id}|{member.source_version}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sample_wave_a(
    scored: Sequence[ScoredEvaluationMember], *, plan: ModelReliabilityPlan
) -> tuple[WaveSampleMember, ...]:
    """按冻结六层计划抽取240条并保留纳入概率与分析权重。

    Notes:
        抽样单位是帖子；leakage component 用于排除最终参考成员、Wave B
        去重及后续 cluster bootstrap，而不是把某个平台或作者用于分层。
    """

    if len(scored) != plan.expected_eligible_count or len(
        {item.identity for item in scored}
    ) != len(scored):
        raise ModelReliabilityStudyError(
            "model_reliability_scored_population_invalid"
        )
    grouped: dict[str, list[ScoredEvaluationMember]] = defaultdict(list)
    for member in scored:
        grouped[wave_a_stratum(member)].append(member)
    counts = {stratum: len(grouped[stratum]) for stratum in plan.wave_a_allocations}
    allocation = _redistributed_allocations(counts, plan.wave_a_allocations)
    sampled: list[WaveSampleMember] = []
    for stratum in plan.wave_a_allocations:
        ordered = sorted(
            grouped[stratum],
            key=lambda member: (_random_key(plan.random_seed, "wave-a", member), member.identity),
        )
        sample_count = allocation[stratum]
        population_count = counts[stratum]
        inclusion_probability = sample_count / population_count
        for member in ordered[:sample_count]:
            task_id = hashlib.sha256(
                (
                    f"{plan.plan_id}|wave-a|{member.source_post_id}|"
                    f"{member.source_version}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            sampled.append(
                WaveSampleMember(
                    task_id=task_id,
                    source_post_id=member.source_post_id,
                    source_version=member.source_version,
                    component_id=member.component_id,
                    normalized_sha256=member.normalized_sha256,
                    stratum=stratum,
                    stratum_population_count=population_count,
                    stratum_sample_count=sample_count,
                    inclusion_probability=inclusion_probability,
                    analysis_weight=1.0 / inclusion_probability,
                    sparse_p_unrelated=member.sparse_p_unrelated,
                    qwen_p_unrelated=member.qwen_p_unrelated,
                )
            )
    sampled.sort(key=lambda item: item.task_id)
    if (
        len(sampled) != plan.wave_a_sample_size
        or len({item.task_id for item in sampled}) != len(sampled)
        or any(
            not 0.0 < item.inclusion_probability <= 1.0 for item in sampled
        )
    ):
        raise ModelReliabilityStudyError(
            "model_reliability_wave_a_sample_invalid"
        )
    return tuple(sampled)


def wave_a_stratum_counts(
    scored: Sequence[ScoredEvaluationMember],
) -> Mapping[str, int]:
    """返回不含身份和概率的 Wave A 人口分层计数。"""

    return dict(sorted(Counter(wave_a_stratum(item) for item in scored).items()))
