"""正式清洗 baseline 的开发证据误差分析。

本模块只接受训练折外概率与验证概率，不读取切分 manifest 中的测试成员，也不
生成测试概率。输出仅包含聚合统计和不可逆 review key，不复制帖子正文、作者
身份或平台字段。
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    precision_recall_fscore_support,
)

from tourism_ugc_study.cleaning.reference_evidence import REFERENCE_FIELDS

from .formal_training import load_frozen_baseline_model


LENGTH_BANDS = (
    ("0000-0299", 0, 300),
    ("0300-0599", 300, 600),
    ("0600-1199", 600, 1200),
    ("1200-2399", 1200, 2400),
    ("2400+", 2400, None),
)
PROBABILITY_BANDS = (
    ("0.00-0.10", 0.00, 0.10, False),
    ("0.10-0.25", 0.10, 0.25, False),
    ("0.25-0.50", 0.25, 0.50, False),
    ("0.50-0.75", 0.50, 0.75, False),
    ("0.75-0.90", 0.75, 0.90, False),
    ("0.90-1.00", 0.90, 1.00, True),
)
DEVELOPMENT_KEYS = {
    "positive_class",
    "threshold_status",
    "train_oof",
    "validation",
}
PROBABILITY_ROW_KEYS = {
    "source_post_id",
    "source_version",
    "split_name",
    "tourism_label",
    "margin",
    "p_unrelated",
}


class BaselineErrorAnalysisError(RuntimeError):
    """开发误差证据不满足失败关闭契约时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、作者、身份或私有路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化误差分析失败。

        Args:
            reason_code: 供 CLI、测试和运行记录使用的稳定失败码。
        """

        super().__init__("formal baseline development error analysis failed")
        self.reason_code = reason_code


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BaselineErrorAnalysisError("baseline_analysis_file_hash_failed") from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为对象的 UTF-8 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineErrorAnalysisError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise BaselineErrorAnalysisError(reason_code)
    return value


def _load_reference_rows(path: Path) -> Mapping[tuple[int, int], Mapping[str, str]]:
    """读取最终参考 CSV 并建立唯一身份索引。"""

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise BaselineErrorAnalysisError(
                    "baseline_analysis_reference_field_mismatch"
                )
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BaselineErrorAnalysisError(
            "baseline_analysis_reference_unreadable"
        ) from exc
    indexed: dict[tuple[int, int], Mapping[str, str]] = {}
    try:
        for row in rows:
            identity = (int(row["source_post_id"]), int(row["source_version"]))
            if identity in indexed:
                raise BaselineErrorAnalysisError(
                    "baseline_analysis_reference_identity_repeated"
                )
            indexed[identity] = row
    except (KeyError, TypeError, ValueError) as exc:
        raise BaselineErrorAnalysisError(
            "baseline_analysis_reference_identity_invalid"
        ) from exc
    return indexed


def _length_band(length: int) -> str:
    """把规范化正文字符数映射到固定、跨运行可比较的长度层。"""

    for name, lower, upper in LENGTH_BANDS:
        if length >= lower and (upper is None or length < upper):
            return name
    raise BaselineErrorAnalysisError("baseline_analysis_text_length_invalid")


def _probability_band(probability: float) -> str:
    """把无关概率映射到描述性概率区间。"""

    for name, lower, upper, include_upper in PROBABILITY_BANDS:
        if probability >= lower and (
            probability < upper or (include_upper and probability <= upper)
        ):
            return name
    raise BaselineErrorAnalysisError("baseline_analysis_probability_invalid")


def _review_key(model_id: str, identity: tuple[int, int]) -> str:
    """生成不可逆且绑定模型的误判复核键。"""

    payload = f"{model_id}:{identity[0]}:{identity[1]}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _validated_probability_rows(
    raw_rows: Any,
    *,
    expected_split: str,
) -> tuple[dict[str, Any], ...]:
    """校验单个开发集合的概率行。"""

    if not isinstance(raw_rows, list) or not raw_rows:
        raise BaselineErrorAnalysisError("baseline_analysis_probability_rows_invalid")
    validated: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping) or set(raw) != PROBABILITY_ROW_KEYS:
            raise BaselineErrorAnalysisError(
                "baseline_analysis_probability_row_contract_mismatch"
            )
        try:
            identity = (int(raw["source_post_id"]), int(raw["source_version"]))
            probability = float(raw["p_unrelated"])
            margin = float(raw["margin"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BaselineErrorAnalysisError(
                "baseline_analysis_probability_value_invalid"
            ) from exc
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
            raise BaselineErrorAnalysisError(
                "baseline_analysis_probability_value_invalid"
            )
        seen.add(identity)
        validated.append(
            {
                "identity": identity,
                "tourism_label": str(raw["tourism_label"]),
                "p_unrelated": probability,
            }
        )
    return tuple(validated)


def _aggregate_group(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """聚合一个长度层或概率层的诊断错误率。"""

    count = len(rows)
    errors = sum(bool(row["is_error"]) for row in rows)
    labels = Counter(str(row["tourism_label"]) for row in rows)
    return {
        "count": count,
        "error_count": errors,
        "error_rate": errors / count if count else None,
        "label_counts": dict(sorted(labels.items())),
    }


def _split_analysis(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """计算单个开发集合的类别、长度与概率区间诊断。"""

    labels = [str(row["tourism_label"]) for row in rows]
    y_true = [1 if label == "unrelated" else 0 for label in labels]
    probabilities = [float(row["p_unrelated"]) for row in rows]
    predictions = [1 if value >= 0.5 else 0 for value in probabilities]
    related_as_related = sum(
        truth == 0 and predicted == 0
        for truth, predicted in zip(y_true, predictions, strict=True)
    )
    related_as_unrelated = sum(
        truth == 0 and predicted == 1
        for truth, predicted in zip(y_true, predictions, strict=True)
    )
    unrelated_as_related = sum(
        truth == 1 and predicted == 0
        for truth, predicted in zip(y_true, predictions, strict=True)
    )
    unrelated_as_unrelated = sum(
        truth == 1 and predicted == 1
        for truth, predicted in zip(y_true, predictions, strict=True)
    )
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true,
        predictions,
        average="binary",
        pos_label=1,
        zero_division=0,
    )
    count = len(rows)
    correct = related_as_related + unrelated_as_unrelated
    lengths = [int(row["text_length"]) for row in rows]
    by_length = {
        name: _aggregate_group(
            [row for row in rows if str(row["length_band"]) == name]
        )
        for name, _, _ in LENGTH_BANDS
    }
    by_probability = {
        name: _aggregate_group(
            [row for row in rows if str(row["probability_band"]) == name]
        )
        for name, _, _, _ in PROBABILITY_BANDS
    }
    return {
        "count": count,
        "label_counts": dict(sorted(Counter(labels).items())),
        "diagnostic_cutoff": 0.5,
        "diagnostic_cutoff_is_routing_threshold": False,
        "accuracy": correct / count,
        "correct_count": correct,
        "precision_unrelated": float(precision),
        "recall_unrelated": float(recall),
        "f1_unrelated": float(f1),
        "pr_auc_unrelated": float(average_precision_score(y_true, probabilities)),
        "brier_score": float(brier_score_loss(y_true, probabilities)),
        "log_loss": float(log_loss(y_true, probabilities, labels=[0, 1])),
        "confusion": {
            "related_as_related": related_as_related,
            "related_as_unrelated": related_as_unrelated,
            "unrelated_as_related": unrelated_as_related,
            "unrelated_as_unrelated": unrelated_as_unrelated,
        },
        "text_length": {
            "minimum": min(lengths),
            "median": float(median(lengths)),
            "maximum": max(lengths),
            "bands": by_length,
        },
        "probability_bands": by_probability,
    }


def analyze_baseline_development_errors(
    package_dir: str | Path,
    reference_csv: str | Path,
) -> Mapping[str, Any]:
    """分析训练 OOF 与验证误差，并保持锁定测试不可见。

    Args:
        package_dir: 已封存 baseline 训练包目录。
        reference_csv: 训练 manifest 绑定的唯一最终参考 CSV。

    Returns:
        只含聚合指标和误判 review key 的可序列化结果。

    Raises:
        BaselineErrorAnalysisError: artifact、谱系、概率或参考连接不一致。
        FormalTrainingError: 冻结训练包完整性校验失败。
    """

    try:
        package = Path(package_dir).expanduser().resolve(strict=True)
        reference_path = Path(reference_csv).expanduser().resolve(strict=True)
    except OSError as exc:
        raise BaselineErrorAnalysisError("baseline_analysis_input_not_found") from exc
    # 复用冻结模型加载器完成 manifest 与全部 artifact 摘要校验；不调用预测。
    load_frozen_baseline_model(package)
    manifest = _load_json(
        package / "training-manifest.json",
        "baseline_analysis_manifest_unreadable",
    )
    if (
        manifest.get("artifact_kind") != "formal-cleaning-baseline"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("platform_used") is not False
    ):
        raise BaselineErrorAnalysisError("baseline_analysis_manifest_state_invalid")
    lineage = manifest.get("lineage")
    artifacts = manifest.get("artifacts")
    if not isinstance(lineage, Mapping) or not isinstance(artifacts, Mapping):
        raise BaselineErrorAnalysisError("baseline_analysis_manifest_sections_missing")
    if _file_sha256(reference_path) != lineage.get("reference_csv_sha256"):
        raise BaselineErrorAnalysisError("baseline_analysis_reference_hash_mismatch")
    development_details = artifacts.get("development_probabilities")
    if not isinstance(development_details, Mapping):
        raise BaselineErrorAnalysisError("baseline_analysis_probability_artifact_missing")
    development_path = package / str(development_details.get("filename", ""))
    if _file_sha256(development_path) != development_details.get("sha256"):
        raise BaselineErrorAnalysisError("baseline_analysis_probability_hash_mismatch")
    development = _load_json(
        development_path,
        "baseline_analysis_probability_unreadable",
    )
    if (
        set(development) != DEVELOPMENT_KEYS
        or development.get("positive_class") != "unrelated"
        or development.get("threshold_status") != "UNSET"
    ):
        raise BaselineErrorAnalysisError("baseline_analysis_probability_contract_invalid")

    train_rows = _validated_probability_rows(
        development["train_oof"], expected_split="train_oof"
    )
    validation_rows = _validated_probability_rows(
        development["validation"], expected_split="validation"
    )
    train_identities = {row["identity"] for row in train_rows}
    validation_identities = {row["identity"] for row in validation_rows}
    if train_identities & validation_identities:
        raise BaselineErrorAnalysisError("baseline_analysis_development_overlap")
    split_counts = manifest.get("split_counts")
    if (
        not isinstance(split_counts, Mapping)
        or split_counts.get("train") != len(train_rows)
        or split_counts.get("validation") != len(validation_rows)
    ):
        raise BaselineErrorAnalysisError("baseline_analysis_split_count_mismatch")

    reference_rows = _load_reference_rows(reference_path)
    model_id = str(manifest.get("model_id") or "")
    enriched_by_split: dict[str, list[dict[str, Any]]] = {
        "train_oof": [],
        "validation": [],
    }
    errors: list[dict[str, Any]] = []
    for split_name, rows in (
        ("train_oof", train_rows),
        ("validation", validation_rows),
    ):
        for row in rows:
            identity = row["identity"]
            reference = reference_rows.get(identity)
            if reference is None or reference.get("tourism_label") != row["tourism_label"]:
                raise BaselineErrorAnalysisError(
                    "baseline_analysis_reference_join_mismatch"
                )
            text = str(reference.get("normalized_model_text") or "")
            if not text:
                raise BaselineErrorAnalysisError("baseline_analysis_text_blank")
            probability = float(row["p_unrelated"])
            predicted_label = "unrelated" if probability >= 0.5 else "related"
            length = len(text)
            enriched = {
                "tourism_label": row["tourism_label"],
                "p_unrelated": probability,
                "predicted_label": predicted_label,
                "is_error": predicted_label != row["tourism_label"],
                "text_length": length,
                "length_band": _length_band(length),
                "probability_band": _probability_band(probability),
            }
            enriched_by_split[split_name].append(enriched)
            if enriched["is_error"]:
                errors.append(
                    {
                        "review_key": _review_key(model_id, identity),
                        "split": split_name,
                        "actual_label": row["tourism_label"],
                        "predicted_label": predicted_label,
                        "p_unrelated": probability,
                        "text_length": length,
                        "length_band": enriched["length_band"],
                    }
                )

    return {
        "artifact_kind": "formal-cleaning-baseline-development-error-analysis",
        "analysis_status": "development_only",
        "model_id": model_id,
        "reference_csv_sha256": lineage["reference_csv_sha256"],
        "development_probabilities_sha256": development_details["sha256"],
        "test_status": "locked_not_opened",
        "test_members_read": False,
        "test_probabilities_present": False,
        "threshold_status": "UNSET",
        "diagnostic_cutoff": 0.5,
        "diagnostic_cutoff_is_routing_threshold": False,
        "platform_used": False,
        "length_band_contract": [name for name, _, _ in LENGTH_BANDS],
        "splits": {
            name: _split_analysis(rows)
            for name, rows in enriched_by_split.items()
        },
        "error_count": len(errors),
        "error_records": sorted(
            errors,
            key=lambda row: (str(row["split"]), str(row["review_key"])),
        ),
    }


def apply_error_type_coding(
    analysis: Mapping[str, Any],
    coding: Mapping[str, Any],
) -> Mapping[str, Any]:
    """把完整人工错误类型编码绑定到开发误差分析。

    Args:
        analysis: 已生成的开发误差分析。
        coding: 只含 ``review_key`` 和稳定类型码的 finalized 编码 artifact。

    Returns:
        增加逐条类型码与聚合计数的新分析对象；不修改输入。

    Raises:
        BaselineErrorAnalysisError: 模型身份、类型码或误判成员不能一一对应。
    """

    if (
        coding.get("artifact_kind")
        != "formal-cleaning-baseline-development-error-type-coding"
        or coding.get("status") != "finalized"
        or coding.get("model_id") != analysis.get("model_id")
    ):
        raise BaselineErrorAnalysisError("baseline_analysis_error_coding_identity_invalid")
    codebook = coding.get("codebook")
    records = coding.get("records")
    if not isinstance(codebook, Mapping) or not codebook or not isinstance(records, list):
        raise BaselineErrorAnalysisError("baseline_analysis_error_coding_contract_invalid")
    if any(
        not isinstance(code, str)
        or not code
        or not isinstance(description, str)
        or not description
        for code, description in codebook.items()
    ):
        raise BaselineErrorAnalysisError("baseline_analysis_error_codebook_invalid")
    coding_by_key: dict[str, str] = {}
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"review_key", "error_type"}:
            raise BaselineErrorAnalysisError("baseline_analysis_error_coding_record_invalid")
        review_key = str(record["review_key"])
        error_type = str(record["error_type"])
        if (
            not review_key
            or review_key in coding_by_key
            or error_type not in codebook
        ):
            raise BaselineErrorAnalysisError("baseline_analysis_error_coding_record_invalid")
        coding_by_key[review_key] = error_type

    raw_errors = analysis.get("error_records")
    if not isinstance(raw_errors, list):
        raise BaselineErrorAnalysisError("baseline_analysis_error_records_missing")
    analysis_keys = {str(record["review_key"]) for record in raw_errors}
    if set(coding_by_key) != analysis_keys:
        raise BaselineErrorAnalysisError("baseline_analysis_error_coding_members_mismatch")

    coded_errors: list[dict[str, Any]] = []
    overall: Counter[str] = Counter()
    by_split: dict[str, Counter[str]] = {
        "train_oof": Counter(),
        "validation": Counter(),
    }
    by_direction: dict[str, Counter[str]] = {
        "related_as_unrelated": Counter(),
        "unrelated_as_related": Counter(),
    }
    for raw in raw_errors:
        record = dict(raw)
        error_type = coding_by_key[str(record["review_key"])]
        record["error_type"] = error_type
        coded_errors.append(record)
        overall[error_type] += 1
        split = str(record["split"])
        if split not in by_split:
            raise BaselineErrorAnalysisError("baseline_analysis_error_split_invalid")
        by_split[split][error_type] += 1
        direction = f"{record['actual_label']}_as_{record['predicted_label']}"
        if direction not in by_direction:
            raise BaselineErrorAnalysisError("baseline_analysis_error_direction_invalid")
        by_direction[direction][error_type] += 1

    merged = dict(analysis)
    merged["error_records"] = coded_errors
    merged["error_type_coding"] = {
        "status": "finalized",
        "scope": "all_train_oof_and_validation_errors_at_diagnostic_cutoff_0.5",
        "codebook": dict(sorted(codebook.items())),
        "overall_counts": dict(sorted(overall.items())),
        "split_counts": {
            split: dict(sorted(counts.items()))
            for split, counts in by_split.items()
        },
        "direction_counts": {
            direction: dict(sorted(counts.items()))
            for direction, counts in by_direction.items()
        },
    }
    return merged


def render_baseline_development_error_summary(
    analysis: Mapping[str, Any],
) -> str:
    """把去敏开发误差分析渲染为中文多行摘要。

    Args:
        analysis: :func:`analyze_baseline_development_errors` 的返回值。

    Returns:
        不含正文、作者、源身份或私有路径的报告。
    """

    lines = [
        "baseline 开发误差分析",
        "=====================",
        f"模型 ID：{analysis['model_id']}",
        "范围：训练 OOF + 验证；锁定测试未读取",
        "诊断分界：p_unrelated=0.500（不是路由阈值）",
    ]
    for split_name, label in (("train_oof", "训练 OOF"), ("validation", "验证")):
        split = analysis["splits"][split_name]
        confusion = split["confusion"]
        error_count = split["count"] - split["correct_count"]
        lines.extend(
            (
                "",
                f"{label}（n={split['count']}）",
                f"  准确率：{split['accuracy'] * 100:.1f}%（{split['correct_count']}/{split['count']}）",
                f"  错误：{error_count} 条；related→unrelated "
                f"{confusion['related_as_unrelated']} 条；unrelated→related "
                f"{confusion['unrelated_as_related']} 条",
                f"  unrelated P/R/F1：{split['precision_unrelated'] * 100:.1f}% / "
                f"{split['recall_unrelated'] * 100:.1f}% / {split['f1_unrelated'] * 100:.1f}%",
                f"  PR-AUC：{split['pr_auc_unrelated'] * 100:.1f}%；"
                f"Brier：{split['brier_score']:.4f}；Log loss：{split['log_loss']:.4f}",
                "  文本长度层错误率：",
            )
        )
        for band, values in split["text_length"]["bands"].items():
            if values["count"]:
                lines.append(
                    f"    {band}: {values['error_count']}/{values['count']} "
                    f"({values['error_rate'] * 100:.1f}%)"
                )
    lines.extend(
        (
            "",
            "边界：本报告不选择阈值、不设验收门、不读取测试成员，平台未参与。",
        )
    )
    coding = analysis.get("error_type_coding")
    if isinstance(coding, Mapping):
        codebook = coding["codebook"]
        lines.extend(("", "人工错误类型（训练 OOF + 验证全部误判）"))
        for error_type, count in sorted(
            coding["overall_counts"].items(),
            key=lambda item: (-int(item[1]), str(item[0])),
        ):
            lines.append(f"  {codebook[error_type]}：{count} 条")
    return "\n".join(lines)
