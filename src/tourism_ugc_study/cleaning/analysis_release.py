"""分析发布的纯文本帖子投影。

模块只组合显式选定的帖子最终决定与文本去重簇，不访问 SQLite、源库或网络。
正式接受仍由仓储层复验快照、人工审计和不可变发布包后完成。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePath
from types import MappingProxyType
from typing import Literal, Mapping, Sequence
from urllib.parse import urlparse

from .analysis_dedup import AnalysisDedupBuild, PostIdentity
from .post_decision import PostDecision


ReleaseMode = Literal["formal", "smoke"]
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_METADATA_PARTS = frozenset(
    {"absolute_path", "author", "caption", "content", "cookie", "credential",
     "password", "raw", "secret", "text", "title", "token", "ugc", "url", "uri"}
)


@dataclass(frozen=True)
class ReleaseRequest:
    """一次发布投影的显式、去敏请求身份。"""

    release_id: str
    run_id: str
    mode: ReleaseMode
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class AnalysisPostMember:
    """一个绑定发布和运行的帖子版本成员。"""

    release_id: str
    run_id: str
    source_post_id: int
    source_version: int
    cluster_id: str | None = None
    member_reason_code: str = "post_decision_keep"

    @property
    def identity(self) -> PostIdentity:
        """返回稳定帖子版本身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class ReleaseQualityReport:
    """只包含计数和哈希的发布质量摘要。"""

    release_id: str
    run_id: str
    mode: ReleaseMode
    request_metadata_sha256: str
    input_lineage_sha256: str
    post_decision_counts: Mapping[str, int]
    post_reason_counts: Mapping[str, int]
    dedup_counts: Mapping[str, int]
    output_member_counts: Mapping[str, int]
    report_sha256: str


@dataclass(frozen=True)
class AnalysisReleaseProjection:
    """文本清洗允许发布的两个帖子集合。"""

    release_id: str
    run_id: str
    mode: ReleaseMode
    analysis_posts_eligible: tuple[AnalysisPostMember, ...]
    analysis_posts_deduplicated: tuple[AnalysisPostMember, ...]
    quality_report: ReleaseQualityReport
    manifest_sha256: str
    acceptance_state: str = "projection_only"
    requires_repository_acceptance: bool = True


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a safe explicit identifier")
    if value.casefold() == "latest":
        raise ValueError(f"{field} cannot use a mutable latest alias")


def _validate_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be lowercase hexadecimal sha256")


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and (parsed.netloc or parsed.scheme == "file"))


def _validate_metadata(value: object) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        raise ValueError("release metadata cannot contain floating point values")
    if isinstance(value, str):
        if PurePath(value).is_absolute() or _looks_like_url(value):
            raise ValueError("release metadata cannot contain paths or urls")
        if not _SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError("release metadata strings must be stable identifiers")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not _SAFE_METADATA_KEY.fullmatch(key):
                raise ValueError("release metadata keys must use lowercase snake_case")
            if any(part in key for part in _FORBIDDEN_METADATA_PARTS):
                raise ValueError("release metadata contains a sensitive field")
            _validate_metadata(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_metadata(child)
        return
    raise ValueError("release metadata contains an unsupported value type")


def _normalized(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _normalized(child) for key, child in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalized(child) for child in value]
    return value


def _validate_request(request: ReleaseRequest) -> str:
    _validate_identifier(request.release_id, "release_id")
    _validate_identifier(request.run_id, "run_id")
    if request.mode not in {"formal", "smoke"}:
        raise ValueError("release mode must be formal or smoke")
    _validate_metadata(request.metadata)
    return _canonical_sha256(_normalized(request.metadata))


def _validate_decisions(
    decisions: Sequence[PostDecision],
) -> dict[PostIdentity, PostDecision]:
    by_identity: dict[PostIdentity, PostDecision] = {}
    for decision in decisions:
        identity = decision.source_post_id, decision.source_version
        if min(identity) <= 0 or identity in by_identity:
            raise ValueError("post decisions require unique positive identities")
        if decision.decision not in {"keep", "review", "exclude"}:
            raise ValueError("unsupported post decision")
        _validate_sha256(decision.decision_sha256, "post decision sha256")
        for reason in decision.reason_codes:
            _validate_identifier(reason, "post decision reason code")
        by_identity[identity] = decision
    return by_identity


def _dedup_index(
    dedup: AnalysisDedupBuild,
    identities: set[PostIdentity],
) -> dict[PostIdentity, tuple[str, PostIdentity]]:
    _validate_sha256(dedup.manifest_sha256, "analysis dedup manifest sha256")
    index: dict[PostIdentity, tuple[str, PostIdentity]] = {}
    cluster_ids: set[str] = set()
    for cluster in dedup.clusters:
        _validate_identifier(cluster.cluster_id, "analysis dedup cluster id")
        if cluster.cluster_id in cluster_ids or not cluster.members:
            raise ValueError("invalid analysis dedup cluster")
        cluster_ids.add(cluster.cluster_id)
        if cluster.representative not in cluster.members:
            raise ValueError("analysis dedup representative is not a member")
        for identity in cluster.members:
            if identity in index:
                raise ValueError("post appears in more than one analysis dedup cluster")
            index[identity] = (cluster.cluster_id, cluster.representative)
    if set(index) != identities:
        raise ValueError("analysis dedup clusters must partition all post decisions")
    return index


def _post_members(
    request: ReleaseRequest,
    decisions: Mapping[PostIdentity, PostDecision],
    dedup: AnalysisDedupBuild,
) -> tuple[tuple[AnalysisPostMember, ...], tuple[AnalysisPostMember, ...]]:
    index = _dedup_index(dedup, set(decisions))
    eligible_ids = {
        identity for identity, decision in decisions.items() if decision.decision == "keep"
    }
    eligible = tuple(
        AnalysisPostMember(
            request.release_id, request.run_id, *identity, index[identity][0]
        )
        for identity in sorted(eligible_ids)
    )
    deduplicated: list[AnalysisPostMember] = []
    for cluster in sorted(dedup.clusters, key=lambda item: item.cluster_id):
        available = tuple(identity for identity in cluster.members if identity in eligible_ids)
        if not available:
            continue
        if cluster.representative in eligible_ids:
            representative = cluster.representative
            reason = "analysis_dedup_cluster_representative"
        else:
            representative = min(available)
            reason = "analysis_dedup_eligible_representative_fallback"
        deduplicated.append(
            AnalysisPostMember(
                request.release_id,
                request.run_id,
                *representative,
                cluster.cluster_id,
                reason,
            )
        )
    return eligible, tuple(sorted(deduplicated))


def build_analysis_release(
    request: ReleaseRequest,
    post_decisions: Sequence[PostDecision],
    analysis_dedup: AnalysisDedupBuild,
) -> AnalysisReleaseProjection:
    """构建合格帖子和去重帖子集合，并生成确定性去敏报告。"""

    metadata_sha256 = _validate_request(request)
    decisions = _validate_decisions(post_decisions)
    eligible, deduplicated = _post_members(request, decisions, analysis_dedup)
    lineage_sha256 = _canonical_sha256(
        {
            "analysis_dedup_manifest_sha256": analysis_dedup.manifest_sha256,
            "post_decisions": [
                [*identity, decision.decision, decision.decision_sha256]
                for identity, decision in sorted(decisions.items())
            ],
        }
    )
    decision_counts = Counter(decision.decision for decision in decisions.values())
    reason_counts = Counter(
        reason for decision in decisions.values() for reason in decision.reason_codes
    )
    dedup_counts = {
        "cluster_count": len(analysis_dedup.clusters),
        "member_count": len(decisions),
        "eligible_representative_count": len(deduplicated),
        "eligible_nonrepresentative_count": len(eligible) - len(deduplicated),
    }
    output_counts = {
        "analysis_posts_eligible": len(eligible),
        "analysis_posts_deduplicated": len(deduplicated),
    }
    report_payload = {
        "release_id": request.release_id,
        "run_id": request.run_id,
        "mode": request.mode,
        "request_metadata_sha256": metadata_sha256,
        "input_lineage_sha256": lineage_sha256,
        "post_decision_counts": dict(sorted(decision_counts.items())),
        "post_reason_counts": dict(sorted(reason_counts.items())),
        "dedup_counts": dedup_counts,
        "output_member_counts": output_counts,
    }
    report = ReleaseQualityReport(
        release_id=request.release_id,
        run_id=request.run_id,
        mode=request.mode,
        request_metadata_sha256=metadata_sha256,
        input_lineage_sha256=lineage_sha256,
        post_decision_counts=MappingProxyType(dict(sorted(decision_counts.items()))),
        post_reason_counts=MappingProxyType(dict(sorted(reason_counts.items()))),
        dedup_counts=MappingProxyType(dedup_counts),
        output_member_counts=MappingProxyType(output_counts),
        report_sha256=_canonical_sha256(report_payload),
    )
    manifest = {
        "acceptance_state": "projection_only",
        "analysis_posts_eligible": [
            [member.source_post_id, member.source_version, member.cluster_id,
             member.member_reason_code] for member in eligible
        ],
        "analysis_posts_deduplicated": [
            [member.source_post_id, member.source_version, member.cluster_id,
             member.member_reason_code] for member in deduplicated
        ],
        "mode": request.mode,
        "quality_report_sha256": report.report_sha256,
        "release_id": request.release_id,
        "run_id": request.run_id,
    }
    return AnalysisReleaseProjection(
        release_id=request.release_id,
        run_id=request.run_id,
        mode=request.mode,
        analysis_posts_eligible=eligible,
        analysis_posts_deduplicated=deduplicated,
        quality_report=report,
        manifest_sha256=_canonical_sha256(manifest),
    )
