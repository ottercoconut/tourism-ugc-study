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
import sqlite3
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
    """参考证据不满足不可变校验契约时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("reference evidence validation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ReferenceValidationResult:
    """700 条完成标签验证后的非敏感摘要。"""

    csv_sha256: str
    sample_run_id: str
    row_count: int
    label_counts: Mapping[str, int]
    frame_counts: Mapping[str, int]
    member_manifest_sha256: str
    database_annotation_count: int


@dataclass(frozen=True)
class MigrationManifestResult:
    """500/200 样本迁移 manifest 的封存摘要。"""

    output_path: Path
    output_sha256: str
    sample_run_id: str
    population_count: int
    probability_count: int
    targeted_count: int
    member_count: int
    member_manifest_sha256: str


def _sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    """以 SQLite ``mode=ro`` 打开派生库，避免校验器产生任何写入。"""

    resolved = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def _member_manifest(connection: sqlite3.Connection, sample_run_id: str) -> str:
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
    return tuple(
        connection.execute(
            """
            SELECT m.source_post_id, m.source_version, m.platform_key,
                   m.sample_frame, m.selection_reason_code, m.selection_rank,
                   m.inclusion_probability_ppm, m.analysis_weight,
                   r.normalized_model_text,
                   c.platform_key AS candidate_platform_key
            FROM text_sample_members AS m
            JOIN text_candidate_corpus_members AS c
              ON c.source_post_id = m.source_post_id
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
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise ReferenceEvidenceError("reference_csv_field_contract_mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceEvidenceError("reference_csv_could_not_be_read") from exc
    return rows


def _validate_manifest_shape(manifest: Mapping[str, Any], csv_path: Path) -> None:
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


def validate_reference_evidence(
    csv_path: str | Path,
    manifest_path: str | Path,
    derived_db: str | Path,
) -> ReferenceValidationResult:
    """验证完成 CSV、轮次 manifest、样本成员和规范化文本的一致性。

    失败时返回固定 ``reason_code``，不回显文本或作者信息。校验器要求数据库
    中没有导入标签；已存在的 ``text_post_annotations`` 行数非零会直接失败。
    """

    csv_file = Path(csv_path).expanduser().resolve(strict=True)
    manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    csv_hash = _sha256_bytes(csv_file)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceEvidenceError("reference_manifest_could_not_be_read") from exc
    if not isinstance(manifest, Mapping):
        raise ReferenceEvidenceError("reference_manifest_not_an_object")
    _validate_manifest_shape(manifest, csv_file)
    rows = _load_reference_rows(csv_file)
    completed = manifest["completed_csv"]
    sample = manifest["sample_run"]
    if completed.get("sha256") != csv_hash:
        raise ReferenceEvidenceError("reference_csv_hash_mismatch")
    if int(completed.get("row_count", -1)) != len(rows):
        raise ReferenceEvidenceError("reference_csv_row_count_mismatch")
    label_counts = Counter(row.get("tourism_label", "").strip() for row in rows)
    if set(label_counts) - ALLOWED_LABELS or any(not row.get("tourism_label", "").strip() for row in rows):
        raise ReferenceEvidenceError("reference_label_value_invalid")
    expected_counts = completed.get("label_counts")
    normalized_expected_counts = (
        {str(key): int(value) for key, value in expected_counts.items()}
        if isinstance(expected_counts, Mapping)
        else None
    )
    if normalized_expected_counts is None or any(
        int(label_counts.get(key, 0)) != value
        for key, value in normalized_expected_counts.items()
    ) or any(key not in normalized_expected_counts for key in label_counts):
        raise ReferenceEvidenceError("reference_label_counts_mismatch")
    identities = {(row.get("source_post_id", ""), row.get("source_version", "")) for row in rows}
    if len(identities) != len(rows) or len({row.get("task_id", "") for row in rows}) != len(rows):
        raise ReferenceEvidenceError("reference_identity_not_unique")
    if any(not row.get("task_id", "").strip() for row in rows):
        raise ReferenceEvidenceError("reference_task_identity_missing")

    sample_run_id = str(sample["sample_run_id"])
    connection = _readonly_connection(derived_db)
    try:
        sample_row = connection.execute(
            "SELECT * FROM text_sampling_runs WHERE sample_run_id = ?", (sample_run_id,)
        ).fetchone()
        if sample_row is None or sample_row["seal_status"] != "finalized":
            raise ReferenceEvidenceError("sample_run_not_finalized")
        db_member_hash = _member_manifest(connection, sample_run_id)
        if db_member_hash != sample_row["member_manifest_sha256"] or db_member_hash != sample.get(
            "member_manifest_sha256"
        ):
            raise ReferenceEvidenceError("sample_member_manifest_mismatch")
        for key in ("population_count", "probability_count", "targeted_count"):
            if int(sample_row[key]) != int(sample[key]):
                raise ReferenceEvidenceError(f"sample_{key}_mismatch")
        member_rows = _member_rows(connection, sample_run_id)
        if len(member_rows) != int(sample_row["probability_count"]) + int(sample_row["targeted_count"]):
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
        if frame_counts != Counter({"probability": int(sample["probability_count"]), "targeted": int(sample["targeted_count"])}):
            raise ReferenceEvidenceError("sample_frame_counts_mismatch")
        annotation_count = int(connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0])
        if annotation_count != 0:
            raise ReferenceEvidenceError("reference_labels_imported_into_database")
    finally:
        connection.close()
    return ReferenceValidationResult(
        csv_sha256=csv_hash,
        sample_run_id=sample_run_id,
        row_count=len(rows),
        label_counts=dict(sorted(label_counts.items())),
        frame_counts=dict(sorted(frame_counts.items())),
        member_manifest_sha256=db_member_hash,
        database_annotation_count=annotation_count,
    )


def _migration_payload(connection: sqlite3.Connection, sample_run_id: str) -> dict[str, Any]:
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


def write_sample_migration_manifest(
    derived_db: str | Path,
    output_path: str | Path,
    *,
    sample_run_id: str | None = None,
) -> MigrationManifestResult:
    """从封存样本运行生成 500/200 成员迁移 manifest，并回读复验其哈希。"""

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
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    output_hash = hashlib.sha256(encoded).hexdigest()
    reread = json.loads(output.read_text(encoding="utf-8"))
    if reread != payload:
        raise ReferenceEvidenceError("migration_manifest_roundtrip_mismatch")
    return MigrationManifestResult(
        output_path=output,
        output_sha256=output_hash,
        sample_run_id=str(payload["sample_run_id"]),
        population_count=int(payload["population_count"]),
        probability_count=int(payload["probability_count"]),
        targeted_count=int(payload["targeted_count"]),
        member_count=len(payload["members"]),
        member_manifest_sha256=str(payload["member_manifest_sha256"]),
    )


def validate_sample_migration_manifest(
    manifest_path: str | Path,
    derived_db: str | Path,
) -> MigrationManifestResult:
    """验证已生成迁移 manifest 与派生库封存样本完全一致。"""

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
    return MigrationManifestResult(
        output_path=path,
        output_sha256=_sha256_bytes(path),
        sample_run_id=str(payload["sample_run_id"]),
        population_count=int(payload["population_count"]),
        probability_count=int(payload["probability_count"]),
        targeted_count=int(payload["targeted_count"]),
        member_count=len(payload["members"]),
        member_manifest_sha256=str(payload["member_manifest_sha256"]),
    )
