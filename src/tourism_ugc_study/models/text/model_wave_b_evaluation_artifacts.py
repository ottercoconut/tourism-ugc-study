"""Wave B 完成表收口、模板恢复与独立评价的不可变 artifact。"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .model_reliability_artifacts import _load_scored_members, _validate_wave_a_package
from .model_reliability_config import load_model_reliability_plan
from .model_reliability_evaluation import LabeledWaveMember
from .model_routing_policy_config import load_model_routing_policy_plan
from .model_routing_selection_config import load_model_routing_selection_plan
from .model_wave_b_artifacts import (
    _file_sha256,
    _load_json,
    _sha256_bytes,
    _validate_code_version,
    _validate_policy_package,
    _validate_wave_b_package,
    _wave_a_components,
)
from .model_wave_b_evaluation import evaluate_wave_b_policy
from .model_wave_b_study import eligible_wave_b_population


class ModelWaveBEvaluationArtifactError(RuntimeError):
    """Wave B 完成表、模板恢复或评价 artifact 失败。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员、标签明细或私有路径的稳定失败码。"""

        super().__init__("formal model wave b evaluation artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class WaveBEvaluationResult:
    """Wave B 独立评价的去敏结果。"""

    evaluation_id: str
    wave_id: str
    policy_id: str
    status: str
    evidence_status: str
    reused: bool
    sample_count: int
    determinate_count: int
    uncertain_count: int
    label_counts: Mapping[str, int]
    selected_metrics: Mapping[str, Any]
    comparator_metrics: Mapping[str, Any]
    overall_metrics: Mapping[str, Mapping[str, Any]]
    paired_risk_deltas: Mapping[str, Any]
    completed_csv_sha256: str
    restored_task_sha256: str
    report_sha256: str
    package_manifest_sha256: str
    labels_entered_fit: bool
    threshold_status: str
    deployment_status: str
    audit_status: str
    test_status: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序、紧凑、拒绝 NaN 且换行结尾的规范 JSON。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录落盘并原子替换，避免完成表或模板半写。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_wave_package_allow_completed_task(
    package: Path,
    *,
    expected_manifest_sha256: str,
    expected_wave_id: str,
    expected_policy_id: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """验证 Wave B 包，但暂时允许公开任务的标签列已被原位填写。"""

    manifest_path = package / "wave-b-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _load_json(
        manifest_path, "model_wave_b_evaluation_wave_manifest_invalid"
    )
    artifacts = manifest.get("artifacts")
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or manifest.get("artifact_kind") != "formal-cleaning-model-wave-b"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("wave_id") != expected_wave_id
        or manifest.get("policy_id") != expected_policy_id
        or manifest.get("status") != "WAVE_B_LABELING"
        or manifest.get("sample_count") != 360
        or manifest.get("fit_call_count") != 0
        or manifest.get("prediction_call_count") != 0
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status")
        != "FROZEN_FOR_WAVE_B_EVALUATION"
        or manifest.get("deployment_status") != "NOT_AUTHORIZED"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
    ):
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_evaluation_wave_manifest_invalid"
        )
    for key, filename in (
        ("private_map", "private-map.json"),
        ("policy_manifest", "policy-manifest.json"),
    ):
        details = artifacts.get(key)
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_evaluation_wave_artifact_invalid"
            )
    private_map = _load_json(
        package / "private-map.json",
        "model_wave_b_evaluation_private_map_invalid",
    )
    records = private_map.get("records")
    if (
        private_map.get("artifact_kind")
        != "formal-cleaning-model-wave-b-private-map"
        or private_map.get("wave_id") != expected_wave_id
        or private_map.get("policy_id") != expected_policy_id
        or private_map.get("labels_entered_fit") is not False
        or private_map.get("platform_used") is not False
        or not isinstance(records, list)
        or len(records) != 360
    ):
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_evaluation_private_map_invalid"
        )
    return manifest, private_map


def _parse_completed_rows(
    path: Path,
    *,
    wave_id: str,
    private_map: Mapping[str, Any],
) -> tuple[list[Mapping[str, str]], tuple[LabeledWaveMember, ...], bytes]:
    """校验四列完成表并同时重建原始空白模板字节。"""

    try:
        raw = path.read_bytes()
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_completed_csv_invalid"
        ) from exc
    expected_records = private_map["records"]
    expected = {str(item["task_id"]): item for item in expected_records}
    if (
        not raw.startswith(codecs.BOM_UTF8)
        or fields
        != [
            "task_id",
            "sample_run_id",
            "normalized_model_text",
            "tourism_label",
        ]
        or len(rows) != 360
        or len(expected) != 360
        or len({row["task_id"] for row in rows}) != 360
        or {row["task_id"] for row in rows} != set(expected)
    ):
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_completed_csv_invalid"
        )
    labeled: list[LabeledWaveMember] = []
    for row in rows:
        hidden = expected[row["task_id"]]
        if (
            row["sample_run_id"] != wave_id
            or row["tourism_label"]
            not in {"related", "unrelated", "uncertain"}
            or hashlib.sha256(
                row["normalized_model_text"].encode("utf-8")
            ).hexdigest()
            != hidden["normalized_sha256"]
        ):
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_completed_row_invalid"
            )
        try:
            labeled.append(
                LabeledWaveMember(
                    task_id=row["task_id"],
                    source_post_id=int(hidden["source_post_id"]),
                    source_version=int(hidden["source_version"]),
                    component_id=str(hidden["component_id"]),
                    stratum=str(hidden["stratum"]),
                    inclusion_probability=float(hidden["inclusion_probability"]),
                    analysis_weight=float(hidden["analysis_weight"]),
                    sparse_p_unrelated=float(hidden["sparse_p_unrelated"]),
                    qwen_p_unrelated=float(hidden["qwen_p_unrelated"]),
                    tourism_label=row["tourism_label"],
                )
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_completed_row_invalid"
            ) from exc
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "tourism_label": ""})
    blank_bytes = codecs.BOM_UTF8 + output.getvalue().encode("utf-8")
    labeled.sort(key=lambda item: item.task_id)
    return rows, tuple(labeled), blank_bytes


def _seal_completed_csv_and_restore_task(
    completed_source: Path,
    completed_output: Path,
    *,
    expected_task_sha256: str,
    expected_existing_completed_sha256: str | None,
    blank_bytes: bytes,
) -> tuple[str, str]:
    """保全完成表后恢复任务模板，并返回两个文件摘要。"""

    source_bytes = completed_source.read_bytes()
    completed_sha256 = hashlib.sha256(source_bytes).hexdigest()
    restored_sha256 = hashlib.sha256(blank_bytes).hexdigest()
    if restored_sha256 != expected_task_sha256:
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_completed_not_label_only_change"
        )
    if completed_output.exists():
        if (
            expected_existing_completed_sha256 is None
            or _file_sha256(completed_output)
            != expected_existing_completed_sha256
            or expected_existing_completed_sha256 != completed_sha256
        ):
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_existing_completed_csv_invalid"
            )
    else:
        _atomic_write(completed_output, source_bytes)
    _atomic_write(completed_source, blank_bytes)
    if (
        _file_sha256(completed_source) != expected_task_sha256
        or _file_sha256(completed_output) != completed_sha256
    ):
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_completion_seal_failed"
        )
    return completed_sha256, restored_sha256


def _evaluation_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> WaveBEvaluationResult:
    """从评价 manifest 生成不含私有标签明细的结果。"""

    return WaveBEvaluationResult(
        evaluation_id=str(manifest["evaluation_id"]),
        wave_id=str(manifest["wave_id"]),
        policy_id=str(manifest["policy_id"]),
        status=str(manifest["status"]),
        evidence_status=str(manifest["evidence_status"]),
        reused=reused,
        sample_count=int(manifest["sample_count"]),
        determinate_count=int(manifest["determinate_count"]),
        uncertain_count=int(manifest["uncertain_count"]),
        label_counts=dict(manifest["label_counts"]),
        selected_metrics=dict(manifest["selected_metrics"]),
        comparator_metrics=dict(manifest["comparator_metrics"]),
        overall_metrics={
            key: dict(value) for key, value in manifest["overall_metrics"].items()
        },
        paired_risk_deltas=dict(manifest["paired_risk_deltas"]),
        completed_csv_sha256=str(manifest["completed_csv_sha256"]),
        restored_task_sha256=str(manifest["restored_task_sha256"]),
        report_sha256=str(manifest["artifacts"]["report"]["sha256"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        labels_entered_fit=bool(manifest["labels_entered_fit"]),
        threshold_status=str(manifest["threshold_status"]),
        deployment_status=str(manifest["deployment_status"]),
        audit_status=str(manifest["audit_status"]),
        test_status=str(manifest["test_status"]),
    )


def _validate_existing_evaluation(
    package: Path,
    *,
    expected_manifest_sha256: str,
    expected_evaluation_id: str,
) -> WaveBEvaluationResult:
    """验证既有 Wave B 独立评价包的全部文件摘要。"""

    path = package / "evaluation-manifest.json"
    manifest_bytes = path.read_bytes()
    manifest = _load_json(path, "model_wave_b_evaluation_manifest_invalid")
    artifacts = manifest.get("artifacts")
    expected = {
        "report": "evaluation-report.json",
        "labeled_records": "labeled-records.json",
        "completion_receipt": "completion-receipt.json",
        "policy_manifest": "policy-manifest.json",
        "evidence_plan": "evidence-plan.yaml",
    }
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or manifest.get("artifact_kind")
        != "formal-cleaning-model-wave-b-evaluation"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("evaluation_id") != expected_evaluation_id
        or manifest.get("status") != "WAVE_B_EVALUATION_COMPLETE"
        or manifest.get("fit_call_count") != 0
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("deployment_status") != "NOT_AUTHORIZED"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
        or set(artifacts) != set(expected)
    ):
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_evaluation_manifest_invalid"
        )
    for key, filename in expected.items():
        details = artifacts[key]
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_evaluation_artifact_invalid"
            )
    return _evaluation_result(manifest, manifest_bytes, reused=True)


def evaluate_wave_b_package(
    completed_source: str | Path,
    completed_output: str | Path,
    wave_b_package: str | Path,
    policy_package: str | Path,
    scored_package: str | Path,
    wave_a_package: str | Path,
    reliability_plan_path: str | Path,
    policy_plan_path: str | Path,
    evidence_plan_path: str | Path,
    artifact_root: str | Path,
    *,
    code_version: str,
    expected_wave_id: str,
    expected_wave_manifest_sha256: str,
    expected_policy_manifest_sha256: str,
    expected_existing_completed_sha256: str | None = None,
    expected_existing_manifest_sha256: str | None = None,
) -> WaveBEvaluationResult:
    """保全原位完成表、恢复任务模板并评价冻结策略。

    该入口不运行预测或训练，不扫描阈值，也不自动决定是否部署。只有当
    清空标签能逐字节恢复原任务 SHA-256 时，才允许修复被原位填写的任务包。
    """

    version = _validate_code_version(code_version)
    reliability_plan = load_model_reliability_plan(reliability_plan_path)
    policy = load_model_routing_policy_plan(policy_plan_path)
    evidence_plan = load_model_routing_selection_plan(evidence_plan_path)
    wave_directory = Path(wave_b_package).expanduser().resolve(strict=True)
    policy_directory = Path(policy_package).expanduser().resolve(strict=True)
    scored_directory = Path(scored_package).expanduser().resolve(strict=True)
    wave_a_directory = Path(wave_a_package).expanduser().resolve(strict=True)
    source_path = Path(completed_source).expanduser().resolve(strict=True)
    output_path = Path(completed_output).expanduser().resolve()
    if source_path != wave_directory / "wave-b-tourism-relevance-annotation.csv":
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_completed_source_invalid"
        )
    _validate_policy_package(
        policy_directory,
        expected_manifest_sha256=expected_policy_manifest_sha256,
        plan=policy,
    )
    wave_manifest, private_map = _load_wave_package_allow_completed_task(
        wave_directory,
        expected_manifest_sha256=expected_wave_manifest_sha256,
        expected_wave_id=expected_wave_id,
        expected_policy_id=policy.policy_id,
    )
    task_details = wave_manifest["artifacts"]["review_task"]
    if (
        not isinstance(task_details, Mapping)
        or task_details.get("filename")
        != "wave-b-tourism-relevance-annotation.csv"
    ):
        raise ModelWaveBEvaluationArtifactError(
            "model_wave_b_evaluation_wave_manifest_invalid"
        )
    expected_task_sha256 = str(task_details["sha256"])
    if _file_sha256(source_path) == expected_task_sha256:
        if (
            expected_existing_completed_sha256 is None
            or not output_path.is_file()
            or _file_sha256(output_path) != expected_existing_completed_sha256
        ):
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_restored_task_requires_completed_hash"
            )
        rows, labeled, blank_bytes = _parse_completed_rows(
            output_path,
            wave_id=str(wave_manifest["wave_id"]),
            private_map=private_map,
        )
        completed_sha256 = expected_existing_completed_sha256
        restored_sha256 = hashlib.sha256(blank_bytes).hexdigest()
        if restored_sha256 != expected_task_sha256:
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_completed_not_label_only_change"
            )
    else:
        rows, labeled, blank_bytes = _parse_completed_rows(
            source_path,
            wave_id=str(wave_manifest["wave_id"]),
            private_map=private_map,
        )
        completed_sha256, restored_sha256 = _seal_completed_csv_and_restore_task(
            source_path,
            output_path,
            expected_task_sha256=expected_task_sha256,
            expected_existing_completed_sha256=expected_existing_completed_sha256,
            blank_bytes=blank_bytes,
        )
    _validate_wave_b_package(
        wave_directory,
        expected_manifest_sha256=expected_wave_manifest_sha256,
        plan=policy,
    )
    scored = _load_scored_members(
        scored_directory,
        expected_manifest_sha256=policy.scored_manifest_sha256,
        plan=reliability_plan,
    )
    _wave_a_manifest, wave_a_map = _validate_wave_a_package(
        wave_a_directory,
        expected_manifest_sha256=policy.wave_a_manifest_sha256,
        plan=reliability_plan,
    )
    eligible = eligible_wave_b_population(
        scored,
        excluded_wave_a_components=_wave_a_components(wave_a_map),
        plan=policy,
    )
    report = evaluate_wave_b_policy(
        labeled,
        eligible,
        policy=policy,
        evidence_plan=evidence_plan,
    )
    label_counts = {
        label: sum(row["tourism_label"] == label for row in rows)
        for label in ("related", "unrelated", "uncertain")
    }
    evaluation_id = _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": "wave-b-frozen-policy-independent-evaluation-v1",
                "wave_manifest_sha256": expected_wave_manifest_sha256,
                "policy_manifest_sha256": expected_policy_manifest_sha256,
                "completed_csv_sha256": completed_sha256,
                "evidence_plan_sha256": evidence_plan.plan_sha256,
            }
        )
    )[:32]
    root = Path(artifact_root).expanduser().resolve()
    package = root / evaluation_id
    if package.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelWaveBEvaluationArtifactError(
                "model_wave_b_existing_evaluation_requires_manifest_hash"
            )
        return _validate_existing_evaluation(
            package,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            expected_evaluation_id=evaluation_id,
        )
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{evaluation_id}.", dir=root)
    )
    try:
        (temporary / "evaluation-report.json").write_bytes(
            _canonical_bytes(report)
        )
        (temporary / "labeled-records.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-model-wave-b-labels",
                    "evaluation_id": evaluation_id,
                    "wave_id": wave_manifest["wave_id"],
                    "records": [asdict(item) for item in labeled],
                    "labels_entered_fit": False,
                    "platform_used": False,
                }
            )
        )
        (temporary / "completion-receipt.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-model-wave-b-completion-receipt",
                    "wave_id": wave_manifest["wave_id"],
                    "completed_csv_sha256": completed_sha256,
                    "restored_task_sha256": restored_sha256,
                    "label_counts": label_counts,
                    "only_label_column_changed": True,
                    "completed_csv_committed": False,
                }
            )
        )
        shutil.copyfile(
            policy_directory / "policy-manifest.json",
            temporary / "policy-manifest.json",
        )
        shutil.copyfile(evidence_plan_path, temporary / "evidence-plan.yaml")
        artifacts = {
            "report": {
                "filename": "evaluation-report.json",
                "sha256": _file_sha256(temporary / "evaluation-report.json"),
            },
            "labeled_records": {
                "filename": "labeled-records.json",
                "sha256": _file_sha256(temporary / "labeled-records.json"),
            },
            "completion_receipt": {
                "filename": "completion-receipt.json",
                "sha256": _file_sha256(temporary / "completion-receipt.json"),
            },
            "policy_manifest": {
                "filename": "policy-manifest.json",
                "sha256": _file_sha256(temporary / "policy-manifest.json"),
            },
            "evidence_plan": {
                "filename": "evidence-plan.yaml",
                "sha256": _file_sha256(temporary / "evidence-plan.yaml"),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-model-wave-b-evaluation",
            "artifact_status": "immutable",
            "evaluation_id": evaluation_id,
            "wave_id": wave_manifest["wave_id"],
            "wave_manifest_sha256": expected_wave_manifest_sha256,
            "policy_id": policy.policy_id,
            "policy_manifest_sha256": expected_policy_manifest_sha256,
            "status": report["status"],
            "evidence_status": report["evidence_status"],
            "sample_count": report["sample_count"],
            "determinate_count": report["determinate_count"],
            "uncertain_count": report["uncertain_count"],
            "label_counts": label_counts,
            "completed_csv_sha256": completed_sha256,
            "restored_task_sha256": restored_sha256,
            "selected_metrics": report["selected_strategy"],
            "comparator_metrics": report["matched_workload_comparator"],
            "overall_metrics": report["overall_metrics"],
            "paired_risk_deltas": report["paired_risk_deltas"],
            "mechanical_acceptance_gate": False,
            "may_change_model_or_threshold": False,
            "labels_entered_fit": False,
            "fit_call_count": 0,
            "prediction_call_count": 0,
            "test_status": "locked_not_opened",
            "threshold_status": "FROZEN_FOR_WAVE_B_EVALUATION",
            "deployment_status": "NOT_AUTHORIZED",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
            "lineage": {"code_version": version},
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "evaluation-manifest.json").write_bytes(manifest_bytes)
        temporary.rename(package)
        temporary = None
        return _evaluation_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _percentage(value: Any) -> str:
    """把可空比例渲染为百分比。"""

    return "N/A" if value is None else f"{float(value) * 100:.2f}%"


def render_wave_b_evaluation_result(
    result: WaveBEvaluationResult, *, output_format: str = "human"
) -> str:
    """渲染 Wave B 独立评价的 JSON 或中文摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    selected = result.selected_metrics
    comparator = result.comparator_metrics
    return "\n".join(
        [
            "# Wave B 冻结策略独立评价",
            "",
            f"评价 ID：{result.evaluation_id}",
            f"状态：{result.status}；证据：{result.evidence_status}",
            (
                f"标签：related={result.label_counts.get('related', 0)}；"
                f"unrelated={result.label_counts.get('unrelated', 0)}；"
                f"uncertain={result.label_counts.get('uncertain', 0)}"
            ),
            "",
            "固定三段策略（不是新一轮阈值选择）",
            (
                "  Qwen 0.14/0.86：人工率"
                f"{_percentage(selected['manual_review_rate'])}；保留端误留"
                f"{_percentage(selected['auto_keep_unrelated_retention']['weighted_error_risk'])}；"
                "排除端UGC误删"
                f"{_percentage(selected['auto_exclude_related_loss']['weighted_error_risk'])}；"
                "自动决策准确率"
                f"{_percentage(selected['weighted_auto_decision_accuracy'])}"
            ),
            (
                "  sparse 0.20/0.80：人工率"
                f"{_percentage(comparator['manual_review_rate'])}；保留端误留"
                f"{_percentage(comparator['auto_keep_unrelated_retention']['weighted_error_risk'])}；"
                "排除端UGC误删"
                f"{_percentage(comparator['auto_exclude_related_loss']['weighted_error_risk'])}；"
                "自动决策准确率"
                f"{_percentage(comparator['weighted_auto_decision_accuracy'])}"
            ),
            "",
            "统计边界：仅描述性独立评价，不设机械通过门，不允许换模型或改阈值。",
            "fit调用：0；预测调用：0；锁定测试：locked_not_opened。",
            "部署：NOT_AUTHORIZED；审计：UNSET；自动清洗决定：未生成。",
        ]
    )
