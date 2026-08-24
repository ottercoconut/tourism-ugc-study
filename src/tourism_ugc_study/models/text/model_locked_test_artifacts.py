"""唯一锁定测试的证据加载、纯预测、判读和不可变封存。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np

from tourism_ugc_study.cleaning.config import StableCleaningConfig
from tourism_ugc_study.cleaning.reference_evidence import (
    REFERENCE_FIELDS,
    validate_reference_evidence,
)
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .model_deployment_acceptance_config import (
    ModelDeploymentAcceptancePlan,
    load_model_deployment_acceptance_plan,
)
from .model_locked_test import (
    LockedTestObservation,
    evaluate_locked_test,
    routing_action,
)
from .qwen_embedding_config import load_qwen_embedding_plan
from .qwen_embedding_runtime import LocalQwenHeadTailEncoder
from .qwen_head_challenger import FrozenQwenHeadModel
from .qwen_head_tail_config import load_qwen_head_tail_plan
from .sparse_challenger_artifacts import load_verified_baseline_split_bindings
from .sparse_challenger_config import load_sparse_challenger_plan


class ModelLockedTestArtifactError(RuntimeError):
    """锁定测试输入、运行或不可变性失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露测试正文、成员、标签、概率或路径的失败码。"""

        super().__init__("formal locked test artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class LockedTestDocument:
    """一次开启后才允许物化的去标识测试文本与人工标签。"""

    member_key: str
    component_id: str
    normalized_model_text: str
    tourism_label: str


@dataclass(frozen=True)
class LockedTestPackageResult:
    """锁定测试不可变运行包的公开去敏摘要。"""

    test_run_id: str
    status: str
    reused: bool
    package_manifest_sha256: str
    report_sha256: str
    probability_artifact_sha256: str
    test_count: int
    related_count: int
    unrelated_count: int
    auto_keep_count: int
    manual_review_count: int
    auto_exclude_count: int
    auto_keep_unrelated_events: int
    auto_exclude_related_events: int
    automatic_coverage_rate: float
    overall_metrics: Mapping[str, Any]
    test_status: str
    test_access_count: int
    audit_status: str
    deployment_status: str
    may_generate_provisional_routing: bool
    may_generate_formal_auto_decisions: bool
    fit_call_count: int
    prediction_call_count: int


def _canonical_bytes(value: object) -> bytes:
    """生成排序、紧凑、拒绝 NaN 且以换行结束的规范 JSON。"""

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
    """流式计算文件 SHA-256 并统一隐藏读取路径。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """加载顶层为映射的 JSON，并转换为稳定失败码。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelLockedTestArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelLockedTestArtifactError(reason_code)
    return value


def _validate_acceptance_package(
    package: str | Path,
    *,
    plan: ModelDeploymentAcceptancePlan,
    expected_manifest_sha256: str,
) -> Mapping[str, Any]:
    """要求最终计划已封存且仍未读取测试成员。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_acceptance_package_invalid"
        ) from exc
    manifest_path = directory / "acceptance-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelLockedTestArtifactError(
            "model_locked_test_acceptance_manifest_invalid"
        )
    manifest = _load_json(
        manifest_path, "model_locked_test_acceptance_manifest_invalid"
    )
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("status") != "ACCEPTANCE_AND_AUDIT_FROZEN"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("plan_sha256") != plan.plan_sha256
        or manifest.get("selected_model_id") != plan.qwen_model_id
        or manifest.get("t_keep") != plan.locked_test.t_keep
        or manifest.get("t_exclude") != plan.locked_test.t_exclude
        or manifest.get("test_count") != plan.test_count
        or manifest.get("locked_test_access_count") != 1
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_members_read") is not False
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("may_open_locked_test") is not True
        or manifest.get("audit_status") != "FROZEN_NOT_RUN"
        or manifest.get("deployment_status") != "NOT_AUTHORIZED"
        or manifest.get("fit_call_count") != 0
        or manifest.get("prediction_call_count") != 0
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
    ):
        raise ModelLockedTestArtifactError(
            "model_locked_test_acceptance_manifest_invalid"
        )
    for key, filename in {
        "plan": "acceptance-plan.yaml",
        "report": "freeze-report.json",
    }.items():
        details = artifacts.get(key)
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelLockedTestArtifactError(
                "model_locked_test_acceptance_artifact_invalid"
            )
    return manifest


def _load_locked_test_evidence(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    split_anchor_package: str | Path,
    sparse_plan_path: str | Path,
    *,
    plan: ModelDeploymentAcceptancePlan,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
) -> tuple[LockedTestDocument, ...]:
    """在正式开启点校验700条证据，并只物化148条测试记录。"""

    sparse_plan = load_sparse_challenger_plan(sparse_plan_path)
    if (
        sparse_plan.baseline_model_id != plan.split_anchor_model_id
        or sparse_plan.reference_csv_sha256
        != "3eb3ea233c05bf76b5bdda548701895c9551726d74b6f9742523e695aeac73fd"
        or sparse_plan.test_manifest_sha256 != plan.test_manifest_sha256
    ):
        raise ModelLockedTestArtifactError(
            "model_locked_test_split_plan_invalid"
        )
    try:
        split_directory = Path(split_anchor_package).expanduser().resolve(
            strict=True
        )
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_split_package_invalid"
        ) from exc
    baseline_manifest, split, assignment_bindings = (
        load_verified_baseline_split_bindings(split_directory, sparse_plan)
    )
    if (
        _file_sha256(split_directory / "split-manifest.json")
        != plan.split_artifact_sha256
        or split.get("manifest_sha256") != plan.split_manifest_sha256
        or split.get("test_manifest_sha256") != plan.test_manifest_sha256
        or baseline_manifest.get("model_id") != plan.split_anchor_model_id
    ):
        raise ModelLockedTestArtifactError(
            "model_locked_test_split_package_invalid"
        )
    test_components = {
        identity: component_id
        for identity, (component_id, split_name) in assignment_bindings.items()
        if split_name == "test"
    }
    if len(test_components) != plan.test_count:
        raise ModelLockedTestArtifactError("model_locked_test_count_invalid")
    reference = validate_reference_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        expected_label_guide_version=config.label_guide_version,
        expected_normalization_rule_id=str(
            config.artifacts["normalization_version_lock"]
        ),
        normalization_config=normalization_config,
    )
    if (
        reference.csv_sha256 != sparse_plan.reference_csv_sha256
        or reference.manifest_sha256
        != baseline_manifest["lineage"].get("reference_manifest_sha256")
    ):
        raise ModelLockedTestArtifactError(
            "model_locked_test_reference_binding_invalid"
        )
    try:
        verified_csv = Path(csv_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_reference_invalid"
        ) from exc
    documents: list[LockedTestDocument] = []
    seen: set[tuple[int, int]] = set()
    try:
        with verified_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise ModelLockedTestArtifactError(
                    "model_locked_test_reference_fields_invalid"
                )
            for row in reader:
                identity = (
                    int(row["source_post_id"].strip()),
                    int(row["source_version"].strip()),
                )
                component_id = test_components.get(identity)
                if component_id is None:
                    continue
                if identity in seen:
                    raise ModelLockedTestArtifactError(
                        "model_locked_test_identity_duplicate"
                    )
                seen.add(identity)
                label = row["tourism_label"].strip()
                text = row["normalized_model_text"]
                if label not in {"related", "unrelated"} or not text.strip():
                    raise ModelLockedTestArtifactError(
                        "model_locked_test_record_invalid"
                    )
                member_payload = (
                    f"{plan.plan_sha256}:{identity[0]}:{identity[1]}"
                ).encode("utf-8")
                documents.append(
                    LockedTestDocument(
                        member_key=hashlib.sha256(member_payload).hexdigest(),
                        component_id=component_id,
                        normalized_model_text=text,
                        tourism_label=label,
                    )
                )
    except (OSError, UnicodeError, csv.Error, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ModelLockedTestArtifactError):
            raise
        raise ModelLockedTestArtifactError(
            "model_locked_test_reference_invalid"
        ) from exc
    if seen != set(test_components) or len(documents) != plan.test_count:
        raise ModelLockedTestArtifactError("model_locked_test_members_incomplete")
    return tuple(sorted(documents, key=lambda item: item.member_key))


def _predict_qwen_locked_test(
    documents: Sequence[LockedTestDocument],
    *,
    qwen_package: str | Path,
    qwen_base_plan_path: str | Path,
    projection_plan_path: str | Path,
    model_dir: str | Path,
    plan: ModelDeploymentAcceptancePlan,
    show_progress: bool,
) -> tuple[np.ndarray, Mapping[str, Any], Mapping[str, Any]]:
    """校验唯一模型与公开权重后执行一次纯编码和纯预测。"""

    try:
        package = Path(qwen_package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_qwen_package_invalid"
        ) from exc
    manifest_path = package / "training-manifest.json"
    if _file_sha256(manifest_path) != plan.qwen_training_manifest_sha256:
        raise ModelLockedTestArtifactError(
            "model_locked_test_qwen_manifest_invalid"
        )
    manifest = _load_json(manifest_path, "model_locked_test_qwen_manifest_invalid")
    model_path = package / "model.joblib"
    if (
        manifest.get("run_id") != plan.qwen_run_id
        or manifest.get("model_id") != plan.qwen_model_id
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("auto_cleaning_decisions_present") is not False
        or _file_sha256(model_path) != plan.qwen_model_artifact_sha256
    ):
        raise ModelLockedTestArtifactError(
            "model_locked_test_qwen_manifest_invalid"
        )
    try:
        model = joblib.load(model_path)
    except Exception as exc:  # 已验证私有 artifact 的反序列化异常统一去敏。
        raise ModelLockedTestArtifactError(
            "model_locked_test_qwen_model_invalid"
        ) from exc
    if not isinstance(model, FrozenQwenHeadModel):
        raise ModelLockedTestArtifactError("model_locked_test_qwen_model_invalid")
    base_plan = load_qwen_embedding_plan(qwen_base_plan_path)
    projection_plan = load_qwen_head_tail_plan(projection_plan_path)
    if (
        base_plan.plan_sha256 != projection_plan.qwen_base_plan_sha256
        or projection_plan.test_manifest_sha256 != plan.test_manifest_sha256
    ):
        raise ModelLockedTestArtifactError("model_locked_test_qwen_plan_invalid")
    encoder = LocalQwenHeadTailEncoder(
        model_dir,
        base_plan=base_plan,
        projection_plan=projection_plan,
    )
    encoded = encoder.encode_head_tail_with_diagnostics(
        [item.normalized_model_text for item in documents],
        show_progress=show_progress,
    )
    probabilities = np.asarray(
        model.predict_p_unrelated(encoded.embeddings), dtype=float
    )
    if probabilities.shape != (len(documents),):
        raise ModelLockedTestArtifactError(
            "model_locked_test_probability_shape_invalid"
        )
    return probabilities, asdict(encoded.diagnostics), asdict(
        encoder.execution_receipt
    )


def _test_run_id(
    plan: ModelDeploymentAcceptancePlan,
    *,
    acceptance_manifest_sha256: str,
    code_version: str,
) -> str:
    """在读取测试成员前生成唯一、可预判的正式运行身份。"""

    payload = {
        "artifact_kind": "formal-cleaning-model-locked-test",
        "plan_sha256": plan.plan_sha256,
        "acceptance_manifest_sha256": acceptance_manifest_sha256,
        "qwen_training_manifest_sha256": plan.qwen_training_manifest_sha256,
        "qwen_model_artifact_sha256": plan.qwen_model_artifact_sha256,
        "test_manifest_sha256": plan.test_manifest_sha256,
        "code_version": code_version,
    }
    return _sha256_bytes(_canonical_bytes(payload))[:32]


def _validate_existing_package(
    package: Path,
    *,
    expected_run_id: str,
    expected_manifest_sha256: str,
) -> tuple[Mapping[str, Any], str]:
    """严格验证并复用一次已开启的锁定测试，不再读取测试证据。"""

    manifest_path = package / "test-manifest.json"
    manifest_sha256 = _file_sha256(manifest_path)
    if manifest_sha256 != expected_manifest_sha256:
        raise ModelLockedTestArtifactError(
            "model_locked_test_existing_manifest_mismatch"
        )
    manifest = _load_json(manifest_path, "model_locked_test_existing_manifest_invalid")
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("test_run_id") != expected_run_id
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("test_status") != "opened_once"
        or manifest.get("test_access_count") != 1
        or not isinstance(artifacts, Mapping)
    ):
        raise ModelLockedTestArtifactError(
            "model_locked_test_existing_manifest_invalid"
        )
    for key, filename in {
        "report": "test-report.json",
        "probabilities": "test-probabilities.json",
    }.items():
        details = artifacts.get(key)
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelLockedTestArtifactError(
                "model_locked_test_existing_artifact_invalid"
            )
    return manifest, manifest_sha256


def _find_existing_plan_run(
    root: Path, *, plan_id: str
) -> tuple[Path, Mapping[str, Any]] | None:
    """跨代码版本查找同一计划已经消耗的唯一测试访问。"""

    matches: list[tuple[Path, Mapping[str, Any]]] = []
    try:
        candidates = tuple(root.iterdir())
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_artifact_root_invalid"
        ) from exc
    for candidate in candidates:
        manifest_path = candidate / "test-manifest.json"
        if not candidate.is_dir() or not manifest_path.is_file():
            continue
        manifest = _load_json(
            manifest_path, "model_locked_test_existing_manifest_invalid"
        )
        if manifest.get("plan_id") == plan_id:
            matches.append((candidate, manifest))
    if len(matches) > 1:
        raise ModelLockedTestArtifactError(
            "model_locked_test_multiple_plan_runs_detected"
        )
    return matches[0] if matches else None


def _result_from_manifest(
    manifest: Mapping[str, Any], manifest_sha256: str, *, reused: bool
) -> LockedTestPackageResult:
    """从已验证 manifest 构造不含成员级信息的摘要。"""

    routing = manifest["routing"]
    artifacts = manifest["artifacts"]
    return LockedTestPackageResult(
        test_run_id=str(manifest["test_run_id"]),
        status=str(manifest["status"]),
        reused=reused,
        package_manifest_sha256=manifest_sha256,
        report_sha256=str(artifacts["report"]["sha256"]),
        probability_artifact_sha256=str(artifacts["probabilities"]["sha256"]),
        test_count=int(manifest["test_count"]),
        related_count=int(manifest["related_count"]),
        unrelated_count=int(manifest["unrelated_count"]),
        auto_keep_count=int(routing["auto_keep_count"]),
        manual_review_count=int(routing["manual_review_count"]),
        auto_exclude_count=int(routing["auto_exclude_count"]),
        auto_keep_unrelated_events=int(routing["auto_keep_unrelated_events"]),
        auto_exclude_related_events=int(routing["auto_exclude_related_events"]),
        automatic_coverage_rate=float(routing["automatic_coverage_rate"]),
        overall_metrics=dict(manifest["overall_metrics"]),
        test_status=str(manifest["test_status"]),
        test_access_count=int(manifest["test_access_count"]),
        audit_status=str(manifest["audit_status"]),
        deployment_status=str(manifest["deployment_status"]),
        may_generate_provisional_routing=bool(
            manifest["may_generate_provisional_routing"]
        ),
        may_generate_formal_auto_decisions=bool(
            manifest["may_generate_formal_auto_decisions"]
        ),
        fit_call_count=int(manifest["fit_call_count"]),
        prediction_call_count=int(manifest["prediction_call_count"]),
    )


def run_locked_test_package(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    split_anchor_package: str | Path,
    sparse_plan_path: str | Path,
    qwen_package: str | Path,
    qwen_base_plan_path: str | Path,
    projection_plan_path: str | Path,
    model_dir: str | Path,
    acceptance_plan_path: str | Path,
    acceptance_package: str | Path,
    artifact_root: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    code_version: str,
    expected_acceptance_manifest_sha256: str,
    expected_existing_manifest_sha256: str | None = None,
    show_progress: bool = True,
) -> LockedTestPackageResult:
    """开启唯一测试一次，执行纯预测并封存不可变判读包。

    Args:
        csv_path: 唯一最终700条参考 CSV。
        reference_manifest_path: 与参考 CSV 配对的 finalized manifest。
        derived_db: 只读派生 SQLite，仅用于证据完整性校验。
        split_anchor_package: 固定442/110/148切分的 baseline 包。
        sparse_plan_path: 用于复核切分身份的冻结 challenger 计划。
        qwen_package: 唯一 Qwen head-tail 模型包。
        qwen_base_plan_path: Qwen 公开权重与执行计划。
        projection_plan_path: 英文 instruction 与 head-tail 投影计划。
        model_dir: 经哈希验证的本地公开 Qwen 权重目录。
        acceptance_plan_path: 测试前提交的最终判读与审计计划。
        acceptance_package: 已封存且授权单次测试的计划包。
        artifact_root: 锁定测试不可变运行包根目录。
        config: 稳定清洗配置。
        normalization_config: 冻结文本规范化配置。
        code_version: 干净工作树的40位 Git SHA。
        expected_acceptance_manifest_sha256: 正式计划 manifest 摘要。
        expected_existing_manifest_sha256: 严格复用时的测试 manifest 摘要。
        show_progress: 是否显示本地 Qwen 编码进度。

    Returns:
        聚合测试指标、三段动作、判读状态和后续授权边界。

    Raises:
        ModelLockedTestArtifactError: 任一输入、运行或不变性检查失败。
    """

    version = code_version.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise ModelLockedTestArtifactError("model_locked_test_code_version_invalid")
    plan = load_model_deployment_acceptance_plan(acceptance_plan_path)
    _validate_acceptance_package(
        acceptance_package,
        plan=plan,
        expected_manifest_sha256=expected_acceptance_manifest_sha256,
    )
    test_run_id = _test_run_id(
        plan,
        acceptance_manifest_sha256=expected_acceptance_manifest_sha256,
        code_version=version,
    )
    root = Path(artifact_root).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_artifact_root_invalid"
        ) from exc
    existing = _find_existing_plan_run(root, plan_id=plan.plan_id)
    if existing is not None:
        if expected_existing_manifest_sha256 is None:
            raise ModelLockedTestArtifactError(
                "model_locked_test_existing_package_requires_manifest_hash"
            )
        package, existing_manifest = existing
        manifest, manifest_sha256 = _validate_existing_package(
            package,
            expected_run_id=str(existing_manifest.get("test_run_id", "")),
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
        return _result_from_manifest(manifest, manifest_sha256, reused=True)
    package = root / test_run_id

    documents = _load_locked_test_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        split_anchor_package,
        sparse_plan_path,
        plan=plan,
        config=config,
        normalization_config=normalization_config,
    )
    probabilities, encoding_diagnostics, execution = _predict_qwen_locked_test(
        documents,
        qwen_package=qwen_package,
        qwen_base_plan_path=qwen_base_plan_path,
        projection_plan_path=projection_plan_path,
        model_dir=model_dir,
        plan=plan,
        show_progress=show_progress,
    )
    observations = tuple(
        LockedTestObservation(
            member_key=document.member_key,
            component_id=document.component_id,
            tourism_label=document.tourism_label,
            p_unrelated=float(probability),
        )
        for document, probability in zip(documents, probabilities, strict=True)
    )
    report = dict(evaluate_locked_test(observations, plan=plan))
    report.update(
        {
            "test_run_id": test_run_id,
            "plan_id": plan.plan_id,
            "acceptance_manifest_sha256": expected_acceptance_manifest_sha256,
            "encoding_diagnostics": dict(encoding_diagnostics),
            "execution": dict(execution),
        }
    )
    report_bytes = _canonical_bytes(report)
    probability_payload = {
        "artifact_kind": "formal-cleaning-model-locked-test-probabilities",
        "test_run_id": test_run_id,
        "test_manifest_sha256": plan.test_manifest_sha256,
        "model_id": plan.qwen_model_id,
        "t_keep": plan.locked_test.t_keep,
        "t_exclude": plan.locked_test.t_exclude,
        "fit_call_count": 0,
        "prediction_call_count": 1,
        "platform_used": False,
        "records": [
            {
                "member_key": item.member_key,
                "component_id": item.component_id,
                "tourism_label": item.tourism_label,
                "p_unrelated": item.p_unrelated,
                "routing_action": routing_action(
                    item.p_unrelated,
                    t_keep=plan.locked_test.t_keep,
                    t_exclude=plan.locked_test.t_exclude,
                ),
            }
            for item in observations
        ],
    }
    probabilities_bytes = _canonical_bytes(probability_payload)
    routing = report["routing"]
    label_counts = {
        "related": sum(item.tourism_label == "related" for item in observations),
        "unrelated": sum(
            item.tourism_label == "unrelated" for item in observations
        ),
    }
    manifest = {
        "artifact_kind": "formal-cleaning-model-locked-test",
        "artifact_status": "immutable",
        "test_run_id": test_run_id,
        "status": report["status"],
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "lineage": {
            "code_version": version,
            "acceptance_manifest_sha256": expected_acceptance_manifest_sha256,
            "reference_csv_sha256": (
                "3eb3ea233c05bf76b5bdda548701895c9551726d74b6f9742523e695aeac73fd"
            ),
            "test_manifest_sha256": plan.test_manifest_sha256,
            "qwen_training_manifest_sha256": plan.qwen_training_manifest_sha256,
            "qwen_model_artifact_sha256": plan.qwen_model_artifact_sha256,
        },
        "model_id": plan.qwen_model_id,
        "t_keep": plan.locked_test.t_keep,
        "t_exclude": plan.locked_test.t_exclude,
        "test_count": plan.test_count,
        "related_count": label_counts["related"],
        "unrelated_count": label_counts["unrelated"],
        "routing": routing,
        "overall_metrics": report["overall_metrics"],
        "gates": report["gates"],
        "test_status": "opened_once",
        "test_access_count": 1,
        "test_members_read": True,
        "test_probabilities_present": True,
        "audit_status": "FROZEN_NOT_RUN",
        "deployment_status": report["deployment_status"],
        "may_generate_provisional_routing": report[
            "may_generate_provisional_routing"
        ],
        "may_generate_formal_auto_decisions": False,
        "fit_call_count": 0,
        "prediction_call_count": 1,
        "auto_cleaning_decisions_present": False,
        "platform_used": False,
        "artifacts": {
            "report": {
                "filename": "test-report.json",
                "sha256": _sha256_bytes(report_bytes),
            },
            "probabilities": {
                "filename": "test-probabilities.json",
                "sha256": _sha256_bytes(probabilities_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    try:
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            staging = Path(temporary) / test_run_id
            staging.mkdir()
            (staging / "test-report.json").write_bytes(report_bytes)
            (staging / "test-probabilities.json").write_bytes(
                probabilities_bytes
            )
            (staging / "test-manifest.json").write_bytes(manifest_bytes)
            os.replace(staging, package)
    except OSError as exc:
        raise ModelLockedTestArtifactError(
            "model_locked_test_artifact_write_failed"
        ) from exc
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    return _result_from_manifest(manifest, manifest_sha256, reused=False)


def render_locked_test_result(
    result: LockedTestPackageResult, *, output_format: str
) -> str:
    """把唯一测试结果渲染为机器 JSON 或朴素中文摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelLockedTestArtifactError(
            "model_locked_test_output_format_invalid"
        )
    artifact_word = "复用" if result.reused else "首次开启并封存"
    metrics = result.overall_metrics
    confusion = metrics["confusion"]
    correct = float(
        confusion["related_as_related"] + confusion["unrelated_as_unrelated"]
    )
    accuracy = correct / float(metrics["count"])
    next_step = (
        "正式自动决定：未生成；下一门是两个自动尾部的部署审计。"
        if result.may_generate_provisional_routing
        else "正式自动决定：未生成；部署审计不启动，当前策略降级为人工处理。"
    )
    return "\n".join(
        [
            "# Qwen 锁定测试结果",
            "",
            f"测试运行 ID：{result.test_run_id}",
            f"artifact：{artifact_word}",
            f"测试成员：n={result.test_count}（单次开启已消耗）",
            "",
            "三段式路由",
            (
                f"  自动保留 / 人工 / 自动排除：{result.auto_keep_count} / "
                f"{result.manual_review_count} / {result.auto_exclude_count}"
            ),
            f"  自动覆盖率：{result.automatic_coverage_rate:.2%}",
            f"  保留端误留无关：{result.auto_keep_unrelated_events}",
            f"  排除端误删UGC：{result.auto_exclude_related_events}",
            "",
            "总体诊断（0.5不是路由阈值）",
            f"  Accuracy：{accuracy:.2%}",
            f"  log loss：{float(metrics['log_loss']):.4f}",
            f"  unrelated PR-AUC：{float(metrics['pr_auc_unrelated']):.4f}",
            f"  Brier：{float(metrics['brier_score']):.4f}",
            "",
            f"判读：{result.status}",
            f"部署状态：{result.deployment_status}",
            next_step,
        ]
    )
