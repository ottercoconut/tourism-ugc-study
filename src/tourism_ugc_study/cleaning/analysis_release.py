"""分析发布四类成员与去敏质量报告的纯领域投影。

本模块只组合调用方显式选定的帖子决定、分析去重簇、图片来源关系和已经
封存的图片技术决定。它不访问 SQLite、源库、图片或网络，也不执行发布接受：
``formal``/``smoke`` 仅作为请求身份进入 manifest，真正的质量门、不可变写入
与 ``accepted`` 状态转换必须由仓储层在事务内完成。

图片领域在这里没有主题相关性轴。路线图、推荐计划图或游戏截图只要上游
#10 技术决定为 ``keep``，就与其他内容图使用完全相同的发布规则；本模块不
接收商业属性、视觉主题相关性或任何原始文本字段。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from types import MappingProxyType
from typing import Literal, Mapping, Sequence
from urllib.parse import urlparse

from .analysis_dedup import AnalysisDedupBuild, PostIdentity
from .post_decision import PostDecision


ReleaseMode = Literal["formal", "smoke"]
ImageRole = Literal["author_avatar", "page", "content"]
ImageTechnicalAction = Literal["keep", "review", "exclude"]
ContentProjectionStatus = Literal["keep", "review", "exclude", "blocked"]
ImageIdentity = tuple[int, int]

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_METADATA_KEY_PARTS = frozenset(
    {
        "absolute_path",
        "author",
        "caption",
        "content",
        "cookie",
        "credential",
        "password",
        "raw",
        "secret",
        "text",
        "title",
        "token",
        "ugc",
        "url",
        "uri",
    }
)


@dataclass(frozen=True)
class ReleaseRequest:
    """一次分析发布投影的显式、去敏请求身份。

    ``release_id`` 和 ``run_id`` 均为必填，且不能使用 ``latest`` 等可变别名。
    ``mode`` 明确区分正式请求与合成/冒烟请求，但两者都只得到领域投影，不能
    在此模块被接受。``metadata`` 只允许由标量、序列和映射组成的去敏数据；
    构建前会递归拒绝路径、URL、原始内容、作者字段与令牌字段，输出只保留其
    SHA-256，不回显原值。
    """

    release_id: str
    run_id: str
    mode: ReleaseMode
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class ImageSourceRelation:
    """冻结快照中一条图片槽位与父帖的来源关系。

    图片与帖子都使用 ``(source_id, source_version)`` 身份，避免后来更新的库存
    静默替换本次发布输入。``role`` 必须完整落在头像、页面证据或内容图之一；
    它是采集关系而不是人工技术标签。
    """

    source_image_id: int
    source_version: int
    source_post_id: int
    source_post_version: int
    role: ImageRole

    @property
    def image_identity(self) -> ImageIdentity:
        """返回不含文件路径、URL 和图片内容的稳定图片身份。"""

        return self.source_image_id, self.source_version

    @property
    def post_identity(self) -> PostIdentity:
        """返回该图片槽位绑定的冻结父帖身份。"""

        return self.source_post_id, self.source_post_version


@dataclass(frozen=True, order=True)
class FinalizedImageTechnicalDecision:
    """#10 已封存图片技术决定的最小发布投影。

    接口故意只接收 ``keep/review/exclude`` 动作、构建 ID 与决定哈希，不接收
    主题形式、商业属性或视觉相关性标签。没有对应决定的内容图由发布投影记为
    ``blocked``；调用方不能以非 finalized 行冒充技术结论。
    """

    source_image_id: int
    source_version: int
    decision_action: ImageTechnicalAction
    decision_build_id: str
    decision_sha256: str
    seal_status: str = "finalized"

    @property
    def image_identity(self) -> ImageIdentity:
        """返回决定所针对的冻结图片身份。"""

        return self.source_image_id, self.source_version


@dataclass(frozen=True, order=True)
class AnalysisPostMember:
    """四类输出中帖子集合的一条 release/run-scoped 成员。"""

    release_id: str
    run_id: str
    source_post_id: int
    source_version: int
    cluster_id: str | None = None
    member_reason_code: str = "post_decision_keep"

    @property
    def identity(self) -> PostIdentity:
        """返回成员的帖子版本身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True, order=True)
class AnalysisImageMember:
    """内容合格或页面证据集合的一条 release/run-scoped 成员。"""

    release_id: str
    run_id: str
    source_image_id: int
    source_version: int
    source_post_id: int
    source_post_version: int
    member_reason_code: str

    @property
    def image_identity(self) -> ImageIdentity:
        """返回成员的图片版本身份。"""

        return self.source_image_id, self.source_version


@dataclass(frozen=True, order=True)
class ImagePublicationProjection:
    """每条图片来源关系的角色、技术状态和发布去向。

    ``content_status`` 只对内容图有值；头像与页面关系不会伪造技术决定。
    ``parent_post_not_eligible`` 只在技术 ``keep`` 的内容图因父帖非 ``keep``
    而未发布时出现，它不改写 ``technical_decision_action``。
    """

    release_id: str
    run_id: str
    source_image_id: int
    source_version: int
    source_post_id: int
    source_post_version: int
    role: ImageRole
    content_status: ContentProjectionStatus | None
    technical_decision_action: ImageTechnicalAction | None
    output_collection: str | None
    projection_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseQualityReport:
    """仅含身份、计数、哈希和稳定理由码的质量报告数据。

    所有计数字典在构建时转为只读映射；调用方不能在 manifest 计算后修改。
    ``report_sha256`` 覆盖其他字段的规范 JSON 投影，适合仓储层重新计算校验。
    """

    release_id: str
    run_id: str
    mode: ReleaseMode
    request_metadata_sha256: str
    input_lineage_sha256: str
    post_decision_counts: Mapping[str, int]
    post_reason_counts: Mapping[str, int]
    dedup_counts: Mapping[str, int]
    image_role_counts: Mapping[str, int]
    content_technical_counts: Mapping[str, int]
    image_projection_reason_counts: Mapping[str, int]
    output_member_counts: Mapping[str, int]
    report_sha256: str


@dataclass(frozen=True)
class AnalysisReleaseProjection:
    """完整、确定性的纯领域发布投影。

    四个命名字段就是分析侧允许消费的集合。``image_projections`` 只为数量守恒
    与未发布原因审计服务；``acceptance_state`` 固定为 ``projection_only``，提醒
    上层还必须执行仓储质量门和不可变接受事务。
    """

    release_id: str
    run_id: str
    mode: ReleaseMode
    analysis_posts_eligible: tuple[AnalysisPostMember, ...]
    analysis_posts_deduplicated: tuple[AnalysisPostMember, ...]
    analysis_images_eligible: tuple[AnalysisImageMember, ...]
    analysis_images_evidence_only: tuple[AnalysisImageMember, ...]
    image_projections: tuple[ImagePublicationProjection, ...]
    quality_report: ReleaseQualityReport
    manifest_sha256: str
    acceptance_state: str = "projection_only"
    requires_repository_acceptance: bool = True


def _canonical_json(value: object) -> bytes:
    """以跨输入顺序稳定的 JSON 编码序列化去敏领域数据。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    """返回规范 JSON 数据的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_identifier(value: str, field_name: str) -> None:
    """拒绝空身份、路径形式和可变 ``latest`` 别名。"""

    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe explicit identifier")
    if value.casefold() == "latest":
        raise ValueError(f"{field_name} cannot use a mutable latest alias")


def _validate_sha256(value: str, field_name: str) -> None:
    """验证小写十六进制 SHA-256，禁止把原值塞入哈希字段。"""

    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be lowercase hexadecimal sha256")


def _looks_like_url(value: str) -> bool:
    """检测带 scheme/host 的 URL，普通稳定 ID 不会命中。"""

    parsed = urlparse(value)
    return bool(parsed.scheme and (parsed.netloc or parsed.scheme == "file"))


def _validate_safe_metadata(value: object, *, key_path: tuple[str, ...] = ()) -> None:
    """递归验证请求附加元数据不会泄露内容或访问凭据。

    元数据只用于把 schema、配置或代码版本等去敏身份纳入请求哈希。键名采用
    小写 snake_case；字符串值必须是与发布 ID 同形的稳定标识。这样即使质量
    报告只输出摘要，也不会先接收随后被意外记录的绝对路径、URL 或原始 UGC。
    """

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
            if any(part in key for part in _FORBIDDEN_METADATA_KEY_PARTS):
                raise ValueError("release metadata contains a sensitive field")
            _validate_safe_metadata(child, key_path=(*key_path, key))
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_safe_metadata(child, key_path=key_path)
        return
    raise ValueError("release metadata contains an unsupported value type")


def _normalize_safe_metadata(value: object) -> object:
    """把已验证的任意 Mapping/tuple 规范化为 JSON 原生容器。"""

    if isinstance(value, Mapping):
        return {
            str(key): _normalize_safe_metadata(child)
            for key, child in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_safe_metadata(child) for child in value]
    return value


def _frozen_counts(values: Mapping[str, int]) -> Mapping[str, int]:
    """按键排序并冻结计数，防止调用方在哈希后原地修改。"""

    return MappingProxyType(dict(sorted(values.items())))


def _validate_release_request(request: ReleaseRequest) -> str:
    """验证显式发布请求并返回去敏附加元数据摘要。"""

    _validate_identifier(request.release_id, "release_id")
    _validate_identifier(request.run_id, "run_id")
    if request.mode not in {"formal", "smoke"}:
        raise ValueError("release mode must be formal or smoke")
    _validate_safe_metadata(request.metadata)
    return _sha256(_normalize_safe_metadata(request.metadata))


def _validate_post_decisions(
    decisions: Sequence[PostDecision],
) -> dict[PostIdentity, PostDecision]:
    """验证帖子决定形成 ``keep/review/exclude`` 的无重复完整输入。"""

    by_identity: dict[PostIdentity, PostDecision] = {}
    for decision in decisions:
        identity = decision.source_post_id, decision.source_version
        if identity[0] <= 0 or identity[1] <= 0:
            raise ValueError("positive post decision identity is required")
        if identity in by_identity:
            raise ValueError("duplicate post decision identity")
        if decision.decision not in {"keep", "review", "exclude"}:
            raise ValueError("unsupported post decision")
        _validate_sha256(decision.decision_sha256, "post decision sha256")
        for reason in decision.reason_codes:
            _validate_identifier(reason, "post decision reason code")
        by_identity[identity] = decision
    return by_identity


def _validate_dedup_partition(
    dedup: AnalysisDedupBuild,
    post_identities: set[PostIdentity],
) -> dict[PostIdentity, tuple[str, PostIdentity]]:
    """证明去重簇与帖子决定一一分区，并返回成员到簇/代表的索引。"""

    _validate_sha256(dedup.manifest_sha256, "analysis dedup manifest sha256")
    by_member: dict[PostIdentity, tuple[str, PostIdentity]] = {}
    cluster_ids: set[str] = set()
    for cluster in dedup.clusters:
        _validate_identifier(cluster.cluster_id, "analysis dedup cluster id")
        if cluster.cluster_id in cluster_ids:
            raise ValueError("duplicate analysis dedup cluster id")
        cluster_ids.add(cluster.cluster_id)
        if not cluster.members or cluster.representative not in cluster.members:
            raise ValueError("dedup cluster must have one member representative")
        if len(cluster.members) != len(set(cluster.members)):
            raise ValueError("duplicate member inside analysis dedup cluster")
        for identity in cluster.members:
            if identity in by_member:
                raise ValueError("post appears in more than one analysis dedup cluster")
            by_member[identity] = (cluster.cluster_id, cluster.representative)
    if set(by_member) != post_identities:
        raise ValueError("analysis dedup clusters must partition all post decisions")
    return by_member


def _build_post_members(
    request: ReleaseRequest,
    decisions: Mapping[PostIdentity, PostDecision],
    dedup: AnalysisDedupBuild,
) -> tuple[tuple[AnalysisPostMember, ...], tuple[AnalysisPostMember, ...]]:
    """生成全部合格帖与按合格子集选唯一代表的去重帖集合。"""

    by_member = _validate_dedup_partition(dedup, set(decisions))
    eligible_identities = {
        identity for identity, decision in decisions.items() if decision.decision == "keep"
    }
    eligible = tuple(
        AnalysisPostMember(
            request.release_id,
            request.run_id,
            identity[0],
            identity[1],
            by_member[identity][0],
            "post_decision_keep",
        )
        for identity in sorted(eligible_identities)
    )

    deduplicated: list[AnalysisPostMember] = []
    for cluster in sorted(dedup.clusters, key=lambda item: item.cluster_id):
        eligible_in_cluster = tuple(
            identity for identity in cluster.members if identity in eligible_identities
        )
        if not eligible_in_cluster:
            continue
        # 上游固定代表如果不合格，不能泄漏进分析结果；此时在本簇合格成员中
        # 以同一字典序策略选择稳定代表，并显式记录回退理由。
        if cluster.representative in eligible_identities:
            representative = cluster.representative
            reason = "analysis_dedup_cluster_representative"
        else:
            representative = min(eligible_in_cluster)
            reason = "analysis_dedup_eligible_representative_fallback"
        deduplicated.append(
            AnalysisPostMember(
                request.release_id,
                request.run_id,
                representative[0],
                representative[1],
                cluster.cluster_id,
                reason,
            )
        )
    return eligible, tuple(sorted(deduplicated))


def _validate_image_inputs(
    relations: Sequence[ImageSourceRelation],
    technical_decisions: Sequence[FinalizedImageTechnicalDecision],
    post_identities: set[PostIdentity],
) -> tuple[
    dict[ImageIdentity, ImageSourceRelation],
    dict[ImageIdentity, FinalizedImageTechnicalDecision],
]:
    """验证图片角色完整分区及 finalized 技术决定的引用边界。"""

    by_image: dict[ImageIdentity, ImageSourceRelation] = {}
    for relation in relations:
        if min(
            relation.source_image_id,
            relation.source_version,
            relation.source_post_id,
            relation.source_post_version,
        ) <= 0:
            raise ValueError("positive image relation identity is required")
        if relation.role not in {"author_avatar", "page", "content"}:
            raise ValueError("unsupported image source role")
        if relation.image_identity in by_image:
            raise ValueError("duplicate image source relation identity")
        if relation.post_identity not in post_identities:
            raise ValueError("image relation references an unknown post decision")
        by_image[relation.image_identity] = relation

    by_decision: dict[ImageIdentity, FinalizedImageTechnicalDecision] = {}
    for decision in technical_decisions:
        if decision.source_image_id <= 0 or decision.source_version <= 0:
            raise ValueError("positive image technical decision identity is required")
        if decision.image_identity in by_decision:
            raise ValueError("duplicate image technical decision identity")
        relation = by_image.get(decision.image_identity)
        if relation is None or relation.role != "content":
            raise ValueError("technical decisions may reference content relations only")
        if decision.decision_action not in {"keep", "review", "exclude"}:
            raise ValueError("unsupported image technical decision action")
        if decision.seal_status != "finalized":
            raise ValueError("image technical decision must be finalized")
        _validate_identifier(decision.decision_build_id, "image decision build id")
        _validate_sha256(decision.decision_sha256, "image decision sha256")
        by_decision[decision.image_identity] = decision
    return by_image, by_decision


def _build_image_members(
    request: ReleaseRequest,
    relations: Mapping[ImageIdentity, ImageSourceRelation],
    technical_decisions: Mapping[ImageIdentity, FinalizedImageTechnicalDecision],
    post_decisions: Mapping[PostIdentity, PostDecision],
) -> tuple[
    tuple[AnalysisImageMember, ...],
    tuple[AnalysisImageMember, ...],
    tuple[ImagePublicationProjection, ...],
]:
    """按角色、父帖决定和图片技术动作生成图片集合与审计投影。"""

    eligible: list[AnalysisImageMember] = []
    evidence_only: list[AnalysisImageMember] = []
    projections: list[ImagePublicationProjection] = []
    for image_identity in sorted(relations):
        relation = relations[image_identity]
        decision = technical_decisions.get(image_identity)
        if relation.role == "author_avatar":
            projection = ImagePublicationProjection(
                request.release_id,
                request.run_id,
                *relation.image_identity,
                *relation.post_identity,
                relation.role,
                None,
                None,
                None,
                ("author_avatar_excluded_from_content",),
            )
        elif relation.role == "page":
            evidence_only.append(
                AnalysisImageMember(
                    request.release_id,
                    request.run_id,
                    *relation.image_identity,
                    *relation.post_identity,
                    "page_evidence_only",
                )
            )
            projection = ImagePublicationProjection(
                request.release_id,
                request.run_id,
                *relation.image_identity,
                *relation.post_identity,
                relation.role,
                None,
                None,
                "analysis_images_evidence_only",
                ("page_evidence_only",),
            )
        else:
            parent_is_eligible = (
                post_decisions[relation.post_identity].decision == "keep"
            )
            if decision is None:
                content_status: ContentProjectionStatus = "blocked"
                output_collection = None
                reasons = ("image_technical_decision_missing",)
                technical_action = None
            else:
                content_status = decision.decision_action
                technical_action = decision.decision_action
                output_collection = None
                if decision.decision_action == "keep" and parent_is_eligible:
                    output_collection = "analysis_images_eligible"
                    reasons = ("content_image_eligible",)
                    eligible.append(
                        AnalysisImageMember(
                            request.release_id,
                            request.run_id,
                            *relation.image_identity,
                            *relation.post_identity,
                            "content_image_eligible",
                        )
                    )
                elif decision.decision_action == "keep":
                    # 只在图片自身本可发布时将未发布归因于父帖；技术动作保持
                    # keep，从而不会把文本过滤伪报为图片技术噪声。
                    reasons = ("parent_post_not_eligible",)
                elif decision.decision_action == "review":
                    reasons = ("image_technical_review",)
                else:
                    reasons = ("image_technical_exclude",)
            projection = ImagePublicationProjection(
                request.release_id,
                request.run_id,
                *relation.image_identity,
                *relation.post_identity,
                relation.role,
                content_status,
                technical_action,
                output_collection,
                reasons,
            )
        projections.append(projection)
    return tuple(eligible), tuple(evidence_only), tuple(projections)


def _count_values(values: Sequence[str], domain: Sequence[str]) -> dict[str, int]:
    """对封闭枚举生成包含零项的确定性计数字典。"""

    return {key: sum(value == key for value in values) for key in domain}


def _quality_report_payload(
    request: ReleaseRequest,
    metadata_sha256: str,
    input_lineage_sha256: str,
    post_decisions: Mapping[PostIdentity, PostDecision],
    dedup: AnalysisDedupBuild,
    relations: Mapping[ImageIdentity, ImageSourceRelation],
    projections: Sequence[ImagePublicationProjection],
    posts_eligible: Sequence[AnalysisPostMember],
    posts_deduplicated: Sequence[AnalysisPostMember],
    images_eligible: Sequence[AnalysisImageMember],
    images_evidence_only: Sequence[AnalysisImageMember],
) -> dict[str, object]:
    """从领域对象重建质量报告原始计数并执行数量守恒断言。"""

    post_counts = _count_values(
        [decision.decision for decision in post_decisions.values()],
        ("keep", "review", "exclude"),
    )
    post_reasons: dict[str, int] = {}
    for decision in post_decisions.values():
        for reason in decision.reason_codes:
            post_reasons[reason] = post_reasons.get(reason, 0) + 1

    role_counts = _count_values(
        [relation.role for relation in relations.values()],
        ("author_avatar", "page", "content"),
    )
    content_projections = [item for item in projections if item.role == "content"]
    content_counts = _count_values(
        [item.content_status for item in content_projections if item.content_status],
        ("keep", "review", "exclude", "blocked"),
    )
    projection_reasons: dict[str, int] = {}
    for projection in projections:
        for reason in projection.projection_reason_codes:
            projection_reasons[reason] = projection_reasons.get(reason, 0) + 1

    dedup_counts = {
        "cluster_count": len(dedup.clusters),
        "eligible_representative_count": len(posts_deduplicated),
        "eligible_nonrepresentative_count": (
            len(posts_eligible) - len(posts_deduplicated)
        ),
        "member_count": sum(len(cluster.members) for cluster in dedup.clusters),
        "multi_member_cluster_count": sum(
            len(cluster.members) > 1 for cluster in dedup.clusters
        ),
    }
    output_counts = {
        "analysis_images_eligible": len(images_eligible),
        "analysis_images_evidence_only": len(images_evidence_only),
        "analysis_posts_deduplicated": len(posts_deduplicated),
        "analysis_posts_eligible": len(posts_eligible),
    }

    # 守恒检查由输入重建，不能由调用方传入“已通过”布尔值。
    if sum(post_counts.values()) != len(post_decisions):
        raise ValueError("post decision counts do not conserve")
    if len(posts_eligible) != post_counts["keep"]:
        raise ValueError("eligible post members must equal keep decisions")
    if not 0 <= len(posts_deduplicated) <= len(posts_eligible):
        raise ValueError("deduplicated post member count is invalid")
    if sum(role_counts.values()) != len(relations):
        raise ValueError("image role counts do not conserve")
    if sum(content_counts.values()) != role_counts["content"]:
        raise ValueError("content technical counts do not conserve")
    if len(images_evidence_only) != role_counts["page"]:
        raise ValueError("page relations must equal evidence-only members")
    eligible_projection_count = sum(
        projection.output_collection == "analysis_images_eligible"
        for projection in projections
    )
    if len(images_eligible) != eligible_projection_count:
        raise ValueError("eligible image projections do not conserve")
    if content_counts["keep"] != (
        len(images_eligible) + projection_reasons.get("parent_post_not_eligible", 0)
    ):
        raise ValueError("kept content images must be eligible or parent-filtered")

    return {
        "content_technical_counts": content_counts,
        "dedup_counts": dedup_counts,
        "image_projection_reason_counts": projection_reasons,
        "image_role_counts": role_counts,
        "input_lineage_sha256": input_lineage_sha256,
        "mode": request.mode,
        "output_member_counts": output_counts,
        "post_decision_counts": post_counts,
        "post_reason_counts": post_reasons,
        "release_id": request.release_id,
        "request_metadata_sha256": metadata_sha256,
        "run_id": request.run_id,
    }


def _as_report(payload: Mapping[str, object]) -> ReleaseQualityReport:
    """把已校验 payload 冻结为只读质量报告，并计算报告摘要。"""

    report_hash = _sha256(payload)
    return ReleaseQualityReport(
        release_id=str(payload["release_id"]),
        run_id=str(payload["run_id"]),
        mode=payload["mode"],  # type: ignore[arg-type]
        request_metadata_sha256=str(payload["request_metadata_sha256"]),
        input_lineage_sha256=str(payload["input_lineage_sha256"]),
        post_decision_counts=_frozen_counts(payload["post_decision_counts"]),  # type: ignore[arg-type]
        post_reason_counts=_frozen_counts(payload["post_reason_counts"]),  # type: ignore[arg-type]
        dedup_counts=_frozen_counts(payload["dedup_counts"]),  # type: ignore[arg-type]
        image_role_counts=_frozen_counts(payload["image_role_counts"]),  # type: ignore[arg-type]
        content_technical_counts=_frozen_counts(payload["content_technical_counts"]),  # type: ignore[arg-type]
        image_projection_reason_counts=_frozen_counts(
            payload["image_projection_reason_counts"]  # type: ignore[arg-type]
        ),
        output_member_counts=_frozen_counts(payload["output_member_counts"]),  # type: ignore[arg-type]
        report_sha256=report_hash,
    )


def _member_manifest_payload(
    request: ReleaseRequest,
    posts_eligible: Sequence[AnalysisPostMember],
    posts_deduplicated: Sequence[AnalysisPostMember],
    images_eligible: Sequence[AnalysisImageMember],
    images_evidence_only: Sequence[AnalysisImageMember],
    projections: Sequence[ImagePublicationProjection],
    quality_report: ReleaseQualityReport,
) -> dict[str, object]:
    """将四类成员与投影转换为不含原始内容的规范 manifest 数据。"""

    def post_rows(members: Sequence[AnalysisPostMember]) -> list[list[object]]:
        return [
            [
                item.release_id,
                item.run_id,
                item.source_post_id,
                item.source_version,
                item.cluster_id,
                item.member_reason_code,
            ]
            for item in members
        ]

    def image_rows(members: Sequence[AnalysisImageMember]) -> list[list[object]]:
        return [
            [
                item.release_id,
                item.run_id,
                item.source_image_id,
                item.source_version,
                item.source_post_id,
                item.source_post_version,
                item.member_reason_code,
            ]
            for item in members
        ]

    return {
        "acceptance_state": "projection_only",
        "analysis_images_eligible": image_rows(images_eligible),
        "analysis_images_evidence_only": image_rows(images_evidence_only),
        "analysis_posts_deduplicated": post_rows(posts_deduplicated),
        "analysis_posts_eligible": post_rows(posts_eligible),
        "image_projections": [
            [
                item.release_id,
                item.run_id,
                item.source_image_id,
                item.source_version,
                item.source_post_id,
                item.source_post_version,
                item.role,
                item.content_status,
                item.technical_decision_action,
                item.output_collection,
                item.projection_reason_codes,
            ]
            for item in projections
        ],
        "mode": request.mode,
        "quality_report_sha256": quality_report.report_sha256,
        "release_id": request.release_id,
        "requires_repository_acceptance": True,
        "run_id": request.run_id,
    }


def build_analysis_release(
    request: ReleaseRequest,
    post_decisions: Sequence[PostDecision],
    analysis_dedup: AnalysisDedupBuild,
    image_relations: Sequence[ImageSourceRelation],
    image_technical_decisions: Sequence[FinalizedImageTechnicalDecision],
) -> AnalysisReleaseProjection:
    """构建四类分析集合、图片未发布投影和确定性去敏质量报告。

    调用方必须显式传入发布/运行 ID 以及冻结上游对象；函数从不选择“最新”
    运行。帖子决定和去重成员必须形成同一完整分区，图片角色必须形成完整分区，
    仅内容图可有 finalized #10 技术决定。缺少内容技术决定不是排除，而是
    ``blocked``。任何身份错配、枚举越界、非 finalized 决定、敏感元数据或
    数量不守恒均抛出 ``ValueError``，不返回部分结果。

    返回值无论 ``formal`` 还是 ``smoke`` 都固定为 ``projection_only``；正式
    审计、快照完整性、版本匹配及原子接受由后续仓储门负责。
    """

    metadata_sha256 = _validate_release_request(request)
    decisions_by_post = _validate_post_decisions(post_decisions)
    posts_eligible, posts_deduplicated = _build_post_members(
        request,
        decisions_by_post,
        analysis_dedup,
    )
    relations_by_image, decisions_by_image = _validate_image_inputs(
        image_relations,
        image_technical_decisions,
        set(decisions_by_post),
    )
    images_eligible, images_evidence_only, projections = _build_image_members(
        request,
        relations_by_image,
        decisions_by_image,
        decisions_by_post,
    )
    # 输入谱系摘要绑定全部上游决定、去重 manifest 与图片来源关系。质量报告
    # 只保存这个哈希，不回显任何可能扩张的上游对象。
    input_lineage_sha256 = _sha256(
        {
            "analysis_dedup_manifest_sha256": analysis_dedup.manifest_sha256,
            "image_relations": [
                [
                    *relation.image_identity,
                    *relation.post_identity,
                    relation.role,
                ]
                for relation in sorted(relations_by_image.values())
            ],
            "image_technical_decisions": [
                [
                    *decision.image_identity,
                    decision.decision_action,
                    decision.decision_build_id,
                    decision.decision_sha256,
                ]
                for decision in sorted(decisions_by_image.values())
            ],
            "post_decisions": [
                [*identity, decision.decision, decision.decision_sha256]
                for identity, decision in sorted(decisions_by_post.items())
            ],
        }
    )
    report_payload = _quality_report_payload(
        request,
        metadata_sha256,
        input_lineage_sha256,
        decisions_by_post,
        analysis_dedup,
        relations_by_image,
        projections,
        posts_eligible,
        posts_deduplicated,
        images_eligible,
        images_evidence_only,
    )
    quality_report = _as_report(report_payload)
    manifest_payload = _member_manifest_payload(
        request,
        posts_eligible,
        posts_deduplicated,
        images_eligible,
        images_evidence_only,
        projections,
        quality_report,
    )
    return AnalysisReleaseProjection(
        release_id=request.release_id,
        run_id=request.run_id,
        mode=request.mode,
        analysis_posts_eligible=posts_eligible,
        analysis_posts_deduplicated=posts_deduplicated,
        analysis_images_eligible=images_eligible,
        analysis_images_evidence_only=images_evidence_only,
        image_projections=projections,
        quality_report=quality_report,
        manifest_sha256=_sha256(manifest_payload),
    )
