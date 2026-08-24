"""最终判读与双尾审计计划的输入校验、不可变封存和摘要。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .model_deployment_acceptance_config import (
    ModelDeploymentAcceptancePlan,
    load_model_deployment_acceptance_plan,
)


class ModelDeploymentAcceptanceArtifactError(RuntimeError):
    """输入谱系、不变性或封存边界失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、成员、标签或路径的稳定失败码。"""

        super().__init__("formal deployment acceptance artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class DeploymentAcceptanceFreezeResult:
    """最终判读与审计计划封存后的公开去敏摘要。"""

    freeze_id: str
    plan_id: str
    status: str
    reused: bool
    package_manifest_sha256: str
    plan_sha256: str
    test_count: int
    t_keep: float
    t_exclude: float
    locked_test_access_count: int
    audit_sample_count_per_tail: int
    audit_zero_event_upper: float
    test_status: str
    audit_status: str
    deployment_status: str
    fit_call_count: int
    prediction_call_count: int
    auto_cleaning_decisions_present: bool


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
    """流式计算文件 SHA-256，并隐藏失败路径。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """加载 JSON 映射并统一转换读取与结构异常。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelDeploymentAcceptanceArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelDeploymentAcceptanceArtifactError(reason_code)
    return value


def _resolve_directory(path: str | Path, reason_code: str) -> Path:
    """要求输入包已存在且确为目录。"""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelDeploymentAcceptanceArtifactError(reason_code) from exc
    if not resolved.is_dir():
        raise ModelDeploymentAcceptanceArtifactError(reason_code)
    return resolved


def _validate_inputs(
    plan: ModelDeploymentAcceptancePlan,
    *,
    policy_package: str | Path,
    wave_b_evaluation_package: str | Path,
    wave_b_completed_csv: str | Path,
    qwen_package: str | Path,
    split_anchor_package: str | Path,
) -> Mapping[str, str]:
    """逐层验证 Wave B、模型、切分与私有完成表的冻结身份。"""

    policy_directory = _resolve_directory(
        policy_package, "model_deployment_acceptance_policy_package_invalid"
    )
    wave_b_directory = _resolve_directory(
        wave_b_evaluation_package,
        "model_deployment_acceptance_wave_b_package_invalid",
    )
    qwen_directory = _resolve_directory(
        qwen_package, "model_deployment_acceptance_qwen_package_invalid"
    )
    split_directory = _resolve_directory(
        split_anchor_package, "model_deployment_acceptance_split_package_invalid"
    )
    try:
        completed_path = Path(wave_b_completed_csv).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_completed_csv_invalid"
        ) from exc
    if (
        not completed_path.is_file()
        or _file_sha256(completed_path) != plan.wave_b_completed_csv_sha256
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_completed_csv_invalid"
        )

    policy_manifest_path = policy_directory / "policy-manifest.json"
    if _file_sha256(policy_manifest_path) != plan.policy_manifest_sha256:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_policy_manifest_invalid"
        )
    policy = _load_json(
        policy_manifest_path, "model_deployment_acceptance_policy_manifest_invalid"
    )
    selected = policy.get("selected_strategy")
    if (
        policy.get("policy_id") != plan.policy_id
        or policy.get("status") != "ROUTING_POLICY_FROZEN"
        or policy.get("test_status") != "locked_not_opened"
        or policy.get("deployment_status") != "NOT_AUTHORIZED"
        or policy.get("fit_call_count") != 0
        or policy.get("labels_entered_fit") is not False
        or policy.get("auto_cleaning_decisions_present") is not False
        or policy.get("platform_used") is not False
        or not isinstance(selected, Mapping)
        or selected.get("model_id") != plan.qwen_model_id
        or selected.get("t_keep") != plan.locked_test.t_keep
        or selected.get("t_exclude") != plan.locked_test.t_exclude
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_policy_manifest_invalid"
        )

    wave_b_manifest_path = wave_b_directory / "evaluation-manifest.json"
    if _file_sha256(wave_b_manifest_path) != plan.wave_b_evaluation_manifest_sha256:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_wave_b_manifest_invalid"
        )
    wave_b = _load_json(
        wave_b_manifest_path,
        "model_deployment_acceptance_wave_b_manifest_invalid",
    )
    if (
        wave_b.get("evaluation_id") != plan.wave_b_evaluation_id
        or wave_b.get("policy_id") != plan.policy_id
        or wave_b.get("status") != "WAVE_B_EVALUATION_COMPLETE"
        or wave_b.get("evidence_status")
        != "sufficient_for_descriptive_independent_evaluation"
        or wave_b.get("completed_csv_sha256") != plan.wave_b_completed_csv_sha256
        or wave_b.get("fit_call_count") != 0
        or wave_b.get("prediction_call_count") != 0
        or wave_b.get("labels_entered_fit") is not False
        or wave_b.get("test_status") != "locked_not_opened"
        or wave_b.get("deployment_status") != "NOT_AUTHORIZED"
        or wave_b.get("auto_cleaning_decisions_present") is not False
        or wave_b.get("platform_used") is not False
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_wave_b_manifest_invalid"
        )

    qwen_manifest_path = qwen_directory / "training-manifest.json"
    if _file_sha256(qwen_manifest_path) != plan.qwen_training_manifest_sha256:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_qwen_manifest_invalid"
        )
    qwen = _load_json(
        qwen_manifest_path, "model_deployment_acceptance_qwen_manifest_invalid"
    )
    qwen_lineage = qwen.get("lineage")
    qwen_artifacts = qwen.get("artifacts")
    model_details = (
        qwen_artifacts.get("model") if isinstance(qwen_artifacts, Mapping) else None
    )
    if (
        qwen.get("run_id") != plan.qwen_run_id
        or qwen.get("model_id") != plan.qwen_model_id
        or qwen.get("artifact_status") != "immutable"
        or qwen.get("test_status") != "locked_not_opened"
        or qwen.get("test_probabilities_present") is not False
        or qwen.get("auto_cleaning_decisions_present") is not False
        or qwen.get("platform_used") is not False
        or not isinstance(qwen_lineage, Mapping)
        or qwen_lineage.get("split_manifest_sha256") != plan.split_manifest_sha256
        or qwen_lineage.get("test_manifest_sha256") != plan.test_manifest_sha256
        or not isinstance(model_details, Mapping)
        or model_details.get("filename") != "model.joblib"
        or model_details.get("sha256") != plan.qwen_model_artifact_sha256
        or _file_sha256(qwen_directory / "model.joblib")
        != plan.qwen_model_artifact_sha256
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_qwen_manifest_invalid"
        )

    split_training_path = split_directory / "training-manifest.json"
    split_training = _load_json(
        split_training_path, "model_deployment_acceptance_split_manifest_invalid"
    )
    split_details = split_training.get("artifacts")
    split_file_details = (
        split_details.get("split_manifest")
        if isinstance(split_details, Mapping)
        else None
    )
    split_counts = split_training.get("split_counts")
    if (
        split_training.get("model_id") != plan.split_anchor_model_id
        or split_training.get("test_status") != "locked_not_opened"
        or not isinstance(split_counts, Mapping)
        or split_counts.get("test") != plan.test_count
        or not isinstance(split_file_details, Mapping)
        or split_file_details.get("filename") != "split-manifest.json"
        or split_file_details.get("sha256") != plan.split_artifact_sha256
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_split_manifest_invalid"
        )
    split_path = split_directory / "split-manifest.json"
    if _file_sha256(split_path) != plan.split_artifact_sha256:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_split_manifest_invalid"
        )
    split = _load_json(
        split_path, "model_deployment_acceptance_split_manifest_invalid"
    )
    assignments = split.get("assignments")
    if (
        split.get("test_manifest_sha256") != plan.test_manifest_sha256
        or not isinstance(assignments, list)
        or sum(
            1
            for assignment in assignments
            if isinstance(assignment, Mapping)
            and assignment.get("split_name") == "test"
        )
        != plan.test_count
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_split_manifest_invalid"
        )
    return {
        "policy_manifest_sha256": plan.policy_manifest_sha256,
        "wave_b_evaluation_manifest_sha256": plan.wave_b_evaluation_manifest_sha256,
        "wave_b_completed_csv_sha256": plan.wave_b_completed_csv_sha256,
        "qwen_training_manifest_sha256": plan.qwen_training_manifest_sha256,
        "qwen_model_artifact_sha256": plan.qwen_model_artifact_sha256,
        "split_artifact_sha256": plan.split_artifact_sha256,
        "test_manifest_sha256": plan.test_manifest_sha256,
    }


def _validate_existing_package(
    package: Path,
    *,
    expected_freeze_id: str,
    expected_manifest_sha256: str | None,
) -> tuple[Mapping[str, Any], str]:
    """验证既有包与其全部公开 artifact，禁止内容寻址目录漂移。"""

    manifest_path = package / "acceptance-manifest.json"
    manifest_sha256 = _file_sha256(manifest_path)
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_existing_manifest_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_deployment_acceptance_existing_manifest_invalid"
    )
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("freeze_id") != expected_freeze_id
        or manifest.get("artifact_status") != "immutable"
        or not isinstance(artifacts, Mapping)
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_existing_manifest_invalid"
        )
    expected_filenames = {
        "plan": "acceptance-plan.yaml",
        "report": "freeze-report.json",
    }
    for key, filename in expected_filenames.items():
        details = artifacts.get(key)
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelDeploymentAcceptanceArtifactError(
                "model_deployment_acceptance_existing_artifact_invalid"
            )
    return manifest, manifest_sha256


def _result_from_manifest(
    manifest: Mapping[str, Any], manifest_sha256: str, *, reused: bool
) -> DeploymentAcceptanceFreezeResult:
    """从已校验 manifest 生成不含私有数据的公开结果。"""

    return DeploymentAcceptanceFreezeResult(
        freeze_id=str(manifest["freeze_id"]),
        plan_id=str(manifest["plan_id"]),
        status=str(manifest["status"]),
        reused=reused,
        package_manifest_sha256=manifest_sha256,
        plan_sha256=str(manifest["plan_sha256"]),
        test_count=int(manifest["test_count"]),
        t_keep=float(manifest["t_keep"]),
        t_exclude=float(manifest["t_exclude"]),
        locked_test_access_count=int(manifest["locked_test_access_count"]),
        audit_sample_count_per_tail=int(manifest["audit_sample_count_per_tail"]),
        audit_zero_event_upper=float(manifest["audit_zero_event_upper"]),
        test_status=str(manifest["test_status"]),
        audit_status=str(manifest["audit_status"]),
        deployment_status=str(manifest["deployment_status"]),
        fit_call_count=int(manifest["fit_call_count"]),
        prediction_call_count=int(manifest["prediction_call_count"]),
        auto_cleaning_decisions_present=bool(
            manifest["auto_cleaning_decisions_present"]
        ),
    )


def freeze_deployment_acceptance_package(
    plan_path: str | Path,
    policy_package: str | Path,
    wave_b_evaluation_package: str | Path,
    wave_b_completed_csv: str | Path,
    qwen_package: str | Path,
    split_anchor_package: str | Path,
    artifact_root: str | Path,
    *,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> DeploymentAcceptanceFreezeResult:
    """校验全部既有证据并封存测试判读与双尾审计计划。

    本入口只读取 manifest、模型摘要和 Wave B 完成表摘要；它不读取
    测试标签、不运行预测、不调用 ``fit``，也不生成任何清洗动作。

    Args:
        plan_path: 提交于锁定测试开启前的最终计划 YAML。
        policy_package: 已冻结 Qwen 双阈值策略包。
        wave_b_evaluation_package: 已完成的 Wave B 独立评价包。
        wave_b_completed_csv: 私有 Wave B 完成表，仅核对摘要，不复制。
        qwen_package: 唯一 Qwen 模型不可变训练包。
        split_anchor_package: 保存700条固定切分身份的 baseline 包。
        artifact_root: 新计划包的根目录。
        code_version: 干净工作树对应的40位 Git SHA。
        expected_existing_manifest_sha256: 复用时要求的 manifest 摘要。

    Returns:
        计划包身份、安全状态和审计工作量摘要。

    Raises:
        ModelDeploymentAcceptanceArtifactError: 输入、哈希或不可变性失败。
    """

    if (
        len(code_version) != 40
        or any(character not in "0123456789abcdef" for character in code_version)
    ):
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_code_version_invalid"
        )
    plan_file = Path(plan_path).expanduser()
    try:
        plan_bytes = plan_file.read_bytes()
    except OSError as exc:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_plan_unreadable"
        ) from exc
    plan = load_model_deployment_acceptance_plan(plan_file)
    inputs = _validate_inputs(
        plan,
        policy_package=policy_package,
        wave_b_evaluation_package=wave_b_evaluation_package,
        wave_b_completed_csv=wave_b_completed_csv,
        qwen_package=qwen_package,
        split_anchor_package=split_anchor_package,
    )
    freeze_identity = {
        "artifact_kind": "formal-cleaning-model-deployment-acceptance-freeze",
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "inputs": dict(inputs),
    }
    freeze_id = _sha256_bytes(_canonical_bytes(freeze_identity))[:32]
    root = Path(artifact_root).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_artifact_root_invalid"
        ) from exc
    package = root / freeze_id
    if package.exists():
        manifest, manifest_sha256 = _validate_existing_package(
            package,
            expected_freeze_id=freeze_id,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
        return _result_from_manifest(manifest, manifest_sha256, reused=True)

    report = {
        "artifact_kind": "formal-cleaning-model-deployment-acceptance-freeze-report",
        "freeze_id": freeze_id,
        "plan_id": plan.plan_id,
        "status": "ACCEPTANCE_AND_AUDIT_FROZEN",
        "selected_model": "qwen",
        "selected_model_id": plan.qwen_model_id,
        "t_keep": plan.locked_test.t_keep,
        "t_exclude": plan.locked_test.t_exclude,
        "locked_test": {
            "access_count": plan.locked_test.access_count,
            "test_count": plan.test_count,
            "minimum_auto_keep_support": plan.locked_test.minimum_auto_keep_support,
            "minimum_auto_exclude_support": (
                plan.locked_test.minimum_auto_exclude_support
            ),
            "maximum_auto_keep_unrelated_events": (
                plan.locked_test.maximum_auto_keep_unrelated_events
            ),
            "maximum_auto_exclude_related_events": (
                plan.locked_test.maximum_auto_exclude_related_events
            ),
            "support_failure_status": plan.locked_test.support_failure_status,
            "error_failure_status": plan.locked_test.error_failure_status,
            "pass_status": plan.locked_test.pass_status,
            "diagnostics": list(plan.locked_test.diagnostics),
            "test_members_read": False,
            "test_probabilities_present": False,
        },
        "deployment_audit": {
            "status": "FROZEN_NOT_RUN",
            "sample_count_per_tail": plan.audit.sample_count_per_tail,
            "total_planned_sample_count": plan.audit.sample_count_per_tail * 2,
            "confidence_level": plan.audit.confidence_level,
            "interval_method": plan.audit.interval_method,
            "zero_event_upper_at_full_sample": (
                plan.audit.zero_event_upper_at_full_sample
            ),
            "tails": [asdict(tail) for tail in plan.audit.tails],
            "platform_used": False,
        },
        "guards": {
            "fit_call_count": 0,
            "prediction_call_count": 0,
            "may_retrain": False,
            "may_open_locked_test": True,
            "may_generate_provisional_routing": False,
            "may_generate_formal_auto_decisions": False,
            "deployment_status": "NOT_AUTHORIZED",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
        },
    }
    report_bytes = _canonical_bytes(report)
    manifest = {
        "artifact_kind": "formal-cleaning-model-deployment-acceptance-freeze",
        "artifact_status": "immutable",
        "freeze_id": freeze_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "status": "ACCEPTANCE_AND_AUDIT_FROZEN",
        "lineage": {"code_version": code_version, **dict(inputs)},
        "selected_model": "qwen",
        "selected_model_id": plan.qwen_model_id,
        "t_keep": plan.locked_test.t_keep,
        "t_exclude": plan.locked_test.t_exclude,
        "test_count": plan.test_count,
        "locked_test_access_count": 1,
        "test_status": "locked_not_opened",
        "test_members_read": False,
        "test_probabilities_present": False,
        "audit_sample_count_per_tail": plan.audit.sample_count_per_tail,
        "audit_zero_event_upper": plan.audit.zero_event_upper_at_full_sample,
        "audit_status": "FROZEN_NOT_RUN",
        "deployment_status": "NOT_AUTHORIZED",
        "fit_call_count": 0,
        "prediction_call_count": 0,
        "may_open_locked_test": True,
        "may_generate_provisional_routing": False,
        "may_generate_formal_auto_decisions": False,
        "auto_cleaning_decisions_present": False,
        "platform_used": False,
        "artifacts": {
            "plan": {
                "filename": "acceptance-plan.yaml",
                "sha256": _sha256_bytes(plan_bytes),
            },
            "report": {
                "filename": "freeze-report.json",
                "sha256": _sha256_bytes(report_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    try:
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            staging = Path(temporary) / freeze_id
            staging.mkdir()
            (staging / "acceptance-plan.yaml").write_bytes(plan_bytes)
            (staging / "freeze-report.json").write_bytes(report_bytes)
            (staging / "acceptance-manifest.json").write_bytes(manifest_bytes)
            os.replace(staging, package)
    except OSError as exc:
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_artifact_write_failed"
        ) from exc
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    return _result_from_manifest(manifest, manifest_sha256, reused=False)


def render_deployment_acceptance_freeze_result(
    result: DeploymentAcceptanceFreezeResult, *, output_format: str
) -> str:
    """把封存结果渲染为机器 JSON 或简洁中文摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelDeploymentAcceptanceArtifactError(
            "model_deployment_acceptance_output_format_invalid"
        )
    artifact_word = "复用" if result.reused else "新建并封存"
    return "\n".join(
        [
            "# 锁定测试判读与部署审计计划",
            "",
            f"冻结 ID：{result.freeze_id}",
            f"计划 ID：{result.plan_id}",
            f"artifact：{artifact_word}",
            "",
            "锁定测试",
            (
                f"  n={result.test_count}；"
                f"只允许开启{result.locked_test_access_count}次"
            ),
            (
                f"  Qwen 阈值：T_keep={result.t_keep:.2f} / "
                f"T_exclude={result.t_exclude:.2f}"
            ),
            "  状态：locked_not_opened（测试成员、标签、概率均未读取）",
            "",
            "部署双尾审计",
            f"  auto_keep / auto_exclude 各{result.audit_sample_count_per_tail}条",
            f"  0事件时单侧95%上界：{result.audit_zero_event_upper:.2%}",
            "  任一不利事件暂停对应自动动作；不得补抽样稀释。",
            "",
            "当前未授权正式清洗；未训练、未预测、未生成自动决定。",
        ]
    )
