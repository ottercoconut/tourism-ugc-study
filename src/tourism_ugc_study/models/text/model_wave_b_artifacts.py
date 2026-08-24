"""Wave B 冻结策略与四列人工任务的不可变 artifact。"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .model_reliability_artifacts import (
    _load_scored_members,
    _validate_wave_a_package,
)
from .model_reliability_config import load_model_reliability_plan
from .model_reliability_study import load_eligible_evaluation_population
from .model_routing_policy_config import (
    ModelRoutingPolicyPlan,
    load_model_routing_policy_plan,
)
from .model_wave_b_study import (
    eligible_wave_b_population,
    sample_wave_b,
)


_WAVE_B_ANNOTATION_FILENAME = "wave-b-tourism-relevance-annotation.csv"


class ModelWaveBArtifactError(RuntimeError):
    """策略或 Wave B artifact 输入、谱系与不可变性失败。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、成员身份、概率或私有路径的失败码。"""

        super().__init__("formal model wave b artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RoutingPolicyResult:
    """冻结策略包的去敏结果。"""

    policy_id: str
    status: str
    reused: bool
    selected_model: str
    t_keep: float
    t_exclude: float
    comparator_model: str
    comparator_t_keep: float
    comparator_t_exclude: float
    wave_b_population_count: int
    wave_b_component_count: int
    wave_b_sample_count: int
    package_manifest_sha256: str
    threshold_status: str
    deployment_status: str
    audit_status: str
    test_status: str


@dataclass(frozen=True)
class WaveBResult:
    """Wave B 人工任务包的去敏结果。"""

    wave_id: str
    policy_id: str
    status: str
    reused: bool
    sample_count: int
    eligible_population_count: int
    eligible_component_count: int
    stratum_population_counts: Mapping[str, int]
    stratum_sample_counts: Mapping[str, int]
    task_sha256: str
    private_map_sha256: str
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


def _sha256_bytes(value: bytes) -> str:
    """计算字节流 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算 artifact 文件摘要并统一失败码。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelWaveBArtifactError("model_wave_b_artifact_unreadable") from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层为映射的 JSON 并隐藏路径错误。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelWaveBArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelWaveBArtifactError(reason_code)
    return value


def _validate_code_version(value: str) -> str:
    """要求完整小写 Git SHA。"""

    version = value.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise ModelWaveBArtifactError("model_wave_b_code_version_invalid")
    return version


def _wave_a_components(private_map: Mapping[str, Any]) -> frozenset[str]:
    """从已验证 Wave A 私有映射提取待整体排除的分量。"""

    records = private_map.get("records")
    if not isinstance(records, list) or len(records) != 240:
        raise ModelWaveBArtifactError("model_wave_b_wave_a_map_invalid")
    try:
        components = frozenset(str(record["component_id"]) for record in records)
    except (KeyError, TypeError) as exc:
        raise ModelWaveBArtifactError("model_wave_b_wave_a_map_invalid") from exc
    if len(components) != 227 or any(not item for item in components):
        raise ModelWaveBArtifactError("model_wave_b_wave_a_map_invalid")
    return components


def _validate_selection_package(
    package: Path, plan: ModelRoutingPolicyPlan
) -> None:
    """确认选择报告确实包含研究者指定的两个精确点。"""

    manifest_path = package / "selection-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _load_json(
        manifest_path, "model_wave_b_selection_manifest_invalid"
    )
    artifacts = manifest.get("artifacts")
    if (
        _sha256_bytes(manifest_bytes) != plan.routing_selection_manifest_sha256
        or manifest.get("analysis_id") != plan.routing_selection_id
        or manifest.get("status") != "WAVE_A_SELECTION_READY"
        or manifest.get("fit_call_count") != 0
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
    ):
        raise ModelWaveBArtifactError("model_wave_b_selection_manifest_invalid")
    report_path = package / "routing-selection-report.json"
    report_details = artifacts.get("report")
    if (
        not isinstance(report_details, Mapping)
        or report_details.get("filename") != "routing-selection-report.json"
        or _file_sha256(report_path) != plan.routing_selection_report_sha256
        or report_details.get("sha256") != plan.routing_selection_report_sha256
    ):
        raise ModelWaveBArtifactError("model_wave_b_selection_report_invalid")
    report = _load_json(report_path, "model_wave_b_selection_report_invalid")
    strategies = report.get("strategies")
    if not isinstance(strategies, Mapping):
        raise ModelWaveBArtifactError("model_wave_b_selection_report_invalid")
    expected = {
        "qwen": plan.selected,
        "sparse": plan.comparator,
    }
    for model, strategy in expected.items():
        points = strategies.get(model)
        if not isinstance(points, list):
            raise ModelWaveBArtifactError("model_wave_b_selection_report_invalid")
        matched = [point for point in points if point.get("point_id") == strategy.point_id]
        if len(matched) != 1:
            raise ModelWaveBArtifactError("model_wave_b_selection_point_invalid")
        point = matched[0]
        if (
            point.get("model") != model
            or point.get("t_keep") != strategy.t_keep
            or point.get("t_exclude") != strategy.t_exclude
            or point.get("population_count") != strategy.population_count
            or point.get("auto_keep_count") != strategy.auto_keep_count
            or point.get("manual_review_count") != strategy.manual_review_count
            or point.get("auto_exclude_count") != strategy.auto_exclude_count
            or point.get("is_routing_threshold") is not False
        ):
            raise ModelWaveBArtifactError("model_wave_b_selection_point_invalid")


def _routing_policy_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> RoutingPolicyResult:
    """从已验证 manifest 生成不含私有输入的结果。"""

    selected = manifest["selected_strategy"]
    comparator = manifest["matched_workload_comparator"]
    return RoutingPolicyResult(
        policy_id=str(manifest["policy_id"]),
        status=str(manifest["status"]),
        reused=reused,
        selected_model=str(selected["model"]),
        t_keep=float(selected["t_keep"]),
        t_exclude=float(selected["t_exclude"]),
        comparator_model=str(comparator["model"]),
        comparator_t_keep=float(comparator["t_keep"]),
        comparator_t_exclude=float(comparator["t_exclude"]),
        wave_b_population_count=int(manifest["wave_b_population_count"]),
        wave_b_component_count=int(manifest["wave_b_component_count"]),
        wave_b_sample_count=int(manifest["wave_b_sample_count"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        threshold_status=str(manifest["threshold_status"]),
        deployment_status=str(manifest["deployment_status"]),
        audit_status=str(manifest["audit_status"]),
        test_status=str(manifest["test_status"]),
    )


def _validate_policy_package(
    package: Path,
    *,
    expected_manifest_sha256: str,
    plan: ModelRoutingPolicyPlan,
) -> RoutingPolicyResult:
    """完整验证既有策略包及其两个文件。"""

    manifest_path = package / "policy-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _load_json(manifest_path, "model_wave_b_policy_manifest_invalid")
    artifacts = manifest.get("artifacts")
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or manifest.get("artifact_kind") != "formal-cleaning-model-routing-policy"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("policy_id") != plan.policy_id
        or manifest.get("policy_sha256") != plan.policy_sha256
        or manifest.get("status") != "ROUTING_POLICY_FROZEN"
        or manifest.get("fit_call_count") != 0
        or manifest.get("labels_entered_fit") is not False
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status")
        != "FROZEN_FOR_WAVE_B_EVALUATION"
        or manifest.get("deployment_status") != "NOT_AUTHORIZED"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
        or set(artifacts) != {"policy", "summary"}
    ):
        raise ModelWaveBArtifactError("model_wave_b_policy_manifest_invalid")
    for key, filename in (
        ("policy", "policy.yaml"),
        ("summary", "policy-summary.json"),
    ):
        details = artifacts[key]
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelWaveBArtifactError("model_wave_b_policy_artifact_invalid")
    return _routing_policy_result(manifest, manifest_bytes, reused=True)


def freeze_routing_policy_package(
    scored_package: str | Path,
    wave_a_package: str | Path,
    selection_package: str | Path,
    reliability_plan_path: str | Path,
    policy_plan_path: str | Path,
    artifact_root: str | Path,
    *,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> RoutingPolicyResult:
    """校验证据与容量后封存只供 Wave B 评价的路由策略。

    本入口不读取 Wave B 标签、不运行模型、不调用 ``fit``，也不把策略
    标记为可部署。配置中已冻结的阈值只能被验证，不能由程序选择。
    """

    version = _validate_code_version(code_version)
    reliability_plan = load_model_reliability_plan(reliability_plan_path)
    policy_plan = load_model_routing_policy_plan(policy_plan_path)
    scored_directory = Path(scored_package).expanduser().resolve(strict=True)
    wave_a_directory = Path(wave_a_package).expanduser().resolve(strict=True)
    selection_directory = Path(selection_package).expanduser().resolve(strict=True)
    scored = _load_scored_members(
        scored_directory,
        expected_manifest_sha256=policy_plan.scored_manifest_sha256,
        plan=reliability_plan,
    )
    _wave_manifest, private_map = _validate_wave_a_package(
        wave_a_directory,
        expected_manifest_sha256=policy_plan.wave_a_manifest_sha256,
        plan=reliability_plan,
    )
    _validate_selection_package(selection_directory, policy_plan)
    components = _wave_a_components(private_map)
    eligible_wave_b_population(
        scored,
        excluded_wave_a_components=components,
        plan=policy_plan,
    )
    root = Path(artifact_root).expanduser().resolve()
    package = root / policy_plan.policy_id
    if package.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelWaveBArtifactError(
                "model_wave_b_existing_policy_requires_manifest_hash"
            )
        return _validate_policy_package(
            package,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            plan=policy_plan,
        )
    summary = {
        "artifact_kind": "formal-cleaning-model-routing-policy-summary",
        "policy_id": policy_plan.policy_id,
        "role": "wave_b_independent_evaluation_only",
        "selected_strategy": asdict(policy_plan.selected),
        "matched_workload_comparator": asdict(policy_plan.comparator),
        "wave_b_population_count": policy_plan.wave_b_eligible_population_count,
        "wave_b_component_count": policy_plan.wave_b_eligible_component_count,
        "wave_b_sample_count": policy_plan.wave_b_target_sample_count,
        "strata": [asdict(item) for item in policy_plan.wave_b_strata],
        "threshold_status": "FROZEN_FOR_WAVE_B_EVALUATION",
        "deployment_status": "NOT_AUTHORIZED",
        "audit_status": "UNSET",
        "test_status": "locked_not_opened",
    }
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{policy_plan.policy_id}.", dir=root)
    )
    try:
        shutil.copyfile(policy_plan_path, temporary / "policy.yaml")
        (temporary / "policy-summary.json").write_bytes(_canonical_bytes(summary))
        artifacts = {
            "policy": {
                "filename": "policy.yaml",
                "sha256": _file_sha256(temporary / "policy.yaml"),
            },
            "summary": {
                "filename": "policy-summary.json",
                "sha256": _file_sha256(temporary / "policy-summary.json"),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-model-routing-policy",
            "artifact_status": "immutable",
            "policy_id": policy_plan.policy_id,
            "policy_sha256": policy_plan.policy_sha256,
            "status": "ROUTING_POLICY_FROZEN",
            "selected_strategy": {
                "model": policy_plan.selected.model,
                "model_id": policy_plan.selected.model_id,
                "point_id": policy_plan.selected.point_id,
                "t_keep": policy_plan.selected.t_keep,
                "t_exclude": policy_plan.selected.t_exclude,
            },
            "matched_workload_comparator": {
                "model": policy_plan.comparator.model,
                "model_id": policy_plan.comparator.model_id,
                "point_id": policy_plan.comparator.point_id,
                "t_keep": policy_plan.comparator.t_keep,
                "t_exclude": policy_plan.comparator.t_exclude,
            },
            "scored_frame_id": policy_plan.scored_frame_id,
            "scored_manifest_sha256": policy_plan.scored_manifest_sha256,
            "wave_a_id": policy_plan.wave_a_id,
            "wave_a_manifest_sha256": policy_plan.wave_a_manifest_sha256,
            "routing_selection_id": policy_plan.routing_selection_id,
            "routing_selection_manifest_sha256": (
                policy_plan.routing_selection_manifest_sha256
            ),
            "routing_selection_report_sha256": (
                policy_plan.routing_selection_report_sha256
            ),
            "wave_b_population_count": policy_plan.wave_b_eligible_population_count,
            "wave_b_component_count": policy_plan.wave_b_eligible_component_count,
            "wave_b_sample_count": policy_plan.wave_b_target_sample_count,
            "wave_a_excluded_component_count": len(components),
            "fit_call_count": 0,
            "labels_entered_fit": False,
            "may_open_locked_test": False,
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
        (temporary / "policy-manifest.json").write_bytes(manifest_bytes)
        temporary.rename(package)
        temporary = None
        return _routing_policy_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _wave_b_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> WaveBResult:
    """从 Wave B manifest 生成去敏结果。"""

    return WaveBResult(
        wave_id=str(manifest["wave_id"]),
        policy_id=str(manifest["policy_id"]),
        status=str(manifest["status"]),
        reused=reused,
        sample_count=int(manifest["sample_count"]),
        eligible_population_count=int(manifest["eligible_population_count"]),
        eligible_component_count=int(manifest["eligible_component_count"]),
        stratum_population_counts=dict(manifest["stratum_population_counts"]),
        stratum_sample_counts=dict(manifest["stratum_sample_counts"]),
        task_sha256=str(manifest["artifacts"]["review_task"]["sha256"]),
        private_map_sha256=str(manifest["artifacts"]["private_map"]["sha256"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        labels_entered_fit=bool(manifest["labels_entered_fit"]),
        threshold_status=str(manifest["threshold_status"]),
        deployment_status=str(manifest["deployment_status"]),
        audit_status=str(manifest["audit_status"]),
        test_status=str(manifest["test_status"]),
    )


def _validate_wave_b_package(
    package: Path,
    *,
    expected_manifest_sha256: str,
    plan: ModelRoutingPolicyPlan,
) -> WaveBResult:
    """验证既有 Wave B 包的不可变文件和安全状态。"""

    path = package / "wave-b-manifest.json"
    manifest_bytes = path.read_bytes()
    manifest = _load_json(path, "model_wave_b_manifest_invalid")
    artifacts = manifest.get("artifacts")
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or manifest.get("artifact_kind") != "formal-cleaning-model-wave-b"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("policy_id") != plan.policy_id
        or manifest.get("status") != "WAVE_B_LABELING"
        or manifest.get("sample_count") != plan.wave_b_target_sample_count
        or manifest.get("labels_entered_fit") is not False
        or manifest.get("fit_call_count") != 0
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status")
        != "FROZEN_FOR_WAVE_B_EVALUATION"
        or manifest.get("deployment_status") != "NOT_AUTHORIZED"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
    ):
        raise ModelWaveBArtifactError("model_wave_b_manifest_invalid")
    expected = {
        "review_task": _WAVE_B_ANNOTATION_FILENAME,
        "private_map": "private-map.json",
        "policy_manifest": "policy-manifest.json",
    }
    if set(artifacts) != set(expected):
        raise ModelWaveBArtifactError("model_wave_b_manifest_invalid")
    for key, filename in expected.items():
        details = artifacts[key]
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelWaveBArtifactError("model_wave_b_artifact_hash_mismatch")
    return _wave_b_result(manifest, manifest_bytes, reused=True)


def prepare_wave_b_package(
    reference_csv: str | Path,
    derived_db: str | Path,
    scored_package: str | Path,
    wave_a_package: str | Path,
    policy_package: str | Path,
    reliability_plan_path: str | Path,
    policy_plan_path: str | Path,
    artifact_root: str | Path,
    *,
    normalization_config: TextCleaningConfig,
    code_version: str,
    expected_policy_manifest_sha256: str,
    expected_existing_manifest_sha256: str | None = None,
) -> WaveBResult:
    """生成隐藏模型答案、只含四列的 Wave B 360条人工任务。"""

    version = _validate_code_version(code_version)
    reliability_plan = load_model_reliability_plan(reliability_plan_path)
    policy_plan = load_model_routing_policy_plan(policy_plan_path)
    scored_directory = Path(scored_package).expanduser().resolve(strict=True)
    wave_a_directory = Path(wave_a_package).expanduser().resolve(strict=True)
    policy_directory = Path(policy_package).expanduser().resolve(strict=True)
    _validate_policy_package(
        policy_directory,
        expected_manifest_sha256=expected_policy_manifest_sha256,
        plan=policy_plan,
    )
    scored = _load_scored_members(
        scored_directory,
        expected_manifest_sha256=policy_plan.scored_manifest_sha256,
        plan=reliability_plan,
    )
    _wave_manifest, wave_a_map = _validate_wave_a_package(
        wave_a_directory,
        expected_manifest_sha256=policy_plan.wave_a_manifest_sha256,
        plan=reliability_plan,
    )
    components = _wave_a_components(wave_a_map)
    sample = sample_wave_b(
        scored,
        excluded_wave_a_components=components,
        plan=policy_plan,
    )
    population = load_eligible_evaluation_population(
        reference_csv,
        derived_db,
        plan=reliability_plan,
        normalization_config=normalization_config,
    )
    text_by_identity = {
        item.identity: (
            item.normalized_model_text,
            item.normalized_sha256,
            item.platform_key,
        )
        for item in population.members
    }
    policy_manifest_sha256 = _file_sha256(
        policy_directory / "policy-manifest.json"
    )
    wave_id = _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": "wave-b-cross-action-stratified-srs-v1",
                "annotation_filename": _WAVE_B_ANNOTATION_FILENAME,
                "policy_id": policy_plan.policy_id,
                "policy_manifest_sha256": policy_manifest_sha256,
                "sample": [asdict(item) for item in sample],
            }
        )
    )[:32]
    root = Path(artifact_root).expanduser().resolve()
    package = root / wave_id
    if package.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelWaveBArtifactError(
                "model_wave_b_existing_package_requires_manifest_hash"
            )
        return _validate_wave_b_package(
            package,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            plan=policy_plan,
        )
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{wave_id}.", dir=root)
    )
    try:
        task_path = temporary / _WAVE_B_ANNOTATION_FILENAME
        with task_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "task_id",
                    "sample_run_id",
                    "normalized_model_text",
                    "tourism_label",
                ),
            )
            writer.writeheader()
            for item in sample:
                try:
                    text, normalized_sha256, _platform = text_by_identity[
                        (item.source_post_id, item.source_version)
                    ]
                except KeyError as exc:
                    raise ModelWaveBArtifactError(
                        "model_wave_b_text_binding_mismatch"
                    ) from exc
                if normalized_sha256 != item.normalized_sha256:
                    raise ModelWaveBArtifactError(
                        "model_wave_b_text_binding_mismatch"
                    )
                writer.writerow(
                    {
                        "task_id": item.task_id,
                        "sample_run_id": wave_id,
                        "normalized_model_text": text,
                        "tourism_label": "",
                    }
                )
        private_records = []
        for item in sample:
            _text, _normalized_sha256, platform = text_by_identity[
                (item.source_post_id, item.source_version)
            ]
            private_records.append({**asdict(item), "platform_key": platform})
        (temporary / "private-map.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-model-wave-b-private-map",
                    "wave_id": wave_id,
                    "policy_id": policy_plan.policy_id,
                    "records": private_records,
                    "hidden_from_annotation": [
                        "source_post_id",
                        "source_version",
                        "platform_key",
                        "model_name",
                        "model_probability",
                        "selected_action",
                        "comparator_action",
                        "sampling_stratum",
                        "selection_reason",
                        "inclusion_probability",
                        "analysis_weight",
                    ],
                    "labels_entered_fit": False,
                    "platform_used": False,
                }
            )
        )
        shutil.copyfile(
            policy_directory / "policy-manifest.json",
            temporary / "policy-manifest.json",
        )
        artifacts = {
            "review_task": {
                "filename": _WAVE_B_ANNOTATION_FILENAME,
                "sha256": _file_sha256(task_path),
            },
            "private_map": {
                "filename": "private-map.json",
                "sha256": _file_sha256(temporary / "private-map.json"),
            },
            "policy_manifest": {
                "filename": "policy-manifest.json",
                "sha256": _file_sha256(temporary / "policy-manifest.json"),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-model-wave-b",
            "artifact_status": "immutable",
            "wave_id": wave_id,
            "policy_id": policy_plan.policy_id,
            "policy_manifest_sha256": policy_manifest_sha256,
            "status": "WAVE_B_LABELING",
            "sample_count": len(sample),
            "eligible_population_count": policy_plan.wave_b_eligible_population_count,
            "eligible_component_count": policy_plan.wave_b_eligible_component_count,
            "stratum_population_counts": {
                item.name: item.population_count for item in policy_plan.wave_b_strata
            },
            "stratum_sample_counts": {
                item.name: item.sample_count for item in policy_plan.wave_b_strata
            },
            "sampling_unit": "post",
            "leakage_component_role": "wave_a_exclusion_and_cluster_variance",
            "model_probabilities_hidden": True,
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
        (temporary / "wave-b-manifest.json").write_bytes(manifest_bytes)
        temporary.rename(package)
        temporary = None
        return _wave_b_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def render_wave_b_result(
    result: RoutingPolicyResult | WaveBResult, *, output_format: str = "human"
) -> str:
    """把策略或 Wave B 结果渲染为 JSON 或可读中文。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if isinstance(result, RoutingPolicyResult):
        return "\n".join(
            [
                "# Wave B 路由策略冻结结果",
                "",
                f"策略 ID：{result.policy_id}",
                f"artifact：{'复用' if result.reused else '新建并封存'}",
                f"selected：Qwen；T_keep={result.t_keep:.2f}；T_exclude={result.t_exclude:.2f}",
                (
                    "等人工量 comparator：sparse；"
                    f"T_keep={result.comparator_t_keep:.2f}；"
                    f"T_exclude={result.comparator_t_exclude:.2f}"
                ),
                (
                    f"Wave B 人口：{result.wave_b_population_count}条 / "
                    f"{result.wave_b_component_count}分量；计划抽样"
                    f"{result.wave_b_sample_count}条"
                ),
                "状态：只供Wave B独立评价；部署未授权。",
                "锁定测试：locked_not_opened；审计策略：UNSET；fit调用：0。",
            ]
        )
    return "\n".join(
        [
            "# Wave B 人工标注任务生成结果",
            "",
            f"Wave ID：{result.wave_id}",
            f"策略 ID：{result.policy_id}",
            f"artifact：{'复用' if result.reused else '新建并封存'}",
            (
                f"任务：{result.sample_count}条；评价人口"
                f"{result.eligible_population_count}条 / "
                f"{result.eligible_component_count}分量"
            ),
            "公开表：wave-b-tourism-relevance-annotation.csv；人工只填写tourism_label。",
            "模型、概率、动作、分层、来源与设计权重均只在私有映射。",
            "fit调用：0；预测调用：0；锁定测试：locked_not_opened。",
            "阈值：FROZEN_FOR_WAVE_B_EVALUATION；部署：NOT_AUTHORIZED；审计：UNSET。",
        ]
    )
