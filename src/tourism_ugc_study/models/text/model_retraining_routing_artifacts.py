"""风险—人工率Pareto选择、唯一模型与双阈值的不可变封存。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import joblib

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_routing import (
    ModelRetrainingRoutingError,
    RoutingSelectionResult,
    analyze_retraining_routing,
)
from .model_retraining_training import FrozenRetrainingCandidate
from .model_retraining_training_artifacts import (
    load_retraining_training_package,
)


class ModelRetrainingRoutingArtifactError(RuntimeError):
    """路由选择运行包构建、读取或复用失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露成员、正文、概率或路径的稳定失败码。"""

        super().__init__("formal model retraining routing artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RoutingPolicyPackageResult:
    """唯一模型、双阈值和新审计待办的公开摘要。"""

    policy_id: str
    training_run_id: str
    candidate_name: str
    candidate_id: str
    T_keep: float
    T_exclude: float
    manual_review_rate: float
    auto_exclude_related_rate: float
    auto_keep_unrelated_rate: float
    manual_rate_reduction_percentage_points: float
    material_improvement: bool
    pareto_point_count: int
    scanned_point_count: int
    policy_manifest_sha256: str
    model_sha256: str
    reused: bool
    status: str
    audit_status: str
    historical_test_status: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序、禁止NaN且以换行结束的规范JSON。"""

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
    """计算字节流摘要。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算封存文件摘要。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子发布策略文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingRoutingArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingRoutingArtifactError(reason_code)
    return value


def _result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> RoutingPolicyPackageResult:
    """从策略manifest生成公开结果。"""

    selected = manifest["selected"]
    return RoutingPolicyPackageResult(
        policy_id=str(manifest["policy_id"]),
        training_run_id=str(manifest["training_run_id"]),
        candidate_name=str(selected["candidate_name"]),
        candidate_id=str(selected["candidate_id"]),
        T_keep=float(selected["T_keep"]),
        T_exclude=float(selected["T_exclude"]),
        manual_review_rate=float(selected["manual_review_rate"]),
        auto_exclude_related_rate=float(
            selected["auto_exclude_related_rate"]
        ),
        auto_keep_unrelated_rate=float(
            selected["auto_keep_unrelated_rate"]
        ),
        manual_rate_reduction_percentage_points=float(
            manifest["manual_rate_reduction_percentage_points"]
        ),
        material_improvement=bool(manifest["material_improvement"]),
        pareto_point_count=int(manifest["pareto_point_count"]),
        scanned_point_count=int(manifest["scanned_point_count"]),
        policy_manifest_sha256=_sha256_bytes(manifest_bytes),
        model_sha256=str(manifest["artifacts"]["model"]["sha256"]),
        reused=reused,
        status=str(manifest["status"]),
        audit_status=str(manifest["audit_status"]),
        historical_test_status=str(manifest["historical_test_status"]),
    )


def _validate_existing(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> RoutingPolicyPackageResult:
    """验证既有策略包和唯一模型文件。"""

    manifest_path = directory / "routing-policy-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_routing_manifest_invalid"
    )
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-routing-policy"
        or manifest.get("status") != "ROUTING_STRATEGY_FROZEN_AUDIT_PENDING"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("risk_gate_passed") is not True
        or manifest.get("historical_test_status") != "consumed_not_reopened"
        or manifest.get("audit_status") != "PENDING_NEW_BLIND_AUDIT"
        or manifest.get("automatic_routing_authorized") is not False
        or manifest.get("model_search_stopped") is not True
    ):
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_manifest_invalid"
        )
    for key, filename in {
        "model": "frozen-routing-model.joblib",
        "selection": "routing-selection.json",
        "pareto": "risk-manual-pareto.json",
    }.items():
        details = manifest.get("artifacts", {}).get(key, {})
        if (
            details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingRoutingArtifactError(
                "model_retraining_routing_artifact_hash_mismatch"
            )
    return _result(manifest, manifest_path.read_bytes(), reused=True)


def freeze_retraining_routing_policy_package(
    training_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_training_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> RoutingPolicyPackageResult:
    """分析Wave B OOF，选择并封存唯一模型与双阈值。"""

    models, oof, training_manifest = load_retraining_training_package(
        training_package,
        plan=plan,
        expected_manifest_sha256=expected_training_manifest_sha256,
    )
    if len(code_version) != 40:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_code_version_invalid"
        )
    try:
        selection = analyze_retraining_routing(oof, plan=plan)
    except ModelRetrainingRoutingError:
        raise
    selected = selection.selected
    selected_model = models.get(selected.candidate_name)
    if (
        not isinstance(selected_model, FrozenRetrainingCandidate)
        or selected_model.candidate_id != selected.candidate_id
    ):
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_selected_model_mismatch"
        )
    policy_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{training_manifest['training_run_id']}|"
            f"{selected.candidate_id}|{selected.T_keep:.2f}|"
            f"{selected.T_exclude:.2f}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / policy_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingRoutingArtifactError(
                "model_retraining_routing_existing_requires_manifest_hash"
            )
        return _validate_existing(
            directory,
            plan=plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    selection_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-model-retraining-routing-selection",
            **asdict(selection),
        }
    )
    pareto_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-model-retraining-risk-manual-pareto",
            "population_evidence": selection.population_evidence,
            "confidence_interval_note": (
                "raw Clopper-Pearson ignores design weights; component bootstrap "
                "for selected point is a clustered sensitivity interval; neither is a hard gate"
            ),
            "points": [asdict(item) for item in selection.pareto_frontier],
        }
    )
    try:
        directory.mkdir(parents=True, exist_ok=False)
        model_path = directory / "frozen-routing-model.joblib"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".frozen-routing-model.", dir=directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            joblib.dump(selected_model, temporary, compress=3)
            os.replace(temporary, model_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    except (OSError, ValueError) as exc:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_artifact_write_failed"
        ) from exc
    _atomic_write(directory / "routing-selection.json", selection_bytes)
    _atomic_write(directory / "risk-manual-pareto.json", pareto_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-routing-policy",
        "status": "ROUTING_STRATEGY_FROZEN_AUDIT_PENDING",
        "policy_id": policy_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "training_run_id": str(training_manifest["training_run_id"]),
        "training_manifest_sha256": expected_training_manifest_sha256,
        "selected": asdict(selected),
        "risk_gate_passed": selected.risk_gate_passed,
        "historical_manual_rate": selection.historical_manual_rate,
        "manual_rate_reduction_percentage_points": (
            selection.manual_rate_reduction_percentage_points
        ),
        "material_improvement": selection.material_improvement,
        "scanned_point_count": selection.scanned_point_count,
        "support_eligible_point_count": selection.support_eligible_point_count,
        "risk_eligible_point_count": selection.risk_eligible_point_count,
        "pareto_point_count": len(selection.pareto_frontier),
        "selection_order": list(selection.selection_order),
        "selected_component_bootstrap_intervals": (
            selection.selected_component_bootstrap_intervals
        ),
        "model_refit_count": plan.expected_training_count,
        "sampling_weights_entered_fit": False,
        "platform_used": False,
        "historical_test_status": "consumed_not_reopened",
        "audit_status": "PENDING_NEW_BLIND_AUDIT",
        "automatic_routing_authorized": False,
        "model_search_stopped": True,
        "artifacts": {
            "model": {
                "filename": "frozen-routing-model.joblib",
                "sha256": _file_sha256(model_path),
            },
            "selection": {
                "filename": "routing-selection.json",
                "sha256": _sha256_bytes(selection_bytes),
            },
            "pareto": {
                "filename": "risk-manual-pareto.json",
                "sha256": _sha256_bytes(pareto_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "routing-policy-manifest.json", manifest_bytes)
    return _result(manifest, manifest_bytes, reused=False)


def load_retraining_routing_policy_package(
    package: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> tuple[FrozenRetrainingCandidate, Mapping[str, Any]]:
    """严格读取唯一冻结模型与双阈值供纯预测使用。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_package_unavailable"
        ) from exc
    _validate_existing(
        directory,
        plan=plan,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    manifest = _load_json(
        directory / "routing-policy-manifest.json",
        "model_retraining_routing_manifest_invalid",
    )
    try:
        model = joblib.load(directory / "frozen-routing-model.joblib")
    except (OSError, ValueError) as exc:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_model_invalid"
        ) from exc
    if (
        not isinstance(model, FrozenRetrainingCandidate)
        or model.candidate_id != manifest["selected"]["candidate_id"]
    ):
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_model_invalid"
        )
    return model, manifest


def render_retraining_routing_result(
    result: RoutingPolicyPackageResult, *, output_format: str
) -> str:
    """输出机器JSON或面向研究者的策略选择摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_output_format_invalid"
        )
    improvement = (
        "达到至少3个百分点的明显下降"
        if result.material_improvement
        else "未达到3个百分点；按预注册规则仍接受最佳风险合格结果"
    )
    return "\n".join(
        [
            "# 新模型风险—人工率策略冻结结果",
            "",
            f"策略ID：{result.policy_id}",
            f"唯一候选：{result.candidate_name} [{result.candidate_id}]",
            f"双阈值：T_keep={result.T_keep:.2f} / T_exclude={result.T_exclude:.2f}",
            "",
            "Wave B交叉拟合设计加权证据",
            f"  人工率：{result.manual_review_rate:.2%}",
            f"  自动排除端UGC风险：{result.auto_exclude_related_rate:.2%}（门≤2%）",
            f"  自动保留端无关风险：{result.auto_keep_unrelated_rate:.2%}（门≤5%）",
            f"  相对历史20.36%人工率：{result.manual_rate_reduction_percentage_points:+.2f}个百分点；{improvement}",
            f"  Pareto前沿：{result.pareto_point_count}点 / 扫描{result.scanned_point_count}点",
            "",
            "旧锁定测试：consumed_not_reopened；没有重新判定。",
            "新盲审：PENDING_NEW_BLIND_AUDIT；正式自动路由尚未授权。",
            "固定候选比较已结束，不再扩大模型搜索。",
            f"状态：{result.status}",
        ]
    )
