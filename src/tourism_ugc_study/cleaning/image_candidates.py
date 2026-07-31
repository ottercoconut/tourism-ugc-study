"""图片精确重复簇、pHash 近似对和技术候选信号的纯计算。

候选信号只用于安排后续人工复核，绝不生成排除标签。来源角色已在上游分流，
因此本模块接收的记录均为 `content` 图片，包括路线图和推荐计划图。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping, Sequence

from .config import ImageConfig


_URL_ROLE_HINT = re.compile(
    r"(?:avatar|head|profile|icon|logo|touxiang|sprite|background|default|"
    r"placeholder|error|loading|(?:^|[/_.-])(?:bg|qr)(?:[/_.-]|$))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CandidateImage:
    """候选构建所需的最小无路径投影。"""

    fingerprint_id: str
    row_identity_sha256: str
    source_image_id: int
    source_post_id: int
    author_identity_sha256: str | None
    file_sha256: str
    phash_hex: str
    byte_size: int
    width_px: int
    height_px: int
    is_fully_transparent: bool
    url_role_hint: bool


@dataclass(frozen=True)
class CandidateSignal:
    """一个待复核技术信号及其去敏证据。"""

    fingerprint_id: str
    signal_code: str
    evidence: Mapping[str, int | float | bool]


@dataclass(frozen=True)
class ExactImageCluster:
    """按文件 SHA-256 形成的精确文件簇。"""

    cluster_id: str
    file_sha256: str
    representative_fingerprint_id: str
    member_fingerprint_ids: tuple[str, ...]


@dataclass(frozen=True)
class NearImagePair:
    """两个不同精确簇代表之间的 pHash 候选关系。"""

    left_fingerprint_id: str
    right_fingerprint_id: str
    hamming_distance: int


@dataclass(frozen=True)
class ImageCandidatePlan:
    """可整体持久化并封存的确定性图片候选计划。"""

    members: tuple[CandidateImage, ...]
    signals: tuple[CandidateSignal, ...]
    exact_clusters: tuple[ExactImageCluster, ...]
    near_pairs: tuple[NearImagePair, ...]
    input_manifest_sha256: str
    output_sha256: str


def url_has_role_hint(url: str | None) -> bool:
    """只返回 URL 角色词信号，不返回或持久化 URL 原文。"""

    return bool(url and _URL_ROLE_HINT.search(url))


def phash_hamming_distance(left_hex: str, right_hex: str) -> int:
    """计算固定长度十六进制 pHash 的汉明距离。"""

    if len(left_hex) != len(right_hex) or not left_hex:
        raise ValueError("pHash values must have equal non-zero length")
    return (int(left_hex, 16) ^ int(right_hex, 16)).bit_count()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _technical_signals(record: CandidateImage, config: ImageConfig) -> list[CandidateSignal]:
    signals: list[CandidateSignal] = []
    if record.url_role_hint:
        signals.append(CandidateSignal(record.fingerprint_id, "url_role_hint", {"matched": True}))
    if min(record.width_px, record.height_px) < config.tiny_side_px:
        signals.append(
            CandidateSignal(
                record.fingerprint_id,
                "tiny_dimensions",
                {"width_px": record.width_px, "height_px": record.height_px},
            )
        )
    if record.byte_size < config.tiny_file_bytes:
        signals.append(
            CandidateSignal(record.fingerprint_id, "tiny_file", {"byte_size": record.byte_size})
        )
    aspect_ratio = max(record.width_px, record.height_px) / min(record.width_px, record.height_px)
    if aspect_ratio >= config.extreme_aspect_ratio:
        signals.append(
            CandidateSignal(
                record.fingerprint_id,
                "extreme_aspect_ratio",
                {"aspect_ratio": round(aspect_ratio, 6)},
            )
        )
    if record.is_fully_transparent:
        signals.append(
            CandidateSignal(record.fingerprint_id, "fully_transparent", {"matched": True})
        )
    return signals


def build_image_candidate_plan(
    records: Sequence[CandidateImage],
    config: ImageConfig,
) -> ImageCandidatePlan:
    """以稳定顺序构建精确簇、近似候选和只读信号。

    高频复用按文件 SHA 聚合。缺失作者不计入作者数，也不会共享一个伪造的
    “空作者”身份；因此只有至少三个真实作者身份的文件才可能触发该信号。
    """

    members = tuple(sorted(records, key=lambda item: item.fingerprint_id))
    by_sha: dict[str, list[CandidateImage]] = defaultdict(list)
    for record in members:
        by_sha[record.file_sha256].append(record)

    clusters: list[ExactImageCluster] = []
    signals: list[CandidateSignal] = []
    for record in members:
        signals.extend(_technical_signals(record, config))
    for file_sha256, group in sorted(by_sha.items()):
        fingerprint_ids = tuple(sorted(record.fingerprint_id for record in group))
        cluster_id = _canonical_sha256(
            {"file_sha256": file_sha256, "fingerprint_ids": fingerprint_ids}
        )[:32]
        clusters.append(
            ExactImageCluster(
                cluster_id=cluster_id,
                file_sha256=file_sha256,
                representative_fingerprint_id=fingerprint_ids[0],
                member_fingerprint_ids=fingerprint_ids,
            )
        )
        posts = {record.source_post_id for record in group}
        known_authors = {
            record.author_identity_sha256
            for record in group
            if record.author_identity_sha256 is not None
        }
        if (
            len(posts) >= config.repeated_post_min
            and len(known_authors) >= config.repeated_author_min
        ):
            for record in group:
                signals.append(
                    CandidateSignal(
                        record.fingerprint_id,
                        "high_reuse",
                        {"post_count": len(posts), "known_author_count": len(known_authors)},
                    )
                )

    by_id = {record.fingerprint_id: record for record in members}
    representatives = [by_id[cluster.representative_fingerprint_id] for cluster in clusters]
    near_pairs: list[NearImagePair] = []
    for left_index, left in enumerate(representatives):
        for right in representatives[left_index + 1 :]:
            distance = phash_hamming_distance(left.phash_hex, right.phash_hex)
            if distance <= config.candidate_hamming_max:
                left_id, right_id = sorted((left.fingerprint_id, right.fingerprint_id))
                near_pairs.append(NearImagePair(left_id, right_id, distance))

    signals_tuple = tuple(
        sorted(signals, key=lambda item: (item.fingerprint_id, item.signal_code))
    )
    clusters_tuple = tuple(clusters)
    near_pairs_tuple = tuple(
        sorted(near_pairs, key=lambda item: (item.left_fingerprint_id, item.right_fingerprint_id))
    )
    input_manifest_sha256 = _canonical_sha256(
        [
            {
                "fingerprint_id": record.fingerprint_id,
                "row_identity_sha256": record.row_identity_sha256,
                "source_image_id": record.source_image_id,
                "source_post_id": record.source_post_id,
                "author_identity_sha256": record.author_identity_sha256,
                "file_sha256": record.file_sha256,
                "phash_hex": record.phash_hex,
                "byte_size": record.byte_size,
                "width_px": record.width_px,
                "height_px": record.height_px,
                "is_fully_transparent": record.is_fully_transparent,
                "url_role_hint": record.url_role_hint,
            }
            for record in members
        ]
    )
    output_sha256 = _canonical_sha256(
        {
            "input_manifest_sha256": input_manifest_sha256,
            "signals": [
                [signal.fingerprint_id, signal.signal_code, signal.evidence]
                for signal in signals_tuple
            ],
            "exact_clusters": [
                [cluster.cluster_id, cluster.file_sha256, cluster.member_fingerprint_ids]
                for cluster in clusters_tuple
            ],
            "near_pairs": [
                [pair.left_fingerprint_id, pair.right_fingerprint_id, pair.hamming_distance]
                for pair in near_pairs_tuple
            ],
        }
    )
    return ImageCandidatePlan(
        members=members,
        signals=signals_tuple,
        exact_clusters=clusters_tuple,
        near_pairs=near_pairs_tuple,
        input_manifest_sha256=input_manifest_sha256,
        output_sha256=output_sha256,
    )
