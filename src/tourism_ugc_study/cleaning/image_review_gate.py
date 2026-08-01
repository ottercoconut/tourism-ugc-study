"""图片正式决定前的共同试标与边界一致性硬门。

本模块只读取派生 SQLite 中已封存的复核运行、双标计划和一致性评估，不读取
图片、路径或正式采集库。正式决定与后续保留集审计共享同一入口，避免某个
调用路径漏过 30 张共同试标、50 张边界双标或原始一致率门槛。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .config import CleaningConfig
from .image_evaluation_integrity import (
    ImageEvaluationIntegrityError,
    validate_stored_image_agreement,
)


class ImageReviewGateError(RuntimeError):
    """正式复核证据不足时携带稳定、去敏的失败原因。

    ``reason_code`` 不包含图片身份、路径、人工标签或数据库异常文本；调用仓储
    应把它转换为各自的领域错误，且不得在门禁失败后创建决定或审计父行。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("formal image review gate failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ImageReviewGateResult:
    """通过硬门的复核身份、规模、一致率和证据摘要。

    pilot 与 boundary 的期望量分别是配置上限和当前可用人口的较小值，因此小型
    封闭人口会全查；正式大样本仍严格要求 30/50。``evidence_manifest_sha256``
    绑定实际通过的运行、计划和评估 ID，供决定构建身份使用。
    ``boundary_review_run_ids`` 可能只含首轮，也可能含失败首轮与通过补充轮；
    ``boundary_member_count`` 是被门禁接纳的总人工量，``*_raw_agreement`` 是
    最终用于验收的观测值。返回对象不包含标签或图片路径。
    """

    pilot_review_run_id: str
    pilot_member_count: int
    pilot_raw_agreement: float
    boundary_review_run_ids: tuple[str, ...]
    boundary_member_count: int
    boundary_raw_agreement: float
    evidence_manifest_sha256: str


def _canonical_sha256(value: object) -> str:
    """以排序 JSON 计算稳定 SHA-256，不把创建时间纳入科研身份。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _population_counts(
    connection: sqlite3.Connection,
    candidate_build_id: str,
) -> tuple[int, int]:
    """返回 SHA 代表总数与命中任一候选来源的代表数。

    这里复用决定层的候选定义：技术信号、精确重复或 pHash pair 任一命中即为
    候选。空代表人口说明上游候选构建无可决定对象，不能伪造零规模人工门禁。
    """

    row = connection.execute(
        """
        SELECT COUNT(*) AS total_count,
               SUM(CASE WHEN c.member_count > 1
                    OR EXISTS(SELECT 1 FROM image_candidate_signals s
                      WHERE s.build_id = c.build_id
                        AND s.fingerprint_id = c.representative_fingerprint_id)
                    OR EXISTS(SELECT 1 FROM image_near_candidate_pairs n
                      WHERE n.build_id = c.build_id AND
                       (n.left_fingerprint_id = c.representative_fingerprint_id
                        OR n.right_fingerprint_id = c.representative_fingerprint_id))
                   THEN 1 ELSE 0 END) AS candidate_count
        FROM image_exact_clusters c WHERE c.build_id = ?
        """,
        (candidate_build_id,),
    ).fetchone()
    total = int(row["total_count"] or 0)
    candidates = int(row["candidate_count"] or 0)
    if total <= 0 or candidates <= 0:
        raise ImageReviewGateError("image_review_gate_population_insufficient")
    return total, candidates


def _agreement_rows(
    connection: sqlite3.Connection,
    *,
    candidate_build_id: str,
    review_kind: str,
    guide_version: str,
    config_sha256: str,
    expected_member_count: int,
    config: CleaningConfig,
) -> list[sqlite3.Row]:
    """读取规模正确且具有完整双标计划的最终一致性评估。

    同一运行可能先写 ``incomplete`` 再写完整评估；查询只返回完整计划计数相等
    的行。是否达到阈值由上层显式判断，避免仅凭 ``evaluation_status`` 信任被
    直接 SQL 伪造的数值组合。
    """

    rows = connection.execute(
        """
        SELECT r.review_run_id, r.member_count, p.plan_kind, p.plan_id,
               e.*
        FROM image_review_runs r
        JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
          AND p.seal_status = 'finalized' AND p.member_count = r.member_count
        JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
          AND e.planned_pair_count = r.member_count
          AND e.complete_pair_count = r.member_count
        WHERE r.candidate_build_id = ? AND r.review_kind = ?
          AND r.guide_version = ? AND r.config_sha256 = ?
          AND r.seal_status = 'finalized' AND r.member_count = ?
        ORDER BY r.review_run_id, p.plan_kind, e.evaluation_id
        """,
        (
            candidate_build_id,
            review_kind,
            guide_version,
            config_sha256,
            expected_member_count,
        ),
    ).fetchall()
    # SQL 条件只负责缩小候选范围；真正的可信性来自对原始标注的逐字段重算。
    # 旧版 untrusted 行和任何无法重建的派生行都不会出现在门禁候选中。
    trusted: list[sqlite3.Row] = []
    for row in rows:
        try:
            validate_stored_image_agreement(connection, row, config=config)
        except ImageEvaluationIntegrityError:
            continue
        trusted.append(row)
    return trusted


def validate_formal_image_review_gate(
    connection: sqlite3.Connection,
    *,
    candidate_build_id: str,
    config: CleaningConfig,
) -> ImageReviewGateResult:
    """验证正式决定所需的 pilot、边界双标和 0.80 原始一致率。

    输入连接必须已启用外键并完成迁移。pilot 从候选代表取
    ``min(30,N_candidate)``，boundary 从全部代表取 ``min(50,N_total)``；两者
    都必须具有两个独立槽位的完整评估，且原始一致率不低于配置门槛。边界首轮
    未达标时，只接受一轮 ``boundary_supplement`` 的完整通过结果。失败抛出
    ``ImageReviewGateError``，其 ``reason_code`` 区分人口、pilot、boundary 和
    一致率问题；成功返回 :class:`ImageReviewGateResult`。函数只读当前连接，
    不创建或修补缺失证据，也不接受逐条 LLM 推断替代人工运行。
    """

    build = connection.execute(
        """
        SELECT seal_status FROM image_candidate_builds WHERE build_id = ?
        """,
        (candidate_build_id,),
    ).fetchone()
    if build is None or build["seal_status"] != "finalized":
        raise ImageReviewGateError("finalized_image_candidate_build_required")
    total_count, candidate_count = _population_counts(connection, candidate_build_id)
    pilot_expected = min(config.image_review.pilot_size, candidate_count)
    boundary_expected = min(config.image_review.boundary_double_label_size, total_count)
    threshold = config.image_review.minimum_raw_agreement

    pilot_rows = _agreement_rows(
        connection,
        candidate_build_id=candidate_build_id,
        review_kind="pilot",
        guide_version=config.image_label_guide_version,
        config_sha256=config.sha256,
        expected_member_count=pilot_expected,
        config=config,
    )
    if not pilot_rows:
        raise ImageReviewGateError("image_review_gate_pilot_missing")
    pilot_passed = [
        row
        for row in pilot_rows
        if row["plan_kind"] == "boundary"
        and row["evaluation_status"] == "passed"
        and row["raw_agreement"] is not None
        and float(row["raw_agreement"]) >= threshold
    ]
    if not pilot_passed:
        raise ImageReviewGateError("image_review_gate_pilot_agreement_failed")
    pilot = pilot_passed[0]

    boundary_rows = _agreement_rows(
        connection,
        candidate_build_id=candidate_build_id,
        review_kind="boundary",
        guide_version=config.image_label_guide_version,
        config_sha256=config.sha256,
        expected_member_count=boundary_expected,
        config=config,
    )
    initial = [row for row in boundary_rows if row["plan_kind"] == "boundary"]
    if not initial:
        raise ImageReviewGateError("image_review_gate_boundary_missing")
    initial_passed = [
        row
        for row in initial
        if row["evaluation_status"] == "passed"
        and row["raw_agreement"] is not None
        and float(row["raw_agreement"]) >= threshold
    ]
    accepted_rows: list[sqlite3.Row]
    if initial_passed:
        accepted_rows = [initial_passed[0]]
    else:
        # 补充运行可能因剩余人口不足而少于首轮 50 张，不能用首轮期望量过滤；
        # 但必须显式由 supplement plan 产生并独立达到同一原始一致率门槛。
        supplement_rows = connection.execute(
            """
            SELECT r.review_run_id, r.member_count, p.plan_id, e.evaluation_id,
                   e.*
            FROM image_review_runs r
            JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
              AND p.plan_kind = 'boundary_supplement'
              AND p.seal_status = 'finalized' AND p.member_count = r.member_count
            JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
              AND e.planned_pair_count = r.member_count
              AND e.complete_pair_count = r.member_count
            WHERE r.candidate_build_id = ? AND r.review_kind = 'boundary'
              AND r.guide_version = ? AND r.config_sha256 = ?
              AND r.seal_status = 'finalized' AND e.evaluation_status = 'passed'
              AND e.raw_agreement >= ?
            ORDER BY r.review_run_id, e.evaluation_id
            """,
            (
                candidate_build_id,
                config.image_label_guide_version,
                config.sha256,
                threshold,
            ),
        ).fetchall()
        supplement: list[sqlite3.Row] = []
        for row in supplement_rows:
            try:
                validate_stored_image_agreement(connection, row, config=config)
            except ImageEvaluationIntegrityError:
                continue
            supplement.append(row)
        if not supplement:
            raise ImageReviewGateError("image_review_gate_boundary_agreement_failed")
        accepted_rows = [initial[0], supplement[0]]

    manifest_rows = [
        ["pilot", pilot["review_run_id"], pilot["plan_id"], pilot["evaluation_id"]],
        *[
            ["boundary", row["review_run_id"], row["plan_id"], row["evaluation_id"]]
            for row in accepted_rows
        ],
    ]
    return ImageReviewGateResult(
        str(pilot["review_run_id"]),
        int(pilot["member_count"]),
        float(pilot["raw_agreement"]),
        tuple(str(row["review_run_id"]) for row in accepted_rows),
        sum(int(row["member_count"]) for row in accepted_rows),
        float(accepted_rows[-1]["raw_agreement"]),
        _canonical_sha256(manifest_rows),
    )
