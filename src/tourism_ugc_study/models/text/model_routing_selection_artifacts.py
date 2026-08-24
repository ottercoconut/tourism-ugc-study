"""三段式双阈值选择分析的输入校验、不可变 artifact 与中文摘要。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model_reliability_evaluation import LabeledWaveMember
from .model_reliability_study import ScoredEvaluationMember
from .model_routing_selection import analyze_model_routing_grid
from .model_routing_selection_config import (
    ModelRoutingSelectionPlan,
    load_model_routing_selection_plan,
)


class ModelRoutingSelectionArtifactError(RuntimeError):
    """选择分析输入、谱系或不可变性失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露标签明细、正文、成员或私有路径的稳定失败码。"""

        super().__init__("formal model routing selection artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RoutingSelectionResult:
    """双阈值选择分析包的去敏运行摘要。"""

    analysis_id: str
    status: str
    reused: bool
    sample_count: int
    population_count: int
    threshold_pair_count_per_model: int
    pareto_point_count: int
    report_sha256: str
    summary_sha256: str
    package_manifest_sha256: str
    selected_model: str | None
    selected_t_keep: float | None
    selected_t_exclude: float | None
    labels_entered_fit: bool
    test_status: str
    threshold_status: str
    audit_status: str


@dataclass(frozen=True)
class _RoutingSelectionInputs:
    """通过全部 manifest 与哈希校验的标签、人口概率和总体指标。"""

    members: tuple[LabeledWaveMember, ...]
    scored_population: tuple[ScoredEvaluationMember, ...]
    overall_metrics: Mapping[str, Mapping[str, Any]]
    scored_manifest_sha256: str
    base_evaluation_manifest_sha256: str
    completed_csv_sha256: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序、紧凑、拒绝 NaN 且以换行结尾的规范 JSON。"""

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


def _sha256_bytes(value: bytes) -> str:
    """计算字节流 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算本地 artifact 文件 SHA-256。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """加载 JSON 映射并把路径与内容异常转换为稳定失败码。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRoutingSelectionArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRoutingSelectionArtifactError(reason_code)
    return value


def _validate_artifact(
    package: Path,
    details: Any,
    *,
    expected_filename: str,
    reason_code: str,
) -> Path:
    """验证 manifest artifact 条目的精确文件名和文件摘要。"""

    if (
        not isinstance(details, Mapping)
        or details.get("filename") != expected_filename
    ):
        raise ModelRoutingSelectionArtifactError(reason_code)
    path = package / expected_filename
    if _file_sha256(path) != details.get("sha256"):
        raise ModelRoutingSelectionArtifactError(reason_code)
    return path


def _load_scored_population(
    package: Path, plan: ModelRoutingSelectionPlan
) -> tuple[tuple[ScoredEvaluationMember, ...], str]:
    """校验人口框身份、安全边界和概率文件后加载10,103条无标签记录。"""

    manifest_path = package / "frame-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    manifest = _load_json(
        manifest_path, "model_routing_selection_scored_manifest_invalid"
    )
    if (
        manifest_sha256 != plan.scored_manifest_sha256
        or manifest.get("frame_id") != plan.scored_frame_id
        or manifest.get("status") != "FRAME_SCORED"
        or manifest.get("eligible_count") != 10103
        or manifest.get("sparse_model_id") != plan.sparse_model_id
        or manifest.get("qwen_model_id") != plan.qwen_model_id
        or manifest.get("fit_call_count") != 0
        or manifest.get("labels_read") is not False
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_scored_manifest_invalid"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_scored_manifest_invalid"
        )
    scored_path = _validate_artifact(
        package,
        artifacts.get("scored_frame"),
        expected_filename="scored-frame.json",
        reason_code="model_routing_selection_scored_artifact_invalid",
    )
    payload = _load_json(
        scored_path, "model_routing_selection_scored_artifact_invalid"
    )
    records = payload.get("records")
    if (
        payload.get("artifact_kind")
        != "formal-cleaning-model-reliability-scored-members"
        or payload.get("frame_id") != plan.scored_frame_id
        or payload.get("labels_present") is not False
        or payload.get("platform_used") is not False
        or not isinstance(records, list)
        or len(records) != 10103
    ):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_scored_artifact_invalid"
        )
    try:
        members = tuple(ScoredEvaluationMember(**record) for record in records)
    except (TypeError, ValueError) as exc:
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_scored_artifact_invalid"
        ) from exc
    return members, manifest_sha256


def _load_base_evaluation(
    package: Path, plan: ModelRoutingSelectionPlan
) -> tuple[
    tuple[LabeledWaveMember, ...],
    Mapping[str, Mapping[str, Any]],
    str,
    str,
]:
    """校验基础评价及其私有标签 artifact，不重新读取人工 CSV。"""

    manifest_path = package / "evaluation-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    manifest = _load_json(
        manifest_path, "model_routing_selection_base_evaluation_invalid"
    )
    if (
        manifest_sha256 != plan.base_evaluation_manifest_sha256
        or manifest.get("evaluation_id") != plan.base_evaluation_id
        or manifest.get("status") != "WAVE_A_EXPLORATORY_COMPLETE"
        or manifest.get("wave_id") != plan.wave_id
        or manifest.get("wave_manifest_sha256") != plan.wave_manifest_sha256
        or manifest.get("scored_manifest_sha256") != plan.scored_manifest_sha256
        or manifest.get("completed_csv_sha256") != plan.completed_csv_sha256
        or manifest.get("sample_count") != 240
        or manifest.get("determinate_count") != 240
        or manifest.get("labels_entered_fit") is not False
        or manifest.get("fit_call_count") != 0
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_base_evaluation_invalid"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_base_evaluation_invalid"
        )
    labels_path = _validate_artifact(
        package,
        artifacts.get("labeled_records"),
        expected_filename="labeled-records.json",
        reason_code="model_routing_selection_labeled_records_invalid",
    )
    payload = _load_json(
        labels_path, "model_routing_selection_labeled_records_invalid"
    )
    records = payload.get("records")
    if (
        payload.get("artifact_kind")
        != "formal-cleaning-model-reliability-wave-a-labels"
        or payload.get("evaluation_id") != plan.base_evaluation_id
        or payload.get("labels_entered_fit") is not False
        or payload.get("platform_used") is not False
        or not isinstance(records, list)
        or len(records) != 240
    ):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_labeled_records_invalid"
        )
    try:
        members = tuple(LabeledWaveMember(**record) for record in records)
    except (TypeError, ValueError) as exc:
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_labeled_records_invalid"
        ) from exc
    metrics = manifest.get("overall_metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != {"sparse", "qwen"}:
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_base_evaluation_invalid"
        )
    return members, metrics, manifest_sha256, str(manifest["completed_csv_sha256"])


def _load_routing_selection_inputs(
    scored_package: str | Path,
    base_evaluation_package: str | Path,
    plan: ModelRoutingSelectionPlan,
) -> _RoutingSelectionInputs:
    """加载并交叉核对人口框和 Wave A 基础评价。"""

    scored_directory = Path(scored_package).expanduser().resolve(strict=True)
    evaluation_directory = (
        Path(base_evaluation_package).expanduser().resolve(strict=True)
    )
    scored, scored_sha256 = _load_scored_population(scored_directory, plan)
    members, metrics, evaluation_sha256, completed_sha256 = _load_base_evaluation(
        evaluation_directory, plan
    )
    return _RoutingSelectionInputs(
        members=members,
        scored_population=scored,
        overall_metrics=metrics,
        scored_manifest_sha256=scored_sha256,
        base_evaluation_manifest_sha256=evaluation_sha256,
        completed_csv_sha256=completed_sha256,
    )


def _percentage(value: Any) -> str:
    """把可空比例渲染为稳定百分比文本。"""

    if value is None:
        return "N/A"
    return f"{float(value) * 100:.2f}%"


def render_routing_selection_markdown(report: Mapping[str, Any]) -> str:
    """把完整 JSON 报告压缩为便于研究者选择的中文 Markdown。"""

    lines = [
        "# Wave A 三段式双阈值选择分析",
        "",
        f"状态：`{report['status']}`；只支持研究者选择，不自动冻结策略。",
        "",
        "## 总体模型比较",
        "",
        "| 模型 | Accuracy | UGC误排 | 无关误留 | Log loss | Brier | PR-AUC |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model_name in ("sparse", "qwen"):
        metrics = report["overall_metrics"][model_name]
        lines.append(
            "| {model} | {accuracy} | {loss} | {retention} | {log_loss:.4f} | "
            "{brier:.4f} | {pr_auc:.4f} |".format(
                model=model_name,
                accuracy=_percentage(metrics["weighted_accuracy"]),
                loss=_percentage(metrics["weighted_related_to_unrelated_rate"]),
                retention=_percentage(metrics["weighted_unrelated_to_related_rate"]),
                log_loss=float(metrics["weighted_log_loss"]),
                brier=float(metrics["weighted_brier_score"]),
                pr_auc=float(metrics["weighted_pr_auc_unrelated"]),
            )
        )
    lines.extend(
        [
            "",
            "## 相同目标人工率比较",
            "",
            "| 目标人工率 | 模型 | 实际人工率 | T_keep | T_exclude | 保留端误留 | 排除端UGC误删 | 自动决策准确率 |",
            "|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for comparison in report["equal_workload_comparison"]:
        for model_name in ("sparse", "qwen"):
            point = comparison[model_name]
            lines.append(
                "| {target} | {model} | {realized} | {keep:.2f} | {exclude:.2f} | "
                "{keep_risk} | {exclude_risk} | {accuracy} |".format(
                    target=_percentage(comparison["target_manual_review_rate"]),
                    model=model_name,
                    realized=_percentage(point["realized_manual_review_rate"]),
                    keep=float(point["t_keep"]),
                    exclude=float(point["t_exclude"]),
                    keep_risk=_percentage(point["weighted_keep_error_risk"]),
                    exclude_risk=_percentage(point["weighted_exclude_error_risk"]),
                    accuracy=_percentage(point["weighted_auto_decision_accuracy"]),
                )
            )
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            f"- 每个模型评估 `{report['threshold_pair_count_per_model']}` 组双阈值。",
            f"- 跨模型 Pareto 前沿保留 `{len(report['pareto_frontier_point_ids'])}` 个点，不产生唯一推荐。",
            "- 尾部证据警告只提示样本支持不足，不是通过门。",
            "- 所有新标签仍为 evaluation-only，未进入 fit。",
            "- 锁定测试未开启；T_keep、T_exclude 与审计策略仍为 UNSET。",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_code_version(value: str) -> str:
    """要求分析入口绑定完整小写 Git SHA。"""

    if (
        len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_code_version_invalid"
        )
    return value


def analyze_routing_selection_package(
    scored_package: str | Path,
    base_evaluation_package: str | Path,
    selection_plan_path: str | Path,
    artifact_root: str | Path,
    *,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> RoutingSelectionResult:
    """运行三段式网格分析并原子封存机器报告、可读摘要和谱系。

    Args:
        scored_package: 已封存10,103条双模型概率人口框。
        base_evaluation_package: 已封存240条 Wave A 基础评价包。
        selection_plan_path: 标签前网格与全部输入哈希的独立配置。
        artifact_root: 新分析包根目录。
        code_version: 干净工作树的完整 Git SHA。
        expected_existing_manifest_sha256: 复用既有包时必须提供的 manifest 摘要。

    Returns:
        不包含标签明细、正文或成员身份的分析摘要。

    Raises:
        ModelRoutingSelectionArtifactError: 输入漂移、既有包冲突或封存失败。
    """

    version = _validate_code_version(code_version)
    plan = load_model_routing_selection_plan(selection_plan_path)
    inputs = _load_routing_selection_inputs(
        scored_package, base_evaluation_package, plan
    )
    report = analyze_model_routing_grid(
        inputs.members,
        inputs.scored_population,
        plan=plan,
        overall_metrics=inputs.overall_metrics,
    )
    analysis_id = _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": "three-way-dual-threshold-wave-a-selection-v1",
                "code_version": version,
                "selection_plan_sha256": plan.plan_sha256,
                "scored_manifest_sha256": inputs.scored_manifest_sha256,
                "base_evaluation_manifest_sha256": inputs.base_evaluation_manifest_sha256,
                "completed_csv_sha256": inputs.completed_csv_sha256,
            }
        )
    )[:32]
    root = Path(artifact_root).expanduser().resolve()
    package = root / analysis_id
    if package.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRoutingSelectionArtifactError(
                "model_routing_selection_existing_package_requires_manifest_hash"
            )
        manifest_path = package / "selection-manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        if _sha256_bytes(manifest_bytes) != expected_existing_manifest_sha256:
            raise ModelRoutingSelectionArtifactError(
                "model_routing_selection_manifest_hash_mismatch"
            )
        return _selection_result(
            _load_json(
                manifest_path, "model_routing_selection_manifest_invalid"
            ),
            manifest_bytes,
            reused=True,
        )
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{analysis_id}.", dir=root)
    )
    try:
        report_path = temporary / "routing-selection-report.json"
        summary_path = temporary / "routing-selection-summary.md"
        report_path.write_bytes(_canonical_bytes(report))
        summary_path.write_text(
            render_routing_selection_markdown(report), encoding="utf-8"
        )
        shutil.copyfile(selection_plan_path, temporary / "selection-plan.yaml")
        artifacts = {
            "report": {
                "filename": report_path.name,
                "sha256": _file_sha256(report_path),
            },
            "summary": {
                "filename": summary_path.name,
                "sha256": _file_sha256(summary_path),
            },
            "selection_plan": {
                "filename": "selection-plan.yaml",
                "sha256": _file_sha256(temporary / "selection-plan.yaml"),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-model-routing-selection",
            "artifact_status": "immutable",
            "analysis_id": analysis_id,
            "status": report["status"],
            "selection_plan_id": plan.plan_id,
            "selection_plan_sha256": plan.plan_sha256,
            "prelabel_protocol_commit": plan.prelabel_protocol_commit,
            "scored_frame_id": plan.scored_frame_id,
            "scored_manifest_sha256": inputs.scored_manifest_sha256,
            "wave_id": plan.wave_id,
            "wave_manifest_sha256": plan.wave_manifest_sha256,
            "base_evaluation_id": plan.base_evaluation_id,
            "base_evaluation_manifest_sha256": inputs.base_evaluation_manifest_sha256,
            "completed_csv_sha256": inputs.completed_csv_sha256,
            "sample_count": report["sample_count"],
            "population_count": report["population_count"],
            "threshold_pair_count_per_model": report[
                "threshold_pair_count_per_model"
            ],
            "pareto_point_count": len(report["pareto_frontier_point_ids"]),
            "selected_model": None,
            "selected_t_keep": None,
            "selected_t_exclude": None,
            "labels_entered_fit": False,
            "fit_call_count": 0,
            "may_freeze_threshold_automatically": False,
            "test_status": "locked_not_opened",
            "threshold_status": "UNSET",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
            "lineage": {"code_version": version},
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "selection-manifest.json").write_bytes(manifest_bytes)
        temporary.rename(package)
        temporary = None
        return _selection_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _selection_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> RoutingSelectionResult:
    """严格校验选择 manifest 并构造去敏运行摘要。"""

    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-routing-selection"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("status") != "WAVE_A_SELECTION_READY"
        or manifest.get("selected_model") is not None
        or manifest.get("selected_t_keep") is not None
        or manifest.get("selected_t_exclude") is not None
        or manifest.get("labels_entered_fit") is not False
        or manifest.get("fit_call_count") != 0
        or manifest.get("may_freeze_threshold_automatically") is not False
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_manifest_invalid"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_manifest_invalid"
        )
    return RoutingSelectionResult(
        analysis_id=str(manifest["analysis_id"]),
        status=str(manifest["status"]),
        reused=reused,
        sample_count=int(manifest["sample_count"]),
        population_count=int(manifest["population_count"]),
        threshold_pair_count_per_model=int(
            manifest["threshold_pair_count_per_model"]
        ),
        pareto_point_count=int(manifest["pareto_point_count"]),
        report_sha256=str(artifacts["report"]["sha256"]),
        summary_sha256=str(artifacts["summary"]["sha256"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        selected_model=manifest["selected_model"],
        selected_t_keep=manifest["selected_t_keep"],
        selected_t_exclude=manifest["selected_t_exclude"],
        labels_entered_fit=bool(manifest["labels_entered_fit"]),
        test_status=str(manifest["test_status"]),
        threshold_status=str(manifest["threshold_status"]),
        audit_status=str(manifest["audit_status"]),
    )


def render_routing_selection_result(
    result: RoutingSelectionResult, *, output_format: str = "human"
) -> str:
    """把选择分析运行结果渲染为稳定 JSON 或中文摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRoutingSelectionArtifactError(
            "model_routing_selection_output_format_invalid"
        )
    return "\n".join(
        [
            "Wave A 三段式双阈值选择分析",
            "============================",
            f"分析 ID：{result.analysis_id}",
            f"状态：{result.status}；artifact：{'复用' if result.reused else '新建并封存'}",
            f"证据：Wave A n={result.sample_count}；评价人口={result.population_count}",
            f"网格：每模型{result.threshold_pair_count_per_model}组；跨模型 Pareto 点={result.pareto_point_count}",
            "完整指标、等工作量比较和区间见 routing-selection-summary.md / routing-selection-report.json。",
            "模型与双阈值：尚未选择；程序不会自动冻结。",
            "新标签：evaluation-only，fit调用=0。",
            "锁定测试：locked_not_opened；阈值：UNSET；审计策略：UNSET。",
        ]
    )
