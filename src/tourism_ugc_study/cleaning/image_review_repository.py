"""图片技术噪声人工复核的 SQLite 仓储与追加式 CSV 边界。

本模块只写独立派生 SQLite，不读取或修改正式采集库、原图和 URL。复核成员
来自 Issue #9 已封存候选 build；人工 CSV 只保存匿名哈希和枚举标签。纯抽样、
一致性计算与最终决定分别由解耦模块提供。
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .config import CleaningConfig
from .image_review_annotation import (
    IMAGE_ANNOTATION_COLUMNS,
    AnnotationPair,
    calculate_image_agreement,
    split_codes,
    validate_safe_csv_cell,
    TECHNICAL_NOISE_LABELS,
)
from .image_review_sampling import (
    PHashReviewGroup,
    ReviewCandidate,
    ReviewSelection,
    build_complete_linkage_groups,
    select_boundary_members,
    select_candidate_review_members,
    select_pilot_members,
    selection_manifest,
)
from .schema import connect_derived, migrate_derived


class ImageReviewRepositoryError(RuntimeError):
    """图片人工复核仓储不能安全继续时抛出的去敏领域异常。

    ``reason_code`` 是稳定机器码，不含图片路径、URL、CSV 内容或标注者原值。
    所有校验在事务提交前完成；失败不会覆盖既有证据。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("image review repository operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ReviewRunResult:
    """已封存复核运行的去敏回执。

    ``review_run_id/review_kind`` 标识运行及用途；``member_count`` 与
    ``phash_group_count`` 分别是冻结成员和仅供展示的近同组数量；
    ``member_manifest_sha256`` 绑定成员、顺序、原因与双标计划。回执不含路径、
    图片内容或标签，计数必须与数据库封存行一致。
    """

    review_run_id: str
    review_kind: str
    member_count: int
    phash_group_count: int
    member_manifest_sha256: str


@dataclass(frozen=True)
class AnnotationImportResult:
    """追加式人工标注导入回执；不包含标签原文或机器路径。

    ``import_id`` 由运行和源文件摘要派生，``source_sha256`` 绑定原 CSV 字节；
    ``row_count/accepted_count`` 记录事务级结果。本版整批校验，成功时两计数
    相等，失败时不返回回执也不留下部分 annotation 行。
    """

    import_id: str
    review_run_id: str
    row_count: int
    accepted_count: int
    source_sha256: str


@dataclass(frozen=True)
class DoubleLabelPlanResult:
    """已封存双标计划回执，成员由 manifest 摘要固定。

    ``plan_kind`` 区分边界、唯一补充轮和拟排除动态第二槽；``member_count``
    是实际任务数，``member_manifest_sha256`` 固定成员身份与顺序。计划只分配
    工作量，不预设第二位研究者应给出的标签。
    """

    plan_id: str
    review_run_id: str
    plan_kind: str
    member_count: int
    member_manifest_sha256: str


@dataclass(frozen=True)
class AgreementEvaluationResult:
    """图片边界双标的一致性、κ 状态与后续动作回执。

    计划未完成时 ``raw_agreement/cohen_kappa`` 为空且状态为 incomplete；完成
    后原始一致率决定 passed 或 supplement_required，``kappa_status`` 另行说明
    κ 是否可估。ID 与计数用于证据谱系，回执不携带逐图标签组合。
    """

    evaluation_id: str
    review_run_id: str
    planned_pair_count: int
    complete_pair_count: int
    raw_agreement: float | None
    cohen_kappa: float | None
    kappa_status: str
    evaluation_status: str


@dataclass(frozen=True)
class AdjudicationResult:
    """第三人仲裁的证据身份与最终技术噪声标签。

    ``adjudication_id`` 绑定两个原始槽位、图片、结果和匿名仲裁者；运行与指纹
    字段供决定层校验同源，``technical_noise_label`` 是保留的唯一人工轴。回执
    不表示决定动作，keep/review/exclude 仍由独立决定模块解析。
    """

    adjudication_id: str
    review_run_id: str
    fingerprint_id: str
    technical_noise_label: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算人工 CSV 字节摘要，不把文件内容写入日志或数据库。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ImageReviewRepositoryError("image_annotation_csv_unreadable") from exc
    return digest.hexdigest()


def _load_review_candidates(
    connection: sqlite3.Connection,
    candidate_build_id: str,
) -> tuple[ReviewCandidate, ...]:
    """读取已封存 build 的 SHA 代表和候选来源，不读取路径或 URL。"""

    build = connection.execute(
        "SELECT seal_status FROM image_candidate_builds WHERE build_id = ?",
        (candidate_build_id,),
    ).fetchone()
    if build is None or build["seal_status"] != "finalized":
        raise ImageReviewRepositoryError("finalized_image_candidate_build_required")
    rows = connection.execute(
        """
        SELECT c.cluster_id, c.representative_fingerprint_id AS fingerprint_id,
               c.member_count, f.phash_hex, p.platform_key,
               EXISTS(SELECT 1 FROM image_candidate_signals s
                      WHERE s.build_id = c.build_id
                        AND s.fingerprint_id = c.representative_fingerprint_id) AS has_signal,
               EXISTS(SELECT 1 FROM image_near_candidate_pairs n
                      WHERE n.build_id = c.build_id AND
                       (n.left_fingerprint_id = c.representative_fingerprint_id
                        OR n.right_fingerprint_id = c.representative_fingerprint_id)) AS has_near
        FROM image_exact_clusters c
        JOIN image_candidate_build_members m
          ON m.build_id = c.build_id AND m.fingerprint_id = c.representative_fingerprint_id
        JOIN image_fingerprints f ON f.fingerprint_id = m.fingerprint_id
        JOIN source_post_inventory p ON p.source_post_id = m.source_post_id
        WHERE c.build_id = ?
        ORDER BY c.representative_fingerprint_id
        """,
        (candidate_build_id,),
    ).fetchall()
    return tuple(
        ReviewCandidate(
            fingerprint_id=str(row["fingerprint_id"]),
            exact_cluster_id=str(row["cluster_id"]),
            phash_hex=str(row["phash_hex"]),
            platform_key=str(row["platform_key"]),
            has_technical_signal=bool(row["has_signal"]),
            has_exact_duplicate=int(row["member_count"]) > 1,
            has_phash_candidate=bool(row["has_near"]),
        )
        for row in rows
    )


def create_image_review_run(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    review_kind: str,
    config: CleaningConfig,
    random_seed: int | None = None,
    exclude_fingerprint_ids: Iterable[str] = (),
    code_version: str = "image-review-v1",
) -> ReviewRunResult:
    """从封存候选构建创建并封存试标、候选或边界复核运行。

    ``pilot`` 至多 30 张且双标，``candidate_review`` 导出全部候选代表首槽，
    ``boundary`` 至多 50 张候选/非候选边界双标。可选排除集合仅用于追加边界
    轮次避免重叠。pHash complete-linkage 只随运行保存展示组，不传播标签。
    相同输入幂等复用并返回 :class:`ReviewRunResult`；未知种类、非法种子、未
    封存候选或数据库约束冲突抛出 :class:`ImageReviewRepositoryError`。函数
    只写派生库，不读取图片文件，事务失败不留下 building 成员。
    """

    seed = config.random_seed if random_seed is None else random_seed
    if seed <= 0:
        raise ImageReviewRepositoryError("image_review_seed_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        records = _load_review_candidates(connection, candidate_build_id)
        if review_kind == "pilot":
            selections = select_pilot_members(
                records, seed=seed, size=config.image_review.pilot_size
            )
            planned_count = config.image_review.pilot_size
        elif review_kind == "candidate_review":
            selections = select_candidate_review_members(records, seed=seed)
            planned_count = len(selections)
        elif review_kind == "boundary":
            selections = select_boundary_members(
                records,
                seed=seed,
                size=config.image_review.boundary_double_label_size,
                exclude_fingerprint_ids=exclude_fingerprint_ids,
            )
            planned_count = config.image_review.boundary_double_label_size
        else:
            raise ImageReviewRepositoryError("image_review_kind_invalid")
        selected_ids = {item.fingerprint_id for item in selections}
        selected_records = [record for record in records if record.fingerprint_id in selected_ids]
        groups = build_complete_linkage_groups(
            selected_records,
            seed=seed,
            maximum_distance=config.image.candidate_hamming_max,
        )
        manifest = selection_manifest(selections, groups)
        review_run_id = _canonical_sha256(
            {
                "candidate_build_id": candidate_build_id,
                "review_kind": review_kind,
                "guide_version": config.image_label_guide_version,
                "config_sha256": config.sha256,
                "random_seed": seed,
                "member_manifest_sha256": manifest,
            }
        )[:32]
        existing = connection.execute(
            "SELECT * FROM image_review_runs WHERE review_run_id = ?",
            (review_run_id,),
        ).fetchone()
        result = ReviewRunResult(review_run_id, review_kind, len(selections), len(groups), manifest)
        if existing is not None:
            if existing["seal_status"] != "finalized" or existing["member_manifest_sha256"] != manifest:
                raise ImageReviewRepositoryError("image_review_run_conflict")
            return result
        now = _utcnow()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_review_runs(
                        review_run_id, candidate_build_id, review_kind, guide_version,
                        config_sha256, random_seed, code_version, planned_count,
                        member_count, member_manifest_sha256, seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        review_run_id,
                        candidate_build_id,
                        review_kind,
                        config.image_label_guide_version,
                        config.sha256,
                        seed,
                        code_version,
                        planned_count,
                        len(selections),
                        manifest,
                        now,
                    ),
                )
                for item in selections:
                    connection.execute(
                        """
                        INSERT INTO image_review_members(
                            review_run_id, fingerprint_id, exact_cluster_id,
                            review_reason, stable_rank, requires_double_label
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            review_run_id,
                            item.fingerprint_id,
                            item.exact_cluster_id,
                            item.review_reason,
                            item.stable_rank,
                            int(item.requires_double_label),
                        ),
                    )
                for group in groups:
                    group_manifest = _canonical_sha256(
                        [group.group_id, group.maximum_pair_distance, group.member_fingerprint_ids]
                    )
                    connection.execute(
                        """
                        INSERT INTO image_phash_review_groups(
                            review_run_id, group_id, member_count,
                            maximum_pair_distance, group_manifest_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            review_run_id,
                            group.group_id,
                            len(group.member_fingerprint_ids),
                            group.maximum_pair_distance,
                            group_manifest,
                        ),
                    )
                    for rank, fingerprint_id in enumerate(group.member_fingerprint_ids, start=1):
                        connection.execute(
                            """
                            INSERT INTO image_phash_review_group_members(
                                review_run_id, group_id, fingerprint_id, stable_rank
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (review_run_id, group.group_id, fingerprint_id, rank),
                        )
                connection.execute(
                    "UPDATE image_review_runs SET seal_status = 'finalized' WHERE review_run_id = ?",
                    (review_run_id,),
                )
        except sqlite3.IntegrityError as exc:
            raise ImageReviewRepositoryError("image_review_run_integrity_error") from exc
    return result


def _task_id(review_run_id: str, fingerprint_id: str, slot: int) -> str:
    return _canonical_sha256(["image-review-task-v1", review_run_id, fingerprint_id, slot])[:32]


def export_image_annotation_tasks(
    derived_db: str | Path,
    *,
    review_run_id: str,
    assignment_slot: int,
    output_path: str | Path,
) -> int:
    """导出一个盲标槽位的安全 CSV 模板并返回任务数。

    slot 1 导出全部复核成员；slot 2 只导出运行本身或已封存双标计划要求的
    成员。CSV 不含路径、URL、图片字节、另一槽标签和候选分数；调用方负责在
    受控查看器中用 fingerprint ID 映射图片，导出文件不得提交 Git。成功返回
    写出的数据行数；槽位非法、运行未封存或输出不可写时抛出领域异常，且不
    把本地输出路径写入数据库或标准回执。
    """

    if assignment_slot not in (1, 2):
        raise ImageReviewRepositoryError("image_annotation_slot_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        run = connection.execute(
            "SELECT guide_version, seal_status FROM image_review_runs WHERE review_run_id = ?",
            (review_run_id,),
        ).fetchone()
        if run is None or run["seal_status"] != "finalized":
            raise ImageReviewRepositoryError("finalized_image_review_run_required")
        rows = connection.execute(
            """
            SELECT DISTINCT m.fingerprint_id, m.stable_rank
            FROM image_review_members m
            WHERE m.review_run_id = ? AND (
                ? = 1 OR m.requires_double_label = 1 OR EXISTS (
                  SELECT 1 FROM image_double_label_plans p
                  JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
                  WHERE p.review_run_id = m.review_run_id
                    AND pm.fingerprint_id = m.fingerprint_id
                    AND p.seal_status = 'finalized'
                )
            )
            ORDER BY m.stable_rank
            """,
            (review_run_id, assignment_slot),
        ).fetchall()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=IMAGE_ANNOTATION_COLUMNS)
            writer.writeheader()
            for row in rows:
                fingerprint_id = str(row["fingerprint_id"])
                writer.writerow(
                    {
                        "task_id": _task_id(review_run_id, fingerprint_id, assignment_slot),
                        "review_run_id": review_run_id,
                        "fingerprint_id": fingerprint_id,
                        "assignment_slot": assignment_slot,
                        "technical_noise_label": "",
                        "reason_codes": "",
                        "technical_flags": "",
                        "annotator_hash": "",
                        "guide_version": str(run["guide_version"]),
                        "annotated_at_utc": "",
                    }
                )
    except OSError as exc:
        raise ImageReviewRepositoryError("image_annotation_csv_unwritable") from exc
    return len(rows)


def _read_annotation_csv(path: Path) -> list[dict[str, str]]:
    """严格读取标注 CSV 表头，并在返回前完成公式注入检查。"""

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != IMAGE_ANNOTATION_COLUMNS:
                raise ImageReviewRepositoryError("image_annotation_columns_invalid")
            rows = [dict(row) for row in reader]
    except (OSError, csv.Error) as exc:
        raise ImageReviewRepositoryError("image_annotation_csv_unreadable") from exc
    try:
        for row in rows:
            for value in row.values():
                validate_safe_csv_cell(value)
    except ValueError as exc:
        raise ImageReviewRepositoryError("image_annotation_formula_rejected") from exc
    return rows


def import_image_annotations(
    derived_db: str | Path,
    *,
    csv_path: str | Path,
    imported_by_hash: str,
) -> AnnotationImportResult:
    """校验并追加导入图片技术噪声原始标注。

    所有行必须属于同一已封存运行、匹配导出 task ID/手册/槽位计划，且标注者
    匿名 ID 为 64 位十六进制。相同文件摘要幂等复用；修改文件形成新 import，
    但数据库唯一槽位会拒绝覆盖旧证据。成功返回
    :class:`AnnotationImportResult`；文件、公式、身份、手册、标签、时间或槽位
    任一非法时抛出领域异常并整批不写入。
    """

    if len(imported_by_hash) != 64 or any(c not in "0123456789abcdef" for c in imported_by_hash):
        raise ImageReviewRepositoryError("image_importer_hash_invalid")
    path = Path(csv_path)
    source_sha256 = _file_sha256(path)
    rows = _read_annotation_csv(path)
    if not rows:
        raise ImageReviewRepositoryError("image_annotation_csv_empty")
    run_ids = {row["review_run_id"] for row in rows}
    if len(run_ids) != 1:
        raise ImageReviewRepositoryError("image_annotation_mixed_review_runs")
    review_run_id = next(iter(run_ids))
    import_id = _canonical_sha256(["image-annotation-import-v1", review_run_id, source_sha256])[:32]
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        existing = connection.execute(
            "SELECT row_count, accepted_count FROM image_annotation_imports WHERE import_id = ?",
            (import_id,),
        ).fetchone()
        if existing is not None:
            return AnnotationImportResult(
                import_id, review_run_id, int(existing["row_count"]), int(existing["accepted_count"]), source_sha256
            )
        run = connection.execute(
            "SELECT guide_version, seal_status FROM image_review_runs WHERE review_run_id = ?",
            (review_run_id,),
        ).fetchone()
        if run is None or run["seal_status"] != "finalized":
            raise ImageReviewRepositoryError("finalized_image_review_run_required")
        prepared: list[tuple[object, ...]] = []
        for row in rows:
            try:
                slot = int(row["assignment_slot"])
            except ValueError as exc:
                raise ImageReviewRepositoryError("image_annotation_slot_invalid") from exc
            fingerprint_id = row["fingerprint_id"]
            if row["task_id"] != _task_id(review_run_id, fingerprint_id, slot):
                raise ImageReviewRepositoryError("image_annotation_task_identity_mismatch")
            if row["guide_version"] != run["guide_version"]:
                raise ImageReviewRepositoryError("image_annotation_guide_mismatch")
            label = row["technical_noise_label"]
            if label not in TECHNICAL_NOISE_LABELS:
                raise ImageReviewRepositoryError("image_technical_noise_label_invalid")
            annotator = row["annotator_hash"]
            if len(annotator) != 64 or any(c not in "0123456789abcdef" for c in annotator):
                raise ImageReviewRepositoryError("image_annotator_hash_invalid")
            if not row["annotated_at_utc"]:
                raise ImageReviewRepositoryError("image_annotation_time_required")
            try:
                reasons = split_codes(row["reason_codes"])
                flags = split_codes(row["technical_flags"])
            except ValueError as exc:
                raise ImageReviewRepositoryError("image_annotation_codes_invalid") from exc
            row_sha = _canonical_sha256(row)
            annotation_id = _canonical_sha256(
                ["image-annotation-v1", review_run_id, fingerprint_id, slot, row_sha]
            )[:32]
            prepared.append(
                (
                    annotation_id,
                    import_id,
                    review_run_id,
                    fingerprint_id,
                    slot,
                    annotator,
                    row["guide_version"],
                    label,
                    json.dumps(reasons, separators=(",", ":")),
                    json.dumps(flags, separators=(",", ":")),
                    row["annotated_at_utc"],
                    row_sha,
                )
            )
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_annotation_imports(
                      import_id, review_run_id, source_sha256, imported_by_hash,
                      row_count, accepted_count, rejected_count, status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, 'accepted', ?)
                    """,
                    (import_id, review_run_id, source_sha256, imported_by_hash, len(rows), len(rows), _utcnow()),
                )
                connection.executemany(
                    """
                    INSERT INTO image_review_annotations(
                      annotation_id, import_id, review_run_id, fingerprint_id,
                      assignment_slot, annotator_hash, guide_version,
                      technical_noise_label, reason_codes_json, technical_flags_json,
                      annotated_at_utc, row_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    prepared,
                )
        except sqlite3.IntegrityError as exc:
            raise ImageReviewRepositoryError("image_annotation_integrity_error") from exc
    return AnnotationImportResult(import_id, review_run_id, len(rows), len(rows), source_sha256)


def create_double_label_plan(
    derived_db: str | Path,
    *,
    review_run_id: str,
    plan_kind: str,
    requested_count: int | None = None,
) -> DoubleLabelPlanResult:
    """冻结边界或动态拟排除/uncertain 的第二槽计划。

    ``boundary``/``boundary_supplement`` 使用运行中已标记双标的全部成员；
    ``proposed_exclusion`` 仅在 slot 1 已完成后选择非 ``valid_content`` 成员。
    因此明确有效内容无需额外人工，所有拟排除和 uncertain 都会开启独立 slot 2。
    成功返回封存计划；运行未封存、种类/数量非法或数据库谱系冲突时抛出领域
    异常。相同输入幂等，不会重复增加人工任务。
    """

    if plan_kind not in {"boundary", "boundary_supplement", "proposed_exclusion"}:
        raise ImageReviewRepositoryError("image_double_plan_kind_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        run = connection.execute(
            "SELECT guide_version, seal_status FROM image_review_runs WHERE review_run_id = ?",
            (review_run_id,),
        ).fetchone()
        if run is None or run["seal_status"] != "finalized":
            raise ImageReviewRepositoryError("finalized_image_review_run_required")
        if plan_kind == "proposed_exclusion":
            rows = connection.execute(
                """
                SELECT m.fingerprint_id, m.stable_rank, a.annotation_id,
                       a.technical_noise_label
                FROM image_review_members m
                JOIN image_review_annotations a ON a.review_run_id = m.review_run_id
                 AND a.fingerprint_id = m.fingerprint_id AND a.assignment_slot = 1
                WHERE m.review_run_id = ? AND a.technical_noise_label != 'valid_content'
                  AND NOT EXISTS (SELECT 1 FROM image_review_annotations a2
                    WHERE a2.review_run_id = m.review_run_id
                      AND a2.fingerprint_id = m.fingerprint_id AND a2.assignment_slot = 2)
                ORDER BY m.stable_rank
                """,
                (review_run_id,),
            ).fetchall()
            reason = "slot1_proposed_exclusion_or_uncertain"
            source = [[row["fingerprint_id"], row["annotation_id"], row["technical_noise_label"]] for row in rows]
        else:
            rows = connection.execute(
                """
                SELECT fingerprint_id, stable_rank FROM image_review_members
                WHERE review_run_id = ? AND requires_double_label = 1
                ORDER BY stable_rank
                """,
                (review_run_id,),
            ).fetchall()
            reason = "frozen_boundary_double_label"
            source = [[row["fingerprint_id"], row["stable_rank"]] for row in rows]
        if requested_count is None:
            requested_count = len(rows)
        if requested_count < 0:
            raise ImageReviewRepositoryError("image_double_plan_count_invalid")
        rows = rows[:requested_count]
        source_sha = _canonical_sha256(source)
        member_manifest = _canonical_sha256([str(row["fingerprint_id"]) for row in rows])
        plan_id = _canonical_sha256(
            ["image-double-plan-v1", review_run_id, plan_kind, source_sha, member_manifest]
        )[:32]
        existing = connection.execute(
            "SELECT seal_status FROM image_double_label_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        result = DoubleLabelPlanResult(plan_id, review_run_id, plan_kind, len(rows), member_manifest)
        if existing is not None:
            if existing["seal_status"] != "finalized":
                raise ImageReviewRepositoryError("image_double_plan_conflict")
            return result
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_double_label_plans(
                      plan_id, review_run_id, plan_kind, guide_version, requested_count,
                      member_count, member_manifest_sha256, source_evidence_sha256,
                      seal_status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)
                    """,
                    (
                        plan_id,
                        review_run_id,
                        plan_kind,
                        run["guide_version"],
                        requested_count,
                        len(rows),
                        member_manifest,
                        source_sha,
                        _utcnow(),
                    ),
                )
                for rank, row in enumerate(rows, start=1):
                    connection.execute(
                        """
                        INSERT INTO image_double_label_plan_members(
                          plan_id, fingerprint_id, stable_rank, reason_code
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (plan_id, row["fingerprint_id"], rank, reason),
                    )
                connection.execute(
                    "UPDATE image_double_label_plans SET seal_status = 'finalized' WHERE plan_id = ?",
                    (plan_id,),
                )
        except sqlite3.IntegrityError as exc:
            raise ImageReviewRepositoryError("image_double_plan_integrity_error") from exc
    return result


def create_boundary_supplement(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    prior_review_run_ids: Sequence[str],
    config: CleaningConfig,
) -> tuple[ReviewRunResult, DoubleLabelPlanResult]:
    """在原始一致率未达标时冻结唯一一轮、至多 50 张的边界补充。

    输入必须显式列出同一 candidate build 的既有 boundary 运行，且至少一条最新
    评估为 ``supplement_required``。函数排除所有既有边界成员，以派生种子创建
    新运行和 ``boundary_supplement`` 双标计划；超过配置的单轮补充上限、重复
    请求第二轮或没有剩余成员都会失败，不以 κ 单类别不可估为理由扩样。成功
    返回新运行和双标计划两个封存回执；任一父运行、状态或人口条件不满足时
    抛出领域异常，不会把补充成员混入首轮运行。
    """

    unique_ids = tuple(sorted(set(prior_review_run_ids)))
    if not unique_ids or len(unique_ids) != len(prior_review_run_ids):
        raise ImageReviewRepositoryError("boundary_prior_runs_invalid")
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        placeholders = ",".join("?" for _ in unique_ids)
        runs = connection.execute(
            f"""
            SELECT review_run_id FROM image_review_runs
            WHERE review_run_id IN ({placeholders}) AND candidate_build_id = ?
              AND review_kind = 'boundary' AND seal_status = 'finalized'
            """,
            (*unique_ids, candidate_build_id),
        ).fetchall()
        if len(runs) != len(unique_ids):
            raise ImageReviewRepositoryError("boundary_prior_runs_mismatch")
        supplement_count = connection.execute(
            """
            SELECT COUNT(*) FROM image_double_label_plans p
            JOIN image_review_runs r ON r.review_run_id = p.review_run_id
            WHERE r.candidate_build_id = ?
              AND p.plan_kind = 'boundary_supplement' AND p.seal_status = 'finalized'
            """,
            (candidate_build_id,),
        ).fetchone()[0]
        if int(supplement_count) > 0:
            raise ImageReviewRepositoryError("boundary_supplement_limit_reached")
        latest_statuses = [
            str(row[0])
            for row in connection.execute(
                f"""
                SELECT e.evaluation_status FROM image_agreement_evaluations e
                WHERE e.review_run_id IN ({placeholders})
                  AND e.created_at_utc = (
                    SELECT MAX(e2.created_at_utc) FROM image_agreement_evaluations e2
                    WHERE e2.review_run_id = e.review_run_id
                  )
                """,
                unique_ids,
            )
        ]
        if "supplement_required" not in latest_statuses:
            raise ImageReviewRepositoryError("boundary_supplement_not_required")
        excluded = {
            str(row[0])
            for row in connection.execute(
                f"""
                SELECT fingerprint_id FROM image_review_members
                WHERE review_run_id IN ({placeholders})
                """,
                unique_ids,
            )
        }
    review = create_image_review_run(
        derived_db,
        candidate_build_id=candidate_build_id,
        review_kind="boundary",
        config=config,
        random_seed=config.random_seed + len(unique_ids),
        exclude_fingerprint_ids=excluded,
    )
    if review.member_count > config.image_review.boundary_supplement_max:
        raise ImageReviewRepositoryError("boundary_supplement_size_exceeded")
    plan = create_double_label_plan(
        derived_db,
        review_run_id=review.review_run_id,
        plan_kind="boundary_supplement",
        requested_count=config.image_review.boundary_supplement_max,
    )
    return review, plan


def evaluate_image_agreement(
    derived_db: str | Path,
    *,
    review_run_id: str,
    config: CleaningConfig,
) -> AgreementEvaluationResult:
    """对边界计划的完整 slot 1/2 计算并追加一致性评估。

    计划成员取所有已封存 ``boundary``/``boundary_supplement`` 计划的并集；缺任一
    槽时状态为 incomplete。原始一致率低于 0.80 返回 supplement_required；κ
    单类别不可估被明确记录，不单独触发补样。成功返回可幂等复用的评估回执；
    没有封存计划、证据越界或数据库约束冲突时抛出领域异常。incomplete 也是
    追加式评估证据，但不能满足正式决定门禁。
    """

    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        planned_rows = connection.execute(
            """
            SELECT DISTINCT pm.fingerprint_id
            FROM image_double_label_plans p
            JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
            WHERE p.review_run_id = ? AND p.seal_status = 'finalized'
              AND p.plan_kind IN ('boundary', 'boundary_supplement')
            ORDER BY pm.fingerprint_id
            """,
            (review_run_id,),
        ).fetchall()
        planned_ids = [str(row["fingerprint_id"]) for row in planned_rows]
        if not planned_ids:
            raise ImageReviewRepositoryError("image_boundary_plan_required")
        placeholders = ",".join("?" for _ in planned_ids)
        annotation_rows = connection.execute(
            f"""
            SELECT annotation_id, fingerprint_id, assignment_slot, technical_noise_label
            FROM image_review_annotations
            WHERE review_run_id = ? AND fingerprint_id IN ({placeholders})
            ORDER BY fingerprint_id, assignment_slot
            """,
            (review_run_id, *planned_ids),
        ).fetchall()
        by_id: dict[str, dict[int, str]] = {}
        for row in annotation_rows:
            by_id.setdefault(str(row["fingerprint_id"]), {})[int(row["assignment_slot"])] = str(row["technical_noise_label"])
        pairs = [
            AnnotationPair(identity, slots[1], slots[2])
            for identity in planned_ids
            if 1 in (slots := by_id.get(identity, {})) and 2 in slots
        ]
        report = calculate_image_agreement(
            pairs,
            planned_pair_count=len(planned_ids),
            minimum_raw_agreement=config.image_review.minimum_raw_agreement,
        )
        plan_manifest = _canonical_sha256(planned_ids)
        annotation_manifest = _canonical_sha256(
            [
                [row["annotation_id"], row["fingerprint_id"], row["assignment_slot"]]
                for row in annotation_rows
            ]
        )
        evaluation_id = _canonical_sha256(
            ["image-agreement-v1", review_run_id, plan_manifest, annotation_manifest,
             report.complete_pair_count,
             report.raw_agreement, report.cohen_kappa]
        )[:32]
        existing = connection.execute(
            "SELECT * FROM image_agreement_evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        result = AgreementEvaluationResult(
            evaluation_id,
            review_run_id,
            report.planned_pair_count,
            report.complete_pair_count,
            report.raw_agreement,
            report.cohen_kappa,
            report.kappa_status,
            report.evaluation_status,
        )
        if existing is not None:
            return result
        with connection:
            connection.execute(
                """
                INSERT INTO image_agreement_evaluations(
                  evaluation_id, review_run_id, plan_manifest_sha256,
                  annotation_manifest_sha256,
                  planned_pair_count, complete_pair_count, agreement_count,
                  raw_agreement, cohen_kappa, kappa_status, evaluation_status,
                  label_disagreements_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    review_run_id,
                    plan_manifest,
                    annotation_manifest,
                    report.planned_pair_count,
                    report.complete_pair_count,
                    report.agreement_count,
                    report.raw_agreement,
                    report.cohen_kappa,
                    report.kappa_status,
                    report.evaluation_status,
                    json.dumps(report.label_disagreements, separators=(",", ":")),
                    _utcnow(),
                ),
            )
    return result


def record_image_adjudication(
    derived_db: str | Path,
    *,
    review_run_id: str,
    fingerprint_id: str,
    left_annotation_id: str,
    right_annotation_id: str,
    adjudicator_hash: str,
    guide_version: str,
    technical_noise_label: str,
    reason_codes: Sequence[str],
    adjudicated_at_utc: str,
) -> AdjudicationResult:
    """追加第三人仲裁并显式引用两个独立原始槽位。

    仲裁者匿名哈希不得等于任一原标注者；两条证据必须属于同一运行、图片和
    手册，且只有两槽分歧或任一槽为 ``uncertain`` 才可仲裁。理由只接受与
    CSV 相同的安全 token。SQLite trigger 再次执行约束，原始标签不会被改写。
    相同证据幂等返回 :class:`AdjudicationResult`；身份、标签、理由、必要性或
    谱系非法时抛出领域异常，且不得创建“覆盖”原标注的记录。
    """

    if technical_noise_label not in TECHNICAL_NOISE_LABELS:
        raise ImageReviewRepositoryError("image_technical_noise_label_invalid")
    if len(adjudicator_hash) != 64 or any(c not in "0123456789abcdef" for c in adjudicator_hash):
        raise ImageReviewRepositoryError("image_adjudicator_hash_invalid")
    if not adjudicated_at_utc:
        raise ImageReviewRepositoryError("image_adjudication_time_required")
    try:
        for reason in reason_codes:
            validate_safe_csv_cell(reason)
        reasons = split_codes(";".join(reason_codes))
    except ValueError as exc:
        raise ImageReviewRepositoryError("image_adjudication_codes_invalid") from exc
    left_id, right_id = sorted((left_annotation_id, right_annotation_id))
    evidence_sha = _canonical_sha256(
        [review_run_id, fingerprint_id, left_id, right_id, technical_noise_label, reasons]
    )
    adjudication_id = _canonical_sha256(["image-adjudication-v1", evidence_sha, adjudicator_hash])[:32]
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        existing = connection.execute(
            "SELECT technical_noise_label FROM image_review_adjudications WHERE adjudication_id = ?",
            (adjudication_id,),
        ).fetchone()
        if existing is not None:
            return AdjudicationResult(adjudication_id, review_run_id, fingerprint_id, str(existing[0]))
        evidence_rows = connection.execute(
            """
            SELECT annotation_id, assignment_slot, technical_noise_label
            FROM image_review_annotations
            WHERE annotation_id IN (?, ?) AND review_run_id = ?
              AND fingerprint_id = ? AND guide_version = ?
            ORDER BY assignment_slot
            """,
            (left_id, right_id, review_run_id, fingerprint_id, guide_version),
        ).fetchall()
        if len(evidence_rows) != 2 or {int(row["assignment_slot"]) for row in evidence_rows} != {1, 2}:
            raise ImageReviewRepositoryError("image_adjudication_evidence_invalid")
        source_labels = [str(row["technical_noise_label"]) for row in evidence_rows]
        if source_labels[0] == source_labels[1] and source_labels[0] != "uncertain":
            raise ImageReviewRepositoryError("image_adjudication_not_required")
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_review_adjudications(
                      adjudication_id, review_run_id, fingerprint_id,
                      left_annotation_id, right_annotation_id, adjudicator_hash,
                      guide_version, technical_noise_label, reason_codes_json,
                      adjudicated_at_utc, evidence_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        adjudication_id,
                        review_run_id,
                        fingerprint_id,
                        left_id,
                        right_id,
                        adjudicator_hash,
                        guide_version,
                        technical_noise_label,
                        json.dumps(reasons, separators=(",", ":")),
                        adjudicated_at_utc,
                        evidence_sha,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ImageReviewRepositoryError("image_adjudication_integrity_error") from exc
    return AdjudicationResult(adjudication_id, review_run_id, fingerprint_id, technical_noise_label)
