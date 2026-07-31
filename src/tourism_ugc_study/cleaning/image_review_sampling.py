"""图片候选代表、pHash 展示组和人工复核样本的纯确定性算法。

本模块不读取图片、SQLite 或网络，也不产生标签或排除决定。输入只包含 Issue
#9 已封存候选构建的去敏投影；输出用于冻结人工任务 manifest。pHash 距离始终
只组织复核展示，任何返回对象都没有传播或自动决定字段。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .image_candidates import phash_hamming_distance


@dataclass(frozen=True)
class ReviewCandidate:
    """一个 SHA 精确簇代表的复核抽样输入。

    ``fingerprint_id/exact_cluster_id`` 固定代表身份，``phash_hex`` 仅用于候选
    展示分组；三个布尔量分别说明技术信号、精确重复和 pHash 候选是否存在。
    ``platform_key`` 仅供后续审计分层，不参与标签判断。对象不携带路径、URL、
    图片内容或来源角色，调用方必须只传入 ``content`` 的成功指纹代表。
    """

    fingerprint_id: str
    exact_cluster_id: str
    phash_hex: str
    platform_key: str
    has_technical_signal: bool
    has_exact_duplicate: bool
    has_phash_candidate: bool

    @property
    def is_candidate(self) -> bool:
        """返回是否命中任一复核候选来源，不把命中解释为技术噪声。

        技术信号、SHA 精确重复或 pHash 候选任一为真即返回真；属性无 I/O、无
        概率阈值，也不会根据文件名或来源平台推断最终标签。
        """

        return self.has_technical_signal or self.has_exact_duplicate or self.has_phash_candidate


@dataclass(frozen=True)
class ReviewSelection:
    """一个稳定排序后的人工复核成员。

    ``fingerprint_id/exact_cluster_id`` 绑定 Issue #9 的代表及精确簇；
    ``review_reason`` 只记录为何入样，``requires_double_label`` 表示计划槽位，
    不代表首位标注结果。``stable_rank`` 从 1 开始且在同次选择中唯一。对象
    不携带标签，调用方不得由 ``review_reason`` 推导排除动作。
    """

    fingerprint_id: str
    exact_cluster_id: str
    review_reason: str
    stable_rank: int
    requires_double_label: bool


@dataclass(frozen=True)
class PHashReviewGroup:
    """complete-linkage 约束下的 pHash 展示组。

    任意两成员的汉明距离都不超过 ``maximum_pair_distance``；单例距离为 0。
    组只用于减少界面切换和观察边界，不能作为标签传播或排除依据。
    """

    group_id: str
    member_fingerprint_ids: tuple[str, ...]
    maximum_pair_distance: int


def stable_identity_rank(seed: int, namespace: str, identity: str) -> tuple[str, str]:
    """返回可跨进程复现的随机化排序键。

    输入为冻结随机种子、用途命名空间和公开对象 ID；输出先按 SHA-256、再按
    原身份破平。函数不依赖 Python 哈希随机化，相同输入永远得到相同顺序。
    """

    digest = hashlib.sha256(f"{seed}:{namespace}:{identity}".encode("utf-8")).hexdigest()
    return digest, identity


def _ranked(
    records: Iterable[ReviewCandidate],
    *,
    seed: int,
    namespace: str,
) -> list[ReviewCandidate]:
    """按冻结身份键排序并拒绝重复代表，供所有抽样入口共享。"""

    by_id: dict[str, ReviewCandidate] = {}
    for record in records:
        if record.fingerprint_id in by_id:
            raise ValueError("review candidates must have unique fingerprint ids")
        by_id[record.fingerprint_id] = record
    return sorted(
        by_id.values(),
        key=lambda item: stable_identity_rank(seed, namespace, item.fingerprint_id),
    )


def select_pilot_members(
    records: Sequence[ReviewCandidate],
    *,
    seed: int,
    size: int,
) -> tuple[ReviewSelection, ...]:
    """从真实候选代表稳定选取至多 ``size`` 张共同试标成员。

    试标只从命中候选的代表中抽取；候选少于请求量时全取，空集合合法。每个
    成员设置双标，因为共同试标需要两位研究者独立留下原始证据。``size`` 必须
    为正整数，否则抛出 ``ValueError``，函数不隐式扩大工作量。
    """

    if size <= 0:
        raise ValueError("pilot size must be positive")
    chosen = _ranked(
        (record for record in records if record.is_candidate),
        seed=seed,
        namespace="image-pilot",
    )[:size]
    return tuple(
        ReviewSelection(
            fingerprint_id=record.fingerprint_id,
            exact_cluster_id=record.exact_cluster_id,
            review_reason="pilot",
            stable_rank=index,
            requires_double_label=True,
        )
        for index, record in enumerate(chosen, start=1)
    )


def select_candidate_review_members(
    records: Sequence[ReviewCandidate],
    *,
    seed: int,
) -> tuple[ReviewSelection, ...]:
    """稳定导出全部候选代表，供单人首标与动态双标计划使用。

    技术信号优先记录为 ``technical_signal``，其次为 ``phash_candidate``，仅有
    SHA 精确重复时归为 ``candidate_boundary``。该优先级只使 manifest 原因
    唯一，不表达风险强弱；所有成员首轮仅计划 slot 1，后续拟排除或 uncertain
    才由独立计划开启 slot 2。输入重复代表会抛出 ``ValueError``；输出包含
    全部候选的不可变元组，空候选人口返回空元组且不产生 I/O。
    """

    chosen = _ranked(
        (record for record in records if record.is_candidate),
        seed=seed,
        namespace="image-candidate-review",
    )
    selections: list[ReviewSelection] = []
    for index, record in enumerate(chosen, start=1):
        reason = (
            "technical_signal"
            if record.has_technical_signal
            else "phash_candidate"
            if record.has_phash_candidate
            else "candidate_boundary"
        )
        selections.append(
            ReviewSelection(
                fingerprint_id=record.fingerprint_id,
                exact_cluster_id=record.exact_cluster_id,
                review_reason=reason,
                stable_rank=index,
                requires_double_label=False,
            )
        )
    return tuple(selections)


def select_boundary_members(
    records: Sequence[ReviewCandidate],
    *,
    seed: int,
    size: int,
    exclude_fingerprint_ids: Iterable[str] = (),
) -> tuple[ReviewSelection, ...]:
    """从候选与非候选边界稳定冻结至多 ``size`` 张盲双标成员。

    首先各分配 ``size//2`` 个候选和非候选代表，某一侧不足时由另一侧按稳定
    顺序补齐；排除集合用于补充轮次避免与既有双标成员重叠。边界集是定向富集
    样本，只衡量手册可重复性，不估计总体技术噪声率。``size`` 非正或输入
    代表重复时抛出 ``ValueError``；人口不足时返回全部剩余成员而不复抽。
    """

    if size <= 0:
        raise ValueError("boundary size must be positive")
    excluded = set(exclude_fingerprint_ids)
    eligible = [record for record in records if record.fingerprint_id not in excluded]
    candidates = _ranked(
        (record for record in eligible if record.is_candidate),
        seed=seed,
        namespace="image-boundary-candidate",
    )
    noncandidates = _ranked(
        (record for record in eligible if not record.is_candidate),
        seed=seed,
        namespace="image-boundary-noncandidate",
    )
    candidate_target = (size + 1) // 2
    noncandidate_target = size // 2
    chosen_candidates = candidates[:candidate_target]
    chosen_noncandidates = noncandidates[:noncandidate_target]
    remaining = size - len(chosen_candidates) - len(chosen_noncandidates)
    if remaining:
        pool = candidates[len(chosen_candidates) :] + noncandidates[len(chosen_noncandidates) :]
        pool.sort(key=lambda item: stable_identity_rank(seed, "image-boundary-fill", item.fingerprint_id))
        for record in pool[:remaining]:
            if record.is_candidate:
                chosen_candidates.append(record)
            else:
                chosen_noncandidates.append(record)
    chosen = chosen_candidates + chosen_noncandidates
    chosen.sort(key=lambda item: stable_identity_rank(seed, "image-boundary-output", item.fingerprint_id))
    return tuple(
        ReviewSelection(
            fingerprint_id=record.fingerprint_id,
            exact_cluster_id=record.exact_cluster_id,
            review_reason=("candidate_boundary" if record.is_candidate else "noncandidate_boundary"),
            stable_rank=index,
            requires_double_label=True,
        )
        for index, record in enumerate(chosen, start=1)
    )


def build_complete_linkage_groups(
    records: Sequence[ReviewCandidate],
    *,
    seed: int,
    maximum_distance: int,
) -> tuple[PHashReviewGroup, ...]:
    """以稳定贪心 complete-linkage 组织 pHash 候选展示组。

    只有命中 pHash 候选的代表参与。每个新成员仅在它与组内**所有**成员距离均
    不超过阈值时加入，因此 A-B、B-C 接近但 A-C 越界时三者不会被同组。阈值
    必须位于 0..10；输出含单例但不含任何传播字段，算法不写数据库。
    """

    if not 0 <= maximum_distance <= 10:
        raise ValueError("pHash review distance must be between 0 and 10")
    ordered = _ranked(
        (record for record in records if record.has_phash_candidate),
        seed=seed,
        namespace="image-phash-complete-linkage",
    )
    by_id = {record.fingerprint_id: record for record in ordered}
    groups: list[list[str]] = []
    for record in ordered:
        for group in groups:
            if all(
                phash_hamming_distance(record.phash_hex, by_id[member].phash_hex)
                <= maximum_distance
                for member in group
            ):
                group.append(record.fingerprint_id)
                break
        else:
            groups.append([record.fingerprint_id])

    output: list[PHashReviewGroup] = []
    for group in groups:
        members = tuple(sorted(group))
        pair_distances = [
            phash_hamming_distance(by_id[left].phash_hex, by_id[right].phash_hex)
            for index, left in enumerate(members)
            for right in members[index + 1 :]
        ]
        actual_max = max(pair_distances, default=0)
        group_id = hashlib.sha256(
            ("image-phash-review-v1:" + ":".join(members)).encode("utf-8")
        ).hexdigest()[:32]
        output.append(PHashReviewGroup(group_id, members, actual_max))
    return tuple(sorted(output, key=lambda item: item.group_id))


def selection_manifest(
    selections: Sequence[ReviewSelection],
    groups: Sequence[PHashReviewGroup] = (),
) -> str:
    """计算复核成员与 pHash 展示分组的稳定 SHA-256 manifest。

    输入顺序会被显式排序，避免调用方容器顺序改变身份。摘要只覆盖去敏 ID、
    原因、槽位计划和分组距离，不读取或泄露本地图片路径。返回固定 64 位
    十六进制摘要；调用方负责先保证成员 rank 和组身份的领域不变量。
    """

    payload = [
        "member|"
        + "|".join(
            (
                item.fingerprint_id,
                item.exact_cluster_id,
                item.review_reason,
                str(item.stable_rank),
                str(int(item.requires_double_label)),
            )
        )
        for item in sorted(selections, key=lambda item: item.fingerprint_id)
    ]
    payload.extend(
        "group|"
        + "|".join((group.group_id, str(group.maximum_pair_distance), *group.member_fingerprint_ids))
        for group in sorted(groups, key=lambda item: item.group_id)
    )
    return hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()
