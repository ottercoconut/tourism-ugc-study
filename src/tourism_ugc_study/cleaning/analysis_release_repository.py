"""文本清洗分析发布的 SQLite 仓储与显式验收入口。"""

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
from .analysis_release import AnalysisReleaseProjection, ReleaseRequest, build_analysis_release
from .post_decision import PostDecision
from .release_acceptance import (
    ReleaseAcceptanceRequest,
    ReleaseAcceptanceServiceError,
    accept_verified_release,
)
from .release_artifact import (
    ReleaseArtifactError,
    publish_release_artifact,
    verify_release_artifact,
)
from .schema import (
    ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION,
    DERIVED_SCHEMA_VERSION,
    connect_derived,
    migrate_derived,
)


class AnalysisReleaseRepositoryError(RuntimeError):
    """表示发布证据、不可变包或接受质量门不满足。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("analysis release repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class AnalysisReleaseBuildResult:
    """一次已封存文本发布构建的去敏回执。"""

    release_id: str
    run_id: str
    release_mode: str
    seal_status: str
    reused: bool
    posts_eligible_count: int
    posts_deduplicated_count: int
    release_manifest_sha256: str


@dataclass(frozen=True)
class AnalysisReleaseVerificationResult:
    """数据库与本地不可变包复验一致后的回执。"""

    release_id: str
    run_id: str
    seal_status: str
    release_manifest_sha256: str
    artifact_manifest_sha256: str


@dataclass(frozen=True)
class AnalysisReleaseStatus:
    """显式发布及其运行的最小状态。"""

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
class _ReleaseInputs:
    run: sqlite3.Row
    snapshot: sqlite3.Row
    post_decisions: tuple[PostDecision, ...]
    post_decision_ids: Mapping[tuple[int, int], str]
    dedup: AnalysisDedupBuild
    persisted_cluster_count: int
    persisted_member_count: int
    upstream_hashes: Mapping[str, str]


@dataclass(frozen=True)
class _PreparedRelease:
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_id(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value[0] not in _SAFE_ID_CHARS
        or any(char not in _SAFE_ID_CHARS for char in value)
    ):
        raise AnalysisReleaseRepositoryError(f"invalid_{field}")
    if value.casefold() == "latest":
        raise AnalysisReleaseRepositoryError("mutable_latest_alias_forbidden")


def _validate_request(
    *,
    run_id: str,
    release_id: str,
    release_mode: str | None = None,
    upstream_ids: Sequence[tuple[str, str]] = (),
) -> None:
    _validate_id(run_id, "run_id")
    _validate_id(release_id, "release_id")
    for field, value in upstream_ids:
        _validate_id(value, field)
    if release_mode is not None and release_mode not in {"formal", "smoke"}:
        raise AnalysisReleaseRepositoryError("invalid_release_mode")


def _require(row: sqlite3.Row | None, reason_code: str) -> sqlite3.Row:
    if row is None:
        raise AnalysisReleaseRepositoryError(reason_code)
    return row


def _load_run_snapshot(
    connection: sqlite3.Connection, run_id: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    run = _require(
        connection.execute("SELECT * FROM cleaning_runs WHERE run_id = ?", (run_id,)).fetchone(),
        "cleaning_run_not_found",
    )
    if run["source_snapshot_id"] is None:
        raise AnalysisReleaseRepositoryError("source_snapshot_not_bound")
    snapshot = _require(
        connection.execute(
            "SELECT * FROM source_snapshots WHERE snapshot_id = ? AND run_id = ?",
            (run["source_snapshot_id"], run_id),
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
) -> _ReleaseInputs:
    run, snapshot = _load_run_snapshot(connection, run_id)
    if str(snapshot["snapshot_id"]) != snapshot_id:
        raise AnalysisReleaseRepositoryError("source_snapshot_lineage_mismatch")
    build = _require(
        connection.execute(
            "SELECT * FROM post_decision_builds WHERE decision_build_id = ? "
            "AND run_id = ? AND source_snapshot_id = ?",
            (post_decision_build_id, run_id, snapshot_id),
        ).fetchone(),
        "final_post_decision_build_not_found",
    )
    if build["build_kind"] != "final" or build["seal_status"] != "finalized":
        raise AnalysisReleaseRepositoryError("finalized_final_post_decisions_required")
    if build["text_keep_audit_evaluation_id"] != text_keep_audit_evaluation_id:
        raise AnalysisReleaseRepositoryError("text_keep_audit_lineage_mismatch")

    audit = _require(
        connection.execute(
            """
            SELECT evaluation.*, round.audit_mode,
                   round.seal_status AS round_seal_status, round.run_id AS audit_run_id
            FROM text_keep_audit_evaluations AS evaluation
            JOIN text_keep_audit_rounds AS round
              ON round.audit_round_id = evaluation.audit_round_id
            WHERE evaluation.audit_evaluation_id = ?
            """,
            (text_keep_audit_evaluation_id,),
        ).fetchone(),
        "text_keep_audit_evaluation_not_found",
    )
    if audit["audit_run_id"] != run_id or audit["audit_mode"] != release_mode:
        raise AnalysisReleaseRepositoryError("text_keep_audit_lineage_mismatch")
    if (
        audit["round_seal_status"] != "finalized"
        or audit["seal_status"] != "finalized"
        or audit["evaluation_status"] != "passed"
    ):
        raise AnalysisReleaseRepositoryError("text_keep_audit_not_passed")

    rows = connection.execute(
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
    if len(rows) != int(build["expected_post_count"]):
        raise AnalysisReleaseRepositoryError("post_decision_rows_incomplete")
    decisions: list[PostDecision] = []
    decision_ids: dict[tuple[int, int], str] = {}
    for row in rows:
        identity = int(row["source_post_id"]), int(row["source_version"])
        if identity in decision_ids:
            raise AnalysisReleaseRepositoryError("post_decision_identity_duplicate")
        decisions.append(
            PostDecision(
                source_post_id=identity[0],
                source_version=identity[1],
                decision=str(row["decision_action"]),  # type: ignore[arg-type]
                reason_codes=(str(row["reason_code"]),),
                evidence_ids=tuple(sorted(filter(None, str(row["evidence_ids"] or "").split("|")))),
                rule_version=str(build["decision_version"]),
                decision_sha256=str(row["decision_sha256"]),
            )
        )
        decision_ids[identity] = str(row["decision_id"])

    dedup_parent = _require(
        connection.execute(
            "SELECT * FROM text_dedup_builds WHERE dedup_build_id = ? "
            "AND run_id = ? AND post_decision_build_id = ?",
            (text_dedup_build_id, run_id, post_decision_build_id),
        ).fetchone(),
        "text_dedup_build_not_found",
    )
    if dedup_parent["seal_status"] != "finalized":
        raise AnalysisReleaseRepositoryError("finalized_text_dedup_required")
    cluster_rows = connection.execute(
        "SELECT * FROM text_dedup_clusters WHERE dedup_build_id = ? ORDER BY cluster_id",
        (text_dedup_build_id,),
    ).fetchall()
    member_rows = connection.execute(
        "SELECT * FROM text_dedup_members WHERE dedup_build_id = ? "
        "ORDER BY cluster_id, source_post_id, source_version",
        (text_dedup_build_id,),
    ).fetchall()
    if (
        len(cluster_rows) != int(dedup_parent["cluster_count"])
        or len(member_rows) != int(dedup_parent["member_count"])
    ):
        raise AnalysisReleaseRepositoryError("text_dedup_rows_incomplete")
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in member_rows:
        grouped.setdefault(str(row["cluster_id"]), []).append(row)
    clusters: list[AnalysisDedupCluster] = []
    persisted_identities: set[tuple[int, int]] = set()
    for row in cluster_rows:
        cluster_id = str(row["cluster_id"])
        members = tuple(
            (int(item["source_post_id"]), int(item["source_version"]))
            for item in grouped.get(cluster_id, ())
        )
        representative = (
            int(row["representative_source_post_id"]),
            int(row["representative_source_version"]),
        )
        if len(members) != int(row["member_count"]) or representative not in members:
            raise AnalysisReleaseRepositoryError("text_dedup_rows_incomplete")
        persisted_identities.update(members)
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
        (item.source_post_id, item.source_version)
        for item in decisions if item.decision == "keep"
    }
    if persisted_identities != keep_identities:
        raise AnalysisReleaseRepositoryError("text_dedup_eligible_partition_mismatch")
    for identity in sorted(set(decision_ids) - keep_identities):
        clusters.append(
            AnalysisDedupCluster(
                cluster_id="noneligible-" + _canonical_sha256(identity)[:20],
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
    return _ReleaseInputs(
        run=run,
        snapshot=snapshot,
        post_decisions=tuple(decisions),
        post_decision_ids=decision_ids,
        dedup=dedup,
        persisted_cluster_count=len(cluster_rows),
        persisted_member_count=len(member_rows),
        upstream_hashes={
            "post_decision_manifest_sha256": str(build["decision_manifest_sha256"]),
            "text_dedup_manifest_sha256": str(dedup_parent["member_manifest_sha256"]),
            "text_keep_audit_manifest_sha256": str(audit["evidence_manifest_sha256"]),
        },
    )


def _prepare(
    *,
    inputs: _ReleaseInputs,
    run_id: str,
    release_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
) -> _PreparedRelease:
    request_payload = {
        "release_id": release_id,
        "run_id": run_id,
        "release_mode": release_mode,
        "post_decision_build_id": post_decision_build_id,
        "text_dedup_build_id": text_dedup_build_id,
        "text_keep_audit_evaluation_id": text_keep_audit_evaluation_id,
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
        )
    except ValueError as exc:
        raise AnalysisReleaseRepositoryError("release_domain_projection_invalid") from exc

    eligible_rows = [
        [item.source_post_id, item.source_version, inputs.post_decision_ids[item.identity]]
        for item in projection.analysis_posts_eligible
    ]
    dedup_rows = [
        [item.cluster_id, item.source_post_id, item.source_version]
        for item in projection.analysis_posts_deduplicated
    ]
    member_hashes = {
        "posts_eligible": _canonical_sha256(eligible_rows),
        "posts_deduplicated": _canonical_sha256(dedup_rows),
    }
    quality_report: dict[str, object] = {
        "release_id": release_id,
        "run_id": run_id,
        "release_mode": release_mode,
        "request_manifest_sha256": request_hash,
        "release_manifest_sha256": projection.manifest_sha256,
        "post_decision_counts": dict(projection.quality_report.post_decision_counts),
        "post_reason_counts": dict(projection.quality_report.post_reason_counts),
        "output_member_counts": dict(projection.quality_report.output_member_counts),
        "dedup_counts": {
            "cluster_count": inputs.persisted_cluster_count,
            "member_count": inputs.persisted_member_count,
            "eligible_representative_count": len(projection.analysis_posts_deduplicated),
            "eligible_nonrepresentative_count": (
                len(projection.analysis_posts_eligible)
                - len(projection.analysis_posts_deduplicated)
            ),
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
        "upstream_hashes": dict(inputs.upstream_hashes),
        "member_manifest_hashes": member_hashes,
    }
    artifact_report = {
        "release_id": release_id,
        "run_id": run_id,
        "mode_version": release_mode,
        "schema_version": str(DERIVED_SCHEMA_VERSION),
        "report_sha256": _canonical_sha256(quality_report),
        "counts": {
            "posts_eligible_count": len(projection.analysis_posts_eligible),
            "posts_deduplicated_count": len(projection.analysis_posts_deduplicated),
        },
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
                _canonical_sha256([release_id, "post_eligible", *item.identity])
                for item in projection.analysis_posts_eligible
            ],
        },
        "posts_deduplicated": {
            "count": len(projection.analysis_posts_deduplicated),
            "deidentified_ids": [
                _canonical_sha256([release_id, "post_deduplicated", *item.identity])
                for item in projection.analysis_posts_deduplicated
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


def _insert_rows(
    connection: sqlite3.Connection,
    *,
    prepared: _PreparedRelease,
    run_id: str,
    release_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
    created_at: str,
) -> None:
    projection = prepared.projection
    connection.execute(
        """
        INSERT INTO analysis_release_builds(
          release_id, run_id, source_snapshot_id, release_mode,
          post_decision_build_id, text_dedup_build_id,
          text_keep_audit_evaluation_id, protocol_version, schema_version,
          config_sha256, code_version, request_manifest_sha256,
          posts_eligible_count, posts_deduplicated_count,
          release_manifest_sha256, seal_status, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
        """,
        (
            release_id,
            run_id,
            prepared.inputs.snapshot["snapshot_id"],
            release_mode,
            post_decision_build_id,
            text_dedup_build_id,
            text_keep_audit_evaluation_id,
            prepared.inputs.run["protocol_version"],
            ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION,
            prepared.inputs.run["config_sha256"],
            prepared.inputs.run["code_version"],
            prepared.request_manifest_sha256,
            len(projection.analysis_posts_eligible),
            len(projection.analysis_posts_deduplicated),
            projection.manifest_sha256,
            created_at,
        ),
    )
    for item in projection.analysis_posts_eligible:
        connection.execute(
            "INSERT INTO analysis_posts_eligible VALUES (?, ?, ?, ?, ?)",
            (release_id, run_id, prepared.inputs.post_decision_ids[item.identity], *item.identity),
        )
    for item in projection.analysis_posts_deduplicated:
        connection.execute(
            "INSERT INTO analysis_posts_deduplicated VALUES (?, ?, ?, ?, ?, ?)",
            (release_id, run_id, text_dedup_build_id, item.cluster_id, *item.identity),
        )
    manifests = {
        "release": projection.manifest_sha256,
        **prepared.member_manifest_hashes,
    }
    for kind, digest in manifests.items():
        connection.execute(
            "INSERT INTO analysis_release_manifests VALUES (?, ?, ?, ?, NULL, ?)",
            (release_id, run_id, kind, digest, created_at),
        )
    for kind, report in (
        ("quality_summary", prepared.quality_report),
        ("lineage", prepared.lineage_report),
    ):
        connection.execute(
            "INSERT INTO analysis_release_reports VALUES (?, ?, ?, ?, ?, ?)",
            (release_id, run_id, kind, _canonical_json(report), _canonical_sha256(report), created_at),
        )


def _result(row: sqlite3.Row, reused: bool) -> AnalysisReleaseBuildResult:
    return AnalysisReleaseBuildResult(
        release_id=str(row["release_id"]),
        run_id=str(row["run_id"]),
        release_mode=str(row["release_mode"]),
        seal_status=str(row["seal_status"]),
        reused=reused,
        posts_eligible_count=int(row["posts_eligible_count"]),
        posts_deduplicated_count=int(row["posts_deduplicated_count"]),
        release_manifest_sha256=str(row["release_manifest_sha256"]),
    )


def _stored_member_hashes(connection: sqlite3.Connection, release_id: str) -> dict[str, str]:
    eligible = [
        [int(row["source_post_id"]), int(row["source_version"]), str(row["decision_id"])]
        for row in connection.execute(
            "SELECT * FROM analysis_posts_eligible WHERE release_id = ? "
            "ORDER BY source_post_id, source_version", (release_id,)
        )
    ]
    dedup = [
        [str(row["cluster_id"]), int(row["source_post_id"]), int(row["source_version"])]
        for row in connection.execute(
            "SELECT * FROM analysis_posts_deduplicated WHERE release_id = ? "
            "ORDER BY cluster_id, source_post_id, source_version", (release_id,)
        )
    ]
    return {
        "posts_eligible": _canonical_sha256(eligible),
        "posts_deduplicated": _canonical_sha256(dedup),
    }


def _verify_persisted(
    connection: sqlite3.Connection,
    *,
    prepared: _PreparedRelease,
    row: sqlite3.Row,
    output_root: str | Path,
) -> AnalysisReleaseVerificationResult:
    projection = prepared.projection
    expected = {
        "request_manifest_sha256": prepared.request_manifest_sha256,
        "release_manifest_sha256": projection.manifest_sha256,
        "posts_eligible_count": len(projection.analysis_posts_eligible),
        "posts_deduplicated_count": len(projection.analysis_posts_deduplicated),
        "schema_version": ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise AnalysisReleaseRepositoryError("analysis_release_identity_mismatch")
    if _stored_member_hashes(connection, str(row["release_id"])) != prepared.member_manifest_hashes:
        raise AnalysisReleaseRepositoryError("analysis_release_member_mismatch")
    stored_manifests = {
        str(item["manifest_kind"]): str(item["manifest_sha256"])
        for item in connection.execute(
            "SELECT manifest_kind, manifest_sha256 FROM analysis_release_manifests "
            "WHERE release_id = ? AND manifest_kind != 'artifact'", (row["release_id"],)
        )
    }
    if stored_manifests != {"release": projection.manifest_sha256, **prepared.member_manifest_hashes}:
        raise AnalysisReleaseRepositoryError("analysis_release_manifest_mismatch")
    expected_reports = {
        "quality_summary": prepared.quality_report,
        "lineage": prepared.lineage_report,
    }
    for report in connection.execute(
        "SELECT * FROM analysis_release_reports WHERE release_id = ?", (row["release_id"],)
    ):
        expected_report = expected_reports.pop(str(report["report_kind"]), None)
        if (
            expected_report is None
            or report["report_json"] != _canonical_json(expected_report)
            or report["report_sha256"] != _canonical_sha256(expected_report)
        ):
            raise AnalysisReleaseRepositoryError("analysis_release_report_mismatch")
    if expected_reports:
        raise AnalysisReleaseRepositoryError("analysis_release_report_mismatch")
    artifact_dir = Path(output_root) / str(row["run_id"]) / str(row["release_id"])
    try:
        verify_release_artifact(artifact_dir)
        artifact_hash = _file_sha256(artifact_dir / "artifact-manifest.json")
    except (OSError, ReleaseArtifactError) as exc:
        raise AnalysisReleaseRepositoryError("release_artifact_invalid") from exc
    artifact_row = connection.execute(
        "SELECT * FROM analysis_release_manifests WHERE release_id = ? "
        "AND manifest_kind = 'artifact'", (row["release_id"],)
    ).fetchone()
    if (
        artifact_row is None
        or artifact_row["manifest_sha256"] != artifact_hash
        or artifact_row["artifact_relative_path"]
        != f"{row['run_id']}/{row['release_id']}"
    ):
        raise AnalysisReleaseRepositoryError("release_artifact_manifest_mismatch")
    return AnalysisReleaseVerificationResult(
        release_id=str(row["release_id"]),
        run_id=str(row["run_id"]),
        seal_status=str(row["seal_status"]),
        release_manifest_sha256=str(row["release_manifest_sha256"]),
        artifact_manifest_sha256=artifact_hash,
    )


def _load_prepared_from_ids(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    release_id: str,
    release_mode: str,
    post_decision_build_id: str,
    text_dedup_build_id: str,
    text_keep_audit_evaluation_id: str,
) -> _PreparedRelease:
    _, snapshot = _load_run_snapshot(connection, run_id)
    inputs = _load_post_inputs(
        connection,
        run_id=run_id,
        snapshot_id=str(snapshot["snapshot_id"]),
        release_mode=release_mode,
        post_decision_build_id=post_decision_build_id,
        text_dedup_build_id=text_dedup_build_id,
        text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
    )
    return _prepare(
        inputs=inputs,
        run_id=run_id,
        release_id=release_id,
        release_mode=release_mode,
        post_decision_build_id=post_decision_build_id,
        text_dedup_build_id=text_dedup_build_id,
        text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
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
    output_root: str | Path,
) -> AnalysisReleaseBuildResult:
    """从三个显式文本证据 ID 构建并封存发布与本地包。"""

    upstream = (
        ("post_decision_build_id", post_decision_build_id),
        ("text_dedup_build_id", text_dedup_build_id),
        ("text_keep_audit_evaluation_id", text_keep_audit_evaluation_id),
    )
    _validate_request(
        run_id=run_id, release_id=release_id, release_mode=release_mode,
        upstream_ids=upstream,
    )
    artifact_created: Path | None = None
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            prepared = _load_prepared_from_ids(
                connection,
                run_id=run_id,
                release_id=release_id,
                release_mode=release_mode,
                post_decision_build_id=post_decision_build_id,
                text_dedup_build_id=text_dedup_build_id,
                text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
            )
            existing = connection.execute(
                "SELECT * FROM analysis_release_builds WHERE release_id = ?", (release_id,)
            ).fetchone()
            if existing is not None:
                if existing["seal_status"] == "building":
                    raise AnalysisReleaseRepositoryError("analysis_release_partial_build")
                _verify_persisted(connection, prepared=prepared, row=existing, output_root=output_root)
                return _result(existing, True)
            if connection.execute(
                "SELECT 1 FROM analysis_release_builds WHERE run_id = ? "
                "AND request_manifest_sha256 = ?",
                (run_id, prepared.request_manifest_sha256),
            ).fetchone() is not None:
                raise AnalysisReleaseRepositoryError("analysis_release_request_conflict")
            now = _utcnow()
            with connection:
                _insert_rows(
                    connection,
                    prepared=prepared,
                    run_id=run_id,
                    release_id=release_id,
                    release_mode=release_mode,
                    post_decision_build_id=post_decision_build_id,
                    text_dedup_build_id=text_dedup_build_id,
                    text_keep_audit_evaluation_id=text_keep_audit_evaluation_id,
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
                verify_release_artifact(artifact.artifact_dir)
                artifact_hash = _file_sha256(artifact.artifact_dir / "artifact-manifest.json")
                connection.execute(
                    "INSERT INTO analysis_release_manifests VALUES (?, ?, 'artifact', ?, ?, ?)",
                    (release_id, run_id, artifact_hash, f"{run_id}/{release_id}", now),
                )
                connection.execute(
                    "UPDATE analysis_release_builds SET seal_status = 'finalized', "
                    "finalized_at_utc = ? WHERE release_id = ? AND seal_status = 'building'",
                    (now, release_id),
                )
            artifact_created = None
            stored = _require(
                connection.execute(
                    "SELECT * FROM analysis_release_builds WHERE release_id = ?", (release_id,)
                ).fetchone(),
                "analysis_release_finalize_failed",
            )
            _verify_persisted(connection, prepared=prepared, row=stored, output_root=output_root)
            return _result(stored, False)
    except AnalysisReleaseRepositoryError:
        if artifact_created is not None:
            shutil.rmtree(artifact_created, ignore_errors=True)
        raise
    except (OSError, sqlite3.DatabaseError, ReleaseArtifactError) as exc:
        if artifact_created is not None:
            shutil.rmtree(artifact_created, ignore_errors=True)
        raise AnalysisReleaseRepositoryError("release_database_contract_rejected") from exc


def _prepare_from_stored(
    connection: sqlite3.Connection, *, run_id: str, release_id: str
) -> tuple[sqlite3.Row, _PreparedRelease]:
    row = _require(
        connection.execute(
            "SELECT * FROM analysis_release_builds WHERE release_id = ? AND run_id = ?",
            (release_id, run_id),
        ).fetchone(),
        "analysis_release_not_found",
    )
    prepared = _load_prepared_from_ids(
        connection,
        run_id=run_id,
        release_id=release_id,
        release_mode=str(row["release_mode"]),
        post_decision_build_id=str(row["post_decision_build_id"]),
        text_dedup_build_id=str(row["text_dedup_build_id"]),
        text_keep_audit_evaluation_id=str(row["text_keep_audit_evaluation_id"]),
    )
    return row, prepared


def verify_release(
    derived_db: str | Path,
    *,
    run_id: str,
    release_id: str,
    output_root: str | Path,
) -> AnalysisReleaseVerificationResult:
    """按显式身份离线复验数据库投影与本地包。"""

    _validate_request(run_id=run_id, release_id=release_id)
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            row, prepared = _prepare_from_stored(connection, run_id=run_id, release_id=release_id)
            return _verify_persisted(connection, prepared=prepared, row=row, output_root=output_root)
    except AnalysisReleaseRepositoryError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise AnalysisReleaseRepositoryError("release_verification_failed") from exc


def get_release_status(
    derived_db: str | Path, *, run_id: str, release_id: str
) -> AnalysisReleaseStatus:
    """查询显式发布及运行状态，不回退到其他记录。"""

    _validate_request(run_id=run_id, release_id=release_id)
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            row = _require(
                connection.execute(
                    """
                    SELECT release.*, run.status AS run_status, run.reason_code
                    FROM analysis_release_builds AS release
                    JOIN cleaning_runs AS run ON run.run_id = release.run_id
                    WHERE release.release_id = ? AND release.run_id = ?
                    """,
                    (release_id, run_id),
                ).fetchone(),
                "analysis_release_not_found",
            )
            return AnalysisReleaseStatus(
                release_id, run_id, str(row["release_mode"]), str(row["seal_status"]),
                str(row["run_status"]), row["reason_code"],
            )
    except AnalysisReleaseRepositoryError:
        raise
    except sqlite3.DatabaseError as exc:
        raise AnalysisReleaseRepositoryError("release_status_failed") from exc


def accept_release(
    derived_db: str | Path,
    *,
    run_id: str,
    release_id: str,
    output_root: str | Path,
) -> AnalysisReleaseAcceptanceResult:
    """复验并原子接受一个 formal finalized 文本发布。"""

    _validate_request(run_id=run_id, release_id=release_id)
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            row, prepared = _prepare_from_stored(connection, run_id=run_id, release_id=release_id)
            _verify_persisted(connection, prepared=prepared, row=row, output_root=output_root)
            outcome = accept_verified_release(
                connection,
                request=ReleaseAcceptanceRequest(
                    release_id=release_id,
                    run_id=run_id,
                    artifact_dir=Path(output_root) / run_id / release_id,
                ),
                accepted_at_utc=_utcnow(),
            )
            return AnalysisReleaseAcceptanceResult(
                outcome.release_id,
                outcome.run_id,
                outcome.seal_status,
                outcome.run_status,
                outcome.reused,
            )
    except AnalysisReleaseRepositoryError:
        raise
    except ReleaseAcceptanceServiceError as exc:
        raise AnalysisReleaseRepositoryError(exc.reason_code) from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        raise AnalysisReleaseRepositoryError("release_acceptance_failed") from exc
