"""唯一最终700条不重复建模参考集的只读证据验证器。

训练入口只调用本模块接受 ``final-nonduplicate-model-reference`` CSV 与其唯一
配对的 ``finalized`` manifest。旧完成 CSV、重复复核、候补队列和补充标注文件
均因字段或 artifact 契约不同而被拒绝。
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

from tourism_ugc_study.annotation.reference_artifacts import readonly_connection
from tourism_ugc_study.annotation.reference_contract import (
    ALLOWED_LABELS,
    FINAL_PROBABILITY_COUNT,
    FINAL_REFERENCE_CONTRACT,
    FINAL_REFERENCE_FIELDS,
    FINAL_REFERENCE_ROW_COUNT,
    FINAL_REFERENCE_STATUS,
    FINAL_TARGETED_COUNT,
    ReferenceDatasetError,
    SourceIdentity,
    canonical_sha256,
    text_sha256,
)


REFERENCE_FIELDS = FINAL_REFERENCE_FIELDS


class ReferenceEvidenceError(RuntimeError):
    """最终参考证据不满足失败关闭契约时抛出的去敏异常。

    Attributes:
        reason_code: 不包含正文、作者身份或私有路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化只公开稳定失败码的异常。

        Args:
            reason_code: 供 CLI、测试和训练 manifest 使用的失败码。
        """

        super().__init__("final reference evidence validation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ReferenceValidationResult:
    """最终700条参考集通过验证后的非敏感摘要。

    Attributes:
        csv_sha256: 最终 CSV 原始字节 SHA-256。
        manifest_sha256: 唯一配对 manifest 原始字节 SHA-256。
        row_count: 固定为700。
        label_counts: ``related`` 与 ``unrelated`` 计数。
        frame_counts: 固定为 ``probability=500``、``targeted=200``。
        member_manifest_sha256: 最终成员、标签、框和分量摘要。
        candidate_build_id: 所属冻结候选构建身份。
        probability_estimation_status: ``valid`` 或明确不可用状态。
        database_annotation_count: 数据库标签副本数；成功结果恒为零。
    """

    csv_sha256: str
    manifest_sha256: str
    row_count: int
    label_counts: Mapping[str, int]
    frame_counts: Mapping[str, int]
    member_manifest_sha256: str
    candidate_build_id: str
    probability_estimation_status: str
    database_annotation_count: int


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256。

    Args:
        path: 待读取文件。

    Returns:
        小写十六进制 SHA-256。
    """

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReferenceEvidenceError("final_reference_file_hash_failed") from exc
    return digest.hexdigest()


def _load_manifest(path: Path) -> Mapping[str, Any]:
    """读取最终 manifest JSON 对象。

    Args:
        path: manifest 路径。

    Returns:
        已解析映射。

    Raises:
        ReferenceEvidenceError: 文件不可读、JSON 非法或顶层不是对象。
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceEvidenceError("final_reference_manifest_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ReferenceEvidenceError("final_reference_manifest_not_an_object")
    return value


def _load_rows(path: Path) -> list[dict[str, str]]:
    """按唯一最终字段契约读取 CSV。

    Args:
        path: 最终参考 CSV。

    Returns:
        保留原始字符串值的逐行字典。

    Raises:
        ReferenceEvidenceError: 文件不可读或字段不完全匹配。旧完成 CSV 和所有
            中间 artifact 会在这里直接失败。
    """

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != FINAL_REFERENCE_FIELDS:
                raise ReferenceEvidenceError("final_reference_csv_field_contract_mismatch")
            rows = list(reader)
            expected = set(FINAL_REFERENCE_FIELDS)
            if any(
                set(row) != expected or any(value is None for value in row.values())
                for row in rows
            ):
                raise ReferenceEvidenceError("final_reference_csv_row_structure_invalid")
            return rows
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReferenceEvidenceError("final_reference_csv_unreadable") from exc


def _manifest_count(value: Any, reason_code: str) -> int:
    """解析 manifest 中的非负 JSON 整数。

    Args:
        value: 待检查值。
        reason_code: 类型或范围错误时使用的稳定失败码。

    Returns:
        非负整数。

    Raises:
        ReferenceEvidenceError: 值为布尔型、非整数或负数。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReferenceEvidenceError(reason_code)
    return value


def _positive_identity(value: str) -> int:
    """解析 CSV 中的规范正整数身份。

    Args:
        value: 原始字段。

    Returns:
        正整数身份。

    Raises:
        ReferenceEvidenceError: 字段不是规范正整数。
    """

    stripped = value.strip()
    try:
        parsed = int(stripped)
    except ValueError as exc:
        raise ReferenceEvidenceError("final_reference_source_identity_invalid") from exc
    if parsed <= 0 or str(parsed) != stripped:
        raise ReferenceEvidenceError("final_reference_source_identity_invalid")
    return parsed


def _optional_float(value: str, reason_code: str) -> float | None:
    """解析空白或有限浮点字段。

    Args:
        value: CSV 原始字符串。
        reason_code: 非法时的稳定失败码。

    Returns:
        空白时为 ``None``，否则为有限浮点数。

    Raises:
        ReferenceEvidenceError: 值不能解析或不是有限数。
    """

    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = float(stripped)
    except ValueError as exc:
        raise ReferenceEvidenceError(reason_code) from exc
    if not math.isfinite(parsed):
        raise ReferenceEvidenceError(reason_code)
    return parsed


def _row_projection(row: Mapping[str, str]) -> dict[str, Any]:
    """把最终 CSV 行转换为成员哈希使用的类型稳定投影。

    Args:
        row: 已通过字段契约读取的 CSV 行。

    Returns:
        与最终 artifact 写入器相同的成员对象。
    """

    identity = SourceIdentity(
        _positive_identity(row["source_post_id"]),
        _positive_identity(row["source_version"]),
    )
    return {
        "identity": identity.as_list(),
        "task_id": row["task_id"].strip(),
        "normalized_sha256": text_sha256(row["normalized_model_text"]),
        "tourism_label": row["tourism_label"].strip(),
        "sample_frame": row["sample_frame"].strip(),
        "selection_reason_code": row["selection_reason_code"].strip(),
        "selection_rank": _positive_identity(row["selection_rank"]),
        "inclusion_probability": _optional_float(
            row["inclusion_probability"], "final_reference_probability_invalid"
        ),
        "analysis_weight": _optional_float(
            row["analysis_weight"], "final_reference_weight_invalid"
        ),
        "evidence_origin": row["evidence_origin"].strip(),
        "duplicate_component_id": row["duplicate_component_id"].strip(),
    }


def _sample_member_manifest(
    connection: sqlite3.Connection, sample_run_id: str
) -> str:
    """按旧抽样封存顺序重建数据库成员摘要。

    Args:
        connection: 已以只读模式打开的派生库连接。
        sample_run_id: 最终 manifest 绑定的旧抽样运行身份。

    Returns:
        与一次性迁移入口使用相同字段和顺序的 SHA-256。
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


def _validate_database_bindings(
    rows: list[dict[str, str]],
    projections: list[dict[str, Any]],
    database_by_identity: Mapping[tuple[int, int], sqlite3.Row],
    sample_by_identity: Mapping[tuple[int, int], sqlite3.Row],
    legacy_sample_run_id: str,
) -> None:
    """把最终成员逐条绑定到候选人口和旧抽样谱系。

    Args:
        rows: 最终 CSV 原始行。
        projections: 与 ``rows`` 同序的类型稳定投影。
        database_by_identity: 冻结候选人口的身份索引。
        sample_by_identity: 旧700条抽样成员的身份索引。
        legacy_sample_run_id: 用于复算既有人工任务身份的抽样运行 ID。

    Raises:
        ReferenceEvidenceError: 正文、结构、任务或抽样字段不能与数据库证明一致。
    """

    try:
        for row, projection in zip(rows, projections, strict=True):
            identity = tuple(projection["identity"])
            if identity not in database_by_identity:
                raise ReferenceEvidenceError(
                    "final_reference_member_not_in_candidate_build"
                )
            database_row = database_by_identity[identity]
            if row["normalized_model_text"] != str(
                database_row["normalized_model_text"]
            ):
                raise ReferenceEvidenceError("final_reference_normalized_text_mismatch")
            if str(database_row["structure_status"]) != "usable":
                raise ReferenceEvidenceError("final_reference_structure_not_usable")
            if projection["evidence_origin"] == "supplemental_annotation":
                if identity in sample_by_identity:
                    raise ReferenceEvidenceError("replacement_member_in_legacy_sample")
                expected_task_id = str(database_row["task_id"])
            else:
                sample_row = sample_by_identity.get(identity)
                if sample_row is None:
                    raise ReferenceEvidenceError("existing_member_not_in_legacy_sample")
                expected_task_id = canonical_sha256(
                    [legacy_sample_run_id, identity[0]]
                )[:32]
                expected_probability = (
                    None
                    if sample_row["inclusion_probability_ppm"] is None
                    else int(sample_row["inclusion_probability_ppm"]) / 1_000_000
                )
                expected_weight = (
                    None
                    if sample_row["analysis_weight"] is None
                    else float(sample_row["analysis_weight"])
                )
                if (
                    projection["sample_frame"] != str(sample_row["sample_frame"])
                    or projection["selection_reason_code"]
                    != str(sample_row["selection_reason_code"])
                    or projection["selection_rank"]
                    != int(sample_row["selection_rank"])
                    or projection["inclusion_probability"] != expected_probability
                    or projection["analysis_weight"] != expected_weight
                ):
                    raise ReferenceEvidenceError(
                        "existing_member_sample_lineage_mismatch"
                    )
            if projection["task_id"] != expected_task_id:
                raise ReferenceEvidenceError("final_reference_task_identity_mismatch")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceEvidenceError("final_reference_database_value_invalid") from exc


def _validate_manifest_shape(
    manifest: Mapping[str, Any], csv_path: Path, csv_sha256: str
) -> tuple[str, str, str]:
    """校验最终 manifest 的权威身份、状态、计数和必需哈希。

    Args:
        manifest: 已解析 manifest。
        csv_path: 配对 CSV。
        csv_sha256: CSV 实际字节摘要。

    Returns:
        ``(candidate_build_id, probability_estimation_status, legacy_sample_run_id)``。

    Raises:
        ReferenceEvidenceError: 任一最终契约声明缺失或不一致。
    """

    if manifest.get("artifact_contract") != FINAL_REFERENCE_CONTRACT:
        raise ReferenceEvidenceError("final_reference_manifest_contract_mismatch")
    if manifest.get("status") != FINAL_REFERENCE_STATUS:
        raise ReferenceEvidenceError("final_reference_manifest_not_finalized")
    csv_section = manifest.get("csv")
    identities = manifest.get("identities")
    hashes = manifest.get("hashes")
    probability = manifest.get("probability_estimation")
    evidence_policy = manifest.get("evidence_policy")
    if not all(
        isinstance(value, Mapping)
        for value in (csv_section, identities, hashes, probability, evidence_policy)
    ):
        raise ReferenceEvidenceError("final_reference_manifest_sections_missing")
    if (
        csv_section.get("logical_name") != csv_path.name
        or csv_section.get("field_contract") != FINAL_REFERENCE_CONTRACT
        or csv_section.get("sha256") != csv_sha256
        or hashes.get("output_csv_sha256") != csv_sha256
    ):
        raise ReferenceEvidenceError("final_reference_csv_binding_mismatch")
    required_counts = {
        "row_count": FINAL_REFERENCE_ROW_COUNT,
        "blank_label_count": 0,
        "uncertain_label_count": 0,
        "unique_source_identity_count": FINAL_REFERENCE_ROW_COUNT,
        "confirmed_duplicate_pair_count_in_final_reference": 0,
    }
    for field, expected in required_counts.items():
        if _manifest_count(manifest.get(field), f"final_reference_{field}_invalid") != expected:
            raise ReferenceEvidenceError(f"final_reference_{field}_invalid")
    if manifest.get("sample_frame_counts") != {
        "probability": FINAL_PROBABILITY_COUNT,
        "targeted": FINAL_TARGETED_COUNT,
    }:
        raise ReferenceEvidenceError("final_reference_manifest_frame_counts_invalid")
    if (
        evidence_policy.get("authoritative_csv_count") != 1
        or evidence_policy.get("lineage_artifacts_are_authoritative") is not False
    ):
        raise ReferenceEvidenceError("final_reference_evidence_policy_invalid")
    required_hashes = {
        "input_csv_sha256",
        "input_manifest_sha256",
        "input_member_sha256",
        "duplicate_decision_sha256",
        "initial_duplicate_candidate_sha256",
        "initial_duplicate_decision_manifest_sha256",
        "replacement_duplicate_decision_sha256",
        "replacement_duplicate_candidate_sha256",
        "replacement_duplicate_input_member_sha256",
        "replacement_duplicate_decision_manifest_sha256",
        "replacement_queue_sha256",
        "supplemental_label_csv_sha256",
        "supplemental_label_manifest_sha256",
        "label_resolution_evidence_sha256",
        "label_resolution_manifest_sha256",
        "replacement_label_resolution_evidence_sha256",
        "replacement_label_resolution_manifest_sha256",
        "final_member_sha256",
        "output_csv_sha256",
    }
    if set(hashes) != required_hashes or any(
        not isinstance(hashes[field], str)
        or len(hashes[field]) != 64
        or any(character not in "0123456789abcdef" for character in hashes[field])
        for field in required_hashes
    ):
        raise ReferenceEvidenceError("final_reference_hash_contract_invalid")
    for identity_field in (
        "label_guide_id",
        "candidate_build_id",
        "normalization_rule_id",
        "legacy_sample_run_id",
    ):
        if not isinstance(identities.get(identity_field), str) or not identities[identity_field]:
            raise ReferenceEvidenceError("final_reference_rule_identity_missing")
    duplicate_algorithm = manifest.get("duplicate_candidate_algorithm")
    expected_algorithm_values = {
        "algorithm_id": "normalized-char-3-5gram-tfidf-cosine-all-pairs",
        "analyzer": "char",
        "ngram_range": [3, 5],
        "threshold": 0.80,
        "threshold_role": "candidate_only",
        "lowercase": False,
        "norm": "l2",
        "dtype": "float64",
        "use_idf": True,
        "smooth_idf": True,
        "sublinear_tf": False,
    }
    if (
        not isinstance(duplicate_algorithm, Mapping)
        or set(duplicate_algorithm)
        != set(expected_algorithm_values).union({"numpy", "scikit_learn"})
        or any(
            duplicate_algorithm.get(field) != expected
            for field, expected in expected_algorithm_values.items()
        )
        or not isinstance(duplicate_algorithm.get("numpy"), str)
        or not duplicate_algorithm.get("numpy")
        or not isinstance(duplicate_algorithm.get("scikit_learn"), str)
        or not duplicate_algorithm.get("scikit_learn")
    ):
        raise ReferenceEvidenceError("final_reference_duplicate_algorithm_invalid")
    representative_rule = manifest.get("representative_selection_rule")
    if representative_rule != {
        "primary": "normalized_text_completeness_descending",
        "tie_breaker": "source_identity_ascending",
        "sample_frame_used": False,
        "platform_used": False,
        "tourism_label_used": False,
    }:
        raise ReferenceEvidenceError("final_reference_representative_rule_invalid")
    reviewed_max_queue_rank = manifest.get("replacement_reviewed_max_queue_rank")
    if (
        isinstance(reviewed_max_queue_rank, bool)
        or not isinstance(reviewed_max_queue_rank, int)
        or reviewed_max_queue_rank < 0
    ):
        raise ReferenceEvidenceError("replacement_reviewed_prefix_invalid")
    probability_status = str(probability.get("status") or "")
    if probability_status not in {"valid", "unavailable_after_replacement"}:
        raise ReferenceEvidenceError("probability_estimation_status_invalid")
    probability_replacements = _manifest_count(
        probability.get("probability_replacement_count"),
        "probability_replacement_count_invalid",
    )
    _manifest_count(
        probability.get("targeted_replacement_count"),
        "targeted_replacement_count_invalid",
    )
    if probability_status == "valid" and (
        probability_replacements != 0 or probability.get("reason_code") is not None
    ):
        raise ReferenceEvidenceError("probability_estimation_status_inconsistent")
    if probability_status == "unavailable_after_replacement" and (
        probability_replacements <= 0
        or probability.get("reason_code")
        != "replacement_joint_inclusion_probability_not_proven"
    ):
        raise ReferenceEvidenceError("probability_estimation_unavailable_reason_missing")
    return (
        str(identities["candidate_build_id"]),
        probability_status,
        str(identities["legacy_sample_run_id"]),
    )


def validate_reference_evidence(
    csv_path: str | Path,
    manifest_path: str | Path,
    derived_db: str | Path,
    *,
    expected_label_guide_version: str,
    expected_normalization_rule_id: str | None = None,
) -> ReferenceValidationResult:
    """验证唯一最终700条 CSV、finalized manifest 和只读数据库身份。

    Args:
        csv_path: 最终权威 CSV；旧完成或中间 CSV 不被接受。
        manifest_path: 与该 CSV 唯一配对的 finalized manifest。
        derived_db: 保有候选构建、规范化文本和泄漏谱系的只读派生库。
        expected_label_guide_version: 稳定配置冻结的标签手册身份。
        expected_normalization_rule_id: 可选的冻结规范化规则身份。

    Returns:
        不含正文、作者或路径的验证摘要。

    Raises:
        ReferenceEvidenceError: 任一契约、状态、计数、标签、身份、重复关系、
            权重、哈希、文本或数据库只读约束不满足。
    """

    try:
        csv_file = Path(csv_path).expanduser().resolve(strict=True)
        manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ReferenceEvidenceError("final_reference_artifact_not_found") from exc
    csv_hash = _file_sha256(csv_file)
    manifest_hash = _file_sha256(manifest_file)
    manifest = _load_manifest(manifest_file)
    candidate_build_id, probability_status, legacy_sample_run_id = _validate_manifest_shape(
        manifest, csv_file, csv_hash
    )
    identities_section = manifest["identities"]
    if identities_section.get("label_guide_id") != expected_label_guide_version:
        raise ReferenceEvidenceError("final_reference_label_guide_mismatch")
    if (
        expected_normalization_rule_id is not None
        and identities_section.get("normalization_rule_id") != expected_normalization_rule_id
    ):
        raise ReferenceEvidenceError("final_reference_normalization_rule_mismatch")
    rows = _load_rows(csv_file)
    if len(rows) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceEvidenceError("final_reference_csv_row_count_invalid")
    projections = [_row_projection(row) for row in rows]
    source_identities = [tuple(item["identity"]) for item in projections]
    if len(set(source_identities)) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceEvidenceError("final_reference_identity_not_unique")
    tasks = [item["task_id"] for item in projections]
    if any(not task for task in tasks) or len(set(tasks)) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceEvidenceError("final_reference_task_identity_not_unique")
    components = [item["duplicate_component_id"] for item in projections]
    if any(not component for component in components) or len(set(components)) != len(components):
        raise ReferenceEvidenceError("final_reference_duplicate_component_repeated")
    labels = Counter(item["tourism_label"] for item in projections)
    if set(labels) - ALLOWED_LABELS or sum(labels.values()) != FINAL_REFERENCE_ROW_COUNT:
        raise ReferenceEvidenceError("final_reference_label_not_final")
    if manifest.get("label_counts") != dict(sorted(labels.items())):
        raise ReferenceEvidenceError("final_reference_label_counts_mismatch")
    frames = Counter(item["sample_frame"] for item in projections)
    if frames != {"probability": FINAL_PROBABILITY_COUNT, "targeted": FINAL_TARGETED_COUNT}:
        raise ReferenceEvidenceError("final_reference_frame_counts_invalid")
    evidence_origins = Counter(item["evidence_origin"] for item in projections)
    if set(evidence_origins) - {
        "existing_representative",
        "supplemental_annotation",
    }:
        raise ReferenceEvidenceError("final_reference_evidence_origin_invalid")
    reviewed_max_queue_rank = int(manifest["replacement_reviewed_max_queue_rank"])
    if any(
        item["evidence_origin"] == "supplemental_annotation"
        and item["selection_rank"] > reviewed_max_queue_rank
        for item in projections
    ):
        raise ReferenceEvidenceError("replacement_member_outside_reviewed_prefix")
    supplemental_probability_count = sum(
        item["evidence_origin"] == "supplemental_annotation"
        and item["sample_frame"] == "probability"
        for item in projections
    )
    supplemental_targeted_count = sum(
        item["evidence_origin"] == "supplemental_annotation"
        and item["sample_frame"] == "targeted"
        for item in projections
    )
    probability_section = manifest["probability_estimation"]
    if (
        probability_section.get("probability_replacement_count")
        != supplemental_probability_count
        or probability_section.get("targeted_replacement_count")
        != supplemental_targeted_count
    ):
        raise ReferenceEvidenceError("final_reference_replacement_count_mismatch")
    for item in projections:
        if not item["selection_reason_code"]:
            raise ReferenceEvidenceError("final_reference_lineage_field_blank")
        probability = item["inclusion_probability"]
        weight = item["analysis_weight"]
        if item["sample_frame"] == "targeted":
            if probability is not None or weight is not None:
                raise ReferenceEvidenceError("final_targeted_weight_must_be_blank")
            continue
        if item["evidence_origin"] == "supplemental_annotation":
            if probability is not None or weight is not None:
                raise ReferenceEvidenceError("replacement_probability_weight_inherited")
            continue
        if probability is None or weight is None:
            raise ReferenceEvidenceError("final_probability_weight_missing")
        if (
            not 0.0 < probability <= 1.0
            or not math.isclose(weight, 1.0 / probability, rel_tol=2e-5)
        ):
            raise ReferenceEvidenceError("final_probability_weight_invalid")
    member_hash = canonical_sha256(sorted(projections, key=lambda item: item["identity"]))
    if manifest["hashes"].get("final_member_sha256") != member_hash:
        raise ReferenceEvidenceError("final_reference_member_hash_mismatch")
    final_identity_set = set(source_identities)
    pairs = manifest.get("confirmed_duplicate_pairs")
    if not isinstance(pairs, list):
        raise ReferenceEvidenceError("final_reference_duplicate_pairs_missing")
    lineage_identities = set(final_identity_set)
    parsed_pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for pair in pairs:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(identity, list) or len(identity) != 2 for identity in pair)
        ):
            raise ReferenceEvidenceError("final_reference_duplicate_pair_invalid")
        try:
            left = (_positive_identity(str(pair[0][0])), _positive_identity(str(pair[0][1])))
            right = (_positive_identity(str(pair[1][0])), _positive_identity(str(pair[1][1])))
        except ReferenceEvidenceError as exc:
            raise ReferenceEvidenceError("final_reference_duplicate_pair_invalid") from exc
        if left >= right:
            raise ReferenceEvidenceError("final_reference_duplicate_pair_invalid")
        parsed_pairs.append((left, right))
        lineage_identities.update((left, right))
    parent = {identity: identity for identity in lineage_identities}

    def find(identity: tuple[int, int]) -> tuple[int, int]:
        """返回重复并查集的稳定根并压缩路径。"""

        root = parent[identity]
        if root != identity:
            parent[identity] = find(root)
        return parent[identity]

    def union(left: tuple[int, int], right: tuple[int, int]) -> None:
        """按稳定身份合并两个确认重复分量。"""

        left_root, right_root = find(left), find(right)
        root, child = sorted((left_root, right_root))
        parent[child] = root

    for left, right in parsed_pairs:
        union(left, right)
    by_exact_hash: dict[str, list[tuple[int, int]]] = {}
    for projection in projections:
        by_exact_hash.setdefault(str(projection["normalized_sha256"]), []).append(
            tuple(projection["identity"])
        )
    for exact_members in by_exact_hash.values():
        for identity in exact_members[1:]:
            union(exact_members[0], identity)
    grouped: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for identity in sorted(lineage_identities):
        grouped.setdefault(find(identity), []).append(identity)
    component_by_identity: dict[tuple[int, int], str] = {}
    for component_members in grouped.values():
        final_members = final_identity_set.intersection(component_members)
        if len(final_members) > 1:
            raise ReferenceEvidenceError("final_reference_confirmed_duplicate_present")
        component_id = canonical_sha256(
            [list(identity) for identity in sorted(component_members)]
        )[:32]
        for identity in component_members:
            component_by_identity[identity] = component_id
    for projection in projections:
        identity = tuple(projection["identity"])
        if projection["duplicate_component_id"] != component_by_identity[identity]:
            raise ReferenceEvidenceError("final_reference_duplicate_component_mismatch")
    try:
        connection = readonly_connection(derived_db)
    except ReferenceDatasetError as exc:
        raise ReferenceEvidenceError("final_reference_database_readonly_open_failed") from exc
    try:
        build = connection.execute(
            """
            SELECT rules_version, rules_sha256, status, is_complete_corpus
            FROM text_candidate_builds WHERE build_id = ?
            """,
            (candidate_build_id,),
        ).fetchone()
        if build is None or build["status"] != "finalized" or not int(build["is_complete_corpus"]):
            raise ReferenceEvidenceError("final_reference_candidate_build_not_finalized")
        database_normalization_rule_id = (
            f"{build['rules_version']}+sha256:{build['rules_sha256']}"
        )
        if database_normalization_rule_id != str(
            identities_section["normalization_rule_id"]
        ):
            raise ReferenceEvidenceError("final_reference_database_normalization_mismatch")
        sample = connection.execute(
            """
            SELECT candidate_build_id, guide_version, probability_count,
                   targeted_count, seal_status, member_manifest_sha256
            FROM text_sampling_runs WHERE sample_run_id = ?
            """,
            (legacy_sample_run_id,),
        ).fetchone()
        if (
            sample is None
            or str(sample["candidate_build_id"]) != candidate_build_id
            or str(sample["guide_version"])
            != str(identities_section["label_guide_id"])
            or int(sample["probability_count"]) != FINAL_PROBABILITY_COUNT
            or int(sample["targeted_count"]) != FINAL_TARGETED_COUNT
            or str(sample["seal_status"]) != "finalized"
        ):
            raise ReferenceEvidenceError("final_reference_sample_run_invalid")
        if _sample_member_manifest(connection, legacy_sample_run_id) != str(
            sample["member_manifest_sha256"]
        ):
            raise ReferenceEvidenceError("final_reference_sample_manifest_mismatch")
        sample_rows = connection.execute(
            """
            SELECT source_post_id, source_version, sample_frame,
                   selection_reason_code, selection_rank,
                   inclusion_probability_ppm, analysis_weight
            FROM text_sample_members WHERE sample_run_id = ?
            """,
            (legacy_sample_run_id,),
        ).fetchall()
        sample_by_identity = {
            (int(row["source_post_id"]), int(row["source_version"])): row
            for row in sample_rows
        }
        if len(sample_by_identity) != len(sample_rows):
            raise ReferenceEvidenceError("final_reference_sample_identity_not_unique")
        database_rows = connection.execute(
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
        if any(
            row["task_id"] is None
            or not str(row["task_id"]).strip()
            or row["normalized_model_text"] is None
            or not str(row["normalized_model_text"])
            for row in database_rows
        ):
            raise ReferenceEvidenceError("final_reference_database_member_null")
        database_by_identity = {
            (int(row["source_post_id"]), int(row["source_version"])): row
            for row in database_rows
        }
        if len(database_by_identity) != len(database_rows):
            raise ReferenceEvidenceError("final_reference_database_identity_not_unique")
        annotation_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_post_annotations'"
        ).fetchone()
        annotation_count = (
            int(connection.execute("SELECT COUNT(*) FROM text_post_annotations").fetchone()[0])
            if annotation_table is not None
            else 0
        )
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise ReferenceEvidenceError("reference_database_not_query_only")
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise ReferenceEvidenceError("final_reference_database_contract_invalid") from exc
    finally:
        connection.close()
    if annotation_count:
        raise ReferenceEvidenceError("final_reference_labels_copied_to_database")
    _validate_database_bindings(
        rows,
        projections,
        database_by_identity,
        sample_by_identity,
        legacy_sample_run_id,
    )
    return ReferenceValidationResult(
        csv_sha256=csv_hash,
        manifest_sha256=manifest_hash,
        row_count=FINAL_REFERENCE_ROW_COUNT,
        label_counts=dict(labels),
        frame_counts=dict(frames),
        member_manifest_sha256=member_hash,
        candidate_build_id=candidate_build_id,
        probability_estimation_status=probability_status,
        database_annotation_count=annotation_count,
    )
