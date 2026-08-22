"""700 条完成标签与 500/200 样本框的只读证据校验。

本模块只读取完成 CSV、轮次 manifest 和独立派生库。它不会把标签写入
``text_post_annotations``，也不会修改源库或派生库；输出的迁移 manifest 是
可独立封存的样本成员证据，供后续 schema 重建时复用。
"""

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
from typing import Any, Mapping


REFERENCE_FIELDS: tuple[str, ...] = (
    "task_id",
    "sample_run_id",
    "source_post_id",
    "source_version",
    "platform_key",
    "normalized_model_text",
    "tourism_label",
)
ALLOWED_LABELS = frozenset({"related", "unrelated", "uncertain"})


class ReferenceEvidenceError(RuntimeError):
    """参考证据不满足不可变校验契约时抛出的去敏异常。

    Attributes:
        reason_code: 不包含正文、作者或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("reference evidence validation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ReferenceValidationResult:
    """700 条完成标签验证后的非敏感摘要。

    Attributes:
        csv_sha256: 完成 CSV 的原始字节 SHA-256。
        manifest_sha256: 轮次 manifest 的原始字节 SHA-256。
        sample_run_id: 已校验的抽样运行身份。
        row_count: 完成记录数。
        label_counts: 按标签汇总的记录数。
        frame_counts: 按概率/定向样本框汇总的记录数。
        member_manifest_sha256: 数据库重建的样本成员摘要。
        database_annotation_count: 数据库标签副本数；成功结果恒为零。
    """

    csv_sha256: str
    manifest_sha256: str
    sample_run_id: str
    row_count: int
    label_counts: Mapping[str, int]
    frame_counts: Mapping[str, int]
    member_manifest_sha256: str
    database_annotation_count: int


@dataclass(frozen=True)
class MigrationManifestResult:
    """500/200 样本迁移 manifest 的封存摘要。

    Attributes:
        output_path: 本地输出路径；不得写入公开运行日志。
        output_sha256: manifest 原始字节 SHA-256。
        sample_run_id: 样本运行身份。
        population_count: 原始候选人口数。
        probability_count: 概率样本成员数。
        targeted_count: 定向样本成员数。
        member_count: manifest 中的成员总数。
        member_manifest_sha256: 原 schema 中的成员摘要。
        reused: 是否幂等复用了字节完全一致的既有文件。
    """

    output_path: Path
    output_sha256: str
    sample_run_id: str
    population_count: int
    probability_count: int
    targeted_count: int
    member_count: int
    member_manifest_sha256: str
    reused: bool


def _sha256_bytes(path: Path) -> str:
    """流式计算文件 SHA-256。

    Args:
        path: 待读取文件。

    Returns:
        小写十六进制 SHA-256。
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    """计算对象规范 JSON 的 SHA-256。

    Args:
        value: 可 JSON 序列化对象。

    Returns:
        小写十六进制 SHA-256。
    """

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    """以 SQLite ``mode=ro`` 打开派生库。

    Args:
        path: 已存在的派生 SQLite 路径。

    Returns:
        同时启用 ``query_only`` 和禁用可信 schema 的只读连接。

    Raises:
        FileNotFoundError: 数据库路径不存在。
        sqlite3.Error: 数据库无法只读打开或设置安全参数。
    """

    resolved = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def _member_manifest(connection: sqlite3.Connection, sample_run_id: str) -> str:
    """按原抽样算法顺序重建样本成员摘要。

    Args:
        connection: 已启用只读模式的派生库连接。
        sample_run_id: 封存抽样运行身份。

    Returns:
        包含样本框、纳入概率和分析权重的成员 SHA-256。
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
    members = [
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
    return _canonical_hash(members)


def _member_rows(
    connection: sqlite3.Connection, sample_run_id: str
) -> tuple[sqlite3.Row, ...]:
    """读取抽样时所属候选构建的规范化文本和样本属性。

    Args:
        connection: 已启用只读模式的派生库连接。
        sample_run_id: 封存抽样运行身份。

    Returns:
        按样本框和选择次序排列的唯一成员行。
    """

    return tuple(
        connection.execute(
            """
            SELECT m.source_post_id, m.source_version, m.platform_key,
                   m.sample_frame, m.selection_reason_code, m.selection_rank,
                   m.inclusion_probability_ppm, m.analysis_weight,
                   r.normalized_model_text,
                   c.platform_key AS candidate_platform_key
            FROM text_sample_members AS m
            JOIN text_sampling_runs AS s
              ON s.sample_run_id = m.sample_run_id
            JOIN text_candidate_corpus_members AS c
              ON c.build_id = s.candidate_build_id
             AND c.source_post_id = m.source_post_id
             AND c.source_version = m.source_version
            JOIN text_deterministic_results AS r
              ON r.source_post_id = m.source_post_id
             AND r.source_version = m.source_version
             AND r.task_id = c.task_id
            WHERE m.sample_run_id = ?
            ORDER BY CASE m.sample_frame
                       WHEN 'probability' THEN 1
                       WHEN 'targeted' THEN 2
                       ELSE 3 END,
                     m.selection_rank, m.source_post_id, m.source_version
            """,
            (sample_run_id,),
        ).fetchall()
    )


def _load_reference_rows(csv_path: Path) -> list[dict[str, str]]:
    """按冻结字段顺序读取完成 CSV。

    Args:
        csv_path: 完成 CSV 路径。

    Returns:
        保留原始文本值的逐行字典。

    Raises:
        ReferenceEvidenceError: 文件不可读、编码非法或字段契约不一致。
    """

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise ReferenceEvidenceError("reference_csv_field_contract_mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceEvidenceError("reference_csv_could_not_be_read") from exc
    return rows


def _validate_manifest_shape(
    manifest: Mapping[str, Any],
    csv_path: Path,
    *,
    expected_label_guide_version: str,
) -> None:
    """校验轮次 manifest 的权威段和完成状态。

    Args:
        manifest: 已解析的轮次 manifest。
        csv_path: 与 manifest 配对的完成 CSV。
        expected_label_guide_version: 稳定配置冻结的标签手册身份。

    Raises:
        ReferenceEvidenceError: manifest 缺段、身份不符或尚未封存。
    """

    completed = manifest.get("completed_csv")
    sample = manifest.get("sample_run")
    if not isinstance(completed, Mapping) or not isinstance(sample, Mapping):
        raise ReferenceEvidenceError("reference_manifest_missing_sections")
    if completed.get("logical_name") != csv_path.name:
        raise ReferenceEvidenceError("reference_csv_name_mismatch")
    if completed.get("field_contract") != "text-cleaning-post-review-v1.5":
        raise ReferenceEvidenceError("reference_field_contract_mismatch")
    if sample.get("sample_run_id") is None or sample.get("member_manifest_sha256") is None:
        raise ReferenceEvidenceError("sample_manifest_identity_missing")
    if manifest.get("label_guide_version") != expected_label_guide_version:
        raise ReferenceEvidenceError("reference_label_guide_mismatch")
    if sample.get("seal_status") != "finalized":
        raise ReferenceEvidenceError("sample_manifest_not_finalized")
    if _manifest_count(
        completed.get("blank_label_count"), "reference_blank_label_count_invalid"
    ) != 0:
        raise ReferenceEvidenceError("reference_blank_labels_present")
    if _manifest_count(
        manifest.get("active_csv_count"), "reference_active_csv_count_invalid"
    ) != 1:
        raise ReferenceEvidenceError("reference_active_csv_count_invalid")


def _manifest_count(value: Any, reason_code: str) -> int:
    """解析 manifest 中采用 JSON 整数表示的非负计数。

    Args:
        value: 待解析的 JSON 值。
        reason_code: 类型或范围错误时使用的稳定失败码。

    Returns:
        非负整数。

    Raises:
        ReferenceEvidenceError: 值是布尔值、非整数或负数。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReferenceEvidenceError(reason_code)
    return value


def _positive_identity(value: str, field: str) -> int:
    """解析 CSV 中采用规范十进制表示的正整数身份。

    Args:
        value: CSV 原始字段。
        field: 用于失败原因的公开字段名。

    Returns:
        正整数身份。

    Raises:
        ReferenceEvidenceError: 字段不是规范正整数。
    """

    stripped = value.strip()
    try:
        parsed = int(stripped)
    except ValueError as exc:
        raise ReferenceEvidenceError(f"reference_{field}_invalid") from exc
    if parsed <= 0 or str(parsed) != stripped:
        raise ReferenceEvidenceError(f"reference_{field}_invalid")
    return parsed


def validate_reference_evidence(
    csv_path: str | Path,
    manifest_path: str | Path,
    derived_db: str | Path,
    *,
    expected_label_guide_version: str,
) -> ReferenceValidationResult:
    """验证完成 CSV、轮次 manifest、样本成员和规范化文本。

    Args:
        csv_path: 唯一完成 CSV 路径。
        manifest_path: 与完成 CSV 配对的轮次 manifest。
        derived_db: 只读派生 SQLite 路径。
        expected_label_guide_version: 稳定配置冻结的标签手册身份。

    Returns:
        不包含正文、作者或本机路径的验证摘要。

    Raises:
        ReferenceEvidenceError: 任一哈希、身份、标签、成员、权重、文本、版本或
            数据库副本约束不满足。异常只暴露稳定失败码。
    """

    csv_file = Path(csv_path).expanduser().resolve(strict=True)
    manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    csv_hash = _sha256_bytes(csv_file)
    manifest_hash = _sha256_bytes(manifest_file)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceEvidenceError("reference_manifest_could_not_be_read") from exc
    if not isinstance(manifest, Mapping):
        raise ReferenceEvidenceError("reference_manifest_not_an_object")
    _validate_manifest_shape(
        manifest,
        csv_file,
        expected_label_guide_version=expected_label_guide_version,
    )
    rows = _load_reference_rows(csv_file)
    completed = manifest["completed_csv"]
    sample = manifest["sample_run"]
    if completed.get("sha256") != csv_hash:
        raise ReferenceEvidenceError("reference_csv_hash_mismatch")
    if _manifest_count(
        completed.get("row_count"), "reference_csv_row_count_invalid"
    ) != len(rows):
        raise ReferenceEvidenceError("reference_csv_row_count_mismatch")
    label_counts = Counter(row.get("tourism_label", "").strip() for row in rows)
    if set(label_counts) - ALLOWED_LABELS or any(not row.get("tourism_label", "").strip() for row in rows):
        raise ReferenceEvidenceError("reference_label_value_invalid")
    expected_counts = completed.get("label_counts")
    if not isinstance(expected_counts, Mapping) or set(expected_counts) != ALLOWED_LABELS:
        raise ReferenceEvidenceError("reference_label_counts_mismatch")
    normalized_expected_counts = {
        str(key): _manifest_count(value, "reference_label_counts_invalid")
        for key, value in expected_counts.items()
    }
    if any(
        int(label_counts.get(key, 0)) != value
        for key, value in normalized_expected_counts.items()
    ):
        raise ReferenceEvidenceError("reference_label_counts_mismatch")
    identities = {(row.get("source_post_id", ""), row.get("source_version", "")) for row in rows}
    if len(identities) != len(rows) or len({row.get("task_id", "") for row in rows}) != len(rows):
        raise ReferenceEvidenceError("reference_identity_not_unique")
    if any(not row.get("task_id", "").strip() for row in rows):
        raise ReferenceEvidenceError("reference_task_identity_missing")

    sample_run_id = str(sample["sample_run_id"])
    for row in rows:
        if row["sample_run_id"].strip() != sample_run_id:
            raise ReferenceEvidenceError("reference_sample_run_id_mismatch")
        source_post_id = _positive_identity(row["source_post_id"], "source_post_id")
        _positive_identity(row["source_version"], "source_version")
        expected_task_id = _canonical_hash([sample_run_id, source_post_id])[:32]
        if row["task_id"].strip() != expected_task_id:
            raise ReferenceEvidenceError("reference_task_identity_mismatch")
    connection = _readonly_connection(derived_db)
    try:
        sample_row = connection.execute(
            "SELECT * FROM text_sampling_runs WHERE sample_run_id = ?", (sample_run_id,)
        ).fetchone()
        if sample_row is None or sample_row["seal_status"] != "finalized":
            raise ReferenceEvidenceError("sample_run_not_finalized")
        if str(sample_row["guide_version"]) != expected_label_guide_version:
            raise ReferenceEvidenceError("sample_database_guide_mismatch")
        source_snapshot = manifest.get("source_snapshot")
        candidate_build = manifest.get("candidate_build")
        if not isinstance(source_snapshot, Mapping) or not isinstance(candidate_build, Mapping):
            raise ReferenceEvidenceError("reference_manifest_lineage_missing")
        if str(sample_row["source_snapshot_id"]) != str(source_snapshot.get("snapshot_id", "")):
            raise ReferenceEvidenceError("sample_source_snapshot_mismatch")
        if str(sample_row["candidate_build_id"]) != str(candidate_build.get("build_id", "")):
            raise ReferenceEvidenceError("sample_candidate_build_mismatch")
        db_member_hash = _member_manifest(connection, sample_run_id)
        if db_member_hash != sample_row["member_manifest_sha256"] or db_member_hash != sample.get(
            "member_manifest_sha256"
        ):
            raise ReferenceEvidenceError("sample_member_manifest_mismatch")
        sample_counts = {
            key: _manifest_count(sample.get(key), f"sample_{key}_invalid")
            for key in ("population_count", "probability_count", "targeted_count")
        }
        for key, manifest_count in sample_counts.items():
            if int(sample_row[key]) != manifest_count:
                raise ReferenceEvidenceError(f"sample_{key}_mismatch")
        member_rows = _member_rows(connection, sample_run_id)
        if len(member_rows) != sample_counts["probability_count"] + sample_counts["targeted_count"]:
            raise ReferenceEvidenceError("sample_member_join_incomplete")
        by_identity = {
            (str(row["source_post_id"]), str(row["source_version"])): row for row in member_rows
        }
        if len(by_identity) != len(member_rows):
            raise ReferenceEvidenceError("sample_member_identity_not_unique")
        frame_counts: Counter[str] = Counter()
        for item in rows:
            key = (item["source_post_id"].strip(), item["source_version"].strip())
            db_row = by_identity.get(key)
            if db_row is None:
                raise ReferenceEvidenceError("reference_identity_not_in_sample")
            if item["platform_key"].strip() != str(db_row["platform_key"]):
                raise ReferenceEvidenceError("reference_platform_lineage_mismatch")
            if item["normalized_model_text"] != str(db_row["normalized_model_text"]):
                raise ReferenceEvidenceError("reference_normalized_text_mismatch")
            if str(db_row["platform_key"]) != str(db_row["candidate_platform_key"]):
                raise ReferenceEvidenceError("candidate_platform_lineage_mismatch")
            frame = str(db_row["sample_frame"])
            frame_counts[frame] += 1
            if frame == "probability":
                ppm = db_row["inclusion_probability_ppm"]
                weight = db_row["analysis_weight"]
                if ppm is None or weight is None or not math.isclose(
                    float(weight), 1_000_000.0 / int(ppm), rel_tol=0.001
                ):
                    raise ReferenceEvidenceError("probability_weight_mismatch")
            elif frame == "targeted":
                if db_row["inclusion_probability_ppm"] is not None or db_row["analysis_weight"] is not None:
                    raise ReferenceEvidenceError("targeted_weight_must_be_null")
            else:
                raise ReferenceEvidenceError("unexpected_sample_frame")
        if frame_counts != Counter(
            {
                "probability": sample_counts["probability_count"],
                "targeted": sample_counts["targeted_count"],
            }
        ):
            raise ReferenceEvidenceError("sample_frame_counts_mismatch")
        annotation_count = int(connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0])
        if annotation_count != 0:
            raise ReferenceEvidenceError("reference_labels_imported_into_database")
    finally:
        connection.close()
    return ReferenceValidationResult(
        csv_sha256=csv_hash,
        manifest_sha256=manifest_hash,
        sample_run_id=sample_run_id,
        row_count=len(rows),
        label_counts=dict(sorted(label_counts.items())),
        frame_counts=dict(sorted(frame_counts.items())),
        member_manifest_sha256=db_member_hash,
        database_annotation_count=annotation_count,
    )


def _migration_payload(connection: sqlite3.Connection, sample_run_id: str) -> dict[str, Any]:
    """从封存数据库状态构造迁移 manifest 内容。

    Args:
        connection: 已启用只读模式的派生库连接。
        sample_run_id: 待迁移的初始抽样运行身份。

    Returns:
        包含500/200样本框、概率和权重的规范对象。

    Raises:
        ReferenceEvidenceError: 抽样运行未封存、连接不完整或成员摘要不一致。
    """

    sample = connection.execute(
        "SELECT * FROM text_sampling_runs WHERE sample_run_id = ?", (sample_run_id,)
    ).fetchone()
    if sample is None or sample["seal_status"] != "finalized":
        raise ReferenceEvidenceError("sample_run_not_finalized")
    rows = _member_rows(connection, sample_run_id)
    if len(rows) != int(sample["probability_count"]) + int(sample["targeted_count"]):
        raise ReferenceEvidenceError("sample_member_join_incomplete")
    member_manifest = _member_manifest(connection, sample_run_id)
    if member_manifest != sample["member_manifest_sha256"]:
        raise ReferenceEvidenceError("sample_member_manifest_mismatch")
    members = [
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
                float(row["analysis_weight"]) if row["analysis_weight"] is not None else None
            ),
        }
        for row in rows
    ]
    return {
        "artifact_kind": "text-cleaning-sample-migration",
        "artifact_status": "immutable",
        "sample_run_id": str(sample["sample_run_id"]),
        "candidate_build_id": str(sample["candidate_build_id"]),
        "source_snapshot_id": str(sample["source_snapshot_id"]),
        "guide_version": str(sample["guide_version"]),
        "random_seed": int(sample["random_seed"]),
        "population_manifest_sha256": str(sample["population_manifest_sha256"]),
        "population_count": int(sample["population_count"]),
        "probability_count": int(sample["probability_count"]),
        "targeted_count": int(sample["targeted_count"]),
        "member_manifest_sha256": member_manifest,
        "members": members,
    }


def _migration_bytes(payload: Mapping[str, Any]) -> bytes:
    """编码迁移 manifest 的唯一规范字节形式。

    Args:
        payload: 已验证的迁移 manifest 对象。

    Returns:
        UTF-8、排序键、两空格缩进且以换行结尾的 JSON 字节。
    """

    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n"
    )


def _seal_manifest_exclusively(output: Path, encoded: bytes) -> bool:
    """原子发布迁移 manifest，禁止覆盖不同内容。

    Args:
        output: 最终 manifest 路径。
        encoded: 已规范编码并完成内存校验的字节。

    Returns:
        文件已存在且字节完全一致时为 ``True``；首次封存时为 ``False``。

    Raises:
        ReferenceEvidenceError: 目标已存在但内容不同，或封存后字节复核失败。
        OSError: 临时文件或同目录硬链接无法创建。
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
        ) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        try:
            os.link(temporary_path, output)
            reused = False
        except FileExistsError:
            if output.read_bytes() != encoded:
                raise ReferenceEvidenceError("migration_manifest_exists_with_different_content")
            reused = True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if output.read_bytes() != encoded:
        raise ReferenceEvidenceError("migration_manifest_roundtrip_mismatch")
    return reused


def write_sample_migration_manifest(
    derived_db: str | Path,
    output_path: str | Path,
    *,
    sample_run_id: str | None = None,
) -> MigrationManifestResult:
    """从封存样本运行生成并排他封存 500/200 迁移 manifest。

    Args:
        derived_db: 只读派生 SQLite 路径。
        output_path: 待封存 manifest 路径。
        sample_run_id: 显式初始抽样运行；省略时只选择最近封存的初始运行。

    Returns:
        manifest 哈希、成员计数和幂等复用状态。

    Raises:
        ReferenceEvidenceError: 样本不完整、目标存在不同内容或回读不一致。
    """

    output = Path(output_path).expanduser().resolve()
    connection = _readonly_connection(derived_db)
    try:
        if sample_run_id is None:
            row = connection.execute(
                """
                SELECT sample_run_id FROM text_sampling_runs
                WHERE sample_kind = 'initial' AND seal_status = 'finalized'
                ORDER BY created_at_utc DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise ReferenceEvidenceError("initial_sample_run_not_found")
            sample_run_id = str(row["sample_run_id"])
        payload = _migration_payload(connection, sample_run_id)
    finally:
        connection.close()
    encoded = _migration_bytes(payload)
    reused = _seal_manifest_exclusively(output, encoded)
    output_hash = hashlib.sha256(encoded).hexdigest()
    return MigrationManifestResult(
        output_path=output,
        output_sha256=output_hash,
        sample_run_id=str(payload["sample_run_id"]),
        population_count=int(payload["population_count"]),
        probability_count=int(payload["probability_count"]),
        targeted_count=int(payload["targeted_count"]),
        member_count=len(payload["members"]),
        member_manifest_sha256=str(payload["member_manifest_sha256"]),
        reused=reused,
    )


def validate_sample_migration_manifest(
    manifest_path: str | Path,
    derived_db: str | Path,
) -> MigrationManifestResult:
    """验证迁移 manifest 的规范字节和派生库封存样本完全一致。

    Args:
        manifest_path: 已封存迁移 manifest 路径。
        derived_db: 只读派生 SQLite 路径。

    Returns:
        已验证 manifest 的哈希与成员摘要。

    Raises:
        ReferenceEvidenceError: 文件不可读、状态非法、字节非规范或数据库不一致。
    """

    path = Path(manifest_path).expanduser().resolve(strict=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceEvidenceError("migration_manifest_could_not_be_read") from exc
    if not isinstance(payload, Mapping) or payload.get("artifact_status") != "immutable":
        raise ReferenceEvidenceError("migration_manifest_status_invalid")
    connection = _readonly_connection(derived_db)
    try:
        expected = _migration_payload(connection, str(payload.get("sample_run_id", "")))
    finally:
        connection.close()
    if dict(payload) != expected:
        raise ReferenceEvidenceError("migration_manifest_database_mismatch")
    if path.read_bytes() != _migration_bytes(expected):
        raise ReferenceEvidenceError("migration_manifest_noncanonical")
    return MigrationManifestResult(
        output_path=path,
        output_sha256=_sha256_bytes(path),
        sample_run_id=str(payload["sample_run_id"]),
        population_count=int(payload["population_count"]),
        probability_count=int(payload["probability_count"]),
        targeted_count=int(payload["targeted_count"]),
        member_count=len(payload["members"]),
        member_manifest_sha256=str(payload["member_manifest_sha256"]),
        reused=True,
    )
