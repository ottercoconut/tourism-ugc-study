"""将final 700与Wave A/B合并为新模型的不可变训练快照。"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model_retraining_config import ModelRetrainingPlan


class ModelRetrainingSnapshotError(RuntimeError):
    """训练成员、标签、正文绑定或泄漏分量无效时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露帖子、标签、正文或路径的稳定失败码。"""

        super().__init__("formal model retraining snapshot failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RetrainingDocument:
    """新模型使用的一条文本、标签与泄漏分组记录。

    ``source_post_id``与``source_version``仅保存在私有快照中；公开训练报告
    使用``member_key``。抽样权重保留用于Wave B交叉拟合评价，绝不传给fit。
    """

    member_key: str
    source_post_id: int
    source_version: int
    component_id: str
    normalized_model_text: str
    normalized_sha256: str
    tourism_label: str
    evidence_origin: str
    inclusion_probability: float | None
    analysis_weight: float | None
    historical_test_consumed: bool

    @property
    def identity(self) -> tuple[int, int]:
        """返回派生库使用的稳定帖子身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class RetrainingSnapshot:
    """完成全部输入校验后的1,300条训练成员与去敏摘要。"""

    documents: tuple[RetrainingDocument, ...]
    count: int
    related_count: int
    unrelated_count: int
    component_count: int
    origin_counts: Mapping[str, int]
    historical_test_consumed_count: int
    leakage_output_sha256: str
    member_binding_sha256: str


def file_sha256(path: str | Path) -> str:
    """流式计算输入文件SHA-256并统一读取失败语义。"""

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_input_unreadable"
        ) from exc
    return digest.hexdigest()


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    """以URI只读和query-only模式打开派生数据库。"""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_database_readonly_open_failed"
        ) from exc


def _load_component_bindings(
    derived_db: str | Path, *, plan: ModelRetrainingPlan
) -> tuple[Mapping[tuple[int, int], str], str]:
    """读取finalized leakage build的全部身份—分量绑定。"""

    connection = _readonly_connection(derived_db)
    try:
        build = connection.execute(
            """
            SELECT candidate_build_id, input_post_count, component_count,
                   output_sha256, seal_status
            FROM text_leakage_builds WHERE leakage_build_id = ?
            """,
            (plan.leakage_build_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT source_post_id, source_version, component_id
            FROM text_leakage_members
            WHERE leakage_build_id = ?
            ORDER BY source_post_id, source_version
            """,
            (plan.leakage_build_id,),
        ).fetchall()
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    except sqlite3.Error as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_leakage_contract_invalid"
        ) from exc
    finally:
        connection.close()
    if (
        build is None
        or str(build["candidate_build_id"]) != plan.candidate_build_id
        or str(build["seal_status"]) != "finalized"
        or int(build["input_post_count"]) != 13858
        or int(build["component_count"]) != 8087
        or len(rows) != 13858
        or query_only != 1
    ):
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_leakage_contract_invalid"
        )
    try:
        bindings = {
            (int(row["source_post_id"]), int(row["source_version"])): str(
                row["component_id"]
            )
            for row in rows
        }
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_leakage_member_invalid"
        ) from exc
    if (
        len(bindings) != len(rows)
        or any(post_id <= 0 or version <= 0 for post_id, version in bindings)
        or any(not component.strip() for component in bindings.values())
    ):
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_leakage_member_invalid"
        )
    return bindings, str(build["output_sha256"])


def _load_split_test_identities(
    split_manifest_path: str | Path,
) -> set[tuple[int, int]]:
    """读取旧切分中148条已消费测试身份，不读取旧模型概率。"""

    try:
        raw = json.loads(Path(split_manifest_path).read_text(encoding="utf-8"))
        assignments = raw["assignments"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_split_manifest_invalid"
        ) from exc
    if not isinstance(assignments, list) or len(assignments) != 700:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_split_manifest_invalid"
        )
    try:
        test = {
            (int(item["source_post_id"]), int(item["source_version"]))
            for item in assignments
            if item["split_name"] == "test"
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_split_manifest_invalid"
        ) from exc
    if len(test) != 148:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_historical_test_count_invalid"
        )
    return test


def _read_csv(path: str | Path, *, expected_sha256: str) -> list[dict[str, str]]:
    """按UTF-8 BOM兼容方式读取并先验证完整文件身份。"""

    if file_sha256(path) != expected_sha256:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_input_hash_mismatch"
        )
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                raise ValueError
            return [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_csv_invalid"
        ) from exc


def _load_private_map(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_count: int,
) -> Mapping[str, Mapping[str, Any]]:
    """读取Wave私有映射并按task_id建立唯一索引。"""

    if file_sha256(path) != expected_sha256:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_input_hash_mismatch"
        )
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        records = raw["records"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_private_map_invalid"
        ) from exc
    if not isinstance(records, list) or len(records) != expected_count:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_private_map_invalid"
        )
    try:
        indexed = {str(item["task_id"]): item for item in records}
    except (KeyError, TypeError) as exc:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_private_map_invalid"
        ) from exc
    if len(indexed) != len(records) or any(not key for key in indexed):
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_private_map_invalid"
        )
    return indexed


def _member_key(
    plan: ModelRetrainingPlan, source_post_id: int, source_version: int
) -> str:
    """生成只在本次快照内使用的稳定去标识成员键。"""

    return hashlib.sha256(
        (
            f"{plan.plan_id}|training-member|{source_post_id}|{source_version}"
        ).encode("utf-8")
    ).hexdigest()[:24]


def _final_documents(
    rows: Sequence[Mapping[str, str]],
    *,
    bindings: Mapping[tuple[int, int], str],
    historical_test: set[tuple[int, int]],
    plan: ModelRetrainingPlan,
) -> list[RetrainingDocument]:
    """把最终参考CSV投影为训练成员，不沿用其概率与分析权重。"""

    required = {
        "source_post_id",
        "source_version",
        "normalized_model_text",
        "tourism_label",
    }
    documents: list[RetrainingDocument] = []
    for row in rows:
        if not required.issubset(row):
            raise ModelRetrainingSnapshotError(
                "model_retraining_snapshot_final_row_invalid"
            )
        try:
            identity = (int(row["source_post_id"]), int(row["source_version"]))
            text = row["normalized_model_text"]
            label = row["tourism_label"]
            component = bindings[identity]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ModelRetrainingSnapshotError(
                "model_retraining_snapshot_final_row_invalid"
            ) from exc
        if not text.strip() or label not in {"related", "unrelated"}:
            raise ModelRetrainingSnapshotError(
                "model_retraining_snapshot_final_row_invalid"
            )
        normalized_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        documents.append(
            RetrainingDocument(
                member_key=_member_key(plan, *identity),
                source_post_id=identity[0],
                source_version=identity[1],
                component_id=component,
                normalized_model_text=text,
                normalized_sha256=normalized_sha256,
                tourism_label=label,
                evidence_origin="final_reference",
                inclusion_probability=None,
                analysis_weight=None,
                historical_test_consumed=identity in historical_test,
            )
        )
    return documents


def _wave_documents(
    rows: Sequence[Mapping[str, str]],
    private_map: Mapping[str, Mapping[str, Any]],
    *,
    origin: str,
    bindings: Mapping[tuple[int, int], str],
    plan: ModelRetrainingPlan,
) -> list[RetrainingDocument]:
    """把四列完成表与私有映射重新结合为训练成员。"""

    expected_columns = {
        "task_id",
        "sample_run_id",
        "normalized_model_text",
        "tourism_label",
    }
    documents: list[RetrainingDocument] = []
    seen_tasks: set[str] = set()
    for row in rows:
        if set(row) != expected_columns:
            raise ModelRetrainingSnapshotError(
                "model_retraining_snapshot_wave_row_invalid"
            )
        task_id = row["task_id"]
        if not task_id or task_id in seen_tasks or task_id not in private_map:
            raise ModelRetrainingSnapshotError(
                "model_retraining_snapshot_wave_row_invalid"
            )
        seen_tasks.add(task_id)
        mapped = private_map[task_id]
        try:
            identity = (
                int(mapped["source_post_id"]),
                int(mapped["source_version"]),
            )
            component = str(mapped["component_id"])
            normalized_sha256 = str(mapped["normalized_sha256"])
            inclusion_probability = float(mapped["inclusion_probability"])
            analysis_weight = float(mapped["analysis_weight"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ModelRetrainingSnapshotError(
                "model_retraining_snapshot_private_map_invalid"
            ) from exc
        text = row["normalized_model_text"]
        label = row["tourism_label"]
        if (
            bindings.get(identity) != component
            or not text.strip()
            or hashlib.sha256(text.encode("utf-8")).hexdigest()
            != normalized_sha256
            or label not in {"related", "unrelated"}
            or not 0.0 < inclusion_probability <= 1.0
            or analysis_weight <= 0.0
        ):
            raise ModelRetrainingSnapshotError(
                "model_retraining_snapshot_wave_binding_invalid"
            )
        documents.append(
            RetrainingDocument(
                member_key=_member_key(plan, *identity),
                source_post_id=identity[0],
                source_version=identity[1],
                component_id=component,
                normalized_model_text=text,
                normalized_sha256=normalized_sha256,
                tourism_label=label,
                evidence_origin=origin,
                inclusion_probability=inclusion_probability,
                analysis_weight=analysis_weight,
                historical_test_consumed=False,
            )
        )
    if set(private_map) != seen_tasks:
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_wave_membership_invalid"
        )
    return documents


def build_retraining_snapshot(
    final_reference_csv: str | Path,
    wave_a_completed_csv: str | Path,
    wave_a_private_map: str | Path,
    wave_b_completed_csv: str | Path,
    wave_b_private_map: str | Path,
    derived_db: str | Path,
    split_manifest: str | Path,
    *,
    plan: ModelRetrainingPlan,
) -> RetrainingSnapshot:
    """只读校验并合并1,300条新模型训练证据。

    Args:
        final_reference_csv: 最终700条权威标签CSV。
        wave_a_completed_csv: 已封存的Wave A四列完成表。
        wave_a_private_map: Wave A身份、分量和设计权重私有映射。
        wave_b_completed_csv: 已封存的Wave B四列完成表。
        wave_b_private_map: Wave B身份、分量和设计权重私有映射。
        derived_db: 含finalized leakage build的只读派生库。
        split_manifest: 原700条固定切分manifest，仅标记已消费测试成员。
        plan: Issue #49冻结重训计划。

    Returns:
        具有唯一身份、完整标签和finalized分组的训练快照。

    Raises:
        ModelRetrainingSnapshotError: 任一哈希、成员、文本、标签或分量漂移。
    """

    bindings, leakage_sha256 = _load_component_bindings(derived_db, plan=plan)
    historical_test = _load_split_test_identities(split_manifest)
    final_rows = _read_csv(
        final_reference_csv, expected_sha256=plan.reference_csv_sha256
    )
    wave_a_rows = _read_csv(
        wave_a_completed_csv,
        expected_sha256=plan.wave_a_completed_csv_sha256,
    )
    wave_b_rows = _read_csv(
        wave_b_completed_csv,
        expected_sha256=plan.wave_b_completed_csv_sha256,
    )
    wave_a_map = _load_private_map(
        wave_a_private_map,
        expected_sha256=plan.wave_a_private_map_sha256,
        expected_count=plan.expected_origin_counts["wave_a"],
    )
    wave_b_map = _load_private_map(
        wave_b_private_map,
        expected_sha256=plan.wave_b_private_map_sha256,
        expected_count=plan.expected_origin_counts["wave_b"],
    )
    documents = _final_documents(
        final_rows,
        bindings=bindings,
        historical_test=historical_test,
        plan=plan,
    )
    documents.extend(
        _wave_documents(
            wave_a_rows,
            wave_a_map,
            origin="wave_a",
            bindings=bindings,
            plan=plan,
        )
    )
    documents.extend(
        _wave_documents(
            wave_b_rows,
            wave_b_map,
            origin="wave_b",
            bindings=bindings,
            plan=plan,
        )
    )
    documents.sort(key=lambda item: (item.source_post_id, item.source_version))
    identities = [item.identity for item in documents]
    member_keys = [item.member_key for item in documents]
    labels = Counter(item.tourism_label for item in documents)
    origins = Counter(item.evidence_origin for item in documents)
    historical_count = sum(item.historical_test_consumed for item in documents)
    if (
        len(documents) != plan.expected_training_count
        or len(set(identities)) != len(documents)
        or len(set(member_keys)) != len(documents)
        or labels
        != {
            "related": plan.expected_related_count,
            "unrelated": plan.expected_unrelated_count,
        }
        or dict(origins) != dict(plan.expected_origin_counts)
        or historical_count != 148
        or not historical_test.issubset(set(identities))
    ):
        raise ModelRetrainingSnapshotError(
            "model_retraining_snapshot_contract_mismatch"
        )
    binding_payload = [
        {
            "member_key": item.member_key,
            "component_id": item.component_id,
            "normalized_sha256": item.normalized_sha256,
            "tourism_label": item.tourism_label,
            "evidence_origin": item.evidence_origin,
            "historical_test_consumed": item.historical_test_consumed,
        }
        for item in documents
    ]
    member_binding_sha256 = hashlib.sha256(
        json.dumps(
            binding_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return RetrainingSnapshot(
        documents=tuple(documents),
        count=len(documents),
        related_count=labels["related"],
        unrelated_count=labels["unrelated"],
        component_count=len({item.component_id for item in documents}),
        origin_counts=dict(sorted(origins.items())),
        historical_test_consumed_count=historical_count,
        leakage_output_sha256=leakage_sha256,
        member_binding_sha256=member_binding_sha256,
    )
