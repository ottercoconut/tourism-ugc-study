"""分析发布的派生 SQLite 仓储与显式接受事务。

本模块是 Issue #11 的持久化边界。调用方必须给出运行、发布和全部上游构建
身份；仓储不会查询“最新”记录，也不接受调用方自报的决定、计数或通过状态。
所有成员都从同一冻结快照及已封存证据重新投影，磁盘发布包只包含计数、哈希、
理由码和稳定去标识成员摘要。

构建与接受故意分成两个动作：``formal`` 与 ``smoke`` 都可在证据完整时封存，
但只有 formal 发布能够在重新验证快照、审计、任务和本地包后把运行与发布按
顺序置为 ``accepted``。任何失败都只通过稳定 ``reason_code`` 暴露，不泄漏
SQLite 文本、绝对路径或原始 UGC。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .analysis_dedup import AnalysisDedupBuild, AnalysisDedupCluster
from .analysis_release import (
    AnalysisReleaseProjection,
    FinalizedImageTechnicalDecision,
    ImageSourceRelation,
    ReleaseRequest,
    build_analysis_release,
)
from .post_decision import PostDecision
from .release_artifact import (
    ReleaseArtifactError,
    publish_release_artifact,
    verify_release_artifact,
)
from .release_acceptance import (
    ReleaseAcceptanceRequest,
    ReleaseAcceptanceServiceError,
    accept_verified_release,
)
from .schema import (
    ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION,
    DERIVED_SCHEMA_VERSION,
    connect_derived,
    migrate_derived,
)


class AnalysisReleaseRepositoryError(RuntimeError):
    """表示发布证据、不可变包或接受质量门不满足。

    ``reason_code`` 是供 CLI 和编排器使用的稳定机器码。异常消息固定，不包含
    路径、SQL、逐条身份或底层异常详情；事务失败表示没有合法部分结果可消费。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("analysis release repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class AnalysisReleaseBuildResult:
    """一次已封存发布构建的去敏回执。"""

    release_id: str
    run_id: str
    release_mode: str
    seal_status: str
    reused: bool
    posts_eligible_count: int
    posts_deduplicated_count: int
    images_eligible_count: int
    images_evidence_only_count: int
    release_manifest_sha256: str


@dataclass(frozen=True)
class AnalysisReleaseVerificationResult:
    """数据库与本地不可变包重算一致后的去敏回执。"""

    release_id: str
    run_id: str
    seal_status: str
    release_manifest_sha256: str
    artifact_manifest_sha256: str


@dataclass(frozen=True)
class AnalysisReleaseStatus:
    """显式 run/release 查询的最小状态，不返回文件路径。"""

    release_id: str
    run_id: str
    release_mode: str
    seal_status: str
    run_status: str
    reason_code: str | None


@dataclass(frozen=True)
class AnalysisReleaseAcceptanceResult:
    """formal 发布完成显式接受事务后的回执。"""

    release_id: str
    run_id: str
    seal_status: str
    run_status: str
    reused: bool


@dataclass(frozen=True)
class _ImageMemberBinding:
    """领域图片身份到 SQLite 外键的内部绑定。"""

    manifest_row_id: str
    fingerprint_id: str | None
    decision_id: str | None
    role_decision_id: str


@dataclass(frozen=True)
class _ReleaseInputs:
    """从显式上游 ID 重读并验证后的完整发布输入。"""

    run: sqlite3.Row
    snapshot: sqlite3.Row
    post_decisions: tuple[PostDecision, ...]
    post_decision_ids: Mapping[tuple[int, int], str]
    dedup: AnalysisDedupBuild
    persisted_dedup_cluster_count: int
    persisted_dedup_member_count: int
    image_relations: tuple[ImageSourceRelation, ...]
    image_decisions: tuple[FinalizedImageTechnicalDecision, ...]
    image_bindings: Mapping[tuple[int, int], _ImageMemberBinding]
    upstream_hashes: Mapping[str, str]


@dataclass(frozen=True)
class _PreparedRelease:
    """领域投影及其可持久化报告、manifest 与发布包摘要。"""

    inputs: _ReleaseInputs
    projection: AnalysisReleaseProjection
    request_manifest_sha256: str
    quality_report: Mapping[str, object]
    lineage_report: Mapping[str, object]
    member_manifest_hashes: Mapping[str, str]
    artifact_report: Mapping[str, object]
    artifact_manifest: Mapping[str, object]
    artifact_members: Mapping[str, object]


_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_IMAGE_AUDIT_INTEGRITY_STATE = "finalized"


def _utcnow() -> str:
    """返回带时区且秒级稳定的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: object) -> str:
    """返回键序稳定、无多余空白的 JSON 文本。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: object) -> str:
    """对规范 JSON 值计算小写 SHA-256。"""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """分块计算普通文件哈希，供 artifact manifest 与快照门复核。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_explicit_id(value: str, field: str) -> None:
    """拒绝空值、路径字符、超长身份和可变 ``latest`` 别名。"""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value[0] not in _SAFE_ID_CHARS
        or any(character not in _SAFE_ID_CHARS for character in value)
    ):
        raise AnalysisReleaseRepositoryError(f"invalid_{field}")
    if value.casefold() == "latest":
        raise AnalysisReleaseRepositoryError("mutable_latest_alias_forbidden")


def _validate_request_identifiers(
    *,
    run_id: str,
    release_id: str,
    release_mode: str | None = None,
    upstream_ids: Sequence[tuple[str, str]] = (),
) -> None:
    """集中验证公开 API 的全部显式身份，避免任一查询退化为隐式选择。"""

    _validate_explicit_id(run_id, "run_id")
    _validate_explicit_id(release_id, "release_id")
    for field, value in upstream_ids:
        _validate_explicit_id(value, field)
    if release_mode is not None and release_mode not in {"formal", "smoke"}:
        raise AnalysisReleaseRepositoryError("invalid_release_mode")


def _require_row(row: sqlite3.Row | None, reason_code: str) -> sqlite3.Row:
    """把缺失行转换为不泄漏查询参数的稳定领域错误。"""

    if row is None:
        raise AnalysisReleaseRepositoryError(reason_code)
    return row


def _load_run_and_snapshot(connection: sqlite3.Connection, run_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    """读取运行显式绑定的唯一冻结快照，不按时间或主键猜测。"""

    run = _require_row(
        connection.execute(
            "SELECT * FROM cleaning_runs WHERE run_id = ?", (run_id,)
        ).fetchone(),
        "cleaning_run_not_found",
    )
    snapshot_id = run["source_snapshot_id"]
    if snapshot_id is None:
        raise AnalysisReleaseRepositoryError("source_snapshot_not_bound")
    snapshot = _require_row(
        connection.execute(
            "SELECT * FROM source_snapshots WHERE snapshot_id = ? AND run_id = ?",
            (snapshot_id, run_id),
        ).fetchone(),
        "source_snapshot_lineage_mismatch",
    )
    if snapshot["input_contract_status"] != "accepted":
        raise AnalysisReleaseRepositoryError("source_snapshot_input_rejected")
    return run, snapshot


def _load_post_inputs(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    snapshot_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
) -> tuple[
    tuple[PostDecision, ...],
    dict[tuple[int, int], str],
    AnalysisDedupBuild,
    int,
    int,
    dict[str, str],
]:
    """重读 final 帖子决定、同模式审计和已封存分析去重分区。"""

    build = _require_row(
        connection.execute(
            """
            SELECT * FROM post_decision_builds
            WHERE decision_build_id = ? AND run_id = ? AND source_snapshot_id = ?
            """,
            (post_decision_build_id, run_id, snapshot_id),
        ).fetchone(),
        "final_post_decision_build_not_found",
    )
    if build["build_kind"] != "final" or build["seal_status"] != "finalized":
        raise AnalysisReleaseRepositoryError("finalized_final_post_decisions_required")
    if build["text_keep_audit_evaluation_id"] != text_keep_audit_evaluation_id:
        raise AnalysisReleaseRepositoryError("text_keep_audit_lineage_mismatch")

    audit = _require_row(
        connection.execute(
            """
            SELECT evaluation.*, round.audit_mode, round.seal_status AS round_seal_status,
                   round.run_id AS audit_run_id
            FROM text_keep_audit_evaluations AS evaluation
            JOIN text_keep_audit_rounds AS round
              ON round.audit_round_id = evaluation.audit_round_id
            WHERE evaluation.audit_evaluation_id = ?
            """,
            (text_keep_audit_evaluation_id,),
        ).fetchone(),
        "text_keep_audit_evaluation_not_found",
    )
    if audit["audit_run_id"] != run_id:
        raise AnalysisReleaseRepositoryError("text_keep_audit_lineage_mismatch")
    if audit["audit_mode"] != release_mode:
        raise AnalysisReleaseRepositoryError("text_keep_audit_mode_mismatch")
    if (
        audit["round_seal_status"] != "finalized"
        or audit["seal_status"] != "finalized"
        or audit["evaluation_status"] != "passed"
    ):
        raise AnalysisReleaseRepositoryError("text_keep_audit_not_passed")

    decision_rows = connection.execute(
        """
        SELECT d.*, GROUP_CONCAT(l.evidence_id, '|') AS evidence_ids
        FROM post_decisions AS d
        LEFT JOIN post_decision_evidence_links AS l ON l.decision_id = d.decision_id
        WHERE d.decision_build_id = ?
        GROUP BY d.decision_id
        ORDER BY d.source_post_id, d.source_version
        """,
        (post_decision_build_id,),
    ).fetchall()
    if len(decision_rows) != int(build["expected_post_count"]):
        raise AnalysisReleaseRepositoryError("post_decision_rows_incomplete")
    decisions: list[PostDecision] = []
    decision_ids: dict[tuple[int, int], str] = {}
    for row in decision_rows:
        identity = int(row["source_post_id"]), int(row["source_version"])
        if identity in decision_ids:
            raise AnalysisReleaseRepositoryError("post_decision_identity_duplicate")
        evidence_ids = tuple(
            sorted(filter(None, str(row["evidence_ids"] or "").split("|")))
        )
        decisions.append(
            PostDecision(
                source_post_id=identity[0],
                source_version=identity[1],
                decision=str(row["decision_action"]),  # type: ignore[arg-type]
                reason_codes=(str(row["reason_code"]),),
                evidence_ids=evidence_ids,
                rule_version=str(build["decision_version"]),
                decision_sha256=str(row["decision_sha256"]),
            )
        )
        decision_ids[identity] = str(row["decision_id"])

    dedup_parent = _require_row(
        connection.execute(
            """
            SELECT * FROM text_dedup_builds
            WHERE dedup_build_id = ? AND run_id = ? AND post_decision_build_id = ?
            """,
            (text_dedup_build_id, run_id, post_decision_build_id),
        ).fetchone(),
        "text_dedup_build_not_found",
    )
    if dedup_parent["seal_status"] != "finalized":
        raise AnalysisReleaseRepositoryError("finalized_text_dedup_required")

    cluster_rows = connection.execute(
        """
        SELECT * FROM text_dedup_clusters
        WHERE dedup_build_id = ? ORDER BY cluster_id
        """,
        (text_dedup_build_id,),
    ).fetchall()
    member_rows = connection.execute(
        """
        SELECT * FROM text_dedup_members
        WHERE dedup_build_id = ? ORDER BY cluster_id, source_post_id, source_version
        """,
        (text_dedup_build_id,),
    ).fetchall()
    if (
        len(cluster_rows) != int(dedup_parent["cluster_count"])
        or len(member_rows) != int(dedup_parent["member_count"])
    ):
        raise AnalysisReleaseRepositoryError("text_dedup_rows_incomplete")
    members_by_cluster: dict[str, list[sqlite3.Row]] = {}
    for row in member_rows:
        members_by_cluster.setdefault(str(row["cluster_id"]), []).append(row)
    clusters: list[AnalysisDedupCluster] = []
    dedup_identities: set[tuple[int, int]] = set()
    for row in cluster_rows:
        cluster_id = str(row["cluster_id"])
        members = tuple(
            (int(member["source_post_id"]), int(member["source_version"]))
            for member in members_by_cluster.get(cluster_id, ())
        )
        representative = (
            int(row["representative_source_post_id"]),
            int(row["representative_source_version"]),
        )
        if len(members) != int(row["member_count"]) or representative not in members:
            raise AnalysisReleaseRepositoryError("text_dedup_rows_incomplete")
        dedup_identities.update(members)
        clusters.append(
            AnalysisDedupCluster(
                cluster_id=cluster_id,
                members=members,
                representative=representative,
                representative_strategy=str(dedup_parent["selection_strategy"]),
                representative_reason=str(row["representative_reason_code"]),
                relation_evidence_ids=(),
                human_label_conflict=False,
                review_members=(),
            )
        )
    keep_identities = {
        (decision.source_post_id, decision.source_version)
        for decision in decisions
        if decision.decision == "keep"
    }
    if dedup_identities != keep_identities:
        raise AnalysisReleaseRepositoryError("text_dedup_eligible_partition_mismatch")

    # 纯领域 build_analysis_release 要求去重对象覆盖全部帖子决定，而 v24 SQLite
    # 有意只持久化 keep 人口。为复用同一领域投影，在内存中给非 keep 决定补充
    # 不会进入任何输出的单成员簇；真实去重计数仍从已封存表重算并进入报告。
    for identity in sorted(set(decision_ids) - keep_identities):
        cluster_id = "noneligible-" + _canonical_sha256(identity)[:20]
        clusters.append(
            AnalysisDedupCluster(
                cluster_id=cluster_id,
                members=(identity,),
                representative=identity,
                representative_strategy="not_applicable",
                representative_reason="post_not_eligible_for_analysis_dedup",
                relation_evidence_ids=(),
                human_label_conflict=False,
                review_members=(),
            )
        )
    dedup = AnalysisDedupBuild(
        rule_version=str(dedup_parent["dedup_version"]),
        clusters=tuple(clusters),
        review_members=(),
        manifest_sha256=str(dedup_parent["member_manifest_sha256"]),
    )
    hashes = {
        "post_decision_manifest_sha256": str(build["decision_manifest_sha256"]),
        "text_dedup_manifest_sha256": str(dedup_parent["member_manifest_sha256"]),
        "text_keep_audit_manifest_sha256": str(audit["evidence_manifest_sha256"]),
    }
    return (
        tuple(decisions),
        decision_ids,
        dedup,
        len(cluster_rows),
        len(member_rows),
        hashes,
    )


def _load_image_inputs(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    snapshot_id: str,
    image_decision_build_id: str,
    image_keep_audit_evaluation_id: str,
) -> tuple[
    tuple[ImageSourceRelation, ...],
    tuple[FinalizedImageTechnicalDecision, ...],
    dict[tuple[int, int], _ImageMemberBinding],
    dict[str, str],
]:
    """从绑定 manifest 展开角色与 SHA 精确簇代表决定，不读取 pHash。"""

    parent = _require_row(
        connection.execute(
            """
            SELECT d.*, c.run_id AS candidate_run_id, c.manifest_id,
                   c.seal_status AS candidate_seal_status,
                   m.source_snapshot_id, m.accepted_row_count, m.status AS manifest_status,
                   m.source_sha256 AS image_manifest_sha256
            FROM image_decision_builds AS d
            JOIN image_candidate_builds AS c ON c.build_id = d.candidate_build_id
            JOIN image_manifest_imports AS m ON m.manifest_id = c.manifest_id
            WHERE d.decision_build_id = ?
            """,
            (image_decision_build_id,),
        ).fetchone(),
        "image_decision_build_not_found",
    )
    if (
        parent["candidate_run_id"] != run_id
        or parent["source_snapshot_id"] != snapshot_id
    ):
        raise AnalysisReleaseRepositoryError("image_decision_lineage_mismatch")
    if parent["seal_status"] != "finalized" or parent["candidate_seal_status"] != "finalized":
        raise AnalysisReleaseRepositoryError("finalized_image_decisions_required")
    if parent["manifest_status"] not in {"accepted", "accepted_with_rejections"}:
        raise AnalysisReleaseRepositoryError("image_manifest_not_accepted")

    audit = _require_row(
        connection.execute(
            """
            SELECT e.*, r.decision_build_id, r.seal_status AS round_seal_status,
                   r.integrity_status, r.population_manifest_sha256
            FROM image_keep_audit_evaluations AS e
            JOIN image_keep_audit_rounds AS r ON r.audit_round_id = e.audit_round_id
            WHERE e.audit_evaluation_id = ?
            """,
            (image_keep_audit_evaluation_id,),
        ).fetchone(),
        "image_keep_audit_evaluation_not_found",
    )
    if audit["decision_build_id"] != image_decision_build_id:
        raise AnalysisReleaseRepositoryError("image_keep_audit_lineage_mismatch")
    if (
        audit["round_seal_status"] != "finalized"
        or audit["integrity_status"] != _IMAGE_AUDIT_INTEGRITY_STATE
        or audit["seal_status"] != "finalized"
        or audit["evaluation_status"] != "passed"
    ):
        raise AnalysisReleaseRepositoryError("image_keep_audit_not_passed")

    role_rows = connection.execute(
        """
        SELECT row.manifest_row_id, row.source_image_id, row.source_post_id,
               row.relation_role, role.role_decision_id, role.relation_role AS decided_role,
               role.handling_action, image_observation.source_version AS image_version,
               post_observation.source_version AS post_version
        FROM image_manifest_rows AS row
        LEFT JOIN image_role_results AS role ON role.manifest_row_id = row.manifest_row_id
        LEFT JOIN source_image_observations AS image_observation
          ON image_observation.snapshot_id = ?
         AND image_observation.source_image_id = row.source_image_id
         AND image_observation.change_kind != 'missing'
        LEFT JOIN source_post_observations AS post_observation
          ON post_observation.snapshot_id = ?
         AND post_observation.source_post_id = row.source_post_id
         AND post_observation.change_kind != 'missing'
        WHERE row.manifest_id = ? AND row.validation_status = 'accepted'
        ORDER BY row.manifest_row_id, role.role_decision_id
        """,
        (snapshot_id, snapshot_id, parent["manifest_id"]),
    ).fetchall()
    grouped_roles: dict[str, list[sqlite3.Row]] = {}
    for row in role_rows:
        grouped_roles.setdefault(str(row["manifest_row_id"]), []).append(row)
    if len(grouped_roles) != int(parent["accepted_row_count"]):
        raise AnalysisReleaseRepositoryError("image_manifest_relationships_incomplete")

    relations: list[ImageSourceRelation] = []
    bindings: dict[tuple[int, int], _ImageMemberBinding] = {}
    relation_by_manifest_row: dict[str, ImageSourceRelation] = {}
    expected_actions = {
        "author_avatar": "exclude_from_content",
        "page": "evidence_only",
        "content": "inspect_content",
    }
    for manifest_row_id, rows in grouped_roles.items():
        if len(rows) != 1:
            raise AnalysisReleaseRepositoryError("image_role_evidence_ambiguous")
        row = rows[0]
        if row["role_decision_id"] is None or row["image_version"] is None or row["post_version"] is None:
            raise AnalysisReleaseRepositoryError("image_manifest_relationships_incomplete")
        role = str(row["relation_role"])
        if row["decided_role"] != role or row["handling_action"] != expected_actions.get(role):
            raise AnalysisReleaseRepositoryError("image_role_evidence_mismatch")
        relation = ImageSourceRelation(
            source_image_id=int(row["source_image_id"]),
            source_version=int(row["image_version"]),
            source_post_id=int(row["source_post_id"]),
            source_post_version=int(row["post_version"]),
            role=role,  # type: ignore[arg-type]
        )
        if relation.image_identity in bindings:
            raise AnalysisReleaseRepositoryError("image_source_relationship_duplicate")
        relations.append(relation)
        relation_by_manifest_row[manifest_row_id] = relation
        bindings[relation.image_identity] = _ImageMemberBinding(
            manifest_row_id=manifest_row_id,
            fingerprint_id=None,
            decision_id=None,
            role_decision_id=str(row["role_decision_id"]),
        )

    decision_rows = connection.execute(
        """
        SELECT cluster.cluster_id, cluster.member_count,
               cluster.representative_fingerprint_id,
               member.fingerprint_id, member.is_representative,
               fingerprint.manifest_row_id, decision.decision_id,
               decision.decision_action, decision.technical_noise_label,
               decision.provenance, decision.evidence_id,
               decision.decision_sha256
        FROM image_decision_builds AS decision_build
        JOIN image_exact_clusters AS cluster
          ON cluster.build_id = decision_build.candidate_build_id
        JOIN image_exact_cluster_members AS member
          ON member.build_id = cluster.build_id AND member.cluster_id = cluster.cluster_id
        JOIN image_fingerprints AS fingerprint
          ON fingerprint.fingerprint_id = member.fingerprint_id
        JOIN image_decisions AS decision
          ON decision.decision_build_id = decision_build.decision_build_id
         AND decision.fingerprint_id = member.fingerprint_id
        WHERE decision_build.decision_build_id = ?
        ORDER BY cluster.cluster_id, member.fingerprint_id
        """,
        (image_decision_build_id,),
    ).fetchall()
    content_manifest_ids = {
        manifest_row_id
        for manifest_row_id, relation in relation_by_manifest_row.items()
        if relation.role == "content"
    }
    if (
        len(decision_rows) != len(content_manifest_ids)
        or len(decision_rows) != int(parent["expected_decision_count"])
        or {str(row["manifest_row_id"]) for row in decision_rows}
        != content_manifest_ids
    ):
        raise AnalysisReleaseRepositoryError("image_content_decision_partition_incomplete")

    rows_by_fingerprint = {
        str(row["fingerprint_id"]): row for row in decision_rows
    }
    if len(rows_by_fingerprint) != len(decision_rows):
        raise AnalysisReleaseRepositoryError("image_content_decision_partition_incomplete")
    confirmed_noise = {
        "site_background",
        "site_ui",
        "placeholder_or_error",
        "tracking_or_qr_only",
    }
    for row in decision_rows:
        if row["decision_action"] == "exclude" and (
            row["provenance"] not in {"double_agreement", "adjudication"}
            or row["technical_noise_label"] not in confirmed_noise
            or row["evidence_id"] is None
        ):
            raise AnalysisReleaseRepositoryError("image_exclusion_evidence_invalid")

    propagation_runs = connection.execute(
        """
        SELECT propagation.*, cluster.representative_fingerprint_id,
               cluster.member_count AS exact_member_count,
               source.fingerprint_id AS source_fingerprint_id,
               source.decision_action AS source_action,
               source.provenance AS source_provenance,
               source.technical_noise_label AS source_label,
               source.decision_sha256 AS source_decision_sha256
        FROM image_sha_propagation_runs AS propagation
        LEFT JOIN image_exact_clusters AS cluster
          ON cluster.build_id = propagation.candidate_build_id
         AND cluster.cluster_id = propagation.exact_cluster_id
        LEFT JOIN image_decisions AS source
          ON source.decision_id = propagation.representative_decision_id
         AND source.decision_build_id = propagation.decision_build_id
        WHERE propagation.decision_build_id = ?
        ORDER BY propagation.propagation_run_id
        """,
        (image_decision_build_id,),
    ).fetchall()
    propagation_overrides: dict[str, sqlite3.Row] = {}
    exact_members_by_cluster: dict[str, set[str]] = {}
    for row in decision_rows:
        exact_members_by_cluster.setdefault(str(row["cluster_id"]), set()).add(
            str(row["fingerprint_id"])
        )
    for propagation in propagation_runs:
        member_rows = connection.execute(
            """
            SELECT fingerprint_id, propagated_label, source_decision_id
            FROM image_sha_propagation_members
            WHERE propagation_run_id = ? ORDER BY fingerprint_id
            """,
            (propagation["propagation_run_id"],),
        ).fetchall()
        cluster_id = str(propagation["exact_cluster_id"])
        expected_members = exact_members_by_cluster.get(cluster_id, set())
        actual_members = {str(member["fingerprint_id"]) for member in member_rows}
        source_fingerprint = str(propagation["source_fingerprint_id"] or "")
        source_row = rows_by_fingerprint.get(source_fingerprint)
        valid = (
            propagation["candidate_build_id"] == parent["candidate_build_id"]
            and propagation["seal_status"] == "finalized"
            and source_row is not None
            and source_fingerprint == propagation["representative_fingerprint_id"]
            and propagation["source_action"] == "exclude"
            and propagation["source_provenance"]
            in {"double_agreement", "adjudication"}
            and propagation["source_label"] in confirmed_noise
            and propagation["technical_noise_label"] == propagation["source_label"]
            and int(propagation["expected_member_count"])
            == int(propagation["exact_member_count"] or -1)
            and len(member_rows) == int(propagation["expected_member_count"])
            and actual_members == expected_members
            and all(
                member["source_decision_id"]
                == propagation["representative_decision_id"]
                and member["propagated_label"] == propagation["source_label"]
                for member in member_rows
            )
        )
        if not valid or any(
            fingerprint_id in propagation_overrides
            for fingerprint_id in actual_members
        ):
            raise AnalysisReleaseRepositoryError("image_sha_propagation_invalid")
        for fingerprint_id in actual_members:
            propagation_overrides[fingerprint_id] = propagation

    decisions: list[FinalizedImageTechnicalDecision] = []
    for row in decision_rows:
        manifest_row_id = str(row["manifest_row_id"])
        relation = relation_by_manifest_row[manifest_row_id]
        fingerprint_id = str(row["fingerprint_id"])
        propagation = propagation_overrides.get(fingerprint_id)
        action = "exclude" if propagation is not None else str(row["decision_action"])
        decision_sha256 = (
            str(propagation["source_decision_sha256"])
            if propagation is not None
            else str(row["decision_sha256"])
        )
        decisions.append(
            FinalizedImageTechnicalDecision(
                source_image_id=relation.source_image_id,
                source_version=relation.source_version,
                decision_action=action,  # type: ignore[arg-type]
                decision_build_id=image_decision_build_id,
                decision_sha256=decision_sha256,
            )
        )
        bindings[relation.image_identity] = _ImageMemberBinding(
            manifest_row_id=manifest_row_id,
            fingerprint_id=fingerprint_id,
            decision_id=str(row["decision_id"]),
            role_decision_id=bindings[relation.image_identity].role_decision_id,
        )
    hashes = {
        "image_decision_manifest_sha256": str(parent["decision_manifest_sha256"]),
        "image_keep_audit_manifest_sha256": str(audit["evidence_manifest_sha256"]),
        "image_manifest_sha256": str(parent["image_manifest_sha256"]),
    }
    return tuple(relations), tuple(decisions), bindings, hashes


def _load_release_inputs(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
    image_decision_build_id: str,
    image_keep_audit_evaluation_id: str,
) -> _ReleaseInputs:
    """组合文本与图片的显式谱系，并拒绝跨快照或跨运行拼接。"""

    run, snapshot = _load_run_and_snapshot(connection, run_id)
    snapshot_id = str(snapshot["snapshot_id"])
    (
        post_decisions,
        post_ids,
        dedup,
        dedup_cluster_count,
        dedup_member_count,
        text_hashes,
    ) = _load_post_inputs(
        connection,
        run_id=run_id,
        snapshot_id=snapshot_id,
        release_mode=release_mode,
        post_decision_build_id=post_decision_build_id,
        text_dedup_build_id=text_dedup_build_id,
        text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
    )
    relations, image_decisions, bindings, image_hashes = _load_image_inputs(
        connection,
        run_id=run_id,
        snapshot_id=snapshot_id,
        image_decision_build_id=image_decision_build_id,
        image_keep_audit_evaluation_id=image_keep_audit_evaluation_id,
    )
    post_identities = {
        (decision.source_post_id, decision.source_version) for decision in post_decisions
    }
    if any(relation.post_identity not in post_identities for relation in relations):
        raise AnalysisReleaseRepositoryError("image_parent_post_version_mismatch")
    return _ReleaseInputs(
        run=run,
        snapshot=snapshot,
        post_decisions=post_decisions,
        post_decision_ids=post_ids,
        dedup=dedup,
        persisted_dedup_cluster_count=dedup_cluster_count,
        persisted_dedup_member_count=dedup_member_count,
        image_relations=relations,
        image_decisions=image_decisions,
        image_bindings=bindings,
        upstream_hashes={**text_hashes, **image_hashes},
    )


def _manifest_hashes(
    projection: AnalysisReleaseProjection,
    inputs: _ReleaseInputs,
) -> dict[str, str]:
    """从四类领域成员和真实 SQLite 外键重算五类数据库 manifest。"""

    posts_eligible = sorted([
        [
            inputs.post_decision_ids[item.identity],
            item.source_post_id,
            item.source_version,
        ]
        for item in projection.analysis_posts_eligible
    ])
    posts_deduplicated = sorted([
        [item.cluster_id, item.source_post_id, item.source_version]
        for item in projection.analysis_posts_deduplicated
    ])
    images_eligible = []
    for item in projection.analysis_images_eligible:
        binding = inputs.image_bindings[item.image_identity]
        images_eligible.append(
            [
                binding.decision_id,
                binding.fingerprint_id,
                binding.manifest_row_id,
                item.source_image_id,
                item.source_version,
                item.source_post_id,
                item.source_post_version,
            ]
        )
    images_eligible.sort()
    images_evidence = []
    for item in projection.analysis_images_evidence_only:
        binding = inputs.image_bindings[item.image_identity]
        images_evidence.append(
            [
                binding.manifest_row_id,
                binding.role_decision_id,
                item.source_image_id,
                item.source_version,
                item.source_post_id,
                item.source_post_version,
            ]
        )
    images_evidence.sort()
    return {
        "release": projection.manifest_sha256,
        "posts_eligible": _canonical_sha256(posts_eligible),
        "posts_deduplicated": _canonical_sha256(posts_deduplicated),
        "images_eligible": _canonical_sha256(images_eligible),
        "images_evidence_only": _canonical_sha256(images_evidence),
    }


def _deidentified_id(release_id: str, kind: str, identity: Sequence[object]) -> str:
    """为本地交付包生成发布域内不可逆、稳定的成员身份。"""

    return hashlib.sha256(
        _canonical_json([release_id, kind, *identity]).encode("utf-8")
    ).hexdigest()


def _prepare_release(
    *,
    inputs: _ReleaseInputs,
    run_id: str,
    release_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
    image_decision_build_id: str,
    image_keep_audit_evaluation_id: str,
) -> _PreparedRelease:
    """调用纯领域投影并生成数据库与 artifact 的全部确定性摘要。"""

    request_payload = {
        "release_id": release_id,
        "run_id": run_id,
        "release_mode": release_mode,
        "post_decision_build_id": post_decision_build_id,
        "text_dedup_build_id": text_dedup_build_id,
        "text_keep_audit_evaluation_id": text_keep_audit_evaluation_id,
        "image_decision_build_id": image_decision_build_id,
        "image_keep_audit_evaluation_id": image_keep_audit_evaluation_id,
        "source_snapshot_id": str(inputs.snapshot["snapshot_id"]),
        "source_snapshot_sha256": str(inputs.snapshot["snapshot_sha256"]),
        "schema_version": DERIVED_SCHEMA_VERSION,
        "protocol_version": str(inputs.run["protocol_version"]),
        "config_sha256": str(inputs.run["config_sha256"]),
        "code_version": str(inputs.run["code_version"]),
        **inputs.upstream_hashes,
    }
    request_hash = _canonical_sha256(request_payload)
    try:
        projection = build_analysis_release(
            ReleaseRequest(
                release_id=release_id,
                run_id=run_id,
                mode=release_mode,  # type: ignore[arg-type]
                metadata={
                    "schema_version": DERIVED_SCHEMA_VERSION,
                    "protocol_version": str(inputs.run["protocol_version"]),
                    "config_sha256": str(inputs.run["config_sha256"]),
                    "code_version": str(inputs.run["code_version"]),
                    "request_manifest_sha256": request_hash,
                },
            ),
            inputs.post_decisions,
            inputs.dedup,
            inputs.image_relations,
            inputs.image_decisions,
        )
    except ValueError as exc:
        raise AnalysisReleaseRepositoryError("release_domain_projection_invalid") from exc
    if any(item.content_status == "blocked" for item in projection.image_projections):
        raise AnalysisReleaseRepositoryError("image_content_decision_partition_incomplete")

    member_hashes = _manifest_hashes(projection, inputs)
    quality_report: dict[str, object] = {
        "release_id": release_id,
        "run_id": run_id,
        "release_mode": release_mode,
        "request_manifest_sha256": request_hash,
        "release_manifest_sha256": projection.manifest_sha256,
        "post_decision_counts": dict(projection.quality_report.post_decision_counts),
        "post_reason_counts": dict(projection.quality_report.post_reason_counts),
        "image_role_counts": dict(projection.quality_report.image_role_counts),
        "content_technical_counts": dict(
            projection.quality_report.content_technical_counts
        ),
        "image_projection_reason_counts": dict(
            projection.quality_report.image_projection_reason_counts
        ),
        "output_member_counts": dict(projection.quality_report.output_member_counts),
        "dedup_counts": {
            "cluster_count": inputs.persisted_dedup_cluster_count,
            "member_count": inputs.persisted_dedup_member_count,
            "eligible_representative_count": len(
                projection.analysis_posts_deduplicated
            ),
            "eligible_nonrepresentative_count": len(
                projection.analysis_posts_eligible
            )
            - len(projection.analysis_posts_deduplicated),
        },
    }
    lineage_report: dict[str, object] = {
        "release_id": release_id,
        "run_id": run_id,
        "schema_version": DERIVED_SCHEMA_VERSION,
        "protocol_version": str(inputs.run["protocol_version"]),
        "code_version": str(inputs.run["code_version"]),
        "source_snapshot_id": str(inputs.snapshot["snapshot_id"]),
        "source_snapshot_sha256": str(inputs.snapshot["snapshot_sha256"]),
        "post_decision_build_id": post_decision_build_id,
        "text_dedup_build_id": text_dedup_build_id,
        "text_keep_audit_evaluation_id": text_keep_audit_evaluation_id,
        "image_decision_build_id": image_decision_build_id,
        "image_keep_audit_evaluation_id": image_keep_audit_evaluation_id,
        "upstream_hashes": dict(inputs.upstream_hashes),
        "member_manifest_hashes": member_hashes,
    }
    flat_counts = {
        **{
            f"posts_{key}_count": int(value)
            for key, value in projection.quality_report.post_decision_counts.items()
        },
        "posts_eligible_count": len(projection.analysis_posts_eligible),
        "posts_deduplicated_count": len(projection.analysis_posts_deduplicated),
        "images_eligible_count": len(projection.analysis_images_eligible),
        "images_evidence_only_count": len(
            projection.analysis_images_evidence_only
        ),
    }
    artifact_report = {
        "release_id": release_id,
        "run_id": run_id,
        "mode_version": release_mode,
        "schema_version": str(DERIVED_SCHEMA_VERSION),
        "report_sha256": _canonical_sha256(quality_report),
        "counts": flat_counts,
    }
    artifact_manifest = {
        "release_id": release_id,
        "run_id": run_id,
        "mode_version": release_mode,
        "schema_version": str(DERIVED_SCHEMA_VERSION),
        "release_manifest_sha256": projection.manifest_sha256,
        "request_manifest_sha256": request_hash,
        "input_versions": {
            "protocol_version": str(inputs.run["protocol_version"]),
            "code_version": str(inputs.run["code_version"]),
        },
        "input_hashes": dict(inputs.upstream_hashes),
    }
    artifact_members = {
        "posts_eligible": {
            "count": len(projection.analysis_posts_eligible),
            "deidentified_ids": [
                _deidentified_id(
                    release_id, "post_eligible", (item.source_post_id, item.source_version)
                )
                for item in projection.analysis_posts_eligible
            ],
        },
        "posts_deduplicated": {
            "count": len(projection.analysis_posts_deduplicated),
            "deidentified_ids": [
                _deidentified_id(
                    release_id,
                    "post_deduplicated",
                    (item.source_post_id, item.source_version),
                )
                for item in projection.analysis_posts_deduplicated
            ],
        },
        "images_eligible": {
            "count": len(projection.analysis_images_eligible),
            "deidentified_ids": [
                _deidentified_id(
                    release_id,
                    "image_eligible",
                    (item.source_image_id, item.source_version),
                )
                for item in projection.analysis_images_eligible
            ],
        },
        "images_evidence_only": {
            "count": len(projection.analysis_images_evidence_only),
            "deidentified_ids": [
                _deidentified_id(
                    release_id,
                    "image_evidence",
                    (item.source_image_id, item.source_version),
                )
                for item in projection.analysis_images_evidence_only
            ],
        },
    }
    return _PreparedRelease(
        inputs=inputs,
        projection=projection,
        request_manifest_sha256=request_hash,
        quality_report=quality_report,
        lineage_report=lineage_report,
        member_manifest_hashes=member_hashes,
        artifact_report=artifact_report,
        artifact_manifest=artifact_manifest,
        artifact_members=artifact_members,
    )


def _insert_release_rows(
    connection: sqlite3.Connection,
    *,
    prepared: _PreparedRelease,
    run_id: str,
    release_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
    image_decision_build_id: str,
    image_keep_audit_evaluation_id: str,
    created_at: str,
) -> None:
    """在单一事务中追加父行、四类成员、报告和五类数据库 manifest。"""

    projection = prepared.projection
    inputs = prepared.inputs
    connection.execute(
        """
        INSERT INTO analysis_release_builds(
          release_id, run_id, source_snapshot_id, release_mode,
          post_decision_build_id, text_dedup_build_id,
          text_keep_audit_evaluation_id, image_decision_build_id,
          image_keep_audit_evaluation_id, protocol_version, schema_version,
          config_sha256, code_version, request_manifest_sha256,
          posts_eligible_count, posts_deduplicated_count,
          images_eligible_count, images_evidence_only_count,
          release_manifest_sha256, seal_status, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
        """,
        (
            release_id,
            run_id,
            inputs.snapshot["snapshot_id"],
            release_mode,
            post_decision_build_id,
            text_dedup_build_id,
            text_keep_audit_evaluation_id,
            image_decision_build_id,
            image_keep_audit_evaluation_id,
            inputs.run["protocol_version"],
            ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION,
            inputs.run["config_sha256"],
            inputs.run["code_version"],
            prepared.request_manifest_sha256,
            len(projection.analysis_posts_eligible),
            len(projection.analysis_posts_deduplicated),
            len(projection.analysis_images_eligible),
            len(projection.analysis_images_evidence_only),
            projection.manifest_sha256,
            created_at,
        ),
    )
    connection.executemany(
        """
        INSERT INTO analysis_posts_eligible(
          release_id, run_id, decision_id, source_post_id, source_version
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                release_id,
                run_id,
                inputs.post_decision_ids[item.identity],
                item.source_post_id,
                item.source_version,
            )
            for item in projection.analysis_posts_eligible
        ],
    )
    connection.executemany(
        """
        INSERT INTO analysis_posts_deduplicated(
          release_id, run_id, dedup_build_id, cluster_id,
          source_post_id, source_version
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                release_id,
                run_id,
                text_dedup_build_id,
                item.cluster_id,
                item.source_post_id,
                item.source_version,
            )
            for item in projection.analysis_posts_deduplicated
        ],
    )
    connection.executemany(
        """
        INSERT INTO analysis_images_eligible(
          release_id, run_id, decision_id, fingerprint_id, manifest_row_id,
          source_image_id, source_image_version, source_post_id, source_post_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                release_id,
                run_id,
                inputs.image_bindings[item.image_identity].decision_id,
                inputs.image_bindings[item.image_identity].fingerprint_id,
                inputs.image_bindings[item.image_identity].manifest_row_id,
                item.source_image_id,
                item.source_version,
                item.source_post_id,
                item.source_post_version,
            )
            for item in projection.analysis_images_eligible
        ],
    )
    connection.executemany(
        """
        INSERT INTO analysis_images_evidence_only(
          release_id, run_id, manifest_row_id, role_decision_id,
          source_image_id, source_image_version, source_post_id, source_post_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                release_id,
                run_id,
                inputs.image_bindings[item.image_identity].manifest_row_id,
                inputs.image_bindings[item.image_identity].role_decision_id,
                item.source_image_id,
                item.source_version,
                item.source_post_id,
                item.source_post_version,
            )
            for item in projection.analysis_images_evidence_only
        ],
    )
    reports = {
        "quality_summary": prepared.quality_report,
        "lineage": prepared.lineage_report,
    }
    for kind, payload in reports.items():
        connection.execute(
            """
            INSERT INTO analysis_release_reports(
              release_id, run_id, report_kind, report_json, report_sha256, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                release_id,
                run_id,
                kind,
                _canonical_json(payload),
                _canonical_sha256(payload),
                created_at,
            ),
        )
    for kind, digest in prepared.member_manifest_hashes.items():
        connection.execute(
            """
            INSERT INTO analysis_release_manifests(
              release_id, run_id, manifest_kind, manifest_sha256,
              artifact_relative_path, created_at_utc
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (release_id, run_id, kind, digest, created_at),
        )


def _release_result(row: sqlite3.Row, *, reused: bool) -> AnalysisReleaseBuildResult:
    """把父表行转换为不含本地路径的构建回执。"""

    return AnalysisReleaseBuildResult(
        release_id=str(row["release_id"]),
        run_id=str(row["run_id"]),
        release_mode=str(row["release_mode"]),
        seal_status=str(row["seal_status"]),
        reused=reused,
        posts_eligible_count=int(row["posts_eligible_count"]),
        posts_deduplicated_count=int(row["posts_deduplicated_count"]),
        images_eligible_count=int(row["images_eligible_count"]),
        images_evidence_only_count=int(row["images_evidence_only_count"]),
        release_manifest_sha256=str(row["release_manifest_sha256"]),
    )


def _stored_member_manifest_hashes(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    release_manifest_sha256: str,
) -> dict[str, str]:
    """直接从四张发布成员表重算 manifest，防止只相信父表计数或自报哈希。"""

    posts_eligible = sorted([
        [row["decision_id"], row["source_post_id"], row["source_version"]]
        for row in connection.execute(
            """
            SELECT decision_id, source_post_id, source_version
            FROM analysis_posts_eligible WHERE release_id = ?
            ORDER BY source_post_id, source_version
            """,
            (release_id,),
        )
    ])
    posts_deduplicated = sorted([
        [row["cluster_id"], row["source_post_id"], row["source_version"]]
        for row in connection.execute(
            """
            SELECT cluster_id, source_post_id, source_version
            FROM analysis_posts_deduplicated WHERE release_id = ?
            ORDER BY cluster_id, source_post_id, source_version
            """,
            (release_id,),
        )
    ])
    images_eligible = sorted([
        [
            row["decision_id"],
            row["fingerprint_id"],
            row["manifest_row_id"],
            row["source_image_id"],
            row["source_image_version"],
            row["source_post_id"],
            row["source_post_version"],
        ]
        for row in connection.execute(
            """
            SELECT decision_id, fingerprint_id, manifest_row_id, source_image_id,
                   source_image_version, source_post_id, source_post_version
            FROM analysis_images_eligible WHERE release_id = ?
            ORDER BY manifest_row_id
            """,
            (release_id,),
        )
    ])
    images_evidence = sorted([
        [
            row["manifest_row_id"],
            row["role_decision_id"],
            row["source_image_id"],
            row["source_image_version"],
            row["source_post_id"],
            row["source_post_version"],
        ]
        for row in connection.execute(
            """
            SELECT manifest_row_id, role_decision_id, source_image_id,
                   source_image_version, source_post_id, source_post_version
            FROM analysis_images_evidence_only WHERE release_id = ?
            ORDER BY manifest_row_id
            """,
            (release_id,),
        )
    ])
    return {
        "release": release_manifest_sha256,
        "posts_eligible": _canonical_sha256(posts_eligible),
        "posts_deduplicated": _canonical_sha256(posts_deduplicated),
        "images_eligible": _canonical_sha256(images_eligible),
        "images_evidence_only": _canonical_sha256(images_evidence),
    }


def _verify_persisted_release(
    connection: sqlite3.Connection,
    *,
    prepared: _PreparedRelease,
    run_id: str,
    release_id: str,
    output_root: str | Path,
) -> AnalysisReleaseVerificationResult:
    """重算 DB 子行、报告、manifest 及磁盘包，拒绝任何半成品或篡改。"""

    row = _require_row(
        connection.execute(
            "SELECT * FROM analysis_release_builds WHERE release_id = ? AND run_id = ?",
            (release_id, run_id),
        ).fetchone(),
        "analysis_release_not_found",
    )
    if row["seal_status"] not in {"finalized", "accepted"}:
        raise AnalysisReleaseRepositoryError("analysis_release_partial_build")
    if (
        row["request_manifest_sha256"] != prepared.request_manifest_sha256
        or row["release_manifest_sha256"] != prepared.projection.manifest_sha256
    ):
        raise AnalysisReleaseRepositoryError("analysis_release_request_conflict")
    table_counts = {
        "posts_eligible_count": "analysis_posts_eligible",
        "posts_deduplicated_count": "analysis_posts_deduplicated",
        "images_eligible_count": "analysis_images_eligible",
        "images_evidence_only_count": "analysis_images_evidence_only",
    }
    for field, table in table_counts.items():
        actual = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE release_id = ?", (release_id,)
            ).fetchone()[0]
        )
        if actual != int(row[field]):
            raise AnalysisReleaseRepositoryError("analysis_release_member_count_mismatch")
    stored_manifests = {
        str(item["manifest_kind"]): str(item["manifest_sha256"])
        for item in connection.execute(
            "SELECT manifest_kind, manifest_sha256 FROM analysis_release_manifests WHERE release_id = ?",
            (release_id,),
        )
    }
    actual_manifests = _stored_member_manifest_hashes(
        connection,
        release_id=release_id,
        release_manifest_sha256=str(row["release_manifest_sha256"]),
    )
    for kind, digest in prepared.member_manifest_hashes.items():
        if stored_manifests.get(kind) != digest or actual_manifests.get(kind) != digest:
            raise AnalysisReleaseRepositoryError("analysis_release_manifest_mismatch")
    expected_reports = {
        "quality_summary": prepared.quality_report,
        "lineage": prepared.lineage_report,
    }
    report_rows = connection.execute(
        "SELECT report_kind, report_json, report_sha256 FROM analysis_release_reports WHERE release_id = ?",
        (release_id,),
    ).fetchall()
    if len(report_rows) != 2:
        raise AnalysisReleaseRepositoryError("analysis_release_report_incomplete")
    for report in report_rows:
        kind = str(report["report_kind"])
        expected = expected_reports.get(kind)
        if (
            expected is None
            or report["report_json"] != _canonical_json(expected)
            or report["report_sha256"] != _canonical_sha256(expected)
        ):
            raise AnalysisReleaseRepositoryError("analysis_release_report_mismatch")

    artifact_dir = Path(output_root) / run_id / release_id
    try:
        verify_release_artifact(artifact_dir)
        artifact_hash = _file_sha256(artifact_dir / "artifact-manifest.json")
    except (OSError, ReleaseArtifactError) as exc:
        raise AnalysisReleaseRepositoryError("release_artifact_invalid") from exc
    artifact_row = connection.execute(
        """
        SELECT manifest_sha256, artifact_relative_path
        FROM analysis_release_manifests
        WHERE release_id = ? AND manifest_kind = 'artifact'
        """,
        (release_id,),
    ).fetchone()
    if (
        artifact_row is None
        or artifact_row["manifest_sha256"] != artifact_hash
        or artifact_row["artifact_relative_path"] != f"{run_id}/{release_id}"
    ):
        raise AnalysisReleaseRepositoryError("release_artifact_manifest_mismatch")
    return AnalysisReleaseVerificationResult(
        release_id=release_id,
        run_id=run_id,
        seal_status=str(row["seal_status"]),
        release_manifest_sha256=str(row["release_manifest_sha256"]),
        artifact_manifest_sha256=artifact_hash,
    )


def build_release(
    derived_db: str | Path,
    *,
    run_id: str,
    release_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
    image_decision_build_id: str,
    image_keep_audit_evaluation_id: str,
    output_root: str | Path,
) -> AnalysisReleaseBuildResult:
    """从显式版本事实构建并封存 SQLite 发布与不可变本地包。

    同一完整请求在数据库和磁盘都通过重算后幂等复用。相同 ``release_id`` 的
    身份冲突、building 半成品、已有但未绑定的磁盘目录或任一上游证据不完整
    都会拒绝，不覆盖旧记录或本地文件。
    """

    upstream_ids = (
        ("post_decision_build_id", post_decision_build_id),
        ("text_dedup_build_id", text_dedup_build_id),
        ("text_keep_audit_evaluation_id", text_keep_audit_evaluation_id),
        ("image_decision_build_id", image_decision_build_id),
        ("image_keep_audit_evaluation_id", image_keep_audit_evaluation_id),
    )
    _validate_request_identifiers(
        run_id=run_id,
        release_id=release_id,
        release_mode=release_mode,
        upstream_ids=upstream_ids,
    )
    artifact_created: Path | None = None
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            inputs = _load_release_inputs(
                connection,
                run_id=run_id,
                release_mode=release_mode,
                post_decision_build_id=post_decision_build_id,
                text_dedup_build_id=text_dedup_build_id,
                text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
                image_decision_build_id=image_decision_build_id,
                image_keep_audit_evaluation_id=image_keep_audit_evaluation_id,
            )
            prepared = _prepare_release(
                inputs=inputs,
                run_id=run_id,
                release_id=release_id,
                release_mode=release_mode,
                post_decision_build_id=post_decision_build_id,
                text_dedup_build_id=text_dedup_build_id,
                text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
                image_decision_build_id=image_decision_build_id,
                image_keep_audit_evaluation_id=image_keep_audit_evaluation_id,
            )
            existing = connection.execute(
                "SELECT * FROM analysis_release_builds WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            if existing is not None:
                if existing["seal_status"] == "building":
                    raise AnalysisReleaseRepositoryError("analysis_release_partial_build")
                _verify_persisted_release(
                    connection,
                    prepared=prepared,
                    run_id=run_id,
                    release_id=release_id,
                    output_root=output_root,
                )
                return _release_result(existing, reused=True)
            duplicate_request = connection.execute(
                """
                SELECT release_id FROM analysis_release_builds
                WHERE run_id = ? AND request_manifest_sha256 = ?
                """,
                (run_id, prepared.request_manifest_sha256),
            ).fetchone()
            if duplicate_request is not None:
                raise AnalysisReleaseRepositoryError("analysis_release_request_conflict")

            now = _utcnow()
            with connection:
                _insert_release_rows(
                    connection,
                    prepared=prepared,
                    run_id=run_id,
                    release_id=release_id,
                    release_mode=release_mode,
                    post_decision_build_id=post_decision_build_id,
                    text_dedup_build_id=text_dedup_build_id,
                    text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
                    image_decision_build_id=image_decision_build_id,
                    image_keep_audit_evaluation_id=image_keep_audit_evaluation_id,
                    created_at=now,
                )
                artifact = publish_release_artifact(
                    output_root=output_root,
                    run_id=run_id,
                    release_id=release_id,
                    report=prepared.artifact_report,
                    manifest=prepared.artifact_manifest,
                    member_summaries=prepared.artifact_members,
                )
                artifact_created = artifact.artifact_dir
                # release_artifact 的原子重命名只证明写入完成；DB 封存前还要从
                # 目标目录重新读取并验证文件集合、内容边界、尺寸和全部哈希。
                verify_release_artifact(artifact.artifact_dir)
                artifact_hash = _file_sha256(
                    artifact.artifact_dir / "artifact-manifest.json"
                )
                connection.execute(
                    """
                    INSERT INTO analysis_release_manifests(
                      release_id, run_id, manifest_kind, manifest_sha256,
                      artifact_relative_path, created_at_utc
                    ) VALUES (?, ?, 'artifact', ?, ?, ?)
                    """,
                    (
                        release_id,
                        run_id,
                        artifact_hash,
                        f"{run_id}/{release_id}",
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE analysis_release_builds
                    SET seal_status = 'finalized', finalized_at_utc = ?
                    WHERE release_id = ? AND run_id = ? AND seal_status = 'building'
                    """,
                    (now, release_id, run_id),
                )
            artifact_created = None
            stored = _require_row(
                connection.execute(
                    "SELECT * FROM analysis_release_builds WHERE release_id = ?",
                    (release_id,),
                ).fetchone(),
                "analysis_release_finalize_failed",
            )
            _verify_persisted_release(
                connection,
                prepared=prepared,
                run_id=run_id,
                release_id=release_id,
                output_root=output_root,
            )
            return _release_result(stored, reused=False)
    except AnalysisReleaseRepositoryError:
        if artifact_created is not None:
            shutil.rmtree(artifact_created, ignore_errors=True)
        raise
    except ReleaseArtifactError as exc:
        if artifact_created is not None:
            shutil.rmtree(artifact_created, ignore_errors=True)
        raise AnalysisReleaseRepositoryError("release_artifact_conflict") from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        if artifact_created is not None:
            shutil.rmtree(artifact_created, ignore_errors=True)
        raise AnalysisReleaseRepositoryError("release_database_contract_rejected") from exc


def _prepare_from_stored(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    release_id: str,
) -> tuple[sqlite3.Row, _PreparedRelease]:
    """按显式发布父行中的冻结 ID 重建领域输入，不选择其他版本。"""

    row = _require_row(
        connection.execute(
            "SELECT * FROM analysis_release_builds WHERE release_id = ? AND run_id = ?",
            (release_id, run_id),
        ).fetchone(),
        "analysis_release_not_found",
    )
    inputs = _load_release_inputs(
        connection,
        run_id=run_id,
        release_mode=str(row["release_mode"]),
        post_decision_build_id=str(row["post_decision_build_id"]),
        text_dedup_build_id=str(row["text_dedup_build_id"]),
        text_keep_audit_evaluation_id=str(row["text_keep_audit_evaluation_id"]),
        image_decision_build_id=str(row["image_decision_build_id"]),
        image_keep_audit_evaluation_id=str(row["image_keep_audit_evaluation_id"]),
    )
    prepared = _prepare_release(
        inputs=inputs,
        run_id=run_id,
        release_id=release_id,
        release_mode=str(row["release_mode"]),
        post_decision_build_id=str(row["post_decision_build_id"]),
        text_dedup_build_id=str(row["text_dedup_build_id"]),
        text_keep_audit_evaluation_id=str(row["text_keep_audit_evaluation_id"]),
        image_decision_build_id=str(row["image_decision_build_id"]),
        image_keep_audit_evaluation_id=str(row["image_keep_audit_evaluation_id"]),
    )
    return row, prepared


def verify_release(
    derived_db: str | Path,
    *,
    run_id: str,
    release_id: str,
    output_root: str | Path,
) -> AnalysisReleaseVerificationResult:
    """按显式 run/release ID 离线复验数据库投影与本地包。"""

    _validate_request_identifiers(run_id=run_id, release_id=release_id)
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            _, prepared = _prepare_from_stored(
                connection,
                run_id=run_id,
                release_id=release_id,
            )
            return _verify_persisted_release(
                connection,
                prepared=prepared,
                run_id=run_id,
                release_id=release_id,
                output_root=output_root,
            )
    except AnalysisReleaseRepositoryError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise AnalysisReleaseRepositoryError("release_verification_failed") from exc


def get_release_status(
    derived_db: str | Path,
    *,
    run_id: str,
    release_id: str,
) -> AnalysisReleaseStatus:
    """查询一个显式发布及其运行状态；不存在时不会回退到其他发布。"""

    _validate_request_identifiers(run_id=run_id, release_id=release_id)
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            row = _require_row(
                connection.execute(
                    """
                    SELECT release.release_id, release.run_id, release.release_mode,
                           release.seal_status, run.status AS run_status, run.reason_code
                    FROM analysis_release_builds AS release
                    JOIN cleaning_runs AS run ON run.run_id = release.run_id
                    WHERE release.release_id = ? AND release.run_id = ?
                    """,
                    (release_id, run_id),
                ).fetchone(),
                "analysis_release_not_found",
            )
            return AnalysisReleaseStatus(
                release_id=release_id,
                run_id=run_id,
                release_mode=str(row["release_mode"]),
                seal_status=str(row["seal_status"]),
                run_status=str(row["run_status"]),
                reason_code=(
                    str(row["reason_code"]) if row["reason_code"] is not None else None
                ),
            )
    except AnalysisReleaseRepositoryError:
        raise
    except sqlite3.DatabaseError as exc:
        raise AnalysisReleaseRepositoryError("release_status_query_failed") from exc


def accept_release(
    derived_db: str | Path,
    *,
    run_id: str,
    release_id: str,
    output_root: str | Path,
) -> AnalysisReleaseAcceptanceResult:
    """显式接受一个 formal finalized 发布。

    仓储只重建数据库投影并复验不可变包，然后把显式身份、本地包目录与确定性
    图像投影状态交给验收服务。服务在同一事务中重读质量门、以 ``mode=ro``
    复验快照、构造 attestation，并按 ``attestation -> run -> release`` 顺序
    一次性授权。任一步失败都不会留下部分 accepted 状态。
    """

    _validate_request_identifiers(run_id=run_id, release_id=release_id)
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            _, prepared = _prepare_from_stored(
                connection,
                run_id=run_id,
                release_id=release_id,
            )
            _verify_persisted_release(
                connection,
                prepared=prepared,
                run_id=run_id,
                release_id=release_id,
                output_root=output_root,
            )
            outcome = accept_verified_release(
                connection,
                request=ReleaseAcceptanceRequest(
                    release_id=release_id,
                    run_id=run_id,
                    artifact_dir=Path(output_root) / run_id / release_id,
                    image_projection_ready=not any(
                        item.content_status in {"blocked", "review"}
                        for item in prepared.projection.image_projections
                    ),
                ),
                accepted_at_utc=_utcnow(),
            )
            return AnalysisReleaseAcceptanceResult(
                release_id=outcome.release_id,
                run_id=outcome.run_id,
                seal_status=outcome.seal_status,
                run_status=outcome.run_status,
                reused=outcome.reused,
            )
    except AnalysisReleaseRepositoryError:
        raise
    except ReleaseAcceptanceServiceError as exc:
        raise AnalysisReleaseRepositoryError(exc.reason_code) from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        raise AnalysisReleaseRepositoryError("release_acceptance_gate_rejected") from exc
