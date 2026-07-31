"""图片决定快照与 SHA 精确簇标签传播的派生 SQLite 仓储。

决定只消费图片复核的追加式证据；pHash pair/group 在本模块的表结构和 API 中
均不能作为传播来源。传播只遍历 Issue #9 的 ``image_exact_cluster_members``，
不修改原图、正式采集库或原始标注。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CleaningConfig
from .image_review_decision import DecisionEvidence, resolve_image_decision
from .image_review_gate import ImageReviewGateError, validate_formal_image_review_gate
from .schema import connect_derived, migrate_derived


class ImageDecisionRepositoryError(RuntimeError):
    """决定或传播谱系不完整时抛出的去敏领域异常。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("image decision repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class DecisionBuildResult:
    """已封存代表决定快照的身份与动作计数。"""

    decision_build_id: str
    candidate_build_id: str
    decision_count: int
    keep_count: int
    review_count: int
    exclude_count: int
    decision_manifest_sha256: str


@dataclass(frozen=True)
class PropagationResult:
    """SHA 精确簇传播批次的无敏感统计与总 manifest。"""

    decision_build_id: str
    propagation_run_count: int
    propagated_member_count: int
    output_manifest_sha256: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_flags(
    connection: sqlite3.Connection,
    candidate_build_id: str,
) -> dict[str, bool]:
    """返回每个 SHA 代表是否命中信号、精确重复或 pHash 候选。"""

    rows = connection.execute(
        """
        SELECT c.representative_fingerprint_id AS fingerprint_id,
               (c.member_count > 1
                OR EXISTS(SELECT 1 FROM image_candidate_signals s
                  WHERE s.build_id = c.build_id
                    AND s.fingerprint_id = c.representative_fingerprint_id)
                OR EXISTS(SELECT 1 FROM image_near_candidate_pairs n
                  WHERE n.build_id = c.build_id AND
                   (n.left_fingerprint_id = c.representative_fingerprint_id
                    OR n.right_fingerprint_id = c.representative_fingerprint_id))) AS is_candidate
        FROM image_exact_clusters c WHERE c.build_id = ?
        ORDER BY c.representative_fingerprint_id
        """,
        (candidate_build_id,),
    ).fetchall()
    return {str(row["fingerprint_id"]): bool(row["is_candidate"]) for row in rows}


def _decision_evidence(
    connection: sqlite3.Connection,
    candidate_build_id: str,
    guide_version: str,
) -> tuple[dict[str, list[DecisionEvidence]], str]:
    """选择同手册版本的正式证据链并计算证据 manifest。

    pilot 用于冻结手册，boundary 用于验证手册可重复性和候选边界；两者都不
    直接产生代表决定。这里只消费正式 ``candidate_review`` 的运行内证据链，
    防止把不同目的或不同运行的槽位拼成伪双标。manifest 绑定当前手册版本的
    所有候选复核证据，使追加第二槽或仲裁必然产生新决定构建身份。
    """

    annotations = connection.execute(
        """
        SELECT rr.review_run_id, rr.review_kind, r.fingerprint_id,
               a.annotation_id, a.assignment_slot,
               a.technical_noise_label
        FROM image_review_runs rr
        JOIN image_review_members r ON r.review_run_id = rr.review_run_id
        JOIN image_review_annotations a ON a.review_run_id = r.review_run_id
          AND a.fingerprint_id = r.fingerprint_id
        WHERE rr.candidate_build_id = ? AND rr.guide_version = ?
          AND rr.review_kind = 'candidate_review'
          AND rr.seal_status = 'finalized'
        ORDER BY r.fingerprint_id, rr.review_kind, rr.review_run_id, a.annotation_id
        """,
        (candidate_build_id, guide_version),
    ).fetchall()
    adjudications = connection.execute(
        """
        SELECT rr.review_run_id, rr.review_kind, r.fingerprint_id,
               a.adjudication_id, a.technical_noise_label
        FROM image_review_runs rr
        JOIN image_review_members r ON r.review_run_id = rr.review_run_id
        JOIN image_review_adjudications a ON a.review_run_id = r.review_run_id
          AND a.fingerprint_id = r.fingerprint_id
        WHERE rr.candidate_build_id = ? AND rr.guide_version = ?
          AND rr.review_kind = 'candidate_review'
          AND rr.seal_status = 'finalized'
        ORDER BY r.fingerprint_id, rr.review_kind, rr.review_run_id, a.adjudication_id
        """,
        (candidate_build_id, guide_version),
    ).fetchall()
    by_run: dict[tuple[str, str], list[DecisionEvidence]] = {}
    run_kinds: dict[tuple[str, str], str] = {}
    run_keys_by_fingerprint: dict[str, set[tuple[str, str]]] = {}
    manifest_rows: list[list[object]] = []
    for row in annotations:
        evidence = DecisionEvidence(
            str(row["annotation_id"]),
            "annotation",
            str(row["technical_noise_label"]),
            int(row["assignment_slot"]),
        )
        key = (str(row["fingerprint_id"]), str(row["review_run_id"]))
        by_run.setdefault(key, []).append(evidence)
        run_kinds[key] = str(row["review_kind"])
        run_keys_by_fingerprint.setdefault(key[0], set()).add(key)
        manifest_rows.append(
            [
                row["review_run_id"],
                row["fingerprint_id"],
                evidence.evidence_id,
                "annotation",
                evidence.assignment_slot,
            ]
        )
    for row in adjudications:
        evidence = DecisionEvidence(
            str(row["adjudication_id"]),
            "adjudication",
            str(row["technical_noise_label"]),
            None,
        )
        key = (str(row["fingerprint_id"]), str(row["review_run_id"]))
        by_run.setdefault(key, []).append(evidence)
        run_kinds[key] = str(row["review_kind"])
        run_keys_by_fingerprint.setdefault(key[0], set()).add(key)
        manifest_rows.append(
            [
                row["review_run_id"],
                row["fingerprint_id"],
                evidence.evidence_id,
                "adjudication",
                None,
            ]
        )

    # 候选复核可因修正规则而追加新运行；每个决定只能选择一条运行内证据链，
    # 不能把两个运行的 slot 1/2 混成伪造的“双人一致”。
    by_id: dict[str, list[DecisionEvidence]] = {}
    priorities = {"candidate_review": 0, "boundary": 1}
    for fingerprint_id, run_keys in sorted(run_keys_by_fingerprint.items()):
        selected = min(
            run_keys,
            key=lambda key: (priorities[run_kinds[key]], key[1]),
        )
        by_id[fingerprint_id] = by_run[selected]
    return by_id, _canonical_sha256(manifest_rows)


def build_image_decisions(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    config: CleaningConfig,
) -> DecisionBuildResult:
    """解析全部 SHA 代表证据并创建不可变决定快照。

    构建前先执行共同试标、边界双标和原始一致率硬门。候选代表随后完成必要
    人工链：slot 1 valid 可单人保留；拟排除需双标同标签或合法仲裁。非候选
    无证据时以无标签默认保留。任一门禁、代表证据或谱系不完整时整次失败。
    """

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        build = connection.execute(
            "SELECT seal_status FROM image_candidate_builds WHERE build_id = ?",
            (candidate_build_id,),
        ).fetchone()
        if build is None or build["seal_status"] != "finalized":
            raise ImageDecisionRepositoryError("finalized_image_candidate_build_required")
        try:
            review_gate = validate_formal_image_review_gate(
                connection,
                candidate_build_id=candidate_build_id,
                config=config,
            )
        except ImageReviewGateError as exc:
            raise ImageDecisionRepositoryError(exc.reason_code) from exc
        flags = _candidate_flags(connection, candidate_build_id)
        evidence_by_id, raw_evidence_manifest = _decision_evidence(
            connection,
            candidate_build_id,
            config.image_label_guide_version,
        )
        evidence_manifest = _canonical_sha256(
            [
                "image-formal-decision-evidence-v2",
                review_gate.evidence_manifest_sha256,
                raw_evidence_manifest,
            ]
        )
        resolved: list[tuple[str, object]] = []
        try:
            for fingerprint_id, is_candidate in sorted(flags.items()):
                decision = resolve_image_decision(
                    evidence_by_id.get(fingerprint_id, ()),
                    is_candidate=is_candidate,
                )
                resolved.append((fingerprint_id, decision))
        except ValueError as exc:
            raise ImageDecisionRepositoryError("image_decision_evidence_incomplete") from exc
        decision_rows = [
            {
                "fingerprint_id": fingerprint_id,
                "technical_noise_label": decision.technical_noise_label,
                "decision_action": decision.decision_action,
                "provenance": decision.provenance,
                "evidence_id": decision.evidence_id,
                "evidence_ids": decision.evidence_ids,
            }
            for fingerprint_id, decision in resolved
        ]
        decision_manifest = _canonical_sha256(decision_rows)
        decision_build_id = _canonical_sha256(
            [
                "image-decision-build-v1",
                candidate_build_id,
                config.image_label_guide_version,
                evidence_manifest,
                decision_manifest,
            ]
        )[:32]
        counts = {
            action: sum(row["decision_action"] == action for row in decision_rows)
            for action in ("keep", "review", "exclude")
        }
        result = DecisionBuildResult(
            decision_build_id,
            candidate_build_id,
            len(decision_rows),
            counts["keep"],
            counts["review"],
            counts["exclude"],
            decision_manifest,
        )
        existing = connection.execute(
            "SELECT seal_status, decision_manifest_sha256 FROM image_decision_builds WHERE decision_build_id = ?",
            (decision_build_id,),
        ).fetchone()
        if existing is not None:
            if existing["seal_status"] != "finalized" or existing["decision_manifest_sha256"] != decision_manifest:
                raise ImageDecisionRepositoryError("image_decision_build_conflict")
            return result
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_decision_builds(
                      decision_build_id, candidate_build_id, guide_version,
                      evidence_manifest_sha256, expected_decision_count,
                      decision_manifest_sha256, seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        decision_build_id,
                        candidate_build_id,
                        config.image_label_guide_version,
                        evidence_manifest,
                        len(decision_rows),
                        decision_manifest,
                        _utcnow(),
                    ),
                )
                for row in decision_rows:
                    decision_sha = _canonical_sha256(row)
                    decision_id = _canonical_sha256(
                        ["image-decision-v1", decision_build_id, row["fingerprint_id"], decision_sha]
                    )[:32]
                    connection.execute(
                        """
                        INSERT INTO image_decisions(
                          decision_id, decision_build_id, fingerprint_id,
                          technical_noise_label, decision_action, provenance,
                          evidence_id, decision_sha256, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            decision_build_id,
                            row["fingerprint_id"],
                            row["technical_noise_label"],
                            row["decision_action"],
                            row["provenance"],
                            row["evidence_id"],
                            decision_sha,
                            _utcnow(),
                        ),
                    )
                connection.execute(
                    "UPDATE image_decision_builds SET seal_status = 'finalized' WHERE decision_build_id = ?",
                    (decision_build_id,),
                )
        except sqlite3.IntegrityError as exc:
            raise ImageDecisionRepositoryError("image_decision_build_integrity_error") from exc
    return result


def propagate_exact_sha_labels(
    derived_db: str | Path,
    *,
    decision_build_id: str,
) -> PropagationResult:
    """把已确认代表标签只传播到同一 SHA-256 精确簇成员。

    仅处理 ``member_count>1`` 且代表决定具有人工标签的簇。成员列表直接来自
    ``image_exact_cluster_members``；API 不接受 pHash group/pair ID，因此近似
    相似在结构上不能触发传播。相同传播运行幂等复用，所有成员可追溯到代表
    ``decision_id`` 和精确 cluster。
    """

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        parent = connection.execute(
            """
            SELECT candidate_build_id, seal_status FROM image_decision_builds
            WHERE decision_build_id = ?
            """,
            (decision_build_id,),
        ).fetchone()
        if parent is None or parent["seal_status"] != "finalized":
            raise ImageDecisionRepositoryError("finalized_image_decision_build_required")
        candidate_build_id = str(parent["candidate_build_id"])
        clusters = connection.execute(
            """
            SELECT c.cluster_id, c.member_count, d.decision_id,
                   d.technical_noise_label
            FROM image_exact_clusters c
            JOIN image_decisions d ON d.decision_build_id = ?
              AND d.fingerprint_id = c.representative_fingerprint_id
            WHERE c.build_id = ? AND c.member_count > 1
              AND d.decision_action = 'exclude'
              AND d.provenance IN ('double_agreement', 'adjudication')
              AND d.technical_noise_label IN (
                'site_background', 'site_ui', 'placeholder_or_error',
                'tracking_or_qr_only'
              )
            ORDER BY c.cluster_id
            """,
            (decision_build_id, candidate_build_id),
        ).fetchall()
        output_rows: list[list[object]] = []
        run_count = 0
        member_count = 0
        for cluster in clusters:
            members = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT fingerprint_id FROM image_exact_cluster_members
                    WHERE build_id = ? AND cluster_id = ? ORDER BY fingerprint_id
                    """,
                    (candidate_build_id, cluster["cluster_id"]),
                )
            ]
            member_manifest = _canonical_sha256(members)
            propagation_run_id = _canonical_sha256(
                ["image-sha-propagation-v1", decision_build_id, cluster["cluster_id"],
                 cluster["decision_id"], member_manifest]
            )[:32]
            existing = connection.execute(
                "SELECT seal_status FROM image_sha_propagation_runs WHERE propagation_run_id = ?",
                (propagation_run_id,),
            ).fetchone()
            if existing is None:
                try:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO image_sha_propagation_runs(
                              propagation_run_id, decision_build_id, candidate_build_id,
                              exact_cluster_id, representative_decision_id,
                              technical_noise_label, expected_member_count,
                              member_manifest_sha256, seal_status, created_at_utc
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                            """,
                            (
                                propagation_run_id,
                                decision_build_id,
                                candidate_build_id,
                                cluster["cluster_id"],
                                cluster["decision_id"],
                                cluster["technical_noise_label"],
                                len(members),
                                member_manifest,
                                _utcnow(),
                            ),
                        )
                        for fingerprint_id in members:
                            connection.execute(
                                """
                                INSERT INTO image_sha_propagation_members(
                                  propagation_run_id, fingerprint_id,
                                  propagated_label, source_decision_id
                                ) VALUES (?, ?, ?, ?)
                                """,
                                (
                                    propagation_run_id,
                                    fingerprint_id,
                                    cluster["technical_noise_label"],
                                    cluster["decision_id"],
                                ),
                            )
                        connection.execute(
                            "UPDATE image_sha_propagation_runs SET seal_status = 'finalized' WHERE propagation_run_id = ?",
                            (propagation_run_id,),
                        )
                except sqlite3.IntegrityError as exc:
                    raise ImageDecisionRepositoryError("image_sha_propagation_integrity_error") from exc
            elif existing["seal_status"] != "finalized":
                raise ImageDecisionRepositoryError("image_sha_propagation_conflict")
            run_count += 1
            member_count += len(members)
            output_rows.append([propagation_run_id, cluster["cluster_id"], member_manifest])
        output_manifest = _canonical_sha256(output_rows)
    return PropagationResult(decision_build_id, run_count, member_count, output_manifest)
