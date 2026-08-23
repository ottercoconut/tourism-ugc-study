"""唯一 sparse challenger 的一次性验证输入、artifact 与报告边界。"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from tourism_ugc_study.cleaning.config import StableCleaningConfig
from tourism_ugc_study.cleaning.reference_evidence import (
    REFERENCE_FIELDS,
    validate_reference_evidence,
)
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .formal_baseline import evaluate_binary_probabilities
from .model_acceptance import load_model_acceptance_policy
from .sparse_challenger_artifacts import (
    SparseChallengerArtifactError,
    load_frozen_sparse_challenger_model,
    load_verified_baseline_split_bindings,
)
from .sparse_challenger_config import load_sparse_challenger_plan
from .sparse_challenger_validation import (
    ChallengerValidationDocument,
    SparseChallengerValidationResult,
    evaluate_sparse_challenger_validation,
)


class SparseChallengerValidationArtifactError(RuntimeError):
    """验证谱系或一次性 artifact 失败时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: 供 CLI、测试和 artifact 使用的失败码。
        """

        super().__init__("sparse challenger validation artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class SparseChallengerValidationEvidence:
    """只包含唯一验证集合与冻结候选的输入。

    Attributes:
        documents: 110条验证文本、标签、分量和 baseline 既有概率。
        frozen_candidate: 已通过完整性检查的唯一候选模型。
        candidate_model_id: challenger 唯一模型身份。
        challenger_run_id: 训练侧 challenger 运行身份。
        challenger_manifest_sha256: challenger 训练 manifest 摘要。
        baseline_manifest_sha256: baseline 训练 manifest 摘要。
        reference_manifest_sha256: 当前最终参考 manifest 摘要。
    """

    documents: tuple[ChallengerValidationDocument, ...]
    frozen_candidate: Any
    candidate_model_id: str
    challenger_run_id: str
    challenger_manifest_sha256: str
    baseline_manifest_sha256: str
    reference_manifest_sha256: str


@dataclass(frozen=True)
class SparseChallengerValidationPackageResult:
    """一次性验证 artifact 的去敏摘要。

    Attributes:
        validation_id: 输入、候选和代码共同决定的内容身份。
        candidate_model_id: 被验证的唯一候选身份。
        status: 固定为 ``frozen``。
        reused: 是否直接复用既有验证 artifact，未重新预测。
        direction_status: 四项方向一致性描述。
        validation_count: 验证记录数。
        baseline_metrics: baseline 验证诊断。
        candidate_metrics: challenger 验证诊断。
        candidate_minus_baseline: challenger 减 baseline 点差。
        direction_checks: 四项方向检查。
        manifest_sha256: 验证 manifest 摘要。
        probabilities_sha256: 私有逐成员概率摘要。
        test_status: 固定为 ``locked_not_opened``。
        threshold_status: 固定为 ``UNSET``。
    """

    validation_id: str
    candidate_model_id: str
    status: str
    reused: bool
    direction_status: str
    validation_count: int
    baseline_metrics: Mapping[str, Any]
    candidate_metrics: Mapping[str, Any]
    candidate_minus_baseline: Mapping[str, float]
    direction_checks: Mapping[str, bool]
    manifest_sha256: str
    probabilities_sha256: str
    test_status: str
    threshold_status: str


def _canonical_bytes(value: object) -> bytes:
    """生成禁止 NaN、排序键且以换行结束的规范 JSON。"""

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
    """计算内存字节 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path, reason_code: str) -> str:
    """流式计算文件 SHA-256 并统一读取失败。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SparseChallengerValidationArtifactError(reason_code) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SparseChallengerValidationArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise SparseChallengerValidationArtifactError(reason_code)
    return value


def _verified_artifact_path(
    package: Path,
    artifacts: Mapping[str, Any],
    logical_name: str,
    expected_filename: str,
    reason_code: str,
) -> Path:
    """校验 manifest 中一个固定文件名及其摘要。"""

    details = artifacts.get(logical_name)
    if (
        not isinstance(details, Mapping)
        or details.get("filename") != expected_filename
    ):
        raise SparseChallengerValidationArtifactError(reason_code)
    path = package / expected_filename
    if _file_sha256(path, reason_code) != details.get("sha256"):
        raise SparseChallengerValidationArtifactError(reason_code)
    return path


def _baseline_validation_probabilities(
    baseline_package: Path,
    baseline_manifest: Mapping[str, Any],
) -> Mapping[tuple[int, int], tuple[str, float]]:
    """读取并复核 baseline 已封存的验证概率，不生成新 baseline 预测。"""

    artifacts = baseline_manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_baseline_artifacts_invalid"
        )
    probabilities_path = _verified_artifact_path(
        baseline_package,
        artifacts,
        "development_probabilities",
        "development-probabilities.json",
        "sparse_validation_baseline_probabilities_invalid",
    )
    metrics_path = _verified_artifact_path(
        baseline_package,
        artifacts,
        "validation_metrics",
        "validation-metrics.json",
        "sparse_validation_baseline_metrics_invalid",
    )
    probabilities = _load_json(
        probabilities_path, "sparse_validation_baseline_probabilities_invalid"
    )
    if (
        set(probabilities)
        != {"positive_class", "threshold_status", "train_oof", "validation"}
        or probabilities.get("positive_class") != "unrelated"
        or probabilities.get("threshold_status") != "UNSET"
        or not isinstance(probabilities.get("validation"), list)
    ):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_baseline_probabilities_invalid"
        )
    values: dict[tuple[int, int], tuple[str, float]] = {}
    for raw in probabilities["validation"]:
        if not isinstance(raw, Mapping) or raw.get("split_name") != "validation":
            raise SparseChallengerValidationArtifactError(
                "sparse_validation_baseline_probability_record_invalid"
            )
        try:
            identity = (int(raw["source_post_id"]), int(raw["source_version"]))
            label = str(raw["tourism_label"])
            probability = float(raw["p_unrelated"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SparseChallengerValidationArtifactError(
                "sparse_validation_baseline_probability_record_invalid"
            ) from exc
        if identity in values or label not in {"related", "unrelated"}:
            raise SparseChallengerValidationArtifactError(
                "sparse_validation_baseline_probability_record_invalid"
            )
        values[identity] = (label, probability)
    stored_metrics = _load_json(
        metrics_path, "sparse_validation_baseline_metrics_invalid"
    )
    ordered = sorted(values.items())
    recomputed = evaluate_binary_probabilities(
        [value[0] for _, value in ordered],
        [value[1] for _, value in ordered],
    )
    if dict(stored_metrics) != recomputed:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_baseline_metrics_mismatch"
        )
    return values


def load_sparse_challenger_validation_evidence(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    baseline_package: str | Path,
    challenger_package: str | Path,
    plan_path: str | Path,
    policy_path: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
) -> SparseChallengerValidationEvidence:
    """校验训练侧已通过状态并只物化冻结验证成员。

    Args:
        csv_path: 当前唯一最终参考 CSV。
        reference_manifest_path: 与 CSV 配对的 finalized manifest。
        derived_db: 只读派生 SQLite，仅用于证据完整性校验。
        baseline_package: 当前 baseline 不可变包。
        challenger_package: 训练侧验收通过的唯一 challenger 包。
        plan_path: 已冻结54候选计划。
        policy_path: 已冻结 UGC 安全策略。
        config: 稳定清洗配置。
        normalization_config: 冻结文本规范化配置。

    Returns:
        只含验证记录和唯一冻结候选的输入。

    Raises:
        SparseChallengerValidationArtifactError: 任一谱系、状态或成员漂移。
        ReferenceEvidenceError: 最终参考证据未通过权威校验。
    """

    try:
        baseline_dir = Path(baseline_package).expanduser().resolve(strict=True)
        challenger_dir = Path(challenger_package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_package_unavailable"
        ) from exc
    plan = load_sparse_challenger_plan(plan_path)
    policy = load_model_acceptance_policy(policy_path)
    if policy.policy_sha256 != plan.acceptance_policy_sha256:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_policy_binding_mismatch"
        )
    try:
        baseline_manifest, _split, assignment_bindings = (
            load_verified_baseline_split_bindings(baseline_dir, plan)
        )
        frozen_candidate = load_frozen_sparse_challenger_model(challenger_dir)
    except SparseChallengerArtifactError as exc:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_training_package_invalid"
        ) from exc
    challenger_manifest_path = challenger_dir / "training-manifest.json"
    challenger_manifest = _load_json(
        challenger_manifest_path, "sparse_validation_challenger_manifest_invalid"
    )
    lineage = challenger_manifest.get("lineage")
    if (
        challenger_manifest.get("acceptance_status") != "passed"
        or challenger_manifest.get("validation_status")
        != "pending_directional_check"
        or challenger_manifest.get("test_status") != "locked_not_opened"
        or challenger_manifest.get("threshold_status") != "UNSET"
        or not isinstance(lineage, Mapping)
        or lineage.get("baseline_model_id") != plan.baseline_model_id
        or lineage.get("reference_csv_sha256") != plan.reference_csv_sha256
        or lineage.get("validation_manifest_sha256")
        != plan.validation_manifest_sha256
        or lineage.get("test_manifest_sha256") != plan.test_manifest_sha256
        or lineage.get("acceptance_policy_sha256") != policy.policy_sha256
    ):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_challenger_not_eligible"
        )
    candidate_model_id = challenger_manifest.get("candidate_model_id")
    challenger_run_id = challenger_manifest.get("run_id")
    if not isinstance(candidate_model_id, str) or not isinstance(
        challenger_run_id, str
    ):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_challenger_manifest_invalid"
        )
    baseline_probabilities = _baseline_validation_probabilities(
        baseline_dir, baseline_manifest
    )
    validation_components = {
        identity: component_id
        for identity, (component_id, split_name) in assignment_bindings.items()
        if split_name == "validation"
    }
    if set(validation_components) != set(baseline_probabilities):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_member_binding_mismatch"
        )
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
        reference.csv_sha256 != plan.reference_csv_sha256
        or reference.manifest_sha256
        != baseline_manifest["lineage"].get("reference_manifest_sha256")
    ):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_reference_binding_mismatch"
        )
    try:
        verified_csv = Path(csv_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_reference_reread_failed"
        ) from exc
    documents: list[ChallengerValidationDocument] = []
    seen: set[tuple[int, int]] = set()
    try:
        with verified_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise SparseChallengerValidationArtifactError(
                    "sparse_validation_reference_fields_invalid"
                )
            for row in reader:
                identity = (
                    int(row["source_post_id"].strip()),
                    int(row["source_version"].strip()),
                )
                component_id = validation_components.get(identity)
                if component_id is None:
                    continue
                if identity in seen:
                    raise SparseChallengerValidationArtifactError(
                        "sparse_validation_identity_duplicate"
                    )
                seen.add(identity)
                expected_label, baseline_probability = baseline_probabilities[
                    identity
                ]
                label = row["tourism_label"].strip()
                text = row["normalized_model_text"]
                if label != expected_label or not text.strip():
                    raise SparseChallengerValidationArtifactError(
                        "sparse_validation_record_mismatch"
                    )
                member_payload = (
                    f"{candidate_model_id}:{identity[0]}:{identity[1]}"
                ).encode("utf-8")
                documents.append(
                    ChallengerValidationDocument(
                        member_key=hashlib.sha256(member_payload).hexdigest(),
                        component_id=component_id,
                        normalized_model_text=text,
                        tourism_label=label,
                        baseline_p_unrelated=baseline_probability,
                    )
                )
    except (OSError, UnicodeError, csv.Error, KeyError, TypeError, ValueError) as exc:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_reference_reread_failed"
        ) from exc
    if seen != set(validation_components):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_members_incomplete"
        )
    return SparseChallengerValidationEvidence(
        documents=tuple(sorted(documents, key=lambda item: item.member_key)),
        frozen_candidate=frozen_candidate,
        candidate_model_id=candidate_model_id,
        challenger_run_id=challenger_run_id,
        challenger_manifest_sha256=_file_sha256(
            challenger_manifest_path,
            "sparse_validation_challenger_manifest_invalid",
        ),
        baseline_manifest_sha256=_file_sha256(
            baseline_dir / "training-manifest.json",
            "sparse_validation_baseline_manifest_invalid",
        ),
        reference_manifest_sha256=reference.manifest_sha256,
    )


def _validation_identity(
    evidence: SparseChallengerValidationEvidence,
    *,
    validation_manifest_sha256: str,
    policy_sha256: str,
    code_version: str,
) -> str:
    """计算一次性验证的稳定内容身份。"""

    payload = {
        "artifact_kind": "formal-cleaning-sparse-challenger-validation",
        "candidate_model_id": evidence.candidate_model_id,
        "challenger_manifest_sha256": evidence.challenger_manifest_sha256,
        "baseline_manifest_sha256": evidence.baseline_manifest_sha256,
        "reference_manifest_sha256": evidence.reference_manifest_sha256,
        "validation_manifest_sha256": validation_manifest_sha256,
        "policy_sha256": policy_sha256,
        "code_version": code_version,
    }
    return _sha256_bytes(_canonical_bytes(payload))[:32]


def _package_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> SparseChallengerValidationPackageResult:
    """从已验证 manifest 构造去敏返回值。"""

    report = manifest["report"]
    artifacts = manifest["artifacts"]
    return SparseChallengerValidationPackageResult(
        validation_id=str(manifest["validation_id"]),
        candidate_model_id=str(manifest["candidate_model_id"]),
        status="frozen",
        reused=reused,
        direction_status=str(report["direction_status"]),
        validation_count=int(report["validation_count"]),
        baseline_metrics=dict(report["baseline_metrics"]),
        candidate_metrics=dict(report["candidate_metrics"]),
        candidate_minus_baseline=dict(report["candidate_minus_baseline"]),
        direction_checks=dict(report["direction_checks"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        probabilities_sha256=str(artifacts["probabilities"]["sha256"]),
        test_status="locked_not_opened",
        threshold_status="UNSET",
    )


def _validate_existing_package(
    package_dir: Path, *, validation_id: str
) -> SparseChallengerValidationPackageResult:
    """复用既有验证包，保证不再次调用候选预测。"""

    manifest_path = package_dir / "validation-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_manifest_unreadable"
        ) from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-sparse-challenger-validation"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("validation_id") != validation_id
        or manifest_bytes != _canonical_bytes(manifest)
    ):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_manifest_invalid"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "probabilities",
        "report",
    }:
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_artifacts_invalid"
        )
    expected = {
        "probabilities": "validation-probabilities.json",
        "report": "validation-report.json",
    }
    for name, filename in expected.items():
        _verified_artifact_path(
            package_dir,
            artifacts,
            name,
            filename,
            "sparse_validation_artifact_hash_mismatch",
        )
    return _package_result(manifest, manifest_bytes, reused=True)


def _report_payload(
    result: SparseChallengerValidationResult,
    *,
    validation_id: str,
    evidence: SparseChallengerValidationEvidence,
) -> dict[str, Any]:
    """生成不含逐成员身份的方向性聚合报告。"""

    return {
        "artifact_kind": "formal-cleaning-sparse-challenger-validation-report",
        "validation_id": validation_id,
        "challenger_run_id": evidence.challenger_run_id,
        "candidate_model_id": evidence.candidate_model_id,
        "role": "directional_check_only",
        "direction_status": result.direction_status,
        "validation_count": result.validation_count,
        "diagnostic_cutoff": result.diagnostic_cutoff,
        "diagnostic_cutoff_is_routing_threshold": False,
        "baseline_metrics": dict(result.baseline_metrics),
        "candidate_metrics": dict(result.candidate_metrics),
        "candidate_minus_baseline": dict(result.candidate_minus_baseline),
        "direction_checks": dict(result.direction_checks),
        "statistical_gate_applied": False,
        "may_expand_search": False,
        "candidate_prediction_calls": 1,
        "test_status": "locked_not_opened",
        "test_members_read": False,
        "test_probabilities_present": False,
        "threshold_status": "UNSET",
        "auto_cleaning_decisions_present": False,
    }


def evaluate_sparse_challenger_validation_package(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    baseline_package: str | Path,
    challenger_package: str | Path,
    plan_path: str | Path,
    policy_path: str | Path,
    artifact_root: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    code_version: str,
) -> SparseChallengerValidationPackageResult:
    """执行唯一候选的一次性无拟合验证并原子封存。

    Args:
        csv_path: 当前唯一最终参考 CSV。
        reference_manifest_path: 与 CSV 配对的 finalized manifest。
        derived_db: 只读派生 SQLite。
        baseline_package: 当前 baseline 不可变包。
        challenger_package: 训练侧验收通过的唯一 challenger 包。
        plan_path: 冻结候选计划。
        policy_path: 冻结 UGC 安全策略。
        artifact_root: 验证 artifact 父目录。
        config: 稳定清洗配置。
        normalization_config: 冻结文本规范化配置。
        code_version: 当前 Git 完整提交身份。

    Returns:
        方向性报告、artifact 摘要和测试/阈值锁定状态。

    Raises:
        SparseChallengerValidationArtifactError: 输入、写入或既有包非法。
        SparseChallengerValidationError: 唯一候选概率或指标非法。

    Notes:
        相同输入形成相同 ``validation_id``；既有包通过校验时只复用结果，
        不再调用候选预测。该入口不接受模型、候选、超参数、测试或阈值参数。
    """

    version = code_version.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_code_version_invalid"
        )
    plan = load_sparse_challenger_plan(plan_path)
    policy = load_model_acceptance_policy(policy_path)
    evidence = load_sparse_challenger_validation_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        baseline_package,
        challenger_package,
        plan_path,
        policy_path,
        config=config,
        normalization_config=normalization_config,
    )
    validation_id = _validation_identity(
        evidence,
        validation_manifest_sha256=plan.validation_manifest_sha256,
        policy_sha256=policy.policy_sha256,
        code_version=version,
    )
    root = Path(artifact_root).expanduser().resolve()
    package_dir = root / validation_id
    if package_dir.exists():
        return _validate_existing_package(package_dir, validation_id=validation_id)
    result = evaluate_sparse_challenger_validation(
        evidence.documents,
        frozen_candidate=evidence.frozen_candidate,
    )
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{validation_id}.", dir=root)
    )
    try:
        probabilities_path = temporary / "validation-probabilities.json"
        report_path = temporary / "validation-report.json"
        probabilities_path.write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": (
                        "formal-cleaning-sparse-challenger-validation-probabilities"
                    ),
                    "validation_id": validation_id,
                    "candidate_model_id": evidence.candidate_model_id,
                    "split_name": "validation",
                    "positive_class": "unrelated",
                    "records": [asdict(item) for item in result.observations],
                    "test_probabilities_present": False,
                }
            )
        )
        report = _report_payload(
            result, validation_id=validation_id, evidence=evidence
        )
        report_path.write_bytes(_canonical_bytes(report))
        artifacts = {
            "probabilities": {
                "filename": probabilities_path.name,
                "sha256": _file_sha256(
                    probabilities_path, "sparse_validation_temporary_unreadable"
                ),
            },
            "report": {
                "filename": report_path.name,
                "sha256": _file_sha256(
                    report_path, "sparse_validation_temporary_unreadable"
                ),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-sparse-challenger-validation",
            "artifact_status": "immutable",
            "validation_id": validation_id,
            "challenger_run_id": evidence.challenger_run_id,
            "candidate_model_id": evidence.candidate_model_id,
            "validation_role": "directional_check_only",
            "validation_access_count": 1,
            "candidate_prediction_calls": 1,
            "may_expand_search": False,
            "test_status": "locked_not_opened",
            "test_members_read": False,
            "test_probabilities_present": False,
            "threshold_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "lineage": {
                "code_version": version,
                "plan_sha256": plan.plan_sha256,
                "policy_sha256": policy.policy_sha256,
                "reference_csv_sha256": plan.reference_csv_sha256,
                "reference_manifest_sha256": evidence.reference_manifest_sha256,
                "train_manifest_sha256": plan.train_manifest_sha256,
                "validation_manifest_sha256": plan.validation_manifest_sha256,
                "test_manifest_sha256": plan.test_manifest_sha256,
                "baseline_manifest_sha256": evidence.baseline_manifest_sha256,
                "challenger_manifest_sha256": (
                    evidence.challenger_manifest_sha256
                ),
            },
            "report": report,
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "validation-manifest.json").write_bytes(manifest_bytes)
        try:
            temporary.rename(package_dir)
        except OSError as exc:
            raise SparseChallengerValidationArtifactError(
                "sparse_validation_publish_failed"
            ) from exc
        temporary = None
        return _package_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def render_sparse_challenger_validation_result(
    result: SparseChallengerValidationPackageResult,
    *,
    output_format: str = "human",
) -> str:
    """渲染机器 JSON 或安全优先的中文验证报告。

    Args:
        result: 已封存或复用的验证摘要。
        output_format: ``human`` 或 ``json``。

    Returns:
        不含逐成员身份、正文或路径的报告。

    Raises:
        SparseChallengerValidationArtifactError: 输出格式未知。
    """

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise SparseChallengerValidationArtifactError(
            "sparse_validation_output_format_invalid"
        )
    baseline = result.baseline_metrics
    candidate = result.candidate_metrics
    delta = result.candidate_minus_baseline
    confusion_baseline = baseline["confusion"]
    confusion_candidate = candidate["confusion"]
    direction_text = (
        "四项方向均一致"
        if result.direction_status == "directionally_consistent"
        else "方向出现混合或反转"
    )
    lines = [
        "Sparse challenger 验证方向性复核",
        "=================================",
        f"验证 ID：{result.validation_id}",
        f"候选模型 ID：{result.candidate_model_id}",
        f"artifact：{'复用既有包，未重新预测' if result.reused else '首次预测并封存'}",
        f"验证集合：n={result.validation_count}；角色=directional_check_only",
        "",
        "固定0.5诊断（不是路由阈值）",
        f"  Accuracy：{baseline['accuracy'] * 100:.2f}% → "
        f"{candidate['accuracy'] * 100:.2f}% "
        f"({delta['accuracy'] * 100:+.2f}个百分点)",
        "  related→unrelated："
        f"{confusion_baseline['related_as_unrelated']} → "
        f"{confusion_candidate['related_as_unrelated']}；率差"
        f"{delta['related_to_unrelated_rate'] * 100:+.2f}个百分点",
        "  unrelated→related："
        f"{confusion_baseline['unrelated_as_related']} → "
        f"{confusion_candidate['unrelated_as_related']}",
        "",
        "概率与排序质量",
        f"  log loss：{baseline['log_loss']:.4f} → "
        f"{candidate['log_loss']:.4f} ({delta['log_loss']:+.4f})",
        f"  unrelated PR-AUC：{baseline['pr_auc_unrelated']:.4f} → "
        f"{candidate['pr_auc_unrelated']:.4f} "
        f"({delta['pr_auc_unrelated']:+.4f})",
        f"  Brier：{baseline['brier_score']:.4f} → "
        f"{candidate['brier_score']:.4f} ({delta['brier_score']:+.4f})",
        "",
        f"方向性结论：{direction_text}（{result.direction_status}）",
        f"统计边界：未对{result.validation_count}条另设显著性或验收门，"
        "不允许扩展搜索或调参。",
        "锁定测试：locked_not_opened；路由阈值：UNSET；自动决定：未生成。",
    ]
    return "\n".join(lines)
