"""盲化标签一致性复核的私有 artifact 读取、绑定与持久化。

生成包包含可编辑的 ``review-task.csv``、不可提交的私有解除盲化映射和包
manifest。正式采集数据库不参与本工作流，锁定测试成员与概率也不被读取。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from tourism_ugc_study.cleaning.reference_evidence import REFERENCE_FIELDS
from tourism_ugc_study.cleaning.reference_evidence import _row_projection
from tourism_ugc_study.annotation.reference_contract import canonical_sha256

from .blind_label_review import (
    BlindLabelReviewError,
    DevelopmentReviewRecord,
    select_blind_label_review,
    summarize_blind_label_review,
)
from .formal_training import FormalTrainingError, load_frozen_baseline_model


TASK_FIELDS = ("review_key", "review_text", "review_label", "review_note")
PROBABILITY_ROW_FIELDS = {
    "source_post_id",
    "source_version",
    "split_name",
    "tourism_label",
    "margin",
    "p_unrelated",
}
MAPPING_RECORD_FIELDS = {
    "review_key",
    "pair_key",
    "selection_group",
    "selection_reason",
    "matching_level",
    "source_post_id",
    "source_version",
    "split_name",
    "original_label",
    "model_label",
    "p_unrelated",
    "length_band",
    "review_text_sha256",
}
LENGTH_BANDS = (
    ("0000-0299", 0, 300),
    ("0300-0599", 300, 600),
    ("0600-1199", 600, 1200),
    ("1200-2399", 1200, 2400),
    ("2400+", 2400, None),
)


@dataclass(frozen=True)
class BlindReviewPackageResult:
    """已原子发布的私有盲化任务包去敏摘要。

    Attributes:
        model_id: 绑定的冻结 baseline 身份。
        review_id: 由输入和选择配置确定的复核身份。
        target_count: 强矛盾或短文本误差目标数。
        control_count: 匹配随机预测正确对照数。
        exact_match_control_count: 同长度层精确匹配的对照数。
        relaxed_match_control_count: 同标签、切分内最近长度匹配的对照数。
        total_count: 任务总行数。
        task_template_sha256: 初始空白任务 CSV 摘要。
        mapping_sha256: 私有解除盲化映射摘要。
        package_manifest_sha256: 包 manifest 摘要。
    """

    model_id: str
    review_id: str
    target_count: int
    control_count: int
    exact_match_control_count: int
    relaxed_match_control_count: int
    total_count: int
    task_template_sha256: str
    mapping_sha256: str
    package_manifest_sha256: str


def _canonical_bytes(value: object) -> bytes:
    """把 JSON 对象编码为唯一、拒绝非有限浮点的 UTF-8 字节。"""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BlindLabelReviewError("blind_review_json_contract_invalid") from exc
    return text.encode("utf-8") + b"\n"


def _sha256_bytes(value: bytes) -> str:
    """计算内存字节的 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256，并把路径失败去敏化。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BlindLabelReviewError("blind_review_file_hash_failed") from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为对象的 UTF-8 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BlindLabelReviewError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise BlindLabelReviewError(reason_code)
    return value


def _load_reference_rows(path: Path) -> Mapping[tuple[int, int], Mapping[str, str]]:
    """严格读取最终参考 CSV，并建立唯一成员索引。"""

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise BlindLabelReviewError("blind_review_reference_fields_invalid")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BlindLabelReviewError("blind_review_reference_unreadable") from exc
    indexed: dict[tuple[int, int], Mapping[str, str]] = {}
    try:
        for row in rows:
            identity = (int(row["source_post_id"]), int(row["source_version"]))
            if identity[0] <= 0 or identity[1] <= 0 or identity in indexed:
                raise BlindLabelReviewError("blind_review_reference_identity_invalid")
            indexed[identity] = row
    except (KeyError, TypeError, ValueError) as exc:
        raise BlindLabelReviewError("blind_review_reference_identity_invalid") from exc
    return indexed


def _length_band(length: int) -> str:
    """把文本字符数映射到冻结长度层。"""

    for name, lower, upper in LENGTH_BANDS:
        if length >= lower and (upper is None or length < upper):
            return name
    raise BlindLabelReviewError("blind_review_text_length_invalid")


def _validated_probability_rows(
    raw_rows: Any,
    *,
    expected_split: str,
) -> tuple[Mapping[str, Any], ...]:
    """校验开发概率行，拒绝测试集合名和成员重复。"""

    if not isinstance(raw_rows, list) or not raw_rows:
        raise BlindLabelReviewError("blind_review_probability_rows_invalid")
    seen: set[tuple[int, int]] = set()
    validated: list[Mapping[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping) or set(raw) != PROBABILITY_ROW_FIELDS:
            raise BlindLabelReviewError("blind_review_probability_contract_invalid")
        try:
            identity = (int(raw["source_post_id"]), int(raw["source_version"]))
            probability = float(raw["p_unrelated"])
            margin = float(raw["margin"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BlindLabelReviewError("blind_review_probability_value_invalid") from exc
        if (
            identity[0] <= 0
            or identity[1] <= 0
            or identity in seen
            or str(raw["split_name"]) != expected_split
            or str(raw["tourism_label"]) not in {"related", "unrelated"}
            or not math.isfinite(probability)
            or not 0.0 <= probability <= 1.0
            or not math.isfinite(margin)
        ):
            raise BlindLabelReviewError("blind_review_probability_value_invalid")
        seen.add(identity)
        validated.append(
            {
                "identity": identity,
                "split_name": expected_split,
                "original_label": str(raw["tourism_label"]),
                "p_unrelated": probability,
            }
        )
    return tuple(validated)


def _load_development_records(
    package_dir: Path,
    reference_csv: Path,
) -> tuple[str, str, str, tuple[DevelopmentReviewRecord, ...]]:
    """加载已验证开发概率与参考文本，但不读取切分 manifest 的测试成员。"""

    try:
        load_frozen_baseline_model(package_dir)
    except (FormalTrainingError, OSError) as exc:
        raise BlindLabelReviewError("blind_review_baseline_package_invalid") from exc
    manifest_path = package_dir / "training-manifest.json"
    manifest = _load_json(manifest_path, "blind_review_training_manifest_invalid")
    if (
        manifest.get("artifact_kind") != "formal-cleaning-baseline"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("platform_used") is not False
    ):
        raise BlindLabelReviewError("blind_review_training_state_invalid")
    lineage = manifest.get("lineage")
    artifacts = manifest.get("artifacts")
    if not isinstance(lineage, Mapping) or not isinstance(artifacts, Mapping):
        raise BlindLabelReviewError("blind_review_training_sections_missing")
    reference_sha256 = _file_sha256(reference_csv)
    if reference_sha256 != lineage.get("reference_csv_sha256"):
        raise BlindLabelReviewError("blind_review_reference_hash_mismatch")
    development_details = artifacts.get("development_probabilities")
    if not isinstance(development_details, Mapping):
        raise BlindLabelReviewError("blind_review_probability_artifact_missing")
    development_path = package_dir / str(development_details.get("filename", ""))
    development_sha256 = _file_sha256(development_path)
    if development_sha256 != development_details.get("sha256"):
        raise BlindLabelReviewError("blind_review_probability_hash_mismatch")
    development = _load_json(
        development_path,
        "blind_review_probability_artifact_invalid",
    )
    if (
        set(development) != {
            "positive_class",
            "threshold_status",
            "train_oof",
            "validation",
        }
        or development.get("positive_class") != "unrelated"
        or development.get("threshold_status") != "UNSET"
    ):
        raise BlindLabelReviewError("blind_review_probability_artifact_invalid")

    train_rows = _validated_probability_rows(
        development["train_oof"], expected_split="train_oof"
    )
    validation_rows = _validated_probability_rows(
        development["validation"], expected_split="validation"
    )
    train_members = {tuple(row["identity"]) for row in train_rows}
    validation_members = {tuple(row["identity"]) for row in validation_rows}
    if train_members & validation_members:
        raise BlindLabelReviewError("blind_review_development_overlap")
    split_counts = manifest.get("split_counts")
    if (
        not isinstance(split_counts, Mapping)
        or split_counts.get("train") != len(train_rows)
        or split_counts.get("validation") != len(validation_rows)
    ):
        raise BlindLabelReviewError("blind_review_split_count_mismatch")

    reference_rows = _load_reference_rows(reference_csv)
    records: list[DevelopmentReviewRecord] = []
    for row in (*train_rows, *validation_rows):
        identity = tuple(row["identity"])
        reference = reference_rows.get(identity)
        if (
            reference is None
            or reference.get("tourism_label") != row["original_label"]
        ):
            raise BlindLabelReviewError("blind_review_reference_join_mismatch")
        text = str(reference.get("normalized_model_text") or "")
        if not text.strip():
            raise BlindLabelReviewError("blind_review_text_blank")
        records.append(
            DevelopmentReviewRecord(
                source_post_id=identity[0],
                source_version=identity[1],
                split_name=str(row["split_name"]),
                original_label=str(row["original_label"]),
                p_unrelated=float(row["p_unrelated"]),
                review_text=text,
                length_band=_length_band(len(text)),
            )
        )
    return (
        str(manifest.get("model_id") or ""),
        _file_sha256(manifest_path),
        development_sha256,
        tuple(records),
    )


def _csv_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    """按冻结列序生成带 BOM 的 Excel 友好 UTF-8 CSV。"""

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=TASK_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _task_contract(mapping_records: Sequence[Mapping[str, Any]]) -> str:
    """计算不受人工填写和行重排影响的任务固定内容摘要。"""

    payload = [
        {
            "review_key": str(record["review_key"]),
            "review_text_sha256": str(record["review_text_sha256"]),
        }
        for record in sorted(mapping_records, key=lambda row: str(row["review_key"]))
    ]
    return _sha256_bytes(_canonical_bytes(payload))


def prepare_blind_label_review_package(
    package_dir: str | Path,
    reference_csv: str | Path,
    output_dir: str | Path,
    *,
    random_seed: int,
    probability_cutoff: float = 0.90,
    short_text_max: int = 600,
    control_ratio: int = 1,
) -> BlindReviewPackageResult:
    """生成并原子发布隐藏模型答案的私有标签复核包。

    Args:
        package_dir: 冻结 baseline 训练包目录。
        reference_csv: 训练 manifest 绑定的唯一最终参考 CSV。
        output_dir: 尚不存在的私有输出目录。
        random_seed: 匹配和打乱的固定种子。
        probability_cutoff: 强矛盾概率门。
        short_text_max: 短文本诊断误差的字符上界。
        control_ratio: 每个目标的匹配随机正确对照数。

    Returns:
        不含正文、成员身份或本机路径的任务包摘要。

    Raises:
        BlindLabelReviewError: 输入、选择、绑定或原子发布失败。
    """

    try:
        package = Path(package_dir).expanduser().resolve(strict=True)
        reference = Path(reference_csv).expanduser().resolve(strict=True)
        destination = Path(output_dir).expanduser().resolve()
    except OSError as exc:
        raise BlindLabelReviewError("blind_review_input_not_found") from exc
    if destination.exists():
        raise BlindLabelReviewError("blind_review_output_exists")
    model_id, training_manifest_sha256, development_sha256, records = (
        _load_development_records(package, reference)
    )
    selection = select_blind_label_review(
        records,
        model_id=model_id,
        random_seed=random_seed,
        probability_cutoff=probability_cutoff,
        short_text_max=short_text_max,
        control_ratio=control_ratio,
    )
    selection_config = {
        "random_seed": random_seed,
        "probability_cutoff": probability_cutoff,
        "short_text_max": short_text_max,
        "control_ratio": control_ratio,
        "matching_fields": ["original_label", "split_name", "length_band"],
        "matching_fallback": "nearest_length_same_label_split",
        "platform_used": False,
    }
    review_identity_payload = {
        "model_id": model_id,
        "reference_csv_sha256": _file_sha256(reference),
        "development_probabilities_sha256": development_sha256,
        "selection_config": selection_config,
        "members": [str(row["review_key"]) for row in selection.mapping_records],
    }
    review_id = _sha256_bytes(_canonical_bytes(review_identity_payload))[:32]
    task_bytes = _csv_bytes(selection.task_rows)
    mapping_payload = {
        "artifact_kind": "formal-cleaning-blind-label-review-map",
        "status": "private_blinded_task_prepared",
        "review_id": review_id,
        "model_id": model_id,
        "test_status": "locked_not_opened",
        "test_members_read": False,
        "test_probabilities_present": False,
        "threshold_status": "UNSET",
        "selection_config": selection_config,
        "target_count": selection.target_count,
        "control_count": selection.control_count,
        "exact_match_control_count": selection.exact_match_control_count,
        "relaxed_match_control_count": selection.relaxed_match_control_count,
        "task_contract_sha256": _task_contract(selection.mapping_records),
        "records": list(selection.mapping_records),
    }
    mapping_bytes = _canonical_bytes(mapping_payload)
    mapping_sha256 = _sha256_bytes(mapping_bytes)
    task_sha256 = _sha256_bytes(task_bytes)
    package_payload = {
        "artifact_kind": "formal-cleaning-blind-label-review-package",
        "status": "private_blinded_task_prepared",
        "review_id": review_id,
        "model_id": model_id,
        "lineage": {
            "training_manifest_sha256": training_manifest_sha256,
            "reference_csv_sha256": _file_sha256(reference),
            "development_probabilities_sha256": development_sha256,
        },
        "artifacts": {
            "review_task": {
                "filename": "review-task.csv",
                "template_sha256": task_sha256,
                "editable_fields": ["review_label", "review_note"],
            },
            "private_map": {
                "filename": "review-map.json",
                "sha256": mapping_sha256,
            },
        },
    }
    package_bytes = _canonical_bytes(package_payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{review_id}.", dir=destination.parent)
    )
    try:
        (temporary / "review-task.csv").write_bytes(task_bytes)
        (temporary / "review-map.json").write_bytes(mapping_bytes)
        (temporary / "package-manifest.json").write_bytes(package_bytes)
        temporary.rename(destination)
    except OSError as exc:
        raise BlindLabelReviewError("blind_review_package_publish_failed") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return BlindReviewPackageResult(
        model_id=model_id,
        review_id=review_id,
        target_count=selection.target_count,
        control_count=selection.control_count,
        exact_match_control_count=selection.exact_match_control_count,
        relaxed_match_control_count=selection.relaxed_match_control_count,
        total_count=selection.target_count + selection.control_count,
        task_template_sha256=task_sha256,
        mapping_sha256=mapping_sha256,
        package_manifest_sha256=_sha256_bytes(package_bytes),
    )


def _load_task_rows(path: Path) -> tuple[Mapping[str, str], ...]:
    """读取人工可编辑任务，并拒绝列变化、空固定字段或重复键。"""

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != TASK_FIELDS:
                raise BlindLabelReviewError("blind_review_task_fields_invalid")
            rows = tuple(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BlindLabelReviewError("blind_review_task_unreadable") from exc
    if not rows:
        raise BlindLabelReviewError("blind_review_task_empty")
    seen: set[str] = set()
    for row in rows:
        review_key = str(row.get("review_key", "")).strip()
        if (
            not review_key
            or review_key in seen
            or not str(row.get("review_text", "")).strip()
        ):
            raise BlindLabelReviewError("blind_review_task_fixed_fields_invalid")
        seen.add(review_key)
    return rows


def summarize_blind_label_review_package(
    review_dir: str | Path,
) -> Mapping[str, Any]:
    """校验已填写任务与私有映射，并生成未应用的解除盲化汇总。

    Args:
        review_dir: 包含任务、私有映射和包 manifest 的本地目录。

    Returns:
        不含正文、成员身份、概率或私有路径的分组汇总。

    Raises:
        BlindLabelReviewError: 包被篡改、任务固定内容变化或回填不完整。
    """

    try:
        directory = Path(review_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise BlindLabelReviewError("blind_review_package_not_found") from exc
    package = _load_json(
        directory / "package-manifest.json",
        "blind_review_package_manifest_invalid",
    )
    if (
        package.get("artifact_kind")
        != "formal-cleaning-blind-label-review-package"
        or package.get("status") != "private_blinded_task_prepared"
    ):
        raise BlindLabelReviewError("blind_review_package_manifest_invalid")
    artifacts = package.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise BlindLabelReviewError("blind_review_package_manifest_invalid")
    map_details = artifacts.get("private_map")
    task_details = artifacts.get("review_task")
    if not isinstance(map_details, Mapping) or not isinstance(task_details, Mapping):
        raise BlindLabelReviewError("blind_review_package_manifest_invalid")
    mapping_path = directory / str(map_details.get("filename", ""))
    if _file_sha256(mapping_path) != map_details.get("sha256"):
        raise BlindLabelReviewError("blind_review_mapping_hash_mismatch")
    mapping = _load_json(mapping_path, "blind_review_mapping_invalid")
    if (
        mapping.get("artifact_kind") != "formal-cleaning-blind-label-review-map"
        or mapping.get("status") != "private_blinded_task_prepared"
        or mapping.get("review_id") != package.get("review_id")
        or mapping.get("model_id") != package.get("model_id")
        or mapping.get("test_status") != "locked_not_opened"
        or mapping.get("test_members_read") is not False
        or mapping.get("test_probabilities_present") is not False
    ):
        raise BlindLabelReviewError("blind_review_mapping_invalid")
    raw_mapping_records = mapping.get("records")
    if not isinstance(raw_mapping_records, list) or not raw_mapping_records:
        raise BlindLabelReviewError("blind_review_mapping_invalid")
    if any(
        not isinstance(record, Mapping) or set(record) != MAPPING_RECORD_FIELDS
        for record in raw_mapping_records
    ):
        raise BlindLabelReviewError("blind_review_mapping_record_invalid")
    if _task_contract(raw_mapping_records) != mapping.get("task_contract_sha256"):
        raise BlindLabelReviewError("blind_review_task_contract_mismatch")

    task_path = directory / str(task_details.get("filename", ""))
    task_rows = _load_task_rows(task_path)
    mapping_by_key = {
        str(record["review_key"]): record for record in raw_mapping_records
    }
    if len(mapping_by_key) != len(raw_mapping_records):
        raise BlindLabelReviewError("blind_review_mapping_record_invalid")
    for row in task_rows:
        record = mapping_by_key.get(str(row["review_key"]).strip())
        if record is None or hashlib.sha256(
            str(row["review_text"]).encode("utf-8")
        ).hexdigest() != record["review_text_sha256"]:
            raise BlindLabelReviewError("blind_review_task_fixed_content_changed")
    summary = dict(
        summarize_blind_label_review(
            raw_mapping_records,
            task_rows,
            model_id=str(package.get("model_id") or ""),
        )
    )
    summary["review_id"] = str(package["review_id"])
    summary["lineage"] = dict(package.get("lineage", {}))
    summary["completed_task_sha256"] = _file_sha256(task_path)
    summary["private_mapping_sha256"] = str(map_details["sha256"])
    return summary


def write_blind_label_review_summary(
    review_dir: str | Path,
    output_path: str | Path,
) -> Mapping[str, Any]:
    """生成汇总并原子写入尚不存在的去敏 JSON artifact。

    Args:
        review_dir: 已完成人工回填的私有复核包目录。
        output_path: 尚不存在的汇总 JSON 文件。

    Returns:
        与输出文件内容一致的可序列化汇总。

    Raises:
        BlindLabelReviewError: 输入校验或输出持久化失败。
    """

    summary = summarize_blind_label_review_package(review_dir)
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise BlindLabelReviewError("blind_review_summary_output_exists")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_bytes(_canonical_bytes(summary))
        temporary.replace(output)
    except OSError as exc:
        raise BlindLabelReviewError("blind_review_summary_write_failed") from exc
    return summary


def apply_approved_blind_label_corrections(
    review_dir: str | Path,
    summary_path: str | Path,
    reference_csv: str | Path,
    reference_manifest: str | Path,
    output_receipt: str | Path,
    *,
    approved_review_keys: Sequence[str],
    expected_reference_csv_sha256: str,
) -> Mapping[str, Any]:
    """把用户明确批准的盲审改动原位应用到最终参考证据。

    Args:
        review_dir: 已完成且能重新通过绑定校验的私有复核包。
        summary_path: 对该任务生成的 ``completed_not_applied`` 汇总。
        reference_csv: 当前唯一最终700条 CSV；按用户要求原位更新。
        reference_manifest: 与 CSV 配对的 finalized manifest。
        output_receipt: 尚不存在的去敏应用回执 JSON。
        approved_review_keys: 用户明确批准应用的改标 review key。
        expected_reference_csv_sha256: 操作前必须精确匹配的 CSV 摘要。

    Returns:
        不含正文、作者或本机路径的应用回执。

    Raises:
        BlindLabelReviewError: 汇总、审批集合、当前标签、manifest 或原子写入
            不能形成一一对应且可追溯的更新。

    Notes:
        未批准的盲审改动会显式记录为维持原标签；函数不接触数据库、测试
        概率、模型或阈值。
    """

    try:
        directory = Path(review_dir).expanduser().resolve(strict=True)
        summary_file = Path(summary_path).expanduser().resolve(strict=True)
        csv_file = Path(reference_csv).expanduser().resolve(strict=True)
        manifest_file = Path(reference_manifest).expanduser().resolve(strict=True)
        receipt_file = Path(output_receipt).expanduser().resolve()
    except OSError as exc:
        raise BlindLabelReviewError("blind_review_apply_input_not_found") from exc
    if receipt_file.exists():
        raise BlindLabelReviewError("blind_review_apply_receipt_exists")
    if (
        len(expected_reference_csv_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_reference_csv_sha256)
        or _file_sha256(csv_file) != expected_reference_csv_sha256
    ):
        raise BlindLabelReviewError("blind_review_apply_reference_hash_mismatch")

    persisted_summary = _load_json(
        summary_file,
        "blind_review_apply_summary_invalid",
    )
    current_summary = summarize_blind_label_review_package(directory)
    if dict(persisted_summary) != dict(current_summary):
        raise BlindLabelReviewError("blind_review_apply_summary_stale")
    if (
        persisted_summary.get("status") != "completed_not_applied"
        or persisted_summary.get("automatic_reference_update") is not False
        or persisted_summary.get("test_members_read") is not False
        or persisted_summary.get("test_probabilities_present") is not False
    ):
        raise BlindLabelReviewError("blind_review_apply_summary_invalid")
    lineage = persisted_summary.get("lineage")
    if (
        not isinstance(lineage, Mapping)
        or lineage.get("reference_csv_sha256") != expected_reference_csv_sha256
    ):
        raise BlindLabelReviewError("blind_review_apply_summary_lineage_mismatch")

    changed = {
        str(record["review_key"]): record
        for record in persisted_summary.get("records", [])
        if isinstance(record, Mapping) and record.get("decision") == "changed"
    }
    approved = tuple(sorted(str(key).strip() for key in approved_review_keys))
    if (
        not approved
        or any(not key for key in approved)
        or len(set(approved)) != len(approved)
        or not set(approved) <= set(changed)
    ):
        raise BlindLabelReviewError("blind_review_apply_approval_invalid")

    package = _load_json(
        directory / "package-manifest.json",
        "blind_review_package_manifest_invalid",
    )
    artifacts = package.get("artifacts")
    if not isinstance(artifacts, Mapping) or not isinstance(
        artifacts.get("private_map"), Mapping
    ):
        raise BlindLabelReviewError("blind_review_package_manifest_invalid")
    map_details = artifacts["private_map"]
    mapping_path = directory / str(map_details.get("filename", ""))
    if _file_sha256(mapping_path) != map_details.get("sha256"):
        raise BlindLabelReviewError("blind_review_mapping_hash_mismatch")
    mapping = _load_json(mapping_path, "blind_review_mapping_invalid")
    raw_mapping_records = mapping.get("records")
    if not isinstance(raw_mapping_records, list):
        raise BlindLabelReviewError("blind_review_mapping_invalid")
    mapping_by_key = {
        str(record["review_key"]): record
        for record in raw_mapping_records
        if isinstance(record, Mapping) and "review_key" in record
    }
    if len(mapping_by_key) != len(raw_mapping_records) or not set(changed) <= set(
        mapping_by_key
    ):
        raise BlindLabelReviewError("blind_review_mapping_record_invalid")

    try:
        with csv_file.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise BlindLabelReviewError("blind_review_apply_reference_fields_invalid")
            reference_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BlindLabelReviewError("blind_review_apply_reference_unreadable") from exc
    if len(reference_rows) != 700:
        raise BlindLabelReviewError("blind_review_apply_reference_count_invalid")
    reference_by_identity: dict[tuple[int, int], dict[str, str]] = {}
    try:
        for row in reference_rows:
            identity = (int(row["source_post_id"]), int(row["source_version"]))
            if identity in reference_by_identity:
                raise BlindLabelReviewError(
                    "blind_review_apply_reference_identity_repeated"
                )
            reference_by_identity[identity] = row
    except (KeyError, TypeError, ValueError) as exc:
        raise BlindLabelReviewError(
            "blind_review_apply_reference_identity_invalid"
        ) from exc

    for review_key, mapping_record in mapping_by_key.items():
        try:
            identity = (
                int(mapping_record["source_post_id"]),
                int(mapping_record["source_version"]),
            )
            original_label = str(mapping_record["original_label"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BlindLabelReviewError("blind_review_mapping_record_invalid") from exc
        row = reference_by_identity.get(identity)
        if row is None or row.get("tourism_label") != original_label:
            raise BlindLabelReviewError(
                "blind_review_apply_reference_label_changed"
            )
        if review_key in approved:
            reviewed_label = str(changed[review_key]["reviewed_label"])
            if reviewed_label not in {"related", "unrelated"}:
                raise BlindLabelReviewError("blind_review_apply_label_invalid")
            row["tourism_label"] = reviewed_label

    csv_stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        csv_stream,
        fieldnames=REFERENCE_FIELDS,
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(reference_rows)
    new_csv_bytes = csv_stream.getvalue().encode("utf-8")
    new_csv_sha256 = _sha256_bytes(new_csv_bytes)
    projections = [_row_projection(row) for row in reference_rows]
    new_member_sha256 = canonical_sha256(
        sorted(projections, key=lambda item: item["identity"])
    )
    label_counts = dict(
        sorted(Counter(row["tourism_label"] for row in reference_rows).items())
    )

    old_csv_bytes = csv_file.read_bytes()
    old_manifest_bytes = manifest_file.read_bytes()
    old_manifest_sha256 = _sha256_bytes(old_manifest_bytes)
    manifest = _load_json(
        manifest_file,
        "blind_review_apply_reference_manifest_invalid",
    )
    if (
        manifest.get("artifact_contract") != "final-nonduplicate-model-reference"
        or manifest.get("status") != "finalized"
        or not isinstance(manifest.get("csv"), Mapping)
        or not isinstance(manifest.get("hashes"), Mapping)
    ):
        raise BlindLabelReviewError("blind_review_apply_reference_manifest_invalid")
    history = manifest.get("label_consistency_reviews", [])
    if not isinstance(history, list) or any(
        isinstance(item, Mapping)
        and item.get("review_id") == persisted_summary.get("review_id")
        for item in history
    ):
        raise BlindLabelReviewError("blind_review_apply_history_invalid")
    approved_records = []
    for key in approved:
        mapping_record = mapping_by_key[key]
        approved_records.append(
            {
                "review_key": key,
                "source_identity": [
                    int(mapping_record["source_post_id"]),
                    int(mapping_record["source_version"]),
                ],
                "original_label": str(changed[key]["original_label"]),
                "final_label": str(changed[key]["reviewed_label"]),
            }
        )
    rejected_records = [
        {
            "review_key": key,
            "source_identity": [
                int(mapping_by_key[key]["source_post_id"]),
                int(mapping_by_key[key]["source_version"]),
            ],
            "original_label": str(record["original_label"]),
            "blind_review_label": str(record["reviewed_label"]),
            "final_label": str(record["original_label"]),
        }
        for key, record in sorted(changed.items())
        if key not in set(approved)
    ]
    summary_sha256 = _file_sha256(summary_file)
    application = {
        "review_id": str(persisted_summary["review_id"]),
        "model_id": str(persisted_summary["model_id"]),
        "status": "applied_with_human_adjudication",
        "reviewed_count": len(persisted_summary["records"]),
        "blind_proposed_change_count": len(changed),
        "approved_change_count": len(approved_records),
        "rejected_change_count": len(rejected_records),
        "summary_sha256": summary_sha256,
        "completed_task_sha256": str(persisted_summary["completed_task_sha256"]),
        "private_mapping_sha256": str(persisted_summary["private_mapping_sha256"]),
        "previous_reference_csv_sha256": expected_reference_csv_sha256,
        "previous_reference_manifest_sha256": old_manifest_sha256,
        "approved_changes": approved_records,
        "rejected_changes": rejected_records,
    }
    updated_manifest = dict(manifest)
    updated_manifest["csv"] = dict(manifest["csv"])
    updated_manifest["hashes"] = dict(manifest["hashes"])
    updated_manifest["csv"]["sha256"] = new_csv_sha256
    updated_manifest["hashes"]["output_csv_sha256"] = new_csv_sha256
    updated_manifest["hashes"]["final_member_sha256"] = new_member_sha256
    updated_manifest["label_counts"] = label_counts
    updated_manifest["label_consistency_reviews"] = [*history, application]
    new_manifest_bytes = _canonical_bytes(updated_manifest)
    new_manifest_sha256 = _sha256_bytes(new_manifest_bytes)
    receipt = {
        "artifact_kind": "formal-cleaning-blind-label-review-application",
        "status": "applied_with_human_adjudication",
        "review_id": str(persisted_summary["review_id"]),
        "model_id": str(persisted_summary["model_id"]),
        "test_status": "locked_not_opened",
        "test_members_read": False,
        "test_probabilities_present": False,
        "platform_used": False,
        "approved_change_count": len(approved_records),
        "rejected_change_count": len(rejected_records),
        "approved_direction_counts": dict(
            sorted(
                Counter(
                    f"{item['original_label']}_to_{item['final_label']}"
                    for item in approved_records
                ).items()
            )
        ),
        "label_counts": label_counts,
        "previous_reference_csv_sha256": expected_reference_csv_sha256,
        "reference_csv_sha256": new_csv_sha256,
        "previous_reference_manifest_sha256": old_manifest_sha256,
        "reference_manifest_sha256": new_manifest_sha256,
        "reference_member_sha256": new_member_sha256,
        "summary_sha256": summary_sha256,
    }
    receipt_bytes = _canonical_bytes(receipt)
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_csv = csv_file.with_name(f".{csv_file.name}.{receipt['review_id']}.tmp")
    temporary_manifest = manifest_file.with_name(
        f".{manifest_file.name}.{receipt['review_id']}.tmp"
    )
    temporary_receipt = receipt_file.with_name(f".{receipt_file.name}.tmp")
    if any(path.exists() for path in (temporary_csv, temporary_manifest, temporary_receipt)):
        raise BlindLabelReviewError("blind_review_apply_temporary_exists")
    try:
        temporary_csv.write_bytes(new_csv_bytes)
        temporary_manifest.write_bytes(new_manifest_bytes)
        temporary_receipt.write_bytes(receipt_bytes)
        temporary_csv.replace(csv_file)
        temporary_manifest.replace(manifest_file)
        temporary_receipt.replace(receipt_file)
    except OSError as exc:
        try:
            csv_file.write_bytes(old_csv_bytes)
            manifest_file.write_bytes(old_manifest_bytes)
        except OSError:
            pass
        raise BlindLabelReviewError("blind_review_apply_publish_failed") from exc
    finally:
        for path in (temporary_csv, temporary_manifest, temporary_receipt):
            if path.exists():
                path.unlink()
    return receipt
