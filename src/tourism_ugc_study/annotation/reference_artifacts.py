"""参考集旧证据读取、只读数据库投影与不可变 artifact 持久化。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .reference_candidates import CandidateComputation
from .reference_contract import (
    ALLOWED_LABELS,
    DuplicateDecision,
    FINAL_PROBABILITY_COUNT,
    FINAL_REFERENCE_CONTRACT,
    FINAL_REFERENCE_FIELDS,
    FINAL_REFERENCE_ROW_COUNT,
    FINAL_REFERENCE_STATUS,
    FINAL_TARGETED_COUNT,
    FinalReferenceRow,
    PairIdentity,
    ReferenceDatasetError,
    ReferencePost,
    SourceIdentity,
    SUPPLEMENTAL_LABEL_FIELDS,
    canonical_json_bytes,
    canonical_sha256,
    text_sha256,
)
from .reference_human_evidence import (
    DUPLICATE_DECISION_FIELDS,
    LABEL_RESOLUTION_FIELDS,
    DuplicateComponentPlan,
    DuplicateLabelConflict,
    load_duplicate_decisions,
    load_label_resolutions,
)
from .reference_replacements import (
    ReplacementAnnotationPlan,
    ReplacementQueue,
    ReplacementSelection,
)


LEGACY_REFERENCE_FIELDS: tuple[str, ...] = (
    "task_id",
    "sample_run_id",
    "source_post_id",
    "source_version",
    "platform_key",
    "normalized_model_text",
    "tourism_label",
)
REPLACEMENT_QUEUE_FIELDS: tuple[str, ...] = (
    "queue_rank",
    "source_post_id",
    "source_version",
    "task_id",
    "normalized_model_text",
    "structure_status",
)


@dataclass(frozen=True)
class LegacyReferenceInput:
    """一次性迁移使用的现有700条完成证据投影。

    Attributes:
        posts: 已校验标签、身份、文本和样本框的700条记录。
        csv_sha256: 旧完成 CSV 原始字节摘要。
        manifest_sha256: 旧 manifest 原始字节摘要。
        input_member_sha256: 旧700条身份、标签和文本哈希摘要。
        sample_run_id: 原抽样运行身份。
        candidate_build_id: 同一冻结候选人口身份。
        candidate_build_sha256: 候选人口成员摘要。
        label_guide_id: 人工标签手册身份。
        normalization_rule_id: 规范化规则身份。
    """

    posts: tuple[ReferencePost, ...]
    csv_sha256: str
    manifest_sha256: str
    input_member_sha256: str
    sample_run_id: str
    candidate_build_id: str
    candidate_build_sha256: str
    label_guide_id: str
    normalization_rule_id: str


@dataclass(frozen=True)
class ArtifactWriteResult:
    """不可变文件写入或幂等复用的去敏摘要。

    Attributes:
        sha256: 文件原始字节摘要。
        reused: 是否复用字节完全一致的既有文件。
    """

    sha256: str
    reused: bool


@dataclass(frozen=True)
class ArtifactPairResult:
    """CSV 与 manifest 配对写入的去敏摘要。

    Attributes:
        csv_sha256: CSV 原始字节摘要。
        manifest_sha256: manifest 原始字节摘要。
        reused: 两个文件是否都为幂等复用。
        row_count: CSV 数据行数。
        status: manifest 的机器可读状态。
    """

    csv_sha256: str
    manifest_sha256: str
    reused: bool
    row_count: int
    status: str


@dataclass(frozen=True)
class LabelResolutionArtifact:
    """已封存标签冲突确认的映射与证据摘要。

    Attributes:
        resolutions: 重复分量 ID 到最终二元标签。
        evidence_sha256: 含 evidence_id 的人工确认稳定摘要。
        manifest_sha256: 配对 finalized manifest 原始字节摘要。
    """

    resolutions: Mapping[str, str]
    evidence_sha256: str
    manifest_sha256: str


def file_sha256(path: str | Path) -> str:
    """流式计算文件 SHA-256。

    Args:
        path: 待读取文件。

    Returns:
        小写十六进制 SHA-256。
    """

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReferenceDatasetError("artifact_file_unreadable") from exc
    return digest.hexdigest()


def readonly_connection(path: str | Path) -> sqlite3.Connection:
    """以 SQLite ``mode=ro`` 和 ``query_only`` 打开派生数据库。

    Args:
        path: 必须已存在的派生数据库。

    Returns:
        使用列名访问、禁止写入和不信任 schema 的连接。

    Raises:
        ReferenceDatasetError: 文件不存在或无法安全只读打开。
    """

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise ReferenceDatasetError("reference_database_readonly_open_failed") from exc


def _read_json_object(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取必须为 JSON 对象的 artifact。

    Args:
        path: JSON 文件路径。
        reason_code: 读取或解析失败时的稳定失败码。

    Returns:
        已解析映射。

    Raises:
        ReferenceDatasetError: 文件不可读、JSON 非法或顶层不是对象。
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceDatasetError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ReferenceDatasetError(reason_code)
    return value


def _positive_identity(value: str) -> int:
    """解析旧 CSV 中采用规范十进制表示的正整数身份。

    Args:
        value: 原始 CSV 值。

    Returns:
        正整数身份。

    Raises:
        ReferenceDatasetError: 值不是规范正整数。
    """

    stripped = value.strip()
    try:
        parsed = int(stripped)
    except ValueError as exc:
        raise ReferenceDatasetError("legacy_reference_source_identity_invalid") from exc
    if parsed <= 0 or str(parsed) != stripped:
        raise ReferenceDatasetError("legacy_reference_source_identity_invalid")
    return parsed


def _validate_complete_csv_rows(
    rows: Sequence[Mapping[str | None, str | None]],
    fields: Sequence[str],
    reason_code: str,
) -> None:
    """验证 CSV 数据行与冻结表头完全对齐。

    Args:
        rows: ``csv.DictReader`` 返回的原始行。
        fields: 预期字段集合。
        reason_code: 短行、长行或缺失单元格的稳定失败码。

    Raises:
        ReferenceDatasetError: 任一行结构不完整或含额外单元格。
    """

    expected = set(fields)
    if any(
        set(row) != expected or any(value is None for value in row.values())
        for row in rows
    ):
        raise ReferenceDatasetError(reason_code)


def _legacy_member_manifest(
    connection: sqlite3.Connection, sample_run_id: str
) -> str:
    """按旧抽样封存顺序重建成员摘要。

    Args:
        connection: 已启用只读模式的派生库连接。
        sample_run_id: 旧抽样运行身份。

    Returns:
        包含样本框、选择秩和合法权重的稳定 SHA-256。
    """

    rows = connection.execute(
        """
        SELECT source_post_id, source_version, platform_key, sample_frame,
               selection_reason_code, selection_rank,
               inclusion_probability_ppm, analysis_weight
        FROM text_sample_members
        WHERE sample_run_id = ?
        ORDER BY CASE sample_frame
                   WHEN 'probability' THEN 1
                   WHEN 'targeted' THEN 2
                   ELSE 3 END,
                 selection_rank, source_post_id, source_version
        """,
        (sample_run_id,),
    ).fetchall()
    return canonical_sha256(
        [
            {
                "source_post_id": int(row["source_post_id"]),
                "source_version": int(row["source_version"]),
                "platform_key": str(row["platform_key"]),
                "sample_frame": str(row["sample_frame"]),
                "selection_reason_code": str(row["selection_reason_code"]),
                "selection_rank": int(row["selection_rank"]),
                "inclusion_probability_ppm": (
                    int(row["inclusion_probability_ppm"])
                    if row["inclusion_probability_ppm"] is not None
                    else None
                ),
                "analysis_weight": (
                    float(row["analysis_weight"])
                    if row["analysis_weight"] is not None
                    else None
                ),
            }
            for row in rows
        ]
    )


def load_legacy_reference_input(
    csv_path: str | Path,
    manifest_path: str | Path,
    derived_db: str | Path,
    *,
    expected_label_guide_id: str,
    normalization_rule_id: str,
) -> LegacyReferenceInput:
    """一次性读取并校验现有700条完成 CSV、manifest 和派生库身份。

    这是 Issue #39 的显式迁移输入，不是训练兼容入口。最终训练验证器不会接受
    该字段契约。数据库始终以只读方式打开，函数不创建、不更新也不删除任何行。

    Args:
        csv_path: 现有700条完成 CSV。
        manifest_path: 与旧 CSV 绑定的完成 manifest。
        derived_db: 保有原抽样、候选构建和规范化文本身份的派生库。
        expected_label_guide_id: 稳定配置要求的标签手册身份。
        normalization_rule_id: 当前冻结规范化规则身份。

    Returns:
        通过文件哈希、700条计数、500/200框、标签、身份和文本校验的输入。

    Raises:
        ReferenceDatasetError: 任一输入、哈希、计数、身份、标签、文本、权重或
            数据库只读约束不满足。
    """

    try:
        csv_file = Path(csv_path).expanduser().resolve(strict=True)
        manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ReferenceDatasetError("legacy_reference_artifact_not_found") from exc
    manifest = _read_json_object(manifest_file, "legacy_reference_manifest_unreadable")
    completed = manifest.get("completed_csv")
    sample_manifest = manifest.get("sample_run")
    candidate_manifest = manifest.get("candidate_build")
    if not all(isinstance(value, Mapping) for value in (completed, sample_manifest, candidate_manifest)):
        raise ReferenceDatasetError("legacy_reference_manifest_contract_invalid")
    if manifest.get("label_guide_version") != expected_label_guide_id:
        raise ReferenceDatasetError("legacy_reference_label_guide_mismatch")
    if manifest.get("active_csv_count") != 1:
        raise ReferenceDatasetError("legacy_reference_active_csv_count_invalid")
    if completed.get("field_contract") != "text-cleaning-post-review-v1.5":
        raise ReferenceDatasetError("legacy_reference_field_contract_mismatch")
    if completed.get("logical_name") != csv_file.name:
        raise ReferenceDatasetError("legacy_reference_csv_name_mismatch")
    csv_hash = file_sha256(csv_file)
    if completed.get("sha256") != csv_hash:
        raise ReferenceDatasetError("legacy_reference_csv_hash_mismatch")
    try:
        with csv_file.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != LEGACY_REFERENCE_FIELDS:
                raise ReferenceDatasetError("legacy_reference_csv_field_contract_mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("legacy_reference_csv_unreadable") from exc
    _validate_complete_csv_rows(
        rows,
        LEGACY_REFERENCE_FIELDS,
        "legacy_reference_csv_row_structure_invalid",
    )
    if len(rows) != FINAL_REFERENCE_ROW_COUNT or completed.get("row_count") != len(rows):
        raise ReferenceDatasetError("legacy_reference_row_count_invalid")
    labels = Counter(row["tourism_label"].strip() for row in rows)
    if set(labels) - ALLOWED_LABELS or sum(labels.values()) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceDatasetError("legacy_reference_label_not_final")
    if completed.get("blank_label_count") != 0:
        raise ReferenceDatasetError("legacy_reference_blank_label_count_invalid")
    expected_legacy_label_counts = {
        "related": labels.get("related", 0),
        "unrelated": labels.get("unrelated", 0),
        "uncertain": 0,
    }
    if completed.get("label_counts") != expected_legacy_label_counts:
        raise ReferenceDatasetError("legacy_reference_label_counts_mismatch")
    sample_run_id = str(sample_manifest.get("sample_run_id") or "")
    candidate_build_id = str(candidate_manifest.get("build_id") or "")
    if not sample_run_id or not candidate_build_id:
        raise ReferenceDatasetError("legacy_reference_lineage_identity_missing")
    connection = readonly_connection(derived_db)
    try:
        sample = connection.execute(
            """
            SELECT candidate_build_id, source_snapshot_id, guide_version,
                   population_count, probability_count, targeted_count,
                   seal_status, member_manifest_sha256
            FROM text_sampling_runs WHERE sample_run_id = ?
            """,
            (sample_run_id,),
        ).fetchone()
        if (
            sample is None
            or sample["seal_status"] != "finalized"
            or str(sample["candidate_build_id"]) != candidate_build_id
            or str(sample["guide_version"]) != expected_label_guide_id
            or int(sample["probability_count"]) != FINAL_PROBABILITY_COUNT
            or int(sample["targeted_count"]) != FINAL_TARGETED_COUNT
        ):
            raise ReferenceDatasetError("legacy_reference_sample_identity_mismatch")
        if (
            sample_manifest.get("seal_status") != "finalized"
            or sample_manifest.get("population_count") != int(sample["population_count"])
            or sample_manifest.get("probability_count") != int(sample["probability_count"])
            or sample_manifest.get("targeted_count") != int(sample["targeted_count"])
            or sample_manifest.get("member_manifest_sha256")
            != str(sample["member_manifest_sha256"])
        ):
            raise ReferenceDatasetError("legacy_reference_sample_manifest_mismatch")
        source_manifest = manifest.get("source_snapshot")
        if (
            not isinstance(source_manifest, Mapping)
            or source_manifest.get("snapshot_id") != str(sample["source_snapshot_id"])
        ):
            raise ReferenceDatasetError("legacy_reference_source_snapshot_mismatch")
        build = connection.execute(
            """
            SELECT rules_version, rules_sha256, status, is_complete_corpus,
                   expected_post_count, processed_post_count, usable_post_count,
                   exact_cluster_count, near_candidate_component_count,
                   corpus_manifest_sha256
            FROM text_candidate_builds WHERE build_id = ?
            """,
            (candidate_build_id,),
        ).fetchone()
        if (
            build is None
            or build["status"] != "finalized"
            or not int(build["is_complete_corpus"])
            or candidate_manifest.get("status") != "finalized"
            or candidate_manifest.get("corpus_manifest_sha256")
            != str(build["corpus_manifest_sha256"])
            or candidate_manifest.get("expected_post_count")
            != int(build["expected_post_count"])
            or candidate_manifest.get("processed_post_count")
            != int(build["processed_post_count"])
            or candidate_manifest.get("usable_post_count")
            != int(build["usable_post_count"])
            or candidate_manifest.get("exact_cluster_count")
            != int(build["exact_cluster_count"])
            or candidate_manifest.get("near_candidate_component_count")
            != int(build["near_candidate_component_count"])
        ):
            raise ReferenceDatasetError("legacy_reference_candidate_manifest_mismatch")
        database_normalization_rule_id = (
            f"{build['rules_version']}+sha256:{build['rules_sha256']}"
        )
        if database_normalization_rule_id != normalization_rule_id:
            raise ReferenceDatasetError("legacy_reference_normalization_rule_mismatch")
        member_manifest_sha256 = _legacy_member_manifest(connection, sample_run_id)
        if member_manifest_sha256 != str(sample["member_manifest_sha256"]):
            raise ReferenceDatasetError("legacy_reference_member_manifest_mismatch")
        database_rows = connection.execute(
            """
            SELECT m.source_post_id, m.source_version, m.platform_key,
                   m.sample_frame,
                   m.selection_reason_code, m.selection_rank,
                   m.inclusion_probability_ppm, m.analysis_weight,
                   c.task_id AS candidate_task_id,
                   c.platform_key AS candidate_platform_key,
                   c.structure_status,
                   r.normalized_model_text
            FROM text_sample_members AS m
            JOIN text_candidate_corpus_members AS c
              ON c.build_id = ?
             AND c.source_post_id = m.source_post_id
             AND c.source_version = m.source_version
            JOIN text_deterministic_results AS r ON r.task_id = c.task_id
            WHERE m.sample_run_id = ?
            ORDER BY m.source_post_id, m.source_version
            """,
            (candidate_build_id, sample_run_id),
        ).fetchall()
        annotation_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_post_annotations'"
        ).fetchone()
        if annotation_table is not None:
            annotation_count = int(
                connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0]
            )
            if annotation_count:
                raise ReferenceDatasetError("legacy_reference_labels_copied_to_database")
        database_query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        if database_query_only != 1:
            raise ReferenceDatasetError("reference_database_not_query_only")
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise ReferenceDatasetError("legacy_reference_database_contract_invalid") from exc
    finally:
        connection.close()
    if len(database_rows) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceDatasetError("legacy_reference_database_member_count_invalid")
    if any(
        row["candidate_task_id"] is None or row["normalized_model_text"] is None
        for row in database_rows
    ):
        raise ReferenceDatasetError("legacy_reference_database_member_null")
    try:
        by_identity = {
            SourceIdentity(int(row["source_post_id"]), int(row["source_version"])): row
            for row in database_rows
        }
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceDatasetError("legacy_reference_database_value_invalid") from exc
    if len(by_identity) != len(database_rows):
        raise ReferenceDatasetError("legacy_reference_database_identity_not_unique")
    posts: list[ReferencePost] = []
    seen_tasks: set[str] = set()
    for row in rows:
        identity = SourceIdentity(
            _positive_identity(row["source_post_id"]),
            _positive_identity(row["source_version"]),
        )
        database_row = by_identity.get(identity)
        if database_row is None:
            raise ReferenceDatasetError("legacy_reference_member_not_in_database")
        if row["sample_run_id"].strip() != sample_run_id:
            raise ReferenceDatasetError("legacy_reference_sample_run_mismatch")
        task_id = row["task_id"].strip()
        if not task_id or task_id in seen_tasks:
            raise ReferenceDatasetError("legacy_reference_task_identity_not_unique")
        expected_task_id = canonical_sha256([sample_run_id, identity.source_post_id])[:32]
        if task_id != expected_task_id:
            raise ReferenceDatasetError("legacy_reference_task_identity_mismatch")
        if row["normalized_model_text"] != str(database_row["normalized_model_text"]):
            raise ReferenceDatasetError("legacy_reference_normalized_text_mismatch")
        if (
            row["platform_key"].strip() != str(database_row["platform_key"])
            or str(database_row["platform_key"])
            != str(database_row["candidate_platform_key"])
        ):
            raise ReferenceDatasetError("legacy_reference_platform_lineage_mismatch")
        frame = str(database_row["sample_frame"])
        ppm = database_row["inclusion_probability_ppm"]
        try:
            probability = None if ppm is None else int(ppm) / 1_000_000
            weight = (
                None
                if database_row["analysis_weight"] is None
                else float(database_row["analysis_weight"])
            )
            selection_rank = int(database_row["selection_rank"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReferenceDatasetError("legacy_reference_database_value_invalid") from exc
        if frame == "probability":
            if (
                probability is None
                or not 0.0 < probability <= 1.0
                or weight is None
                # 旧库把纳入概率量化为整数 ppm，而权重由量化前概率计算；
                # 2e-5 仅覆盖最多半个 ppm 引起的倒数误差，不用于新补样权重。
                or not math.isclose(weight, 1.0 / probability, rel_tol=2e-5)
            ):
                raise ReferenceDatasetError("legacy_probability_weight_invalid")
        elif frame == "targeted":
            if probability is not None or weight is not None:
                raise ReferenceDatasetError("legacy_targeted_weight_must_be_blank")
        else:
            raise ReferenceDatasetError("legacy_reference_sample_frame_invalid")
        posts.append(
            ReferencePost(
                identity=identity,
                task_id=task_id,
                normalized_model_text=row["normalized_model_text"],
                tourism_label=row["tourism_label"].strip(),
                sample_frame=frame,
                selection_reason_code=str(database_row["selection_reason_code"]),
                selection_rank=selection_rank,
                inclusion_probability=probability,
                analysis_weight=weight,
                evidence_origin="existing_representative",
                structure_usable=str(database_row["structure_status"]) == "usable",
            )
        )
        if str(database_row["structure_status"]) != "usable":
            raise ReferenceDatasetError("legacy_reference_structure_not_usable")
        seen_tasks.add(task_id)
    frame_counts = Counter(post.sample_frame for post in posts)
    if frame_counts != {"probability": FINAL_PROBABILITY_COUNT, "targeted": FINAL_TARGETED_COUNT}:
        raise ReferenceDatasetError("legacy_reference_frame_count_invalid")
    input_member_hash = canonical_sha256(
        [
            {
                "identity": post.identity.as_list(),
                "normalized_sha256": post.normalized_sha256,
                "tourism_label": post.tourism_label,
                "sample_frame": post.sample_frame,
            }
            for post in sorted(posts, key=lambda item: item.identity)
        ]
    )
    return LegacyReferenceInput(
        posts=tuple(sorted(posts, key=lambda item: item.identity)),
        csv_sha256=csv_hash,
        manifest_sha256=file_sha256(manifest_file),
        input_member_sha256=input_member_hash,
        sample_run_id=sample_run_id,
        candidate_build_id=candidate_build_id,
        candidate_build_sha256=str(candidate_manifest.get("corpus_manifest_sha256") or ""),
        label_guide_id=expected_label_guide_id,
        normalization_rule_id=normalization_rule_id,
    )


def load_candidate_population(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
) -> tuple[ReferencePost, ...]:
    """从同一冻结候选人口读取候补队列所需无标签投影。

    Args:
        derived_db: 只读派生数据库。
        candidate_build_id: 现有700条所属候选构建身份。

    Returns:
        按稳定源身份排序、不含平台和标签的完整候选人口。

    Raises:
        ReferenceDatasetError: 构建未 finalized、不完整、连接不全或身份重复。
    """

    connection = readonly_connection(derived_db)
    try:
        build = connection.execute(
            """
            SELECT status, is_complete_corpus, processed_post_count,
                   corpus_manifest_sha256
            FROM text_candidate_builds WHERE build_id = ?
            """,
            (candidate_build_id,),
        ).fetchone()
        if build is None or build["status"] != "finalized" or not int(build["is_complete_corpus"]):
            raise ReferenceDatasetError("replacement_candidate_build_not_finalized")
        rows = connection.execute(
            """
            SELECT c.task_id, c.source_post_id, c.source_version,
                   c.structure_status, r.normalized_model_text
            FROM text_candidate_corpus_members AS c
            JOIN text_deterministic_results AS r ON r.task_id = c.task_id
            WHERE c.build_id = ?
            ORDER BY c.source_post_id, c.source_version
            """,
            (candidate_build_id,),
        ).fetchall()
        processed_post_count = int(build["processed_post_count"])
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise ReferenceDatasetError("replacement_candidate_database_contract_invalid") from exc
    finally:
        connection.close()
    if len(rows) != processed_post_count:
        raise ReferenceDatasetError("replacement_candidate_population_incomplete")
    if any(
        row["task_id"] is None
        or not str(row["task_id"]).strip()
        or row["normalized_model_text"] is None
        or not str(row["normalized_model_text"])
        for row in rows
    ):
        raise ReferenceDatasetError("replacement_candidate_required_value_null")
    try:
        posts = tuple(
            ReferencePost(
                identity=SourceIdentity(
                    int(row["source_post_id"]), int(row["source_version"])
                ),
                task_id=str(row["task_id"]),
                normalized_model_text=str(row["normalized_model_text"]),
                tourism_label=None,
                sample_frame=None,
                selection_reason_code="dedup_replacement_queue",
                selection_rank=0,
                inclusion_probability=None,
                analysis_weight=None,
                evidence_origin="supplemental_annotation",
                structure_usable=str(row["structure_status"]) == "usable",
            )
            for row in rows
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceDatasetError("replacement_candidate_database_value_invalid") from exc
    if len({post.identity for post in posts}) != len(posts):
        raise ReferenceDatasetError("replacement_candidate_identity_not_unique")
    if len({post.task_id for post in posts}) != len(posts):
        raise ReferenceDatasetError("replacement_candidate_task_identity_not_unique")
    return posts


def _csv_bytes(fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    """在内存中生成字段顺序冻结的 UTF-8 CSV 字节。

    Args:
        fields: 唯一允许的字段顺序。
        rows: 待写数据行。

    Returns:
        使用 ``\n`` 换行的 UTF-8 CSV。
    """

    import io

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def write_immutable_bytes(path: str | Path, content: bytes) -> ArtifactWriteResult:
    """排他写入不可变 artifact，或幂等复用相同内容。

    Args:
        path: 目标文件。
        content: 完整目标字节。

    Returns:
        内容摘要和复用状态。

    Raises:
        ReferenceDatasetError: 目标已存在但内容不同，或原子发布失败。
    """

    output = Path(path)
    expected_hash = hashlib.sha256(content).hexdigest()
    if output.exists():
        if file_sha256(output) != expected_hash:
            raise ReferenceDatasetError("artifact_identity_content_conflict")
        return ArtifactWriteResult(expected_hash, True)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", dir=output.parent
        )
    except OSError as exc:
        raise ReferenceDatasetError("artifact_write_failed") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, output)
        except FileExistsError:
            if file_sha256(output) != expected_hash:
                raise ReferenceDatasetError("artifact_publish_race_conflict")
            return ArtifactWriteResult(expected_hash, True)
        except OSError as exc:
            raise ReferenceDatasetError("artifact_publish_failed") from exc
    except ReferenceDatasetError:
        raise
    except OSError as exc:
        raise ReferenceDatasetError("artifact_write_failed") from exc
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return ArtifactWriteResult(expected_hash, False)


def write_candidate_review_artifacts(
    computation: CandidateComputation,
    posts: Sequence[ReferencePost],
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    input_hashes: Mapping[str, str],
    reviewed_max_queue_rank: int | None = None,
) -> ArtifactPairResult:
    """写出近重复人工复核 CSV 及其不可变谱系 manifest。

    Args:
        computation: 完整全对候选计算结果。
        posts: 用于提供候选两端规范化文本的输入成员。
        csv_path: 私有人工复核 CSV 目标。
        manifest_path: 与复核 CSV 绑定的 manifest 目标。
        input_hashes: 本轮输入 artifact 的稳定名称到 SHA-256 映射。
        reviewed_max_queue_rank: 候补阶段完成联合复核的冻结前缀末秩；初始700条
            阶段为 ``None``。

    Returns:
        配对 artifact 的哈希、计数和幂等复用状态。
    """

    by_identity = {post.identity: post for post in posts}
    rows = [
        {
            "evidence_id": canonical_sha256(item.pair.as_list())[:32],
            "left_source_post_id": item.pair.left.source_post_id,
            "left_source_version": item.pair.left.source_version,
            "right_source_post_id": item.pair.right.source_post_id,
            "right_source_version": item.pair.right.source_version,
            "similarity": format(item.similarity, ".17g"),
            "exact_normalized_hash_match": str(item.exact_normalized_hash_match).lower(),
            "candidate_reason_code": item.candidate_reason_code,
            "left_normalized_model_text": by_identity[item.pair.left].normalized_model_text,
            "right_normalized_model_text": by_identity[item.pair.right].normalized_model_text,
            "decision": "",
            "review_status": "pending",
            "evidence_origin": "threshold_candidate",
        }
        for item in computation.candidates
        if not item.exact_normalized_hash_match
    ]
    csv_content = _csv_bytes(DUPLICATE_DECISION_FIELDS, rows)
    csv_result = write_immutable_bytes(csv_path, csv_content)
    review_candidate_sha256 = canonical_sha256(
        [
            {
                "pair": [
                    [row["left_source_post_id"], row["left_source_version"]],
                    [row["right_source_post_id"], row["right_source_version"]],
                ],
                "similarity": row["similarity"],
                "exact": False,
                "reason_code": row["candidate_reason_code"],
            }
            for row in rows
        ]
    )
    manifest = {
        "artifact_contract": "final-reference-duplicate-review",
        "status": "pending_human_review" if rows else "complete",
        "csv": {
            "logical_name": Path(csv_path).name,
            "sha256": csv_result.sha256,
            "row_count": len(rows),
        },
        "input_hashes": {
            **dict(sorted(input_hashes.items())),
            "member_sha256": computation.input_member_sha256,
        },
        "candidate_hash": computation.candidate_sha256,
        "review_candidate_sha256": review_candidate_sha256,
        "examined_pair_count": computation.examined_pair_count,
        "expected_pair_count": computation.expected_pair_count,
        "exact_duplicate_pair_count": sum(
            candidate.exact_normalized_hash_match for candidate in computation.candidates
        ),
        "near_candidate_pair_count": len(rows),
        "algorithm": computation.algorithm_identity,
        "input_members": [
            {
                "identity": post.identity.as_list(),
                "normalized_sha256": post.normalized_sha256,
            }
            for post in sorted(posts, key=lambda item: item.identity)
        ],
        "automatic_candidate_pairs": [
            candidate.pair.as_list() for candidate in computation.candidates
        ],
        "reviewed_max_queue_rank": reviewed_max_queue_rank,
    }
    manifest_result = write_immutable_bytes(
        manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    return ArtifactPairResult(
        csv_sha256=csv_result.sha256,
        manifest_sha256=manifest_result.sha256,
        reused=csv_result.reused and manifest_result.reused,
        row_count=len(rows),
        status=str(manifest["status"]),
    )


def _validate_manual_low_similarity_rows(
    rows: Sequence[Mapping[str, str]], manifest: Mapping[str, Any]
) -> None:
    """把人工补入的低阈值边绑定到冻结输入成员。

    Args:
        rows: 完成 CSV 中 ``manual_low_similarity`` 行。
        manifest: 生成原候选任务时冻结的 pending/finalized manifest。

    Raises:
        ReferenceDatasetError: 输入成员摘要、端点正文、阈值角色或候选身份不符。
    """

    input_members = manifest.get("input_members")
    automatic_pairs = manifest.get("automatic_candidate_pairs")
    input_hashes = manifest.get("input_hashes")
    if (
        not isinstance(input_members, list)
        or not isinstance(automatic_pairs, list)
        or not isinstance(input_hashes, Mapping)
        or canonical_sha256(input_members) != input_hashes.get("member_sha256")
    ):
        raise ReferenceDatasetError("duplicate_manual_input_lineage_invalid")
    member_hashes: dict[SourceIdentity, str] = {}
    try:
        for item in input_members:
            if not isinstance(item, Mapping):
                raise ValueError
            identity_value = item["identity"]
            if not isinstance(identity_value, list) or len(identity_value) != 2:
                raise ValueError
            identity = SourceIdentity(int(identity_value[0]), int(identity_value[1]))
            normalized_hash = str(item["normalized_sha256"])
            if len(normalized_hash) != 64 or identity in member_hashes:
                raise ValueError
            member_hashes[identity] = normalized_hash
        candidate_pairs = {
            PairIdentity.of(
                SourceIdentity(int(pair[0][0]), int(pair[0][1])),
                SourceIdentity(int(pair[1][0]), int(pair[1][1])),
            )
            for pair in automatic_pairs
        }
    except (IndexError, KeyError, TypeError, ValueError, ReferenceDatasetError) as exc:
        raise ReferenceDatasetError("duplicate_manual_input_lineage_invalid") from exc
    for row in rows:
        try:
            left = SourceIdentity(
                int(row["left_source_post_id"]), int(row["left_source_version"])
            )
            right = SourceIdentity(
                int(row["right_source_post_id"]), int(row["right_source_version"])
            )
            similarity = float(row["similarity"])
        except (KeyError, TypeError, ValueError, ReferenceDatasetError) as exc:
            raise ReferenceDatasetError("duplicate_manual_edge_invalid") from exc
        pair = PairIdentity.of(left, right)
        if (
            left >= right
            or pair in candidate_pairs
            or not math.isfinite(similarity)
            or not 0.0 <= similarity < 0.80
            or row["exact_normalized_hash_match"].strip().lower() != "false"
            or row["candidate_reason_code"].strip() != "manual_low_similarity"
            or row["decision"].strip() != "duplicate"
            or member_hashes.get(left)
            != text_sha256(row["left_normalized_model_text"])
            or member_hashes.get(right)
            != text_sha256(row["right_normalized_model_text"])
        ):
            raise ReferenceDatasetError("duplicate_manual_edge_invalid")


def seal_duplicate_decision_artifact(
    completed_csv: str | Path,
    pending_manifest: str | Path,
    output_manifest: str | Path,
) -> ArtifactWriteResult:
    """校验人工完成副本并封存 finalized 重复决定 manifest。

    待处理 CSV 是不可变任务输入。人工应复制为新的完成 CSV 后填写决定；本函数
    证明所有原阈值候选仍逐字存在，并允许额外追加
    ``manual_low_similarity`` 行，然后只写新的 finalized manifest。

    Args:
        completed_csv: 人工完成的重复决定 CSV 副本。
        pending_manifest: 生成待处理 CSV 时写出的不可变 manifest。
        output_manifest: 新 finalized manifest 目标；不得与 pending manifest 相同。

    Returns:
        finalized manifest 的哈希和幂等复用状态。

    Raises:
        ReferenceDatasetError: 待处理谱系、候选字段、人工状态或内容哈希不一致。
    """

    pending_path = Path(pending_manifest)
    output_path = Path(output_manifest)
    if pending_path.resolve() == output_path.resolve():
        raise ReferenceDatasetError("duplicate_decision_manifest_must_be_append_only")
    pending = _read_json_object(pending_path, "duplicate_pending_manifest_unreadable")
    if (
        pending.get("artifact_contract") != "final-reference-duplicate-review"
        or pending.get("status") not in {"pending_human_review", "complete"}
    ):
        raise ReferenceDatasetError("duplicate_pending_manifest_invalid")
    decisions = load_duplicate_decisions(completed_csv)
    try:
        with Path(completed_csv).open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("duplicate_completed_csv_unreadable") from exc
    threshold_rows = [row for row in rows if row["evidence_origin"].strip() == "threshold_candidate"]
    manual_rows = [
        row for row in rows if row["evidence_origin"].strip() == "manual_low_similarity"
    ]
    csv_section = pending.get("csv")
    if not isinstance(csv_section, Mapping):
        raise ReferenceDatasetError("duplicate_pending_manifest_invalid")
    pending_csv_path = pending_path.parent / str(csv_section.get("logical_name") or "")
    try:
        if file_sha256(pending_csv_path) != csv_section.get("sha256"):
            raise ReferenceDatasetError("duplicate_pending_csv_hash_mismatch")
        with pending_csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            pending_reader = csv.DictReader(stream)
            if tuple(pending_reader.fieldnames or ()) != DUPLICATE_DECISION_FIELDS:
                raise ReferenceDatasetError("duplicate_pending_csv_field_contract_mismatch")
            pending_rows = list(pending_reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("duplicate_pending_csv_unreadable") from exc
    immutable_fields = tuple(
        field
        for field in DUPLICATE_DECISION_FIELDS
        if field not in {"decision", "review_status"}
    )
    if len(threshold_rows) != len(pending_rows) or any(
        any(completed[field] != original[field] for field in immutable_fields)
        for original, completed in zip(pending_rows, threshold_rows, strict=True)
    ):
        raise ReferenceDatasetError("duplicate_completed_candidates_changed")
    review_hash = canonical_sha256(
        [
            {
                "pair": [
                    [int(row["left_source_post_id"]), int(row["left_source_version"])],
                    [int(row["right_source_post_id"]), int(row["right_source_version"])],
                ],
                "similarity": row["similarity"],
                "exact": row["exact_normalized_hash_match"].strip().lower() == "true",
                "reason_code": row["candidate_reason_code"],
            }
            for row in threshold_rows
        ]
    )
    if (
        len(threshold_rows) != csv_section.get("row_count")
        or review_hash != pending.get("review_candidate_sha256")
    ):
        raise ReferenceDatasetError("duplicate_completed_candidates_changed")
    _validate_manual_low_similarity_rows(manual_rows, pending)
    decision_hash = canonical_sha256(
        [
            {
                "evidence_id": decision.evidence_id,
                "pair": decision.pair.as_list(),
                "decision": decision.decision,
                "evidence_origin": decision.evidence_origin,
            }
            for decision in decisions
        ]
    )
    completed_hash = file_sha256(completed_csv)
    finalized = {
        **pending,
        "status": "finalized",
        "csv": {
            "logical_name": Path(completed_csv).name,
            "sha256": completed_hash,
            "row_count": len(rows),
        },
        "threshold_candidate_count": len(threshold_rows),
        "manual_low_similarity_count": len(rows) - len(threshold_rows),
        "decision_sha256": decision_hash,
        "pending_manifest_sha256": file_sha256(pending_path),
    }
    return write_immutable_bytes(
        output_path,
        json.dumps(finalized, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )


def validate_duplicate_decision_artifact(
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    expected_input_member_sha256: str | None = None,
    expected_candidate_sha256: str | None = None,
    expected_input_hashes: Mapping[str, str] | None = None,
    expected_reviewed_max_queue_rank: int | None = None,
) -> tuple[DuplicateDecision, ...]:
    """验证 finalized 重复决定 CSV 与唯一配对 manifest。

    Args:
        csv_path: 人工完成决定 CSV。
        manifest_path: 由 ``seal_duplicate_decision_artifact`` 生成的 manifest。
        expected_input_member_sha256: 当前重算联合输入成员摘要；提供时必须一致。
        expected_candidate_sha256: 当前重算候选摘要；提供时必须一致。
        expected_input_hashes: 当前阶段要求逐项一致的上游 artifact 摘要。
        expected_reviewed_max_queue_rank: 候补阶段当前复核前缀末秩。

    Returns:
        已由人工证据解析器验证的唯一决定元组。

    Raises:
        ReferenceDatasetError: 状态、文件绑定或决定摘要不一致。
    """

    manifest = _read_json_object(Path(manifest_path), "duplicate_decision_manifest_unreadable")
    csv_section = manifest.get("csv")
    if (
        manifest.get("artifact_contract") != "final-reference-duplicate-review"
        or manifest.get("status") != "finalized"
        or not isinstance(csv_section, Mapping)
        or csv_section.get("logical_name") != Path(csv_path).name
        or csv_section.get("sha256") != file_sha256(csv_path)
    ):
        raise ReferenceDatasetError("duplicate_decision_artifact_not_finalized")
    decisions = load_duplicate_decisions(csv_path)
    if csv_section.get("row_count") != len(decisions):
        raise ReferenceDatasetError("duplicate_decision_row_count_mismatch")
    try:
        with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
            raw_rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("duplicate_decision_csv_unreadable") from exc
    _validate_manual_low_similarity_rows(
        [
            row
            for row in raw_rows
            if row["evidence_origin"].strip() == "manual_low_similarity"
        ],
        manifest,
    )
    decision_hash = canonical_sha256(
        [
            {
                "evidence_id": decision.evidence_id,
                "pair": decision.pair.as_list(),
                "decision": decision.decision,
                "evidence_origin": decision.evidence_origin,
            }
            for decision in decisions
        ]
    )
    if manifest.get("decision_sha256") != decision_hash:
        raise ReferenceDatasetError("duplicate_decision_hash_mismatch")
    input_hashes = manifest.get("input_hashes")
    if not isinstance(input_hashes, Mapping):
        raise ReferenceDatasetError("duplicate_decision_input_hashes_missing")
    if (
        expected_input_member_sha256 is not None
        and input_hashes.get("member_sha256") != expected_input_member_sha256
    ):
        raise ReferenceDatasetError("duplicate_decision_input_member_mismatch")
    if (
        expected_candidate_sha256 is not None
        and manifest.get("candidate_hash") != expected_candidate_sha256
    ):
        raise ReferenceDatasetError("duplicate_decision_candidate_hash_mismatch")
    for key, expected in (expected_input_hashes or {}).items():
        if input_hashes.get(key) != expected:
            raise ReferenceDatasetError("duplicate_decision_upstream_hash_mismatch")
    if (
        expected_reviewed_max_queue_rank is not None
        and manifest.get("reviewed_max_queue_rank")
        != expected_reviewed_max_queue_rank
    ):
        raise ReferenceDatasetError("duplicate_decision_reviewed_prefix_mismatch")
    return decisions


def write_label_conflict_review_artifacts(
    conflicts: Sequence[DuplicateLabelConflict],
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    duplicate_decision_sha256: str,
    component_member_sha256: str,
    label_guide_id: str,
) -> ArtifactPairResult:
    """写出重复分量标签冲突待处理 CSV 与配对 manifest。

    Args:
        conflicts: 不得由程序自动选择标签的冲突分量。
        csv_path: 私有待处理 CSV。
        manifest_path: 配对 pending manifest。
        duplicate_decision_sha256: 形成分量的重复决定摘要。
        component_member_sha256: 全部输入成员与分量摘要。
        label_guide_id: 生成人工任务所依据的标签手册身份。

    Returns:
        待处理 artifact 的哈希、计数和状态。
    """

    rows = []
    for conflict in conflicts:
        members = conflict.members
        rows.append(
            {
                "component_id": conflict.component_id,
                "member_source_identities_json": json.dumps(
                    [member.identity.as_list() for member in members],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "member_labels_json": json.dumps(
                    [member.tourism_label for member in members],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "member_normalized_model_texts_json": json.dumps(
                    [member.normalized_model_text for member in members],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "tourism_label": "",
                "review_status": "pending",
                "evidence_id": canonical_sha256(
                    ["duplicate_label_conflict", conflict.component_id]
                )[:32],
            }
        )
    csv_result = write_immutable_bytes(
        csv_path, _csv_bytes(LABEL_RESOLUTION_FIELDS, rows)
    )
    task_hash = canonical_sha256(
        [
            {
                key: row[key]
                for key in LABEL_RESOLUTION_FIELDS
                if key not in {"tourism_label", "review_status"}
            }
            for row in rows
        ]
    )
    manifest = {
        "artifact_contract": "final-reference-label-conflict-review",
        "status": "pending_human_review" if rows else "complete",
        "csv": {
            "logical_name": Path(csv_path).name,
            "sha256": csv_result.sha256,
            "row_count": len(rows),
        },
        "duplicate_decision_sha256": duplicate_decision_sha256,
        "component_member_sha256": component_member_sha256,
        "label_guide_id": label_guide_id,
        "task_sha256": task_hash,
    }
    manifest_result = write_immutable_bytes(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    return ArtifactPairResult(
        csv_result.sha256,
        manifest_result.sha256,
        csv_result.reused and manifest_result.reused,
        len(rows),
        str(manifest["status"]),
    )


def seal_label_resolution_artifact(
    completed_csv: str | Path,
    pending_manifest: str | Path,
    output_manifest: str | Path,
) -> ArtifactWriteResult:
    """校验冲突标签完成副本并追加 finalized manifest。

    Args:
        completed_csv: 从待处理任务复制并填写的完成 CSV。
        pending_manifest: 冲突任务配对 manifest。
        output_manifest: 新 finalized manifest；不得覆盖 pending 文件。

    Returns:
        finalized manifest 的哈希和复用状态。

    Raises:
        ReferenceDatasetError: 任务内容改变、标签未最终二分或谱系无效。
    """

    pending_path = Path(pending_manifest)
    if pending_path.resolve() == Path(output_manifest).resolve():
        raise ReferenceDatasetError("label_resolution_manifest_must_be_append_only")
    pending = _read_json_object(pending_path, "label_resolution_pending_manifest_unreadable")
    if (
        pending.get("artifact_contract") != "final-reference-label-conflict-review"
        or pending.get("status") not in {"pending_human_review", "complete"}
    ):
        raise ReferenceDatasetError("label_resolution_pending_manifest_invalid")
    csv_section = pending.get("csv")
    if not isinstance(csv_section, Mapping):
        raise ReferenceDatasetError("label_resolution_pending_manifest_invalid")
    pending_csv = pending_path.parent / str(csv_section.get("logical_name") or "")
    try:
        if file_sha256(pending_csv) != csv_section.get("sha256"):
            raise ReferenceDatasetError("label_resolution_pending_csv_hash_mismatch")
        with pending_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            pending_reader = csv.DictReader(stream)
            if tuple(pending_reader.fieldnames or ()) != LABEL_RESOLUTION_FIELDS:
                raise ReferenceDatasetError("duplicate_label_resolution_field_mismatch")
            pending_rows = list(pending_reader)
        with Path(completed_csv).open("r", encoding="utf-8-sig", newline="") as stream:
            completed_reader = csv.DictReader(stream)
            if tuple(completed_reader.fieldnames or ()) != LABEL_RESOLUTION_FIELDS:
                raise ReferenceDatasetError("duplicate_label_resolution_field_mismatch")
            completed_rows = list(completed_reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("duplicate_label_resolution_unreadable") from exc
    _validate_complete_csv_rows(
        pending_rows,
        LABEL_RESOLUTION_FIELDS,
        "duplicate_label_resolution_row_structure_invalid",
    )
    _validate_complete_csv_rows(
        completed_rows,
        LABEL_RESOLUTION_FIELDS,
        "duplicate_label_resolution_row_structure_invalid",
    )
    immutable = tuple(
        field
        for field in LABEL_RESOLUTION_FIELDS
        if field not in {"tourism_label", "review_status"}
    )
    if len(pending_rows) != len(completed_rows) or any(
        any(original[field] != completed[field] for field in immutable)
        for original, completed in zip(pending_rows, completed_rows, strict=True)
    ):
        raise ReferenceDatasetError("label_resolution_task_content_changed")
    resolutions = load_label_resolutions(completed_csv)
    evidence_payload = [
        {
            "component_id": row["component_id"],
            "tourism_label": row["tourism_label"].strip(),
            "evidence_id": row["evidence_id"].strip(),
        }
        for row in completed_rows
    ]
    evidence_hash = canonical_sha256(evidence_payload)
    if len(resolutions) != len(completed_rows):
        raise ReferenceDatasetError("duplicate_label_resolution_identity_invalid")
    finalized = {
        **pending,
        "status": "finalized",
        "csv": {
            "logical_name": Path(completed_csv).name,
            "sha256": file_sha256(completed_csv),
            "row_count": len(completed_rows),
        },
        "evidence_sha256": evidence_hash,
        "pending_manifest_sha256": file_sha256(pending_path),
    }
    return write_immutable_bytes(
        output_manifest,
        json.dumps(finalized, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )


def validate_label_resolution_artifact(
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    expected_duplicate_decision_sha256: str,
    expected_component_member_sha256: str,
    expected_label_guide_id: str,
) -> LabelResolutionArtifact:
    """验证标签冲突确认 CSV 与 finalized manifest 的当前谱系绑定。

    Args:
        csv_path: 冲突标签完成 CSV；无冲突时为只有表头的空文件。
        manifest_path: 唯一配对 finalized manifest。
        expected_duplicate_decision_sha256: 当前重复决定摘要。
        expected_component_member_sha256: 当前成员分量摘要。
        expected_label_guide_id: 当前冻结标签手册身份。

    Returns:
        最终确认映射及证据/manifest 摘要。

    Raises:
        ReferenceDatasetError: 状态、哈希、上游谱系或人工证据不一致。
    """

    manifest_file = Path(manifest_path)
    manifest = _read_json_object(
        manifest_file, "label_resolution_manifest_unreadable"
    )
    csv_section = manifest.get("csv")
    if (
        manifest.get("artifact_contract") != "final-reference-label-conflict-review"
        or manifest.get("status") != "finalized"
        or not isinstance(csv_section, Mapping)
        or csv_section.get("logical_name") != Path(csv_path).name
        or csv_section.get("sha256") != file_sha256(csv_path)
        or manifest.get("duplicate_decision_sha256")
        != expected_duplicate_decision_sha256
        or manifest.get("component_member_sha256")
        != expected_component_member_sha256
        or manifest.get("label_guide_id") != expected_label_guide_id
    ):
        raise ReferenceDatasetError("label_resolution_artifact_not_finalized")
    resolutions = load_label_resolutions(csv_path)
    try:
        with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("duplicate_label_resolution_unreadable") from exc
    if csv_section.get("row_count") != len(rows):
        raise ReferenceDatasetError("label_resolution_row_count_mismatch")
    task_hash = canonical_sha256(
        [
            {
                key: row[key]
                for key in LABEL_RESOLUTION_FIELDS
                if key not in {"tourism_label", "review_status"}
            }
            for row in rows
        ]
    )
    if manifest.get("task_sha256") != task_hash:
        raise ReferenceDatasetError("label_resolution_task_hash_mismatch")
    evidence_hash = canonical_sha256(
        [
            {
                "component_id": row["component_id"],
                "tourism_label": row["tourism_label"].strip(),
                "evidence_id": row["evidence_id"].strip(),
            }
            for row in rows
        ]
    )
    if manifest.get("evidence_sha256") != evidence_hash:
        raise ReferenceDatasetError("label_resolution_evidence_hash_mismatch")
    return LabelResolutionArtifact(
        resolutions,
        evidence_hash,
        file_sha256(manifest_file),
    )


def write_replacement_queue_artifacts(
    queue: ReplacementQueue,
    accepted: Sequence[ReferencePost],
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    candidate_build_id: str,
    candidate_build_sha256: str,
    duplicate_decision_sha256: str,
) -> ArtifactPairResult:
    """写出读取候补标签前冻结的全局候补队列及 manifest。

    Args:
        queue: 平台无关、标签已清空的冻结队列。
        accepted: 初始去重并解决标签冲突后的既有代表。
        csv_path: 私有候补队列 CSV。
        manifest_path: 配对 manifest。
        candidate_build_id: 同一冻结候选人口身份。
        candidate_build_sha256: 候选人口摘要。
        duplicate_decision_sha256: 已确认重复关系摘要。

    Returns:
        配对 artifact 的哈希、计数和复用状态。
    """

    rows = [
        {
            "queue_rank": item.queue_rank,
            "source_post_id": item.post.identity.source_post_id,
            "source_version": item.post.identity.source_version,
            "task_id": item.post.task_id,
            "normalized_model_text": item.post.normalized_model_text,
            "structure_status": "usable" if item.post.structure_usable else "unusable",
        }
        for item in queue.items
    ]
    accepted_probability_count = sum(
        post.sample_frame == "probability" for post in accepted
    )
    accepted_targeted_count = sum(post.sample_frame == "targeted" for post in accepted)
    csv_result = write_immutable_bytes(csv_path, _csv_bytes(REPLACEMENT_QUEUE_FIELDS, rows))
    manifest = {
        "artifact_contract": "final-reference-replacement-queue",
        "status": "frozen",
        "csv": {"logical_name": Path(csv_path).name, "sha256": csv_result.sha256, "row_count": len(rows)},
        "candidate_build_id": candidate_build_id,
        "candidate_build_sha256": candidate_build_sha256,
        "duplicate_decision_sha256": duplicate_decision_sha256,
        "random_seed": queue.random_seed,
        "selection_scope": "global",
        "platform_quota": False,
        "platform_sort": False,
        "labels_read_before_freeze": False,
        "excluded_identity_count": queue.excluded_identity_count,
        "queue_sha256": queue.queue_sha256,
        "accepted_count": len(accepted),
        "accepted_probability_count": accepted_probability_count,
        "accepted_targeted_count": accepted_targeted_count,
        "probability_gap": FINAL_PROBABILITY_COUNT - accepted_probability_count,
        "targeted_gap": FINAL_TARGETED_COUNT - accepted_targeted_count,
    }
    manifest_result = write_immutable_bytes(
        manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    return ArtifactPairResult(
        csv_result.sha256,
        manifest_result.sha256,
        csv_result.reused and manifest_result.reused,
        len(rows),
        "frozen",
    )


def write_supplemental_label_tasks(
    plan: ReplacementAnnotationPlan,
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    queue_sha256: str,
    label_guide_id: str,
) -> ArtifactPairResult:
    """写出完成重复检查后的不可变补充标注任务。

    Args:
        plan: 未读取旅游标签的候补任务计划。
        csv_path: 待处理补充标签 CSV。
        manifest_path: 配对 pending manifest。
        queue_sha256: 冻结候补队列摘要。
        label_guide_id: 本轮人工标注所依据的标签手册身份。

    Returns:
        待处理 CSV/manifest 的哈希、行数和状态。
    """

    rows = [
        {
            "task_id": item.post.task_id,
            "source_post_id": item.post.identity.source_post_id,
            "source_version": item.post.identity.source_version,
            "selection_rank": item.queue_rank,
            "normalized_model_text": item.post.normalized_model_text,
            "tourism_label": label,
            "review_status": "finalized",
        }
        for item, label in plan.carried_labels
    ] + [
        {
            "task_id": item.post.task_id,
            "source_post_id": item.post.identity.source_post_id,
            "source_version": item.post.identity.source_version,
            "selection_rank": item.queue_rank,
            "normalized_model_text": item.post.normalized_model_text,
            "tourism_label": "",
            "review_status": "pending",
        }
        for item in plan.tasks
    ]
    csv_result = write_immutable_bytes(
        csv_path, _csv_bytes(SUPPLEMENTAL_LABEL_FIELDS, rows)
    )
    manifest = {
        "artifact_contract": "final-reference-supplemental-label-tasks",
        "status": "pending_human_review" if plan.tasks else "complete",
        "csv": {
            "logical_name": Path(csv_path).name,
            "sha256": csv_result.sha256,
            "row_count": len(rows),
        },
        "required_count": plan.required_count,
        "reserve_count": plan.reserve_count,
        "task_sha256": plan.task_sha256,
        "replacement_queue_sha256": queue_sha256,
        "duplicate_decision_sha256": plan.duplicate_decision_sha256,
        "reviewed_max_queue_rank": plan.reviewed_max_queue_rank,
        "label_guide_id": label_guide_id,
        "global_queue_frozen_before_any_label": True,
        "binary_class_value_used_for_queue_order_or_frame_assignment": False,
        "prior_terminal_status_used_for_eligibility_or_extension": bool(
            plan.carried_labels
        ),
    }
    manifest_result = write_immutable_bytes(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    return ArtifactPairResult(
        csv_result.sha256,
        manifest_result.sha256,
        csv_result.reused and manifest_result.reused,
        len(rows),
        str(manifest["status"]),
    )


def seal_supplemental_label_artifact(
    completed_csv: str | Path,
    pending_manifest: str | Path,
    output_manifest: str | Path,
) -> ArtifactWriteResult:
    """校验补充标签完成副本并追加 finalized manifest。

    Args:
        completed_csv: 从不可变待处理任务复制并人工填写的完成 CSV。
        pending_manifest: 待处理任务的配对 manifest。
        output_manifest: 新 finalized manifest 目标。

    Returns:
        finalized manifest 的哈希和幂等复用状态。

    Raises:
        ReferenceDatasetError: 任务身份或文本改变、标签未形成允许的终止状态、
            输入不是合法 pending artifact，或试图覆盖 pending manifest。
    """

    pending_path = Path(pending_manifest)
    output_path = Path(output_manifest)
    if pending_path.resolve() == output_path.resolve():
        raise ReferenceDatasetError("supplemental_manifest_must_be_append_only")
    pending = _read_json_object(pending_path, "supplemental_pending_manifest_unreadable")
    if (
        pending.get("artifact_contract") != "final-reference-supplemental-label-tasks"
        or pending.get("status") not in {"pending_human_review", "complete"}
    ):
        raise ReferenceDatasetError("supplemental_pending_manifest_invalid")
    try:
        with Path(completed_csv).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != SUPPLEMENTAL_LABEL_FIELDS:
                raise ReferenceDatasetError("supplemental_label_field_contract_mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("supplemental_label_artifact_unreadable") from exc
    _validate_complete_csv_rows(
        rows,
        SUPPLEMENTAL_LABEL_FIELDS,
        "supplemental_label_row_structure_invalid",
    )
    csv_section = pending.get("csv")
    if not isinstance(csv_section, Mapping) or csv_section.get("row_count") != len(rows):
        raise ReferenceDatasetError("supplemental_label_task_count_changed")
    pending_csv_path = pending_path.parent / str(csv_section.get("logical_name") or "")
    try:
        if file_sha256(pending_csv_path) != csv_section.get("sha256"):
            raise ReferenceDatasetError("supplemental_pending_csv_hash_mismatch")
        with pending_csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            pending_rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceDatasetError("supplemental_pending_csv_unreadable") from exc
    _validate_complete_csv_rows(
        pending_rows,
        SUPPLEMENTAL_LABEL_FIELDS,
        "supplemental_label_row_structure_invalid",
    )
    immutable_fields = (
        "task_id",
        "source_post_id",
        "source_version",
        "selection_rank",
        "normalized_model_text",
    )
    if len(pending_rows) != len(rows) or any(
        any(completed[field] != original[field] for field in immutable_fields)
        for original, completed in zip(pending_rows, rows, strict=True)
    ):
        raise ReferenceDatasetError("supplemental_label_task_content_changed")
    if any(
        original["review_status"].strip() == "finalized"
        and (
            completed["review_status"] != original["review_status"]
            or completed["tourism_label"] != original["tourism_label"]
        )
        for original, completed in zip(pending_rows, rows, strict=True)
    ):
        raise ReferenceDatasetError("supplemental_carried_label_changed")
    identities: set[tuple[int, int]] = set()
    task_payload: list[dict[str, Any]] = []
    for row in rows:
        try:
            identity = (int(row["source_post_id"]), int(row["source_version"]))
            selection_rank = int(row["selection_rank"])
        except ValueError as exc:
            raise ReferenceDatasetError("supplemental_label_identity_invalid") from exc
        if selection_rank <= 0 or str(selection_rank) != row["selection_rank"].strip():
            raise ReferenceDatasetError("supplemental_label_selection_rank_invalid")
        if identity in identities:
            raise ReferenceDatasetError("supplemental_label_identity_not_unique")
        if (
            row["tourism_label"].strip() not in (set(ALLOWED_LABELS) | {"uncertain"})
            or row["review_status"].strip() != "finalized"
        ):
            raise ReferenceDatasetError("supplemental_label_not_final")
        identities.add(identity)
        task_payload.append(
            {
                "identity": [identity[0], identity[1]],
                "task_id": row["task_id"],
                "queue_rank": selection_rank,
                "normalized_sha256": hashlib.sha256(
                    row["normalized_model_text"].encode("utf-8")
                ).hexdigest(),
            }
        )
    completed_task_hash = canonical_sha256(task_payload)
    if completed_task_hash != pending.get("task_sha256"):
        raise ReferenceDatasetError("supplemental_label_task_hash_mismatch")
    finalized = {
        **pending,
        "artifact_contract": "final-reference-supplemental-labels",
        "status": "finalized",
        "csv_sha256": file_sha256(completed_csv),
        "logical_name": Path(completed_csv).name,
        "row_count": len(rows),
        "pending_manifest_sha256": file_sha256(pending_path),
    }
    return write_immutable_bytes(
        output_path,
        json.dumps(finalized, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )


def _final_member_hash(rows: Sequence[FinalReferenceRow]) -> str:
    """计算最终成员、标签、样本框和分量的稳定摘要。

    Args:
        rows: 最终参考集行。

    Returns:
        按源身份排序的成员 SHA-256。
    """

    return canonical_sha256(
        [
            {
                "identity": row.post.identity.as_list(),
                "task_id": row.post.task_id,
                "normalized_sha256": row.post.normalized_sha256,
                "tourism_label": row.post.tourism_label,
                "sample_frame": row.post.sample_frame,
                "selection_reason_code": row.post.selection_reason_code,
                "selection_rank": row.post.selection_rank,
                "inclusion_probability": row.post.inclusion_probability,
                "analysis_weight": row.post.analysis_weight,
                "evidence_origin": row.post.evidence_origin,
                "duplicate_component_id": row.duplicate_component_id,
            }
            for row in sorted(rows, key=lambda item: item.post.identity)
        ]
    )


def write_final_reference_artifacts(
    selection: ReplacementSelection,
    component_plan: DuplicateComponentPlan,
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    legacy_input: LegacyReferenceInput,
    initial_candidate_computation: CandidateComputation,
    initial_duplicate_decision_manifest_sha256: str,
    replacement_queue_sha256: str,
    label_guide_id: str,
    candidate_build_id: str,
    normalization_rule_id: str,
    supplemental_label_csv_sha256: str,
    supplemental_label_manifest_sha256: str,
    label_resolution_evidence_sha256: str,
    label_resolution_manifest_sha256: str,
    replacement_label_resolution_evidence_sha256: str,
    replacement_label_resolution_manifest_sha256: str,
    replacement_candidate_computation: CandidateComputation,
    replacement_duplicate_decision_manifest_sha256: str,
) -> ArtifactPairResult:
    """封存唯一最终700条 CSV 与 finalized manifest。

    Args:
        selection: 已补足500/200的最终成员和概率有效性状态。
        component_plan: 现有700条的确认重复分量计划。
        csv_path: 唯一最终权威 CSV 目标。
        manifest_path: 与 CSV 唯一配对的 finalized manifest 目标。
        legacy_input: 一次性旧证据迁移输入及哈希。
        initial_candidate_computation: 初始700条冻结全对候选计算身份与摘要。
        initial_duplicate_decision_manifest_sha256: 初始重复决定 finalized manifest 摘要。
        replacement_queue_sha256: 读取标签前冻结的候补队列摘要。
        label_guide_id: 标签手册身份。
        candidate_build_id: 候选构建身份。
        normalization_rule_id: 规范化规则身份。
        supplemental_label_csv_sha256: 补充最终标签 CSV 摘要。
        supplemental_label_manifest_sha256: 补充标签 finalized manifest 摘要。
        label_resolution_evidence_sha256: 标签冲突人工确认摘要。
        label_resolution_manifest_sha256: 标签冲突 finalized manifest 摘要。
        replacement_label_resolution_evidence_sha256: 候补标签冲突确认摘要。
        replacement_label_resolution_manifest_sha256: 候补冲突 finalized manifest 摘要。
        replacement_candidate_computation: 候补前缀联合全对计算身份与摘要。
        replacement_duplicate_decision_manifest_sha256: 候补重复决定 finalized
            manifest 摘要。

    Returns:
        最终配对 artifact 的哈希、700条计数和复用状态。

    Raises:
        ReferenceDatasetError: 最终成员、标签、样本框、确认重复边或权重不满足
            权威契约，或目标身份发生内容冲突。
    """

    if len(selection.final_posts) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceDatasetError("final_reference_row_count_invalid")
    if (
        initial_candidate_computation.examined_pair_count != 244_650
        or initial_candidate_computation.expected_pair_count != 244_650
    ):
        raise ReferenceDatasetError("final_reference_initial_all_pairs_incomplete")
    if (
        len(initial_candidate_computation.candidate_sha256) != 64
        or len(initial_duplicate_decision_manifest_sha256) != 64
    ):
        raise ReferenceDatasetError("final_reference_initial_lineage_hash_invalid")
    if (
        replacement_candidate_computation.examined_pair_count
        != replacement_candidate_computation.expected_pair_count
        or replacement_candidate_computation.algorithm_identity
        != initial_candidate_computation.algorithm_identity
    ):
        raise ReferenceDatasetError("final_reference_replacement_all_pairs_incomplete")
    identities = {post.identity for post in selection.final_posts}
    if len(identities) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceDatasetError("final_reference_identity_not_unique")
    all_confirmed_pairs = tuple(
        sorted(
            set(component_plan.confirmed_edges).union(
                selection.confirmed_duplicate_pairs
            )
        )
    )
    lineage_identities = set(component_plan.component_by_identity).union(
        selection.component_by_identity
    )
    parent = {identity: identity for identity in lineage_identities}

    def find(identity: SourceIdentity) -> SourceIdentity:
        root = parent[identity]
        if root != identity:
            parent[identity] = find(root)
        return parent[identity]

    def union(first: SourceIdentity, second: SourceIdentity) -> None:
        first_root, second_root = find(first), find(second)
        root, child = sorted((first_root, second_root))
        parent[child] = root

    for pair in all_confirmed_pairs:
        if pair.left not in parent or pair.right not in parent:
            raise ReferenceDatasetError("final_reference_duplicate_lineage_incomplete")
        union(pair.left, pair.right)
    grouped: dict[SourceIdentity, list[SourceIdentity]] = {}
    for identity in sorted(lineage_identities):
        grouped.setdefault(find(identity), []).append(identity)
    component_ids: dict[SourceIdentity, str] = {}
    for members in grouped.values():
        component_id = canonical_sha256(
            [identity.as_list() for identity in sorted(members)]
        )[:32]
        for identity in members:
            component_ids[identity] = component_id
    final_rows = tuple(
        FinalReferenceRow(post, component_ids[post.identity])
        for post in selection.final_posts
    )
    if any(row.post.tourism_label not in ALLOWED_LABELS for row in final_rows):
        raise ReferenceDatasetError("final_reference_label_not_final")
    if any(not row.post.structure_usable for row in final_rows):
        raise ReferenceDatasetError("final_reference_structure_not_usable")
    frame_counts = Counter(row.post.sample_frame for row in final_rows)
    if frame_counts != {"probability": FINAL_PROBABILITY_COUNT, "targeted": FINAL_TARGETED_COUNT}:
        raise ReferenceDatasetError("final_reference_frame_count_invalid")
    final_component_ids = [row.duplicate_component_id for row in final_rows]
    if len(set(final_component_ids)) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceDatasetError("final_reference_confirmed_duplicate_present")
    for row in final_rows:
        probability = row.post.inclusion_probability
        weight = row.post.analysis_weight
        if row.post.sample_frame == "targeted":
            if probability is not None or weight is not None:
                raise ReferenceDatasetError("final_targeted_weight_must_be_blank")
        elif row.post.evidence_origin == "supplemental_annotation":
            if probability is not None or weight is not None:
                raise ReferenceDatasetError("replacement_probability_weight_inherited")
        elif (
            probability is None
            or weight is None
            or not 0.0 < probability <= 1.0
            # 既有概率由旧库整数 ppm 表示，而权重由量化前概率计算；容差只覆盖
            # 最多半个 ppm 的倒数误差，不适用于新补样（新补样必须留空）。
            or not math.isclose(weight, 1.0 / probability, rel_tol=2e-5)
        ):
            raise ReferenceDatasetError("final_probability_weight_invalid")
    if selection.probability_estimation_status == "valid":
        if (
            selection.probability_replacement_count != 0
            or selection.probability_estimation_reason_code is not None
        ):
            raise ReferenceDatasetError("probability_estimation_status_inconsistent")
    elif selection.probability_estimation_status == "unavailable_after_replacement":
        if (
            selection.probability_replacement_count <= 0
            or selection.probability_estimation_reason_code
            != "replacement_joint_inclusion_probability_not_proven"
        ):
            raise ReferenceDatasetError("probability_estimation_status_inconsistent")
    else:
        raise ReferenceDatasetError("probability_estimation_status_invalid")
    csv_rows = [row.as_csv_row() for row in sorted(final_rows, key=lambda item: item.post.identity)]
    csv_result = write_immutable_bytes(csv_path, _csv_bytes(FINAL_REFERENCE_FIELDS, csv_rows))
    label_counts = Counter(row.post.tourism_label for row in final_rows)
    member_hash = _final_member_hash(final_rows)
    manifest = {
        "artifact_contract": FINAL_REFERENCE_CONTRACT,
        "status": FINAL_REFERENCE_STATUS,
        "csv": {
            "logical_name": Path(csv_path).name,
            "field_contract": FINAL_REFERENCE_CONTRACT,
            "sha256": csv_result.sha256,
        },
        "row_count": FINAL_REFERENCE_ROW_COUNT,
        "blank_label_count": 0,
        "uncertain_label_count": 0,
        "unique_source_identity_count": FINAL_REFERENCE_ROW_COUNT,
        "confirmed_duplicate_pair_count_in_final_reference": 0,
        "label_counts": dict(sorted(label_counts.items())),
        "sample_frame_counts": {
            "probability": FINAL_PROBABILITY_COUNT,
            "targeted": FINAL_TARGETED_COUNT,
        },
        "probability_estimation": {
            "status": selection.probability_estimation_status,
            "reason_code": selection.probability_estimation_reason_code,
            "probability_replacement_count": selection.probability_replacement_count,
            "targeted_replacement_count": selection.targeted_replacement_count,
        },
        "replacement_reviewed_max_queue_rank": selection.reviewed_max_queue_rank,
        "identities": {
            "label_guide_id": label_guide_id,
            "candidate_build_id": candidate_build_id,
            "normalization_rule_id": normalization_rule_id,
            "legacy_sample_run_id": legacy_input.sample_run_id,
        },
        "duplicate_candidate_algorithm": initial_candidate_computation.algorithm_identity,
        "representative_selection_rule": {
            "primary": "normalized_text_completeness_descending",
            "tie_breaker": "source_identity_ascending",
            "sample_frame_used": False,
            "platform_used": False,
            "tourism_label_used": False,
        },
        "hashes": {
            "input_csv_sha256": legacy_input.csv_sha256,
            "input_manifest_sha256": legacy_input.manifest_sha256,
            "input_member_sha256": legacy_input.input_member_sha256,
            "duplicate_decision_sha256": component_plan.decision_sha256,
            "initial_duplicate_candidate_sha256": (
                initial_candidate_computation.candidate_sha256
            ),
            "initial_duplicate_decision_manifest_sha256": (
                initial_duplicate_decision_manifest_sha256
            ),
            "replacement_duplicate_decision_sha256": (
                selection.duplicate_decision_sha256
            ),
            "replacement_duplicate_candidate_sha256": (
                replacement_candidate_computation.candidate_sha256
            ),
            "replacement_duplicate_input_member_sha256": (
                replacement_candidate_computation.input_member_sha256
            ),
            "replacement_duplicate_decision_manifest_sha256": (
                replacement_duplicate_decision_manifest_sha256
            ),
            "replacement_queue_sha256": replacement_queue_sha256,
            "supplemental_label_csv_sha256": supplemental_label_csv_sha256,
            "supplemental_label_manifest_sha256": supplemental_label_manifest_sha256,
            "label_resolution_evidence_sha256": label_resolution_evidence_sha256,
            "label_resolution_manifest_sha256": label_resolution_manifest_sha256,
            "replacement_label_resolution_evidence_sha256": (
                replacement_label_resolution_evidence_sha256
            ),
            "replacement_label_resolution_manifest_sha256": (
                replacement_label_resolution_manifest_sha256
            ),
            "final_member_sha256": member_hash,
            "output_csv_sha256": csv_result.sha256,
        },
        "confirmed_duplicate_pairs": [pair.as_list() for pair in all_confirmed_pairs],
        "evidence_policy": {
            "authoritative_csv_count": 1,
            "lineage_artifacts_are_authoritative": False,
        },
    }
    manifest_result = write_immutable_bytes(
        manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    return ArtifactPairResult(
        csv_result.sha256,
        manifest_result.sha256,
        csv_result.reused and manifest_result.reused,
        FINAL_REFERENCE_ROW_COUNT,
        FINAL_REFERENCE_STATUS,
    )
