"""研究者阈值冻结、概率重分流、人工中间层与最终决定的不可变artifact。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model_retraining_audit import load_audit_assessment_package
from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_delivery import (
    DeliveryDecision,
    build_delivery_decisions,
    build_manual_review_members,
    reroute_scored_members,
)
from .model_retraining_delivery_config import ModelRetrainingDeliveryPlan
from .model_retraining_inference import (
    ScoredRoutingMember,
    load_inference_scoring_package,
)
from .model_retraining_routing_artifacts import (
    load_retraining_routing_policy_package,
)
from .model_retraining_snapshot_artifacts import load_retraining_snapshot_package
from .model_retraining_threshold_grid import load_threshold_grid_package


class ModelRetrainingDeliveryArtifactError(RuntimeError):
    """交付谱系、文件持久化或严格复用失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、成员、概率或私有路径的稳定失败码。"""

        super().__init__("formal model retraining delivery artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ResearcherPolicyResult:
    """研究者显式冻结模型、阈值与风险接受依据的公开摘要。"""

    policy_id: str
    candidate_id: str
    T_keep: float
    T_exclude: float
    model_sha256: str
    manifest_sha256: str
    reused: bool
    status: str


@dataclass(frozen=True)
class ReroutingPackageResult:
    """既有概率按新阈值重分流的公开计数。"""

    rerouting_id: str
    policy_id: str
    count: int
    action_counts: Mapping[str, int]
    records_sha256: str
    manifest_sha256: str
    reused: bool
    status: str


@dataclass(frozen=True)
class ManualReviewPackageResult:
    """尚无人工作答的中间层四列表及私有映射摘要。"""

    manual_review_id: str
    count: int
    task_filename: str
    task_sha256: str
    private_map_sha256: str
    manifest_sha256: str
    reused: bool
    status: str


@dataclass(frozen=True)
class DeliveryDecisionPackageResult:
    """研究者策略下完整派生决定的公开计数与只读保证。"""

    decision_run_id: str
    count: int
    action_counts: Mapping[str, int]
    source_counts: Mapping[str, int]
    decisions_sha256: str
    manifest_sha256: str
    source_database_write_count: int
    reused: bool
    status: str


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
    """计算字节流SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算交付artifact摘要。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子发布交付文件，避免半成品被后续阶段读取。"""

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
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_artifact_write_failed"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射并封装稳定失败语义。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingDeliveryArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingDeliveryArtifactError(reason_code)
    return value


def _require_code_version(code_version: str) -> None:
    """要求正式运行绑定完整Git提交身份。"""

    if len(code_version) != 40 or any(
        character not in "0123456789abcdef" for character in code_version
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_code_version_invalid"
        )


def _create_directory(directory: Path) -> None:
    """创建新的内容寻址目录并拒绝非复用覆盖。"""

    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_directory_create_failed"
        ) from exc


def _verify_bindings(
    plan: ModelRetrainingPlan, delivery_plan: ModelRetrainingDeliveryPlan
) -> None:
    """保证交付决策仍绑定原冻结重训计划。"""

    if plan.plan_sha256 != delivery_plan.bindings["retraining_plan_sha256"]:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_plan_binding_mismatch"
        )


def _point_matches(
    point: Mapping[str, Any], delivery_plan: ModelRetrainingDeliveryPlan
) -> bool:
    """逐字段核对研究者选择与已封存阈值网格证据。"""

    if (
        float(point.get("T_keep", -1.0)) != delivery_plan.selection.T_keep
        or float(point.get("T_exclude", -1.0)) != delivery_plan.selection.T_exclude
    ):
        return False
    for key, expected in delivery_plan.selected_point_evidence.items():
        if key == "population_count":
            continue
        actual = point.get(key)
        try:
            matches = (
                not isinstance(actual, bool) and int(actual) == expected
                if isinstance(expected, int)
                else abs(float(actual) - expected) <= 1e-15
            )
        except (TypeError, ValueError):
            return False
        if not matches:
            return False
    return True


def _policy_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> ResearcherPolicyResult:
    """从已验证策略manifest生成公开摘要。"""

    selected = manifest["selected"]
    return ResearcherPolicyResult(
        policy_id=str(manifest["policy_id"]),
        candidate_id=str(selected["candidate_id"]),
        T_keep=float(selected["T_keep"]),
        T_exclude=float(selected["T_exclude"]),
        model_sha256=str(manifest["artifacts"]["model"]["sha256"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_policy_package(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_manifest_sha256: str,
) -> ResearcherPolicyResult:
    """严格验证研究者冻结策略及原模型字节复用。"""

    manifest_path = directory / "routing-policy-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_policy_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_delivery_policy_manifest_invalid"
    )
    selected = manifest.get("selected", {})
    artifacts = manifest.get("artifacts", {})
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-routing-policy"
        or manifest.get("status") != "RESEARCHER_SELECTED_ROUTING_FROZEN"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("delivery_decision_id") != delivery_plan.decision_id
        or manifest.get("acceptance_basis")
        != delivery_plan.selection.acceptance_basis
        or manifest.get("historical_audit_outcome_preserved") is not True
        or manifest.get("automatic_routing_authorized") is not True
        or manifest.get("independent_release_evidence") is not False
        or selected.get("candidate_id") != delivery_plan.selection.candidate_id
        or float(selected.get("T_keep", -1.0)) != delivery_plan.selection.T_keep
        or float(selected.get("T_exclude", -1.0))
        != delivery_plan.selection.T_exclude
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_policy_manifest_invalid"
        )
    for key, filename in {
        "model": "frozen-routing-model.joblib",
        "selection": "researcher-selection.json",
        "evidence": "post-hoc-evidence.json",
    }.items():
        details = artifacts.get(key, {})
        if (
            details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingDeliveryArtifactError(
                "model_retraining_delivery_policy_artifact_hash_mismatch"
            )
    if artifacts["model"]["sha256"] != delivery_plan.bindings["frozen_model_sha256"]:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_model_hash_mismatch"
        )
    return _policy_result(manifest, manifest_path.read_bytes(), reused=True)


def freeze_researcher_routing_policy_package(
    base_policy_package: str | Path,
    threshold_grid_package: str | Path,
    audit_assessment_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> ResearcherPolicyResult:
    """复用冻结模型并以研究者明确接受风险的0.31/0.96创建新策略。

    该函数保留旧审计的单尾放行结论，只把300条转为事后阈值诊断；新策略
    不宣称拥有独立放行证据，也不执行fit、predict或编码。
    """

    _require_code_version(code_version)
    _verify_bindings(plan, delivery_plan)
    model, base_manifest = load_retraining_routing_policy_package(
        base_policy_package,
        plan=plan,
        expected_manifest_sha256=delivery_plan.bindings[
            "base_policy_manifest_sha256"
        ],
    )
    points, grid_manifest = load_threshold_grid_package(
        threshold_grid_package,
        plan=plan,
        expected_manifest_sha256=delivery_plan.bindings[
            "threshold_grid_manifest_sha256"
        ],
    )
    _, audit_manifest = load_audit_assessment_package(
        audit_assessment_package,
        plan=plan,
        expected_manifest_sha256=delivery_plan.bindings[
            "audit_assessment_manifest_sha256"
        ],
    )
    base_directory = Path(base_policy_package).expanduser().resolve(strict=True)
    if (
        str(base_manifest["policy_id"])
        != delivery_plan.bindings["base_policy_id"]
        or model.candidate_id != delivery_plan.selection.candidate_id
        or _file_sha256(base_directory / "frozen-routing-model.joblib")
        != delivery_plan.bindings["frozen_model_sha256"]
        or str(grid_manifest["exploration_id"])
        != delivery_plan.bindings["threshold_exploration_id"]
        or grid_manifest["artifacts"]["grid_csv"]["sha256"]
        != delivery_plan.bindings["threshold_grid_sha256"]
        or int(grid_manifest["population_count"])
        != delivery_plan.selected_point_evidence["population_count"]
        or str(audit_manifest["assessment_id"])
        != delivery_plan.bindings["audit_assessment_id"]
        or str(audit_manifest["status"])
        != delivery_plan.selection.historical_audit_status
        or audit_manifest.get("failed_automatic_actions")
        != [delivery_plan.selection.historical_failed_action]
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_input_binding_mismatch"
        )
    matching = [point for point in points if _point_matches(point, delivery_plan)]
    if len(matching) != 1:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_selected_point_mismatch"
        )
    point = matching[0]
    policy_id = hashlib.sha256(
        (
            f"{delivery_plan.decision_id}|{code_version}|"
            "researcher-selected-routing-policy"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / policy_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingDeliveryArtifactError(
                "model_retraining_delivery_existing_requires_manifest_hash"
            )
        return _validate_policy_package(
            directory,
            plan=plan,
            delivery_plan=delivery_plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    _create_directory(directory)
    model_bytes = (base_directory / "frozen-routing-model.joblib").read_bytes()
    selection_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-researcher-routing-selection",
            "delivery_decision_id": delivery_plan.decision_id,
            "candidate_name": delivery_plan.selection.candidate_name,
            "candidate_id": delivery_plan.selection.candidate_id,
            "T_keep": delivery_plan.selection.T_keep,
            "T_exclude": delivery_plan.selection.T_exclude,
            "acceptance_basis": delivery_plan.selection.acceptance_basis,
            "enabled_automatic_actions": list(
                delivery_plan.selection.enabled_automatic_actions
            ),
        }
    )
    evidence_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-post-hoc-threshold-evidence",
            "selected_point": point,
            "historical_audit_status": str(audit_manifest["status"]),
            "historical_audit_outcome_preserved": True,
            "audit_evidence_role": delivery_plan.selection.audit_evidence_role,
            "independent_release_evidence": False,
            "look_elsewhere_caution": True,
            "selected_after_label_review": True,
        }
    )
    _atomic_write(directory / "frozen-routing-model.joblib", model_bytes)
    _atomic_write(directory / "researcher-selection.json", selection_bytes)
    _atomic_write(directory / "post-hoc-evidence.json", evidence_bytes)
    selected = {
        "candidate_name": delivery_plan.selection.candidate_name,
        "candidate_id": delivery_plan.selection.candidate_id,
        "T_keep": delivery_plan.selection.T_keep,
        "T_exclude": delivery_plan.selection.T_exclude,
        "manual_review_rate": float(point["population_manual_review_rate"]),
        "auto_exclude_related_rate": float(
            point["wave_b_auto_exclude_weighted_risk"]
        ),
        "auto_keep_unrelated_rate": float(
            point["wave_b_auto_keep_weighted_risk"]
        ),
        "minimum_tail_support_passed": bool(
            point["wave_b_minimum_support_passed"]
        ),
        "risk_gate_passed": False,
        "raw_auto_keep_count": int(point["wave_b_auto_keep_count"]),
        "raw_manual_review_count": int(point["wave_b_manual_review_count"]),
        "raw_auto_exclude_count": int(point["wave_b_auto_exclude_count"]),
        "auto_keep_adverse_count": int(
            point["wave_b_auto_keep_adverse_count"]
        ),
        "auto_exclude_adverse_count": int(
            point["wave_b_auto_exclude_adverse_count"]
        ),
        "wave_b_weighted_log_loss": float(
            base_manifest["selected"]["wave_b_weighted_log_loss"]
        ),
    }
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-routing-policy",
        "status": "RESEARCHER_SELECTED_ROUTING_FROZEN",
        "policy_id": policy_id,
        "delivery_decision_id": delivery_plan.decision_id,
        "delivery_config_sha256": delivery_plan.config_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "training_run_id": str(base_manifest["training_run_id"]),
        "training_manifest_sha256": str(base_manifest["training_manifest_sha256"]),
        "base_policy_id": str(base_manifest["policy_id"]),
        "base_policy_manifest_sha256": delivery_plan.bindings[
            "base_policy_manifest_sha256"
        ],
        "threshold_exploration_id": str(grid_manifest["exploration_id"]),
        "threshold_grid_manifest_sha256": delivery_plan.bindings[
            "threshold_grid_manifest_sha256"
        ],
        "historical_audit_assessment_id": str(audit_manifest["assessment_id"]),
        "historical_audit_manifest_sha256": delivery_plan.bindings[
            "audit_assessment_manifest_sha256"
        ],
        "historical_audit_status": str(audit_manifest["status"]),
        "historical_audit_outcome_preserved": True,
        "historical_failed_automatic_actions": list(
            audit_manifest["failed_automatic_actions"]
        ),
        "historical_test_status": "consumed_not_reopened",
        "audit_status": "HISTORICAL_AUDIT_POST_HOC_ONLY",
        "audit_evidence_role": delivery_plan.selection.audit_evidence_role,
        "independent_release_evidence": False,
        "acceptance_basis": delivery_plan.selection.acceptance_basis,
        "automatic_routing_authorized": True,
        "enabled_automatic_actions": list(
            delivery_plan.selection.enabled_automatic_actions
        ),
        "model_search_stopped": True,
        "model_refit_count": 1300,
        "frozen_model_reused_exact_bytes": True,
        "fit_call_count": 0,
        "predict_call_count": 0,
        "encoder_call_count": 0,
        "source_database_write_count": 0,
        "source_records_deleted": 0,
        "platform_used": False,
        "risk_gate_passed": False,
        "risk_gate_interpretation": "researcher_overrode_raw_and_audit_point_gates",
        "historical_manual_rate": float(base_manifest["historical_manual_rate"]),
        "manual_rate_reduction_percentage_points": float(
            base_manifest["historical_manual_rate"]
            - point["population_manual_review_rate"]
        )
        * 100.0,
        "material_improvement": False,
        "pareto_point_count": int(grid_manifest["eligible_point_count"]),
        "scanned_point_count": int(grid_manifest["point_count"]),
        "selected": selected,
        "artifacts": {
            "model": {
                "filename": "frozen-routing-model.joblib",
                "sha256": _sha256_bytes(model_bytes),
            },
            "selection": {
                "filename": "researcher-selection.json",
                "sha256": _sha256_bytes(selection_bytes),
            },
            "evidence": {
                "filename": "post-hoc-evidence.json",
                "sha256": _sha256_bytes(evidence_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "routing-policy-manifest.json", manifest_bytes)
    return _policy_result(manifest, manifest_bytes, reused=False)


def load_researcher_routing_policy_package(
    package: str | Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_manifest_sha256: str,
) -> Mapping[str, Any]:
    """严格读取研究者冻结策略manifest供重分流与交付使用。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_policy_package_unavailable"
        ) from exc
    _validate_policy_package(
        directory,
        plan=plan,
        delivery_plan=delivery_plan,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    return _load_json(
        directory / "routing-policy-manifest.json",
        "model_retraining_delivery_policy_manifest_invalid",
    )


def _rerouting_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> ReroutingPackageResult:
    """从已验证重分流manifest生成公开摘要。"""

    return ReroutingPackageResult(
        rerouting_id=str(manifest["rerouting_id"]),
        policy_id=str(manifest["policy_id"]),
        count=int(manifest["count"]),
        action_counts=dict(manifest["action_counts"]),
        records_sha256=str(manifest["artifacts"]["records"]["sha256"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_rerouting_package(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_manifest_sha256: str,
) -> ReroutingPackageResult:
    """严格验证既有概率重分流包。"""

    manifest_path = directory / "rerouting-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_rerouting_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_delivery_rerouting_manifest_invalid"
    )
    records = manifest.get("artifacts", {}).get("records", {})
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-probability-rerouting"
        or manifest.get("status") != "EXISTING_PROBABILITIES_REROUTED"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("delivery_decision_id") != delivery_plan.decision_id
        or manifest.get("count") != delivery_plan.selected_point_evidence[
            "population_count"
        ]
        or manifest.get("fit_call_count") != 0
        or manifest.get("predict_call_count") != 0
        or manifest.get("encoder_call_count") != 0
        or records.get("filename") != "rerouted-records.json"
        or _file_sha256(directory / "rerouted-records.json")
        != records.get("sha256")
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_rerouting_manifest_invalid"
        )
    return _rerouting_result(manifest, manifest_path.read_bytes(), reused=True)


def reroute_existing_probabilities_package(
    inference_package: str | Path,
    policy_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_policy_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> ReroutingPackageResult:
    """不重新编码或预测，直接按0.31/0.96重分流既有12,558条概率。"""

    _require_code_version(code_version)
    _verify_bindings(plan, delivery_plan)
    records, inference_manifest = load_inference_scoring_package(
        inference_package,
        plan=plan,
        expected_manifest_sha256=delivery_plan.bindings[
            "inference_manifest_sha256"
        ],
    )
    policy_manifest = load_researcher_routing_policy_package(
        policy_package,
        plan=plan,
        delivery_plan=delivery_plan,
        expected_manifest_sha256=expected_policy_manifest_sha256,
    )
    inference_directory = Path(inference_package).expanduser().resolve(strict=True)
    if (
        str(inference_manifest["inference_id"])
        != delivery_plan.bindings["inference_id"]
        or inference_manifest["artifacts"]["records"]["sha256"]
        != delivery_plan.bindings["inference_records_sha256"]
        or _file_sha256(inference_directory / "scored-records.json")
        != delivery_plan.bindings["inference_records_sha256"]
        or str(inference_manifest["candidate_id"])
        != delivery_plan.selection.candidate_id
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_inference_binding_mismatch"
        )
    rerouted = reroute_scored_members(
        records,
        T_keep=delivery_plan.selection.T_keep,
        T_exclude=delivery_plan.selection.T_exclude,
    )
    counts = dict(sorted(Counter(item.provisional_action for item in rerouted).items()))
    expected_counts = {
        "auto_keep": delivery_plan.selected_point_evidence[
            "population_auto_keep_count"
        ],
        "manual_review": delivery_plan.selected_point_evidence[
            "population_manual_review_count"
        ],
        "auto_exclude": delivery_plan.selected_point_evidence[
            "population_auto_exclude_count"
        ],
    }
    if counts != expected_counts:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_rerouting_counts_mismatch"
        )
    rerouting_id = hashlib.sha256(
        (
            f"{delivery_plan.decision_id}|{policy_manifest['policy_id']}|"
            f"{inference_manifest['inference_id']}|{code_version}|reroute"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / rerouting_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingDeliveryArtifactError(
                "model_retraining_delivery_existing_requires_manifest_hash"
            )
        return _validate_rerouting_package(
            directory,
            plan=plan,
            delivery_plan=delivery_plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    _create_directory(directory)
    records_bytes = _canonical_bytes([asdict(item) for item in rerouted])
    _atomic_write(directory / "rerouted-records.json", records_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-probability-rerouting",
        "status": "EXISTING_PROBABILITIES_REROUTED",
        "rerouting_id": rerouting_id,
        "delivery_decision_id": delivery_plan.decision_id,
        "delivery_config_sha256": delivery_plan.config_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "policy_id": str(policy_manifest["policy_id"]),
        "policy_manifest_sha256": expected_policy_manifest_sha256,
        "source_inference_id": str(inference_manifest["inference_id"]),
        "source_inference_manifest_sha256": delivery_plan.bindings[
            "inference_manifest_sha256"
        ],
        "source_probability_records_sha256": delivery_plan.bindings[
            "inference_records_sha256"
        ],
        "candidate_id": delivery_plan.selection.candidate_id,
        "candidate_name": delivery_plan.selection.candidate_name,
        "T_keep": delivery_plan.selection.T_keep,
        "T_exclude": delivery_plan.selection.T_exclude,
        "count": len(rerouted),
        "action_counts": counts,
        "probabilities_reused_without_change": True,
        "fit_call_count": 0,
        "predict_call_count": 0,
        "encoder_call_count": 0,
        "source_database_write_count": 0,
        "source_records_deleted": 0,
        "platform_used": False,
        "artifacts": {
            "records": {
                "filename": "rerouted-records.json",
                "sha256": _sha256_bytes(records_bytes),
            }
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "rerouting-manifest.json", manifest_bytes)
    return _rerouting_result(manifest, manifest_bytes, reused=False)


def load_rerouting_package(
    package: str | Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_manifest_sha256: str,
) -> tuple[tuple[ScoredRoutingMember, ...], Mapping[str, Any]]:
    """严格读取重分流记录供人工表和最终决定使用。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_rerouting_package_unavailable"
        ) from exc
    _validate_rerouting_package(
        directory,
        plan=plan,
        delivery_plan=delivery_plan,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    manifest = _load_json(
        directory / "rerouting-manifest.json",
        "model_retraining_delivery_rerouting_manifest_invalid",
    )
    try:
        raw = json.loads(
            (directory / "rerouted-records.json").read_text(encoding="utf-8")
        )
        records = tuple(ScoredRoutingMember(**item) for item in raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_rerouting_records_invalid"
        ) from exc
    if (
        len(records) != delivery_plan.selected_point_evidence["population_count"]
        or len({item.identity for item in records}) != len(records)
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_rerouting_records_invalid"
        )
    return records, manifest


def _manual_csv_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    """以UTF-8 BOM和固定四列生成人工中间层任务。"""

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "task_id",
            "sample_run_id",
            "normalized_model_text",
            "tourism_label",
        ],
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _manual_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> ManualReviewPackageResult:
    """从已验证人工表manifest生成公开摘要。"""

    return ManualReviewPackageResult(
        manual_review_id=str(manifest["manual_review_id"]),
        count=int(manifest["count"]),
        task_filename=str(manifest["artifacts"]["task"]["filename"]),
        task_sha256=str(manifest["artifacts"]["task"]["sha256"]),
        private_map_sha256=str(manifest["artifacts"]["private_map"]["sha256"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_manual_package(
    directory: Path,
    *,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_manifest_sha256: str,
) -> ManualReviewPackageResult:
    """严格验证人工中间层四列表及私有映射。"""

    manifest_path = directory / "manual-review-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_manual_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_delivery_manual_manifest_invalid"
    )
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-manual-middle-task"
        or manifest.get("status") != "MANUAL_MIDDLE_TASK_FROZEN"
        or manifest.get("delivery_decision_id") != delivery_plan.decision_id
        or manifest.get("count") != delivery_plan.expected_manual_task_count
        or manifest.get("task_columns") != list(delivery_plan.manual_task_columns)
        or manifest.get("model_outputs_present_in_task") is not False
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_manual_manifest_invalid"
        )
    for key, filename in {
        "task": delivery_plan.manual_task_filename,
        "private_map": "private-map.json",
    }.items():
        details = manifest.get("artifacts", {}).get(key, {})
        if (
            details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingDeliveryArtifactError(
                "model_retraining_delivery_manual_artifact_hash_mismatch"
            )
    return _manual_result(manifest, manifest_path.read_bytes(), reused=True)


def prepare_manual_review_package(
    rerouting_package: str | Path,
    audit_assessment_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_rerouting_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> ManualReviewPackageResult:
    """稳定产出尚无人工作答的中间层四列表，不暴露概率或入选原因。"""

    _require_code_version(code_version)
    records, rerouting_manifest = load_rerouting_package(
        rerouting_package,
        plan=plan,
        delivery_plan=delivery_plan,
        expected_manifest_sha256=expected_rerouting_manifest_sha256,
    )
    audit_labels, audit_manifest = load_audit_assessment_package(
        audit_assessment_package,
        plan=plan,
        expected_manifest_sha256=delivery_plan.bindings[
            "audit_assessment_manifest_sha256"
        ],
    )
    audit_member_keys = {str(item["member_key"]) for item in audit_labels}
    if (
        len(audit_member_keys) != 300
        or str(audit_manifest["assessment_id"])
        != delivery_plan.bindings["audit_assessment_id"]
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_audit_members_invalid"
        )
    manual_review_id = hashlib.sha256(
        (
            f"{delivery_plan.decision_id}|{rerouting_manifest['rerouting_id']}|"
            f"{audit_manifest['assessment_id']}|{code_version}|manual-middle"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / manual_review_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingDeliveryArtifactError(
                "model_retraining_delivery_existing_requires_manifest_hash"
            )
        return _validate_manual_package(
            directory,
            delivery_plan=delivery_plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    tasks = build_manual_review_members(
        records,
        excluded_human_member_keys=audit_member_keys,
        sample_run_id=manual_review_id,
    )
    if len(tasks) != delivery_plan.expected_manual_task_count:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_manual_count_mismatch"
        )
    task_rows = [
        {
            "task_id": item.task_id,
            "sample_run_id": item.sample_run_id,
            "normalized_model_text": item.member.normalized_model_text,
            "tourism_label": "",
        }
        for item in tasks
    ]
    private_rows = [
        {
            "task_id": item.task_id,
            "member_key": item.member.member_key,
            "source_post_id": item.member.source_post_id,
            "source_version": item.member.source_version,
            "component_id": item.member.component_id,
            "normalized_sha256": item.member.normalized_sha256,
            "p_unrelated": item.member.p_unrelated,
            "routing_action": item.member.provisional_action,
        }
        for item in tasks
    ]
    task_bytes = _manual_csv_bytes(task_rows)
    private_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-manual-middle-private-map",
            "manual_review_id": manual_review_id,
            "records": private_rows,
        }
    )
    _create_directory(directory)
    _atomic_write(directory / delivery_plan.manual_task_filename, task_bytes)
    _atomic_write(directory / "private-map.json", private_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-manual-middle-task",
        "status": "MANUAL_MIDDLE_TASK_FROZEN",
        "manual_review_id": manual_review_id,
        "delivery_decision_id": delivery_plan.decision_id,
        "delivery_config_sha256": delivery_plan.config_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "rerouting_id": str(rerouting_manifest["rerouting_id"]),
        "rerouting_manifest_sha256": expected_rerouting_manifest_sha256,
        "audit_assessment_id": str(audit_manifest["assessment_id"]),
        "audit_assessment_manifest_sha256": delivery_plan.bindings[
            "audit_assessment_manifest_sha256"
        ],
        "count": len(tasks),
        "already_human_labeled_members_excluded": 300,
        "task_columns": list(delivery_plan.manual_task_columns),
        "task_encoding": "utf-8-sig",
        "human_fills_only": "tourism_label",
        "valid_labels": ["related", "unrelated", "uncertain"],
        "one_record_per_row": True,
        "model_outputs_present_in_task": False,
        "selection_reason_present_in_task": False,
        "private_identity_present_in_task": False,
        "fit_call_count": 0,
        "predict_call_count": 0,
        "source_database_write_count": 0,
        "artifacts": {
            "task": {
                "filename": delivery_plan.manual_task_filename,
                "sha256": _sha256_bytes(task_bytes),
            },
            "private_map": {
                "filename": "private-map.json",
                "sha256": _sha256_bytes(private_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "manual-review-manifest.json", manifest_bytes)
    return _manual_result(manifest, manifest_bytes, reused=False)


def _decision_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> DeliveryDecisionPackageResult:
    """从已验证最终决定manifest生成公开摘要。"""

    return DeliveryDecisionPackageResult(
        decision_run_id=str(manifest["decision_run_id"]),
        count=int(manifest["count"]),
        action_counts=dict(manifest["action_counts"]),
        source_counts=dict(manifest["source_counts"]),
        decisions_sha256=str(manifest["artifacts"]["decisions"]["sha256"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        source_database_write_count=int(manifest["source_database_write_count"]),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_decision_package(
    directory: Path,
    *,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_manifest_sha256: str,
) -> DeliveryDecisionPackageResult:
    """严格验证研究者策略下的最终派生决定。"""

    manifest_path = directory / "delivery-decision-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_decision_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_delivery_decision_manifest_invalid"
    )
    artifact = manifest.get("artifacts", {}).get("decisions", {})
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-delivery-decisions"
        or manifest.get("status") != "ROUTING_DELIVERABLE_READY"
        or manifest.get("delivery_decision_id") != delivery_plan.decision_id
        or manifest.get("count") != delivery_plan.expected_total_decision_count
        or manifest.get("action_counts") != dict(delivery_plan.expected_action_counts)
        or manifest.get("source_counts") != dict(delivery_plan.expected_source_counts)
        or manifest.get("source_database_write_count") != 0
        or manifest.get("source_records_deleted") != 0
        or artifact.get("filename") != "final-decisions.json"
        or _file_sha256(directory / "final-decisions.json")
        != artifact.get("sha256")
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_decision_manifest_invalid"
        )
    return _decision_result(manifest, manifest_path.read_bytes(), reused=True)


def build_delivery_decisions_package(
    snapshot_package: str | Path,
    rerouting_package: str | Path,
    manual_review_package: str | Path,
    audit_assessment_package: str | Path,
    policy_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    expected_rerouting_manifest_sha256: str,
    expected_manual_manifest_sha256: str,
    expected_policy_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> DeliveryDecisionPackageResult:
    """合并1,300+300人工标签、两个自动尾部与中间层，不写源库。"""

    _require_code_version(code_version)
    snapshot, snapshot_manifest = load_retraining_snapshot_package(
        snapshot_package,
        plan=plan,
        expected_manifest_sha256=delivery_plan.bindings[
            "snapshot_manifest_sha256"
        ],
    )
    records, rerouting_manifest = load_rerouting_package(
        rerouting_package,
        plan=plan,
        delivery_plan=delivery_plan,
        expected_manifest_sha256=expected_rerouting_manifest_sha256,
    )
    audit_labels, audit_manifest = load_audit_assessment_package(
        audit_assessment_package,
        plan=plan,
        expected_manifest_sha256=delivery_plan.bindings[
            "audit_assessment_manifest_sha256"
        ],
    )
    policy_manifest = load_researcher_routing_policy_package(
        policy_package,
        plan=plan,
        delivery_plan=delivery_plan,
        expected_manifest_sha256=expected_policy_manifest_sha256,
    )
    manual_directory = Path(manual_review_package).expanduser().resolve(strict=True)
    _validate_manual_package(
        manual_directory,
        delivery_plan=delivery_plan,
        expected_manifest_sha256=expected_manual_manifest_sha256,
    )
    manual_manifest = _load_json(
        manual_directory / "manual-review-manifest.json",
        "model_retraining_delivery_manual_manifest_invalid",
    )
    private_map = _load_json(
        manual_directory / "private-map.json",
        "model_retraining_delivery_manual_private_map_invalid",
    )
    manual_rows = private_map.get("records")
    manual_member_keys = {
        str(item["member_key"]) for item in manual_rows
    } if isinstance(manual_rows, list) else set()
    expected_manual_keys = {
        item.member_key
        for item in records
        if item.provisional_action == "manual_review"
        and item.member_key
        not in {str(label["member_key"]) for label in audit_labels}
    }
    if (
        str(snapshot_manifest["snapshot_id"])
        != delivery_plan.bindings["snapshot_id"]
        or manual_member_keys != expected_manual_keys
        or len(manual_member_keys) != delivery_plan.expected_manual_task_count
    ):
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_manual_coverage_mismatch"
        )
    decisions = build_delivery_decisions(
        snapshot.documents,
        records,
        audit_labels,
        policy_id=str(policy_manifest["policy_id"]),
        delivery_plan=delivery_plan,
    )
    decision_run_id = hashlib.sha256(
        (
            f"{delivery_plan.decision_id}|{policy_manifest['policy_id']}|"
            f"{rerouting_manifest['rerouting_id']}|"
            f"{manual_manifest['manual_review_id']}|{code_version}|delivery"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / decision_run_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingDeliveryArtifactError(
                "model_retraining_delivery_existing_requires_manifest_hash"
            )
        return _validate_decision_package(
            directory,
            delivery_plan=delivery_plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    decisions_bytes = _canonical_bytes([asdict(item) for item in decisions])
    action_counts = dict(sorted(Counter(item.final_action for item in decisions).items()))
    source_counts = dict(sorted(Counter(item.decision_source for item in decisions).items()))
    _create_directory(directory)
    _atomic_write(directory / "final-decisions.json", decisions_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-delivery-decisions",
        "status": "ROUTING_DELIVERABLE_READY",
        "decision_run_id": decision_run_id,
        "delivery_decision_id": delivery_plan.decision_id,
        "delivery_config_sha256": delivery_plan.config_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "policy_id": str(policy_manifest["policy_id"]),
        "policy_manifest_sha256": expected_policy_manifest_sha256,
        "rerouting_id": str(rerouting_manifest["rerouting_id"]),
        "rerouting_manifest_sha256": expected_rerouting_manifest_sha256,
        "manual_review_id": str(manual_manifest["manual_review_id"]),
        "manual_review_manifest_sha256": expected_manual_manifest_sha256,
        "snapshot_id": str(snapshot_manifest["snapshot_id"]),
        "snapshot_manifest_sha256": delivery_plan.bindings[
            "snapshot_manifest_sha256"
        ],
        "audit_assessment_id": str(audit_manifest["assessment_id"]),
        "audit_assessment_manifest_sha256": delivery_plan.bindings[
            "audit_assessment_manifest_sha256"
        ],
        "acceptance_basis": delivery_plan.selection.acceptance_basis,
        "historical_audit_outcome_preserved": True,
        "independent_release_evidence": False,
        "enabled_automatic_actions": list(
            delivery_plan.selection.enabled_automatic_actions
        ),
        "T_keep": delivery_plan.selection.T_keep,
        "T_exclude": delivery_plan.selection.T_exclude,
        "count": len(decisions),
        "action_counts": action_counts,
        "source_counts": source_counts,
        "fit_call_count": 0,
        "predict_call_count": 0,
        "encoder_call_count": 0,
        "source_database_write_count": 0,
        "source_records_deleted": 0,
        "platform_used": False,
        "artifacts": {
            "decisions": {
                "filename": "final-decisions.json",
                "sha256": _sha256_bytes(decisions_bytes),
            }
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "delivery-decision-manifest.json", manifest_bytes)
    return _decision_result(manifest, manifest_bytes, reused=False)


def render_delivery_result(result: object, *, output_format: str) -> str:
    """输出机器JSON或各交付阶段的简洁中文摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingDeliveryArtifactError(
            "model_retraining_delivery_output_format_invalid"
        )
    if isinstance(result, ResearcherPolicyResult):
        return "\n".join(
            [
                "# 研究者双阈值策略冻结",
                "",
                f"策略ID：{result.policy_id}",
                f"模型：{result.candidate_id}（原模型字节SHA-256：{result.model_sha256}）",
                f"阈值：T_keep={result.T_keep:.2f} / T_exclude={result.T_exclude:.2f}",
                "依据：研究者明确接受事后风险；旧审计未通过结论保持不变。",
                "fit/predict/encoder调用：0；新策略不具有独立放行证据。",
                f"状态：{result.status}",
            ]
        )
    if isinstance(result, ReroutingPackageResult):
        return "\n".join(
            [
                "# 既有概率重分流",
                "",
                f"重分流ID：{result.rerouting_id}",
                "动作："
                + " / ".join(
                    f"{key}={value}" for key, value in result.action_counts.items()
                ),
                "只复用既有概率；fit/predict/encoder调用均为0。",
                f"状态：{result.status}",
            ]
        )
    if isinstance(result, ManualReviewPackageResult):
        return "\n".join(
            [
                "# 人工中间层任务表",
                "",
                f"任务ID：{result.manual_review_id}",
                f"待标注：{result.count}条；文件：{result.task_filename}",
                "UTF-8 BOM固定四列；人工只填写tourism_label。",
                f"状态：{result.status}",
            ]
        )
    if isinstance(result, DeliveryDecisionPackageResult):
        return "\n".join(
            [
                "# 数据清洗路由交付",
                "",
                f"决定运行ID：{result.decision_run_id}",
                "动作："
                + " / ".join(
                    f"{key}={value}" for key, value in result.action_counts.items()
                ),
                "证据来源："
                + " / ".join(
                    f"{key}={value}" for key, value in result.source_counts.items()
                ),
                "源数据库写入：0；源记录删除：0。",
                f"状态：{result.status}",
            ]
        )
    raise ModelRetrainingDeliveryArtifactError(
        "model_retraining_delivery_result_type_invalid"
    )
