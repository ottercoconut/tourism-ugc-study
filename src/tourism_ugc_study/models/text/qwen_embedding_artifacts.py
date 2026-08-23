"""Qwen 语义 baseline 的只读证据集成与不可变训练运行包。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import joblib
import numpy as np

from tourism_ugc_study.cleaning.config import StableCleaningConfig
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .model_acceptance import (
    PairedOofObservation,
    evaluate_model_acceptance,
    load_model_acceptance_policy,
)
from .qwen_embedding_baseline import (
    FrozenQwenEmbeddingClassifier,
    QwenEmbeddingBaselineError,
    fit_qwen_embedding_baseline,
    pair_qwen_with_sparse_comparator,
    probability_band_diagnostics,
    risk_coverage_diagnostics,
)
from .qwen_embedding_config import (
    QwenEmbeddingPlan,
    load_qwen_embedding_plan,
)
from .qwen_embedding_runtime import (
    LocalQwenEmbeddingEncoder,
    QwenEncodingDiagnostics,
    QwenExecutionReceipt,
    QwenModelSnapshot,
    validate_qwen_model_directory,
)
from .sparse_challenger import ChallengerDocument
from .sparse_challenger_artifacts import load_sparse_challenger_evidence
from .sparse_challenger_config import load_sparse_challenger_plan


QWEN_EMBEDDING_ALGORITHM_ID = "qwen3-embedding-frozen-logistic-group-oof-v1"


class QwenEmbeddingArtifactError(RuntimeError):
    """语义 baseline 谱系或不可变 artifact 失败时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、成员身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。"""

        super().__init__("qwen embedding baseline artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenEmbeddingEvidenceBundle:
    """只含冻结训练成员和不可逆谱系摘要的语义 baseline 输入。"""

    documents: tuple[ChallengerDocument, ...]
    reference_manifest_sha256: str
    split_manifest_sha256: str
    split_anchor_package_manifest_sha256: str
    train_count: int
    validation_count: int
    test_count: int


@dataclass(frozen=True)
class QwenEmbeddingPackageResult:
    """不可变语义 baseline 运行包的去敏摘要。"""

    run_id: str
    model_id: str
    status: str
    reused: bool
    train_count: int
    fold_count: int
    acceptance_status: str
    acceptance_report: Mapping[str, Any]
    training_metrics: Mapping[str, Any]
    confidence_band_diagnostics: Mapping[str, Any]
    confidence_band_comparison: Mapping[str, Any]
    risk_coverage_diagnostics: tuple[Mapping[str, Any], ...]
    encoding_diagnostics: Mapping[str, Any]
    package_manifest_sha256: str
    classifier_artifact_sha256: str
    embeddings_artifact_sha256: str
    paired_oof_sha256: str
    validation_status: str
    test_status: str
    threshold_status: str
    audit_status: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序、禁止 NaN 且以换行结束的规范 JSON。"""

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
    """计算字节串 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256 并统一失败码。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenEmbeddingArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise QwenEmbeddingArtifactError(reason_code)
    return value


def _runtime_versions() -> Mapping[str, str]:
    """记录训练与本地编码所用的精确依赖版本。"""

    names = (
        "joblib",
        "numpy",
        "scikit-learn",
        "scipy",
        "torch",
        "transformers",
        "sentence-transformers",
        "huggingface-hub",
        "safetensors",
        "tokenizers",
    )
    try:
        return {name: importlib.metadata.version(name) for name in names}
    except importlib.metadata.PackageNotFoundError as exc:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_runtime_dependency_missing"
        ) from exc


def load_qwen_embedding_evidence(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    split_anchor_package: str | Path,
    sparse_plan_path: str | Path,
    *,
    plan: QwenEmbeddingPlan,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
) -> QwenEmbeddingEvidenceBundle:
    """复用已验证切分边界，只物化训练442条语义输入。

    Args:
        csv_path: 唯一最终700条参考 CSV。
        reference_manifest_path: 与 CSV 唯一配对的 finalized manifest。
        derived_db: 只读派生 SQLite。
        split_anchor_package: 冻结切分所在的正式 baseline 包。
        sparse_plan_path: 已完成 sparse challenger 的冻结计划。
        plan: 新语义 baseline 预登记计划。
        config: 稳定清洗配置。
        normalization_config: 冻结文本规范化配置。

    Returns:
        不包含验证或测试记录对象的训练证据。

    Raises:
        QwenEmbeddingArtifactError: 新旧计划、切分或输入谱系漂移。

    Notes:
        既有 sparse 证据加载器已经执行最终 CSV、manifest、只读派生库、
        文本投影与切分校验；本层再次绑定新计划，避免复制两套输入协议。
    """

    sparse_plan = load_sparse_challenger_plan(sparse_plan_path)
    if (
        sparse_plan.baseline_model_id != plan.split_anchor_model_id
        or sparse_plan.reference_csv_sha256 != plan.reference_csv_sha256
        or sparse_plan.train_manifest_sha256 != plan.train_manifest_sha256
        or sparse_plan.validation_manifest_sha256
        != plan.validation_manifest_sha256
        or sparse_plan.test_manifest_sha256 != plan.test_manifest_sha256
    ):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_split_plan_binding_mismatch"
        )
    bundle = load_sparse_challenger_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        split_anchor_package,
        plan=sparse_plan,
        config=config,
        normalization_config=normalization_config,
    )
    return QwenEmbeddingEvidenceBundle(
        documents=bundle.documents,
        reference_manifest_sha256=bundle.reference_manifest_sha256,
        split_manifest_sha256=bundle.baseline_split_manifest_sha256,
        split_anchor_package_manifest_sha256=(
            bundle.baseline_package_manifest_sha256
        ),
        train_count=bundle.train_count,
        validation_count=bundle.validation_count,
        test_count=bundle.test_count,
    )


def load_sparse_comparator_oof(
    comparator_package: str | Path,
    *,
    plan: QwenEmbeddingPlan,
) -> tuple[PairedOofObservation, ...]:
    """校验 sparse comparator 包并读取其训练侧 candidate OOF。

    本函数不读取 sparse 验证概率；计划中的验证身份只记录在研究谱系中，
    新语义模型选择严格只使用训练 OOF。
    """

    try:
        directory = Path(comparator_package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_package_unavailable"
        ) from exc
    manifest_path = directory / "training-manifest.json"
    if _file_sha256(manifest_path) != plan.comparator_package_manifest_sha256:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "qwen_embedding_comparator_manifest_invalid"
    )
    artifacts = manifest.get("artifacts")
    lineage = manifest.get("lineage")
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-sparse-challenger"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != plan.comparator_run_id
        or manifest.get("candidate_model_id") != plan.comparator_model_id
        or manifest.get("acceptance_status") != "passed"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
        or not isinstance(lineage, Mapping)
        or lineage.get("reference_csv_sha256") != plan.reference_csv_sha256
        or lineage.get("train_manifest_sha256") != plan.train_manifest_sha256
        or lineage.get("test_manifest_sha256") != plan.test_manifest_sha256
    ):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_manifest_invalid"
        )
    details = artifacts.get("paired_outer_oof")
    if (
        not isinstance(details, Mapping)
        or details.get("sha256") != plan.comparator_paired_oof_sha256
        or not isinstance(details.get("filename"), str)
    ):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_oof_invalid"
        )
    oof_path = directory / str(details["filename"])
    if _file_sha256(oof_path) != plan.comparator_paired_oof_sha256:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_oof_hash_mismatch"
        )
    payload = _load_json(oof_path, "qwen_embedding_comparator_oof_invalid")
    raw_records = payload.get("records")
    if (
        payload.get("candidate_model_id") != plan.comparator_model_id
        or payload.get("paired_outer_folds") is not True
        or payload.get("platform_used") is not False
        or not isinstance(raw_records, list)
    ):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_oof_invalid"
        )
    records: list[PairedOofObservation] = []
    try:
        for raw in raw_records:
            if not isinstance(raw, Mapping):
                raise TypeError
            records.append(
                PairedOofObservation(
                    member_key=str(raw["member_key"]),
                    component_id=str(raw["component_id"]),
                    tourism_label=str(raw["tourism_label"]),
                    baseline_p_unrelated=float(raw["baseline_p_unrelated"]),
                    candidate_p_unrelated=float(raw["candidate_p_unrelated"]),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_oof_invalid"
        ) from exc
    if len(records) != int(manifest.get("train_count", -1)):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_comparator_oof_invalid"
        )
    return tuple(records)


def _run_identity(
    evidence: QwenEmbeddingEvidenceBundle,
    plan: QwenEmbeddingPlan,
    snapshot: QwenModelSnapshot,
    execution: QwenExecutionReceipt,
    runtime_versions: Mapping[str, str],
    code_version: str,
) -> str:
    """在编码和拟合前计算内容寻址运行身份。"""

    payload = {
        "algorithm_id": QWEN_EMBEDDING_ALGORITHM_ID,
        "code_version": code_version,
        "plan_sha256": plan.plan_sha256,
        "reference_manifest_sha256": evidence.reference_manifest_sha256,
        "split_manifest_sha256": evidence.split_manifest_sha256,
        "comparator_model_id": plan.comparator_model_id,
        "comparator_paired_oof_sha256": plan.comparator_paired_oof_sha256,
        "encoder_revision": snapshot.revision,
        "weights_sha256": snapshot.weights_sha256,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "execution": asdict(execution),
        "runtime_versions": dict(runtime_versions),
    }
    return _sha256_bytes(_canonical_bytes(payload))[:32]


def _model_identity(run_id: str, plan: QwenEmbeddingPlan) -> str:
    """生成不依赖序列化字节的稳定模型身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "run_id": run_id,
                "plan_id": plan.plan_id,
                "encoder_revision": plan.encoder.revision,
                "classifier": asdict(plan.classifier),
            }
        )
    )[:32]


def _artifact_details(path: Path, filename: str) -> Mapping[str, str]:
    """返回 manifest 使用的文件名与摘要。"""

    return {"filename": filename, "sha256": _file_sha256(path / filename)}


def _result_from_manifest(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> QwenEmbeddingPackageResult:
    """从已校验 manifest 构造去敏返回值。"""

    artifacts = manifest["artifacts"]
    report = manifest["training_report"]
    return QwenEmbeddingPackageResult(
        run_id=str(manifest["run_id"]),
        model_id=str(manifest["model_id"]),
        status="frozen",
        reused=reused,
        train_count=int(manifest["train_count"]),
        fold_count=int(manifest["fold_count"]),
        acceptance_status=str(manifest["acceptance_status"]),
        acceptance_report=dict(report["acceptance"]),
        training_metrics=dict(report["training_metrics"]),
        confidence_band_diagnostics=dict(report["confidence_band_diagnostics"]),
        confidence_band_comparison=dict(report["confidence_band_comparison"]),
        risk_coverage_diagnostics=tuple(report["risk_coverage_diagnostics"]),
        encoding_diagnostics=dict(report["encoding_diagnostics"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        classifier_artifact_sha256=str(artifacts["classifier"]["sha256"]),
        embeddings_artifact_sha256=str(artifacts["embeddings"]["sha256"]),
        paired_oof_sha256=str(artifacts["paired_oof"]["sha256"]),
        validation_status=str(manifest["validation_status"]),
        test_status=str(manifest["test_status"]),
        threshold_status=str(manifest["threshold_status"]),
        audit_status=str(manifest["audit_status"]),
    )


def _validate_existing_package(
    package_dir: Path,
    *,
    expected_run_id: str,
    expected_model_id: str,
    plan: QwenEmbeddingPlan,
    snapshot: QwenModelSnapshot,
    execution: QwenExecutionReceipt,
    expected_manifest_sha256: str | None = None,
) -> QwenEmbeddingPackageResult:
    """完整校验并复用既有不可变运行包。"""

    manifest_path = package_dir / "training-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        if (
            expected_manifest_sha256 is not None
            and _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        ):
            raise QwenEmbeddingArtifactError(
                "qwen_embedding_package_manifest_hash_mismatch"
            )
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_package_manifest_unreadable"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise QwenEmbeddingArtifactError("qwen_embedding_package_manifest_invalid")
    artifacts = manifest.get("artifacts")
    lineage = manifest.get("lineage")
    expected_artifacts = {
        "classifier": "classifier.joblib",
        "embeddings": "train-embeddings.npz",
        "training_oof": "training-oof.json",
        "paired_oof": "paired-oof.json",
        "report": "training-report.json",
        "plan": "plan.yaml",
        "acceptance_policy": "acceptance-policy.yaml",
    }
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-qwen-embedding-baseline"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != expected_run_id
        or manifest.get("model_id") != expected_model_id
        or manifest.get("algorithm_id") != QWEN_EMBEDDING_ALGORITHM_ID
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
        or not isinstance(lineage, Mapping)
        or set(artifacts) != set(expected_artifacts)
        or lineage.get("plan_sha256") != plan.plan_sha256
        or lineage.get("encoder_snapshot_sha256") != snapshot.snapshot_sha256
        or lineage.get("execution") != asdict(execution)
    ):
        raise QwenEmbeddingArtifactError("qwen_embedding_package_manifest_invalid")
    for name, details in artifacts.items():
        filename = expected_artifacts[name]
        artifact_path = package_dir / filename
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or not isinstance(details.get("sha256"), str)
            or artifact_path.is_symlink()
            or artifact_path.resolve().parent != package_dir.resolve()
            or _file_sha256(artifact_path) != details["sha256"]
        ):
            raise QwenEmbeddingArtifactError(
                "qwen_embedding_package_artifact_hash_mismatch"
            )
    report = _load_json(
        package_dir / str(artifacts["report"]["filename"]),
        "qwen_embedding_package_report_invalid",
    )
    if report != manifest.get("training_report"):
        raise QwenEmbeddingArtifactError("qwen_embedding_package_report_invalid")
    return _result_from_manifest(manifest, manifest_bytes, reused=True)


def train_qwen_embedding_package(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    split_anchor_package: str | Path,
    sparse_plan_path: str | Path,
    comparator_package: str | Path,
    plan_path: str | Path,
    acceptance_policy_path: str | Path,
    model_dir: str | Path,
    artifact_root: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
    show_progress: bool = True,
) -> QwenEmbeddingPackageResult:
    """编码训练集、执行固定候选 OOF 验收并原子封存运行包。

    该函数不接收验证、测试、平台、阈值或审计参数。训练侧通过只授权后续
    一次验证方向复核，不表示模型可直接清洗全量数据。
    """

    version = code_version.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise QwenEmbeddingArtifactError("qwen_embedding_code_version_invalid")
    plan = load_qwen_embedding_plan(plan_path)
    policy = load_model_acceptance_policy(acceptance_policy_path)
    if (
        policy.policy_sha256 != plan.acceptance_policy_sha256
        or policy.baseline_model_id != plan.comparator_model_id
        or policy.reference_csv_sha256 != plan.reference_csv_sha256
        or policy.train_manifest_sha256 != plan.train_manifest_sha256
        or policy.validation_manifest_sha256 != plan.validation_manifest_sha256
        or policy.test_manifest_sha256 != plan.test_manifest_sha256
    ):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_acceptance_binding_mismatch"
        )
    snapshot = validate_qwen_model_directory(model_dir, plan=plan)
    encoder = LocalQwenEmbeddingEncoder(model_dir, plan=plan)
    execution = encoder.execution_receipt
    evidence = load_qwen_embedding_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        split_anchor_package,
        sparse_plan_path,
        plan=plan,
        config=config,
        normalization_config=normalization_config,
    )
    comparator_oof = load_sparse_comparator_oof(comparator_package, plan=plan)
    runtime_versions = _runtime_versions()
    run_id = _run_identity(
        evidence,
        plan,
        snapshot,
        execution,
        runtime_versions,
        version,
    )
    model_id = _model_identity(run_id, plan)
    root = Path(artifact_root).expanduser().resolve()
    package_dir = root / run_id
    if package_dir.exists():
        if expected_existing_manifest_sha256 is None:
            raise QwenEmbeddingArtifactError(
                "qwen_embedding_existing_package_requires_manifest_hash"
            )
        return _validate_existing_package(
            package_dir,
            expected_run_id=run_id,
            expected_model_id=model_id,
            plan=plan,
            snapshot=snapshot,
            execution=execution,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    encoded = encoder.encode_with_diagnostics(
        [item.normalized_model_text for item in evidence.documents],
        show_progress=show_progress,
    )
    embeddings = encoded.embeddings
    result = fit_qwen_embedding_baseline(
        evidence.documents, embeddings, plan=plan
    )
    paired = pair_qwen_with_sparse_comparator(
        result.oof_probabilities, comparator_oof
    )
    acceptance = evaluate_model_acceptance(
        paired, policy, candidate_model_id=model_id
    )
    labels = [row.tourism_label for row in paired]
    sparse_probabilities = np.asarray(
        [row.baseline_p_unrelated for row in paired], dtype=float
    )
    qwen_probabilities = np.asarray(
        [row.candidate_p_unrelated for row in paired], dtype=float
    )
    sparse_band = probability_band_diagnostics(
        labels,
        sparse_probabilities,
        low=plan.confidence_band_low,
        high=plan.confidence_band_high,
    )
    confidence_band_comparison = {
        "baseline": dict(sparse_band),
        "candidate": dict(result.confidence_band_diagnostics),
        "candidate_minus_baseline": {
            "auto_like_count": (
                result.confidence_band_diagnostics["auto_like_count"]
                - sparse_band["auto_like_count"]
            ),
            "middle_count": (
                result.confidence_band_diagnostics["middle_count"]
                - sparse_band["middle_count"]
            ),
            "middle_rate": (
                result.confidence_band_diagnostics["middle_rate"]
                - sparse_band["middle_rate"]
            ),
        },
    }
    sparse_curve = risk_coverage_diagnostics(
        labels,
        sparse_probabilities,
        confidence_grid=plan.risk_coverage_confidence_grid,
    )
    risk_coverage_comparison = []
    for sparse_row, qwen_row in zip(
        sparse_curve, result.risk_coverage_diagnostics, strict=True
    ):
        sparse_risk = sparse_row["selective_risk"]
        qwen_risk = qwen_row["selective_risk"]
        risk_coverage_comparison.append(
            {
                "confidence": qwen_row["confidence"],
                "is_routing_threshold": False,
                "baseline": dict(sparse_row),
                "candidate": dict(qwen_row),
                "candidate_minus_baseline": {
                    "coverage": qwen_row["coverage"] - sparse_row["coverage"],
                    "error_count": (
                        qwen_row["error_count"] - sparse_row["error_count"]
                    ),
                    "selective_risk": (
                        qwen_risk - sparse_risk
                        if qwen_risk is not None and sparse_risk is not None
                        else None
                    ),
                },
            }
        )
    report = {
        "artifact_kind": "formal-cleaning-qwen-embedding-training-report",
        "training_metrics": dict(result.training_metrics),
        "confidence_band_diagnostics": dict(
            result.confidence_band_diagnostics
        ),
        "confidence_band_comparison": confidence_band_comparison,
        "risk_coverage_diagnostics": risk_coverage_comparison,
        "encoding_diagnostics": asdict(encoded.diagnostics),
        "probability_semantics": (
            "development_sample_conditional_diagnostic_not_population_posterior"
        ),
        "acceptance": dict(acceptance),
        "diagnostic_cutoff_is_routing_threshold": False,
        "confidence_band_is_routing_threshold": False,
        "threshold_status": "UNSET",
        "audit_status": "UNSET",
    }
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.", dir=root)
    )
    try:
        joblib.dump(result.model, temporary / "classifier.joblib")
        with (temporary / "train-embeddings.npz").open("wb") as stream:
            np.savez_compressed(
                stream,
                embeddings=np.asarray(embeddings, dtype=np.float32),
                member_keys=np.asarray(
                    [item.member_key for item in evidence.documents], dtype=str
                ),
            )
        (temporary / "training-oof.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-group-oof",
                    "model_id": model_id,
                    "records": [asdict(row) for row in result.oof_probabilities],
                    "platform_used": False,
                }
            )
        )
        (temporary / "paired-oof.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-paired-oof",
                    "baseline_model_id": plan.comparator_model_id,
                    "candidate_model_id": model_id,
                    "records": [asdict(row) for row in paired],
                    "paired_outer_folds": False,
                    "common_members_and_group_scheme": True,
                    "platform_used": False,
                }
            )
        )
        (temporary / "training-report.json").write_bytes(
            _canonical_bytes(report)
        )
        shutil.copyfile(plan_path, temporary / "plan.yaml")
        shutil.copyfile(
            acceptance_policy_path, temporary / "acceptance-policy.yaml"
        )
        artifacts = {
            "classifier": _artifact_details(temporary, "classifier.joblib"),
            "embeddings": _artifact_details(temporary, "train-embeddings.npz"),
            "training_oof": _artifact_details(temporary, "training-oof.json"),
            "paired_oof": _artifact_details(temporary, "paired-oof.json"),
            "report": _artifact_details(temporary, "training-report.json"),
            "plan": _artifact_details(temporary, "plan.yaml"),
            "acceptance_policy": _artifact_details(
                temporary, "acceptance-policy.yaml"
            ),
        }
        validation_status = (
            "pending_directional_check"
            if acceptance["all_gates_passed"]
            else "not_allowed"
        )
        manifest = {
            "artifact_kind": "formal-cleaning-qwen-embedding-baseline",
            "artifact_status": "immutable",
            "run_id": run_id,
            "model_id": model_id,
            "algorithm_id": QWEN_EMBEDDING_ALGORITHM_ID,
            "train_count": evidence.train_count,
            "fold_count": result.fold_count,
            "acceptance_status": acceptance["status"],
            "validation_status": validation_status,
            "test_status": "locked_not_opened",
            "test_member_metadata_binding_only": True,
            "test_probabilities_present": False,
            "threshold_status": "UNSET",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
            "training_report": report,
            "lineage": {
                "code_version": version,
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "acceptance_policy_sha256": policy.policy_sha256,
                "reference_csv_sha256": plan.reference_csv_sha256,
                "reference_manifest_sha256": evidence.reference_manifest_sha256,
                "train_manifest_sha256": plan.train_manifest_sha256,
                "validation_manifest_sha256": plan.validation_manifest_sha256,
                "test_manifest_sha256": plan.test_manifest_sha256,
                "split_manifest_sha256": evidence.split_manifest_sha256,
                "split_anchor_model_id": plan.split_anchor_model_id,
                "split_anchor_package_manifest_sha256": (
                    evidence.split_anchor_package_manifest_sha256
                ),
                "comparator_run_id": plan.comparator_run_id,
                "comparator_model_id": plan.comparator_model_id,
                "comparator_package_manifest_sha256": (
                    plan.comparator_package_manifest_sha256
                ),
                "comparator_paired_oof_sha256": (
                    plan.comparator_paired_oof_sha256
                ),
                "comparator_validation_id": plan.comparator_validation_id,
                "comparator_validation_manifest_sha256": (
                    plan.comparator_validation_manifest_sha256
                ),
                "encoder_repository": snapshot.repository,
                "encoder_revision": snapshot.revision,
                "encoder_weights_sha256": snapshot.weights_sha256,
                "encoder_snapshot_sha256": snapshot.snapshot_sha256,
                "execution": asdict(execution),
                "encoding_diagnostics": asdict(encoded.diagnostics),
                "random_seed": plan.random_seed,
                "runtime_versions": runtime_versions,
            },
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "training-manifest.json").write_bytes(manifest_bytes)
        try:
            temporary.rename(package_dir)
        except OSError as exc:
            raise QwenEmbeddingArtifactError(
                "qwen_embedding_package_publish_failed"
            ) from exc
        temporary = None
        return _result_from_manifest(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def load_frozen_qwen_embedding_classifier(
    package_dir: str | Path,
    *,
    expected_manifest_sha256: str,
    expected_model_id: str,
    plan: QwenEmbeddingPlan,
    snapshot: QwenModelSnapshot,
    execution: QwenExecutionReceipt,
) -> FrozenQwenEmbeddingClassifier:
    """校验不可变包后加载不包含 Qwen 权重的线性概率头。"""

    try:
        directory = Path(package_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_package_unavailable"
        ) from exc
    manifest = _load_json(
        directory / "training-manifest.json",
        "qwen_embedding_package_manifest_unreadable",
    )
    expected_run_id = str(manifest.get("run_id", ""))
    _validate_existing_package(
        directory,
        expected_run_id=expected_run_id,
        expected_model_id=expected_model_id,
        plan=plan,
        snapshot=snapshot,
        execution=execution,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    try:
        model = joblib.load(
            directory / str(manifest["artifacts"]["classifier"]["filename"])
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_classifier_unreadable"
        ) from exc
    if not isinstance(model, FrozenQwenEmbeddingClassifier):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_classifier_type_invalid"
        )
    if (
        model.plan_id != plan.plan_id
        or model.encoder_revision != plan.encoder.revision
        or model.embedding_dimension != plan.encoder.embedding_dimension
    ):
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_classifier_lineage_invalid"
        )
    return model


def render_qwen_embedding_result(
    result: QwenEmbeddingPackageResult, *, output_format: str = "human"
) -> str:
    """渲染稳定机器 JSON 或面向研究者的中文训练摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise QwenEmbeddingArtifactError(
            "qwen_embedding_output_format_invalid"
        )
    report = result.acceptance_report
    baseline = report["baseline_metrics"]
    candidate = report["candidate_metrics"]
    deltas = report["candidate_minus_baseline"]
    band_comparison = result.confidence_band_comparison
    sparse_band = band_comparison["baseline"]
    band = band_comparison["candidate"]
    decision = (
        "通过训练侧验收，可进入一次验证方向复核"
        if result.acceptance_status == "passed"
        else "未通过训练侧验收，保留 sparse comparator"
    )
    return "\n".join(
        [
            "Qwen3-Embedding 语义 baseline 训练结果",
            "======================================",
            f"运行 ID：{result.run_id}",
            f"模型 ID：{result.model_id}",
            f"artifact：{'复用既有包' if result.reused else '新建并封存'}",
            f"训练证据：n={result.train_count}，leakage-group OOF {result.fold_count}折",
            "",
            "UGC 安全优先验收（固定0.5仅作诊断）",
            "  related→unrelated："
            f"sparse {baseline['related_to_unrelated_rate'] * 100:.2f}% → "
            f"Qwen {candidate['related_to_unrelated_rate'] * 100:.2f}% "
            f"({deltas['related_to_unrelated_rate'] * 100:+.2f}个百分点)",
            "  log loss："
            f"{baseline['log_loss']:.4f} → {candidate['log_loss']:.4f} "
            f"({deltas['log_loss']:+.4f})",
            "  unrelated PR-AUC："
            f"{baseline['pr_auc_unrelated']:.4f} → "
            f"{candidate['pr_auc_unrelated']:.4f} "
            f"({deltas['pr_auc_unrelated']:+.4f})",
            f"  结论：{decision}",
            "",
            "固定0.1/0.9开发概率带（不是路由阈值）",
            "  中间带："
            f"sparse {sparse_band['middle_count']} → Qwen {band['middle_count']} "
            f"({band_comparison['candidate_minus_baseline']['middle_count']:+d})",
            f"  低端误含无关：{band['low_unrelated_count']}；"
            f"高端误含UGC：{band['high_related_count']}",
            "",
            f"验证：{result.validation_status}",
            "锁定测试：locked_not_opened（未生成测试概率）",
            "路由阈值：UNSET；审计策略：UNSET；自动清洗决定：未生成。",
            "概率解释：开发样本条件诊断，不是总体候选人口后验概率。",
        ]
    )
