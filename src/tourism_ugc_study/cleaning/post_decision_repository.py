"""帖子清洗候选决定与最终决定的 SQLite 仓储。

本模块只读取调用方显式给出的运行、快照和证据身份，不选择“最新”记录。
候选构建先冻结文本保留集审计人口；最终构建必须引用该候选构建及其已通过
审计，再调用纯领域规则重新计算每条决定。所有写入均落在派生 SQLite 的单个
事务中，不接触正式采集库，也不把正文、路径或作者身份写入异常。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from .post_decision import (
    HumanTextEvidence,
    ModelDecisionEvidence,
    PostDecision,
    PostDecisionRequest,
    build_post_decisions,
)
from .schema import connect_derived, migrate_derived


AuditMode = Literal["formal", "smoke"]
BuildKind = Literal["candidate", "final"]


class PostDecisionRepositoryError(RuntimeError):
    """决定谱系、证据或封存校验失败时抛出的去敏异常。

    ``reason_code`` 是稳定机器码；异常正文不含 SQLite 原文、文件路径、帖子
    文本或证据内容。调用方应据此暂停运行并修正显式请求，而不是绕过校验。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("post decision repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class PostDecisionBuildRequest:
    """一次候选或最终决定构建的完整显式请求。

    候选请求必须给出覆盖快照的 ``deterministic_result_ids``，可再选择人工
    仲裁和一个文本模型运行。最终请求不重新选择证据，而是给出来源候选构建
    与其保留集审计评估；仓储从候选证据链重建同一输入并重新运行领域规则。
    ``audit_mode`` 用于阻止 formal/smoke 证据混用，不会被隐式推断成 latest。
    """

    run_id: str
    source_snapshot_id: str
    build_kind: BuildKind
    audit_mode: AuditMode
    decision_version: str
    guide_version: str
    rules_sha256: str
    deterministic_result_ids: tuple[str, ...] = ()
    human_adjudication_ids: tuple[str, ...] = ()
    model_run_id: str | None = None
    model_candidate_build_id: str | None = None
    model_algorithm_version: str | None = None
    source_candidate_decision_build_id: str | None = None
    text_keep_audit_evaluation_id: str | None = None


@dataclass(frozen=True)
class PostDecisionBuildResult:
    """一个已封存决定构建的身份、计数和稳定 manifest。"""

    decision_build_id: str
    build_kind: BuildKind
    expected_post_count: int
    keep_count: int
    review_count: int
    exclude_count: int
    input_manifest_sha256: str
    decision_manifest_sha256: str


@dataclass(frozen=True)
class _EvidenceLink:
    """待持久化的真实证据链接，不携带原文或路径。"""

    evidence_kind: str
    evidence_id: str


@dataclass(frozen=True)
class _PreparedDecision:
    """领域决定及其 SQLite 投影字段。"""

    decision: PostDecision
    structure_label: str
    tourism_label: str
    provenance: str
    model_run_id: str | None
    links: tuple[_EvidenceLink, ...]


def _utcnow() -> str:
    """返回秒级 UTC 时间，仅用于追加式对象的创建时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(value: object) -> str:
    """以排序稳定的规范 JSON 计算 SHA-256。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: str) -> bool:
    """判断值是否为小写十六进制 SHA-256。"""

    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _raise(reason_code: str) -> None:
    """集中抛出去敏异常，避免分支意外拼入数据库值。"""

    raise PostDecisionRepositoryError(reason_code)


def _validate_request(request: PostDecisionBuildRequest) -> None:
    """验证请求的结构身份和候选/最终互斥字段。"""

    if not all(
        (
            request.run_id,
            request.source_snapshot_id,
            request.decision_version,
            request.guide_version,
        )
    ) or not _is_sha256(request.rules_sha256):
        _raise("post_decision_request_identity_invalid")
    if request.build_kind not in {"candidate", "final"} or request.audit_mode not in {
        "formal",
        "smoke",
    }:
        _raise("post_decision_request_mode_invalid")
    evidence_ids = (*request.deterministic_result_ids, *request.human_adjudication_ids)
    if any(not value for value in evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
        _raise("post_decision_evidence_identity_invalid")
    if request.build_kind == "candidate":
        if request.source_candidate_decision_build_id is not None or request.text_keep_audit_evaluation_id is not None:
            _raise("candidate_decision_lineage_must_be_empty")
        model_fields = (
            request.model_run_id,
            request.model_candidate_build_id,
            request.model_algorithm_version,
        )
        if any(value is None for value in model_fields) and any(
            value is not None for value in model_fields
        ):
            _raise("candidate_model_identity_incomplete")
    else:
        if not request.source_candidate_decision_build_id or not request.text_keep_audit_evaluation_id:
            _raise("final_decision_lineage_incomplete")
        if (
            request.deterministic_result_ids
            or request.human_adjudication_ids
            or request.model_run_id is not None
            or request.model_candidate_build_id is not None
            or request.model_algorithm_version is not None
        ):
            _raise("final_decision_must_reuse_candidate_evidence")


def _snapshot_population(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    snapshot_id: str,
) -> tuple[sqlite3.Row, ...]:
    """读取指定快照中全部非 missing 帖子版本并核对冻结计数。"""

    snapshot = connection.execute(
        """
        SELECT s.post_count
        FROM source_snapshots AS s
        JOIN cleaning_runs AS r ON r.run_id = s.run_id
        WHERE s.snapshot_id = ? AND s.run_id = ?
          AND r.source_snapshot_id = s.snapshot_id AND r.status != 'accepted'
        """,
        (snapshot_id, run_id),
    ).fetchone()
    if snapshot is None:
        _raise("post_decision_snapshot_lineage_invalid")
    rows = tuple(
        connection.execute(
            """
            SELECT o.source_post_id, o.source_version, p.platform_key
            FROM source_post_observations AS o
            JOIN source_post_inventory AS p ON p.source_post_id = o.source_post_id
            WHERE o.snapshot_id = ? AND o.change_kind != 'missing'
            ORDER BY o.source_post_id, o.source_version
            """,
            (snapshot_id,),
        )
    )
    if len(rows) != int(snapshot["post_count"]):
        _raise("post_decision_snapshot_count_mismatch")
    return rows


def _rows_for_ids(
    connection: sqlite3.Connection,
    *,
    table: str,
    identity_column: str,
    identities: Sequence[str],
    columns: str,
) -> tuple[sqlite3.Row, ...]:
    """按显式 ID 集合查询允许表；表名和列名只由本模块常量传入。"""

    if not identities:
        return ()
    placeholders = ",".join("?" for _ in identities)
    return tuple(
        connection.execute(
            f"SELECT {columns} FROM {table} WHERE {identity_column} IN ({placeholders}) "
            f"ORDER BY {identity_column}",
            tuple(identities),
        )
    )


def _selected_candidate_evidence(
    connection: sqlite3.Connection,
    request: PostDecisionBuildRequest,
    population: Sequence[sqlite3.Row],
) -> tuple[
    dict[tuple[int, int], sqlite3.Row],
    dict[tuple[int, int], tuple[sqlite3.Row, ...]],
    sqlite3.Row | None,
    dict[tuple[int, int], sqlite3.Row],
]:
    """加载并验证候选请求中全部确定性、人工和模型证据。

    确定性结果必须一帖一条完整覆盖快照。人工仲裁必须属于同一帖子版本、
    同一手册，并由请求逐 ID 选择。模型必须显式绑定同运行候选构建、配置、
    手册、算法和 formal/smoke 模式；低风险平台抽审项还必须全部出现在所选
    人工仲裁中，防止模型保留候选绕过已抽中的人工检查。
    """

    identities = {(int(row["source_post_id"]), int(row["source_version"])) for row in population}
    deterministic_rows = _rows_for_ids(
        connection,
        table="text_deterministic_results",
        identity_column="task_id",
        identities=request.deterministic_result_ids,
        columns=(
            "task_id, run_id, source_snapshot_id, source_post_id, source_version, "
            "structure_status, exact_canonical_sha256, rules_version, rules_sha256"
        ),
    )
    if len(deterministic_rows) != len(request.deterministic_result_ids):
        _raise("deterministic_evidence_not_found")
    deterministic_by_post: dict[tuple[int, int], sqlite3.Row] = {}
    for row in deterministic_rows:
        identity = int(row["source_post_id"]), int(row["source_version"])
        if (
            identity not in identities
            or row["run_id"] != request.run_id
            or row["source_snapshot_id"] != request.source_snapshot_id
            or identity in deterministic_by_post
        ):
            _raise("deterministic_evidence_lineage_mismatch")
        deterministic_by_post[identity] = row
    if set(deterministic_by_post) != identities:
        _raise("deterministic_evidence_does_not_cover_snapshot")

    adjudication_rows = _rows_for_ids(
        connection,
        table="text_post_adjudications",
        identity_column="adjudication_id",
        identities=request.human_adjudication_ids,
        columns=(
            "adjudication_id, source_post_id, source_version, structure_label, "
            "tourism_label, guide_version, model_run_id"
        ),
    )
    if len(adjudication_rows) != len(request.human_adjudication_ids):
        _raise("human_adjudication_not_found")
    human_lists: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in adjudication_rows:
        identity = int(row["source_post_id"]), int(row["source_version"])
        if identity not in identities or row["guide_version"] != request.guide_version:
            _raise("human_adjudication_lineage_mismatch")
        human_lists.setdefault(identity, []).append(row)
    human_by_post = {
        identity: tuple(sorted(rows, key=lambda row: str(row["adjudication_id"])))
        for identity, rows in human_lists.items()
    }

    if request.model_run_id is None:
        return deterministic_by_post, human_by_post, None, {}
    model = connection.execute(
        """
        SELECT m.*, c.source_snapshot_id, c.status AS candidate_status,
               r.config_sha256 AS run_config_sha256
        FROM text_model_runs AS m
        JOIN text_candidate_builds AS c ON c.build_id = m.candidate_build_id
        JOIN cleaning_runs AS r ON r.run_id = m.run_id
        WHERE m.model_run_id = ?
        """,
        (request.model_run_id,),
    ).fetchone()
    expected_status = "completed" if request.audit_mode == "formal" else "smoke"
    if model is None:
        _raise("model_run_not_found")
    if (
        model["run_id"] != request.run_id
        or model["source_snapshot_id"] != request.source_snapshot_id
        or model["candidate_build_id"] != request.model_candidate_build_id
        or model["algorithm_version"] != request.model_algorithm_version
        or model["guide_version"] != request.guide_version
        or model["config_sha256"] != model["run_config_sha256"]
        or model["status"] != expected_status
        or model["seal_status"] != "finalized"
        or model["candidate_status"] != "finalized"
    ):
        _raise("model_run_lineage_mismatch")
    required_manifests = (
        model["request_manifest_sha256"],
        model["candidate_prediction_manifest_sha256"],
        model["train_manifest_sha256"],
        model["validation_manifest_sha256"],
        model["test_manifest_sha256"],
        model["prediction_manifest_sha256"],
    )
    if any(value is None or not _is_sha256(str(value)) for value in required_manifests):
        _raise("model_run_seal_manifest_incomplete")
    split_leakage = connection.execute(
        """
        SELECT 1 FROM text_dataset_splits
        WHERE model_run_id = ?
        GROUP BY component_id HAVING COUNT(DISTINCT split_name) > 1 LIMIT 1
        """,
        (request.model_run_id,),
    ).fetchone()
    if split_leakage is not None:
        _raise("model_test_isolation_failed")
    prediction_rows = tuple(
        connection.execute(
            """
            SELECT source_post_id, source_version, suggested_action,
                   low_risk_audit_selected
            FROM text_model_predictions WHERE model_run_id = ?
            ORDER BY source_post_id, source_version
            """,
            (request.model_run_id,),
        )
    )
    prediction_by_post = {
        (int(row["source_post_id"]), int(row["source_version"])): row
        for row in prediction_rows
    }
    if len(prediction_by_post) != len(prediction_rows) or not set(prediction_by_post) <= identities:
        _raise("model_prediction_population_mismatch")

    selected_human_ids = {
        str(row["adjudication_id"])
        for rows in human_by_post.values()
        for row in rows
    }
    for identity, prediction in prediction_by_post.items():
        if not bool(prediction["low_risk_audit_selected"]):
            continue
        matching = [
            row
            for row in human_by_post.get(identity, ())
            if row["model_run_id"] == request.model_run_id
            and str(row["adjudication_id"]) in selected_human_ids
        ]
        if len(matching) != 1:
            _raise("model_platform_audit_incomplete")
    return deterministic_by_post, human_by_post, model, prediction_by_post


def _model_version_manifest(model: sqlite3.Row) -> str:
    """绑定正式模型配置、候选构建、切分和预测封存身份。"""

    return _sha256(
        {
            "algorithm_version": model["algorithm_version"],
            "candidate_build_id": model["candidate_build_id"],
            "candidate_prediction_manifest_sha256": model[
                "candidate_prediction_manifest_sha256"
            ],
            "config_sha256": model["config_sha256"],
            "guide_version": model["guide_version"],
            "prediction_manifest_sha256": model["prediction_manifest_sha256"],
            "request_manifest_sha256": model["request_manifest_sha256"],
            "split_manifest_sha256": model["split_manifest_sha256"],
        }
    )


def _prepare_candidate_decisions(
    connection: sqlite3.Connection,
    request: PostDecisionBuildRequest,
    population: Sequence[sqlite3.Row],
) -> tuple[_PreparedDecision, ...]:
    """把候选证据投影为纯领域请求，并补齐持久化 provenance。"""

    deterministic, human, model, predictions = _selected_candidate_evidence(
        connection, request, population
    )
    domain_requests: list[PostDecisionRequest] = []
    contexts: dict[tuple[int, int], dict[str, object]] = {}
    version_manifest = _model_version_manifest(model) if model is not None else None
    for population_row in population:
        identity = int(population_row["source_post_id"]), int(population_row["source_version"])
        deterministic_row = deterministic[identity]
        human_rows = human.get(identity, ())
        human_evidence = tuple(
            HumanTextEvidence(
                evidence_id=str(row["adjudication_id"]),
                structure_label=str(row["structure_label"]),  # type: ignore[arg-type]
                tourism_label=str(row["tourism_label"]),  # type: ignore[arg-type]
            )
            for row in human_rows
        )
        model_evidence: ModelDecisionEvidence | None = None
        prediction = predictions.get(identity)
        # 未被人工证据覆盖的三类模型候选都显式进入领域层：高风险/边界只会
        # 产生 review，低风险还必须通过模型与平台抽审门。schema v24 为前两
        # 类提供独立 provenance，因而三类均能保存真实 prediction 链接。
        if not human_evidence and prediction is not None:
            assert model is not None and version_manifest is not None
            model_evidence = ModelDecisionEvidence(
                evidence_id=str(model["model_run_id"]),
                model_run_id=str(model["model_run_id"]),
                suggested_action=str(prediction["suggested_action"]),  # type: ignore[arg-type]
                run_mode=request.audit_mode,
                seal_status="finalized",
                required_version_manifest_sha256=version_manifest,
                model_version_manifest_sha256=version_manifest,
                test_set_isolation_passed=True,
                low_risk_threshold_enabled=bool(model["low_risk_enabled"]),
                platform_audit_complete=True,
                text_keep_audit_passed=False,
            )
        elif not human_evidence and deterministic_row["structure_status"] == "invalid":
            # 硬规则结果通过同一个双轴领域接口进入排除，但持久化时保留独立
            # deterministic provenance，不把它伪装成人工标签。
            human_evidence = (
                HumanTextEvidence(
                    evidence_id=str(deterministic_row["task_id"]),
                    structure_label="invalid",
                    tourism_label="not_applicable",
                ),
            )
        domain_requests.append(
            PostDecisionRequest(
                source_post_id=identity[0],
                source_version=identity[1],
                rule_version=request.decision_version,
                build_kind="candidate",
                human_evidence=human_evidence,
                model_evidence=model_evidence,
            )
        )
        contexts[identity] = {
            "deterministic": deterministic_row,
            "human": human_rows,
            "model": model,
            "model_evidence": model_evidence,
        }
    decisions = build_post_decisions(domain_requests)
    return tuple(
        _project_decision(decision, contexts[(decision.source_post_id, decision.source_version)])
        for decision in decisions
    )


def _project_decision(
    decision: PostDecision,
    context: dict[str, object],
) -> _PreparedDecision:
    """将领域输出投影为 schema v24 的双轴、provenance 与真实链接。"""

    deterministic = context["deterministic"]
    assert isinstance(deterministic, sqlite3.Row)
    human_rows = context["human"]
    assert isinstance(human_rows, tuple)
    model_evidence = context["model_evidence"]
    links = [_EvidenceLink("deterministic_result", str(deterministic["task_id"]))]
    links.extend(
        _EvidenceLink("human_adjudication", str(row["adjudication_id"]))
        for row in human_rows
    )
    if isinstance(model_evidence, ModelDecisionEvidence):
        links.append(_EvidenceLink("model_prediction", model_evidence.model_run_id))

    if human_rows:
        label_pairs = {
            (str(row["structure_label"]), str(row["tourism_label"])) for row in human_rows
        }
        if len(label_pairs) == 1 and len(human_rows) == 1:
            structure_label, tourism_label = next(iter(label_pairs))
            if structure_label == "usable" and tourism_label in {"related", "unrelated"}:
                provenance = "human_adjudication"
            elif structure_label == "invalid":
                # schema v24 只允许由硬规则证据形成 deterministic invalid；人工
                # invalid 仍被保守记录为 review，等待规则或决定版本修订。
                structure_label, tourism_label = "uncertain", "uncertain"
                provenance = "insufficient_evidence"
                decision = PostDecision(
                    decision.source_post_id,
                    decision.source_version,
                    "review",
                    ("human_invalid_requires_deterministic_rule",),
                    decision.evidence_ids,
                    decision.rule_version,
                    _sha256(
                        {
                            "source_post_id": decision.source_post_id,
                            "source_version": decision.source_version,
                            "reason": "human_invalid_requires_deterministic_rule",
                            "rule_version": decision.rule_version,
                        }
                    ),
                )
            else:
                provenance = "insufficient_evidence"
        else:
            structure_label, tourism_label = "uncertain", "uncertain"
            provenance = "evidence_conflict"
            # 即使多条人工证据碰巧同标，调用方也没有显式选择唯一仲裁；保守
            # 转为 review，防止 schema 的“一条最终人工证据”约束被输入顺序绕过。
            decision = PostDecision(
                decision.source_post_id,
                decision.source_version,
                "review",
                ("human_evidence_selection_conflict",),
                decision.evidence_ids,
                decision.rule_version,
                _sha256(
                    {
                        "source_post_id": decision.source_post_id,
                        "source_version": decision.source_version,
                        "reason": "human_evidence_selection_conflict",
                        "rule_version": decision.rule_version,
                        "evidence_ids": decision.evidence_ids,
                    }
                ),
            )
    elif deterministic["structure_status"] == "invalid":
        structure_label, tourism_label = "invalid", "not_applicable"
        provenance = "deterministic_invalid"
    elif isinstance(model_evidence, ModelDecisionEvidence):
        structure_label = "usable"
        if model_evidence.suggested_action == "low_risk_keep_candidate":
            tourism_label = "related"
            provenance = "model_low_risk"
        else:
            tourism_label = "uncertain"
            provenance = "model_review_candidate"
    else:
        structure_label = (
            "uncertain" if deterministic["structure_status"] == "uncertain" else "usable"
        )
        tourism_label = "uncertain"
        provenance = "insufficient_evidence"
    return _PreparedDecision(
        decision=decision,
        structure_label=structure_label,
        tourism_label=tourism_label,
        provenance=provenance,
        model_run_id=(
            model_evidence.model_run_id
            if isinstance(model_evidence, ModelDecisionEvidence)
            else None
        ),
        links=tuple(sorted(links, key=lambda item: (item.evidence_kind, item.evidence_id))),
    )


def _prepare_final_decisions(
    connection: sqlite3.Connection,
    request: PostDecisionBuildRequest,
    population: Sequence[sqlite3.Row],
) -> tuple[_PreparedDecision, ...]:
    """从来源候选的真实链接重建领域请求，并打开已通过的文本审计门。"""

    candidate = connection.execute(
        """
        SELECT * FROM post_decision_builds
        WHERE decision_build_id = ? AND run_id = ? AND source_snapshot_id = ?
          AND build_kind = 'candidate' AND seal_status = 'finalized'
        """,
        (
            request.source_candidate_decision_build_id,
            request.run_id,
            request.source_snapshot_id,
        ),
    ).fetchone()
    evaluation = connection.execute(
        """
        SELECT e.evaluation_status, e.seal_status, r.audit_mode,
               r.candidate_decision_build_id
        FROM text_keep_audit_evaluations AS e
        JOIN text_keep_audit_rounds AS r ON r.audit_round_id = e.audit_round_id
        WHERE e.audit_evaluation_id = ?
        """,
        (request.text_keep_audit_evaluation_id,),
    ).fetchone()
    if candidate is None or (
        candidate["decision_version"] != request.decision_version
        or candidate["guide_version"] != request.guide_version
        or candidate["rules_sha256"] != request.rules_sha256
    ):
        _raise("final_candidate_contract_mismatch")
    if evaluation is None or (
        evaluation["evaluation_status"] != "passed"
        or evaluation["seal_status"] != "finalized"
        or evaluation["audit_mode"] != request.audit_mode
        or evaluation["candidate_decision_build_id"]
        != request.source_candidate_decision_build_id
    ):
        _raise("final_text_keep_audit_mismatch")

    candidate_rows = tuple(
        connection.execute(
            """
            SELECT * FROM post_decisions WHERE decision_build_id = ?
            ORDER BY source_post_id, source_version
            """,
            (request.source_candidate_decision_build_id,),
        )
    )
    identities = [(int(row["source_post_id"]), int(row["source_version"])) for row in population]
    if [(int(row["source_post_id"]), int(row["source_version"])) for row in candidate_rows] != identities:
        _raise("final_candidate_population_mismatch")

    prepared: list[_PreparedDecision] = []
    for row in candidate_rows:
        identity = int(row["source_post_id"]), int(row["source_version"])
        links = tuple(
            _EvidenceLink(str(link["evidence_kind"]), str(link["evidence_id"]))
            for link in connection.execute(
                """
                SELECT evidence_kind, evidence_id
                FROM post_decision_evidence_links WHERE decision_id = ?
                ORDER BY evidence_kind, evidence_id
                """,
                (row["decision_id"],),
            )
        )
        human_rows = tuple(
            connection.execute(
                """
                SELECT a.adjudication_id, a.structure_label, a.tourism_label,
                       a.guide_version, a.model_run_id
                FROM post_decision_evidence_links AS l
                JOIN text_post_adjudications AS a ON a.adjudication_id = l.evidence_id
                WHERE l.decision_id = ? AND l.evidence_kind = 'human_adjudication'
                ORDER BY a.adjudication_id
                """,
                (row["decision_id"],),
            )
        )
        human_evidence = tuple(
            HumanTextEvidence(
                str(item["adjudication_id"]),
                str(item["structure_label"]),  # type: ignore[arg-type]
                str(item["tourism_label"]),  # type: ignore[arg-type]
            )
            for item in human_rows
        )
        model_evidence: ModelDecisionEvidence | None = None
        if row["provenance"] in {"model_low_risk", "model_review_candidate"}:
            model = connection.execute(
                "SELECT * FROM text_model_runs WHERE model_run_id = ?",
                (row["model_run_id"],),
            ).fetchone()
            if model is None:
                _raise("final_model_evidence_missing")
            expected_status = "completed" if request.audit_mode == "formal" else "smoke"
            if model["status"] != expected_status or model["seal_status"] != "finalized":
                _raise("final_model_mode_mismatch")
            prediction = connection.execute(
                """
                SELECT suggested_action FROM text_model_predictions
                WHERE model_run_id = ? AND source_post_id = ? AND source_version = ?
                """,
                (row["model_run_id"], identity[0], identity[1]),
            ).fetchone()
            if prediction is None:
                _raise("final_model_prediction_missing")
            manifest = _model_version_manifest(model)
            model_evidence = ModelDecisionEvidence(
                evidence_id=str(model["model_run_id"]),
                model_run_id=str(model["model_run_id"]),
                suggested_action=str(prediction["suggested_action"]),  # type: ignore[arg-type]
                run_mode=request.audit_mode,
                seal_status="finalized",
                required_version_manifest_sha256=manifest,
                model_version_manifest_sha256=manifest,
                test_set_isolation_passed=True,
                low_risk_threshold_enabled=bool(model["low_risk_enabled"]),
                platform_audit_complete=True,
                text_keep_audit_passed=True,
            )
        domain_decision = build_post_decisions(
            (
                PostDecisionRequest(
                    source_post_id=identity[0],
                    source_version=identity[1],
                    rule_version=request.decision_version,
                    build_kind="final",
                    human_evidence=human_evidence,
                    model_evidence=model_evidence,
                ),
            )
        )[0]
        # deterministic invalid 在候选层以硬规则领域证据形成；最终重建时没有
        # 人工 evidence，因此需再次将同一真实 task 映射到领域双轴输入。
        if row["provenance"] == "deterministic_invalid":
            deterministic_id = next(
                (link.evidence_id for link in links if link.evidence_kind == "deterministic_result"),
                None,
            )
            if deterministic_id is None:
                _raise("final_deterministic_evidence_missing")
            domain_decision = build_post_decisions(
                (
                    PostDecisionRequest(
                        identity[0],
                        identity[1],
                        request.decision_version,
                        "final",
                        (
                            HumanTextEvidence(
                                deterministic_id,
                                "invalid",
                                "not_applicable",
                            ),
                        ),
                    ),
                )
            )[0]
        if row["provenance"] in {"insufficient_evidence", "evidence_conflict"}:
            # 候选中的复核状态不能因最终审计而自行改变；领域层仍被调用，
            # 但 schema 投影沿用候选双轴与理由，等待新的人工证据重建候选。
            domain_decision = PostDecision(
                identity[0],
                identity[1],
                "review",
                tuple(str(row["reason_code"]).split("+")),
                tuple(link.evidence_id for link in links),
                request.decision_version,
                str(row["decision_sha256"]),
            )
        prepared.append(
            _PreparedDecision(
                domain_decision,
                str(row["structure_label"]),
                str(row["tourism_label"]),
                str(row["provenance"]),
                str(row["model_run_id"]) if row["model_run_id"] is not None else None,
                links,
            )
        )
    return tuple(prepared)


def _manifest_rows(prepared: Sequence[_PreparedDecision]) -> list[dict[str, object]]:
    """返回不含文本的决定 manifest 行。"""

    return [
        {
            "source_post_id": item.decision.source_post_id,
            "source_version": item.decision.source_version,
            "structure_label": item.structure_label,
            "tourism_label": item.tourism_label,
            "decision_action": item.decision.decision,
            "reason_codes": item.decision.reason_codes,
            "provenance": item.provenance,
            "model_run_id": item.model_run_id,
            "evidence": [
                [link.evidence_kind, link.evidence_id] for link in item.links
            ],
            "decision_sha256": item.decision.decision_sha256,
        }
        for item in prepared
    ]


def _stored_result(
    connection: sqlite3.Connection,
    *,
    request_manifest: str,
    expected_rows: Sequence[_PreparedDecision],
) -> PostDecisionBuildResult | None:
    """复验完全相同请求的已封存构建；不复用 latest 或仅计数相同对象。"""

    row = connection.execute(
        "SELECT * FROM post_decision_builds WHERE input_manifest_sha256 = ?",
        (request_manifest,),
    ).fetchone()
    if row is None:
        return None
    if row["seal_status"] != "finalized":
        _raise("stored_post_decision_build_not_finalized")
    stored_rows = tuple(
        connection.execute(
            """
            SELECT d.*, l.evidence_kind, l.evidence_id
            FROM post_decisions AS d
            JOIN post_decision_evidence_links AS l ON l.decision_id = d.decision_id
            WHERE d.decision_build_id = ?
            ORDER BY d.source_post_id, d.source_version, l.evidence_kind, l.evidence_id
            """,
            (row["decision_build_id"],),
        )
    )
    expected_manifest = _sha256(_manifest_rows(expected_rows))
    if row["decision_manifest_sha256"] != expected_manifest:
        _raise("stored_post_decision_manifest_mismatch")
    stored_identity_links = [
        (
            int(item["source_post_id"]),
            int(item["source_version"]),
            str(item["evidence_kind"]),
            str(item["evidence_id"]),
        )
        for item in stored_rows
    ]
    expected_identity_links = [
        (
            item.decision.source_post_id,
            item.decision.source_version,
            link.evidence_kind,
            link.evidence_id,
        )
        for item in expected_rows
        for link in item.links
    ]
    if stored_identity_links != expected_identity_links:
        _raise("stored_post_decision_evidence_mismatch")
    return PostDecisionBuildResult(
        str(row["decision_build_id"]),
        str(row["build_kind"]),  # type: ignore[arg-type]
        int(row["expected_post_count"]),
        int(row["keep_count"]),
        int(row["review_count"]),
        int(row["exclude_count"]),
        str(row["input_manifest_sha256"]),
        str(row["decision_manifest_sha256"]),
    )


def build_post_decision_snapshot(
    derived_db: str | Path,
    request: PostDecisionBuildRequest,
) -> PostDecisionBuildResult:
    """创建并封存候选或最终帖子决定构建。

    候选构建完整覆盖指定快照的全部非 missing 版本。最终构建只复用显式来源
    候选及其同模式、已通过审计，并重新运行纯领域规则。相同完整请求会复验
    子行后幂等返回；任一身份错配、证据缺失、模式混用或 SQLite 防绕过失败
    均回滚事务并抛出稳定 ``reason_code``。
    """

    _validate_request(request)
    try:
        with connect_derived(derived_db) as connection:
            migrate_derived(connection)
            with connection:
                population = _snapshot_population(
                    connection,
                    run_id=request.run_id,
                    snapshot_id=request.source_snapshot_id,
                )
                prepared = (
                    _prepare_candidate_decisions(connection, request, population)
                    if request.build_kind == "candidate"
                    else _prepare_final_decisions(connection, request, population)
                )
                input_payload = {
                    "audit_mode": request.audit_mode,
                    "build_kind": request.build_kind,
                    "decision_version": request.decision_version,
                    "deterministic_result_ids": sorted(request.deterministic_result_ids),
                    "guide_version": request.guide_version,
                    "human_adjudication_ids": sorted(request.human_adjudication_ids),
                    "model_algorithm_version": request.model_algorithm_version,
                    "model_candidate_build_id": request.model_candidate_build_id,
                    "model_run_id": request.model_run_id,
                    "rules_sha256": request.rules_sha256,
                    "run_id": request.run_id,
                    "source_candidate_decision_build_id": request.source_candidate_decision_build_id,
                    "source_snapshot_id": request.source_snapshot_id,
                    "text_keep_audit_evaluation_id": request.text_keep_audit_evaluation_id,
                }
                input_manifest = _sha256(input_payload)
                stored = _stored_result(
                    connection,
                    request_manifest=input_manifest,
                    expected_rows=prepared,
                )
                if stored is not None:
                    return stored
                decision_manifest = _sha256(_manifest_rows(prepared))
                decision_build_id = _sha256(
                    ["post-decision-build-v1", input_manifest, decision_manifest]
                )[:32]
                counts = {
                    action: sum(item.decision.decision == action for item in prepared)
                    for action in ("keep", "review", "exclude")
                }
                now = _utcnow()
                connection.execute(
                    """
                    INSERT INTO post_decision_builds(
                      decision_build_id, run_id, source_snapshot_id, build_kind,
                      source_candidate_decision_build_id, text_keep_audit_evaluation_id,
                      decision_version, guide_version, rules_sha256,
                      input_manifest_sha256, expected_post_count, keep_count,
                      review_count, exclude_count, decision_manifest_sha256,
                      seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        decision_build_id,
                        request.run_id,
                        request.source_snapshot_id,
                        request.build_kind,
                        request.source_candidate_decision_build_id,
                        request.text_keep_audit_evaluation_id,
                        request.decision_version,
                        request.guide_version,
                        request.rules_sha256,
                        input_manifest,
                        len(prepared),
                        counts["keep"],
                        counts["review"],
                        counts["exclude"],
                        decision_manifest,
                        now,
                    ),
                )
                for item in prepared:
                    decision_id = _sha256(
                        [
                            "post-decision-row-v1",
                            decision_build_id,
                            item.decision.source_post_id,
                            item.decision.source_version,
                        ]
                    )[:32]
                    evidence_manifest = _sha256(
                        [[link.evidence_kind, link.evidence_id] for link in item.links]
                    )
                    connection.execute(
                        """
                        INSERT INTO post_decisions(
                          decision_id, decision_build_id, source_post_id, source_version,
                          structure_label, tourism_label, decision_action, reason_code,
                          provenance, model_run_id, evidence_manifest_sha256,
                          decision_sha256, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            decision_build_id,
                            item.decision.source_post_id,
                            item.decision.source_version,
                            item.structure_label,
                            item.tourism_label,
                            item.decision.decision,
                            "+".join(item.decision.reason_codes),
                            item.provenance,
                            item.model_run_id,
                            evidence_manifest,
                            item.decision.decision_sha256,
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO post_decision_evidence_links(
                          decision_id, evidence_kind, evidence_id,
                          source_post_id, source_version
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                decision_id,
                                link.evidence_kind,
                                link.evidence_id,
                                item.decision.source_post_id,
                                item.decision.source_version,
                            )
                            for link in item.links
                        ],
                    )
                connection.execute(
                    """
                    UPDATE post_decision_builds SET seal_status = 'finalized'
                    WHERE decision_build_id = ? AND seal_status = 'building'
                    """,
                    (decision_build_id,),
                )
                return PostDecisionBuildResult(
                    decision_build_id,
                    request.build_kind,
                    len(prepared),
                    counts["keep"],
                    counts["review"],
                    counts["exclude"],
                    input_manifest,
                    decision_manifest,
                )
    except PostDecisionRepositoryError:
        raise
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PostDecisionRepositoryError("post_decision_contract_rejected") from exc
