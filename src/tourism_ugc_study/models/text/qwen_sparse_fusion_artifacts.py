"""Qwen＋sparse 融合的只读证据集成与不可变训练运行包。"""

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

from .model_acceptance import evaluate_model_acceptance, load_model_acceptance_policy
from .qwen_embedding_artifacts import (
    load_qwen_embedding_evidence,
)
from .qwen_embedding_config import load_qwen_embedding_plan
from .qwen_head_challenger_artifacts import (
    load_cached_qwen_training_evidence,
)
from .qwen_head_challenger_config import load_qwen_head_challenger_plan
from .qwen_head_challenger import QwenHeadMember
from .qwen_sparse_fusion import (
    QwenSparseFusionResult,
    fit_qwen_sparse_fusion_nested,
)
from .qwen_sparse_fusion_config import (
    QwenSparseFusionPlan,
    load_qwen_sparse_fusion_plan,
)
from .sparse_challenger import ChallengerDocument
from .sparse_challenger_config import (
    SparseCandidateSpec,
    load_sparse_challenger_plan,
)


QWEN_SPARSE_FUSION_ALGORITHM_ID = (
    "qwen3-embedding-sparse-logit-fusion-nested-group-oof-v1"
)


class QwenSparseFusionArtifactError(RuntimeError):
    """融合谱系或不可变 artifact 失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("qwen sparse fusion artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenSparseFusionEvidence:
    """同顺序的训练文本、去标识 Qwen 成员与缓存 embedding。"""

    documents: tuple[ChallengerDocument, ...]
    members: tuple[QwenHeadMember, ...]
    embeddings: np.ndarray
    reference_manifest_sha256: str
    split_manifest_sha256: str
    split_anchor_package_manifest_sha256: str


@dataclass(frozen=True)
class QwenSparseFusionPackageResult:
    """不可变第二层运行包的去敏摘要。"""

    run_id: str
    model_id: str
    reused: bool
    train_count: int
    outer_fold_count: int
    final_inner_fold_count: int
    selected_weight: Mapping[str, Any]
    outer_weight_counts: Mapping[str, int]
    outer_qwen_head_counts: Mapping[str, int]
    acceptance_status: str
    acceptance_report: Mapping[str, Any]
    baseline_training_metrics: Mapping[str, Any]
    candidate_training_metrics: Mapping[str, Any]
    package_manifest_sha256: str
    model_artifact_sha256: str
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
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenSparseFusionArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise QwenSparseFusionArtifactError(reason_code)
    return value


def _artifact_details(path: Path, filename: str) -> Mapping[str, str]:
    """返回 manifest 使用的文件名与摘要。"""

    return {"filename": filename, "sha256": _file_sha256(path / filename)}


def _runtime_versions() -> Mapping[str, str]:
    """记录第二层训练实际使用的精确依赖版本。"""

    try:
        return {
            name: importlib.metadata.version(name)
            for name in ("joblib", "numpy", "scikit-learn", "scipy")
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_runtime_dependency_missing"
        ) from exc


def _validate_head_failure_package(
    package: str | Path, *, plan: QwenSparseFusionPlan
) -> None:
    """确认第二层只在第一层已失败且未读验证时启动。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_head_package_unavailable"
        ) from exc
    manifest_path = directory / "training-manifest.json"
    if _file_sha256(manifest_path) != plan.head_package_manifest_sha256:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_head_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "qwen_sparse_fusion_head_manifest_invalid"
    )
    lineage = manifest.get("lineage")
    if (
        manifest.get("artifact_kind") != "formal-cleaning-qwen-head-challenger"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != plan.head_run_id
        or manifest.get("model_id") != plan.head_model_id
        or manifest.get("acceptance_status") != "failed_retain_baseline"
        or manifest.get("validation_status") != "not_allowed"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or not isinstance(lineage, Mapping)
        or lineage.get("plan_sha256") != plan.head_plan_sha256
    ):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_head_manifest_invalid"
        )


def _load_sparse_spec(
    comparator_package: str | Path,
    sparse_plan_path: str | Path,
    *,
    plan: QwenSparseFusionPlan,
) -> SparseCandidateSpec:
    """校验 sparse 正式运行并返回其唯一固定候选规范。"""

    sparse_plan = load_sparse_challenger_plan(sparse_plan_path)
    if sparse_plan.plan_sha256 != plan.sparse_plan_sha256:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_sparse_plan_mismatch"
        )
    candidate = next(
        (
            item
            for item in sparse_plan.candidates
            if item.candidate_id == plan.sparse_candidate_id
        ),
        None,
    )
    if candidate is None:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_sparse_candidate_missing"
        )
    try:
        directory = Path(comparator_package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_sparse_package_unavailable"
        ) from exc
    manifest_path = directory / "training-manifest.json"
    if _file_sha256(manifest_path) != plan.sparse_package_manifest_sha256:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_sparse_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "qwen_sparse_fusion_sparse_manifest_invalid"
    )
    selected = manifest.get("selected_candidate")
    lineage = manifest.get("lineage")
    if (
        manifest.get("artifact_kind") != "formal-cleaning-sparse-challenger"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != plan.sparse_run_id
        or manifest.get("candidate_model_id") != plan.sparse_model_id
        or manifest.get("acceptance_status") != "passed"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or not isinstance(selected, Mapping)
        or selected.get("candidate_id") != plan.sparse_candidate_id
        or not isinstance(lineage, Mapping)
        or lineage.get("plan_sha256") != plan.sparse_plan_sha256
    ):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_sparse_manifest_invalid"
        )
    return candidate


def load_qwen_sparse_fusion_evidence(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    split_anchor_package: str | Path,
    sparse_plan_path: str | Path,
    qwen_source_package: str | Path,
    *,
    fusion_plan: QwenSparseFusionPlan,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    qwen_base_plan_path: str | Path,
    head_plan_path: str | Path,
) -> QwenSparseFusionEvidence:
    """只读加载训练文本并与缓存4B embedding 按成员键对齐。

    Args:
        csv_path: 唯一最终700条参考 CSV。
        reference_manifest_path: 与 CSV 配对的 finalized manifest。
        derived_db: 只读派生 SQLite。
        split_anchor_package: 冻结切分所在 baseline 包。
        sparse_plan_path: 已完成 sparse challenger 的冻结计划。
        qwen_source_package: 首次4B不可变训练包。
        fusion_plan: 第二层预登记计划。
        config: 稳定清洗配置。
        normalization_config: 冻结文本规范化配置。
        qwen_base_plan_path: 首次4B计划。
        head_plan_path: 第一层56候选计划。

    Returns:
        同顺序的训练文档、去标识成员和缓存 embedding。

    Raises:
        QwenSparseFusionArtifactError: 计划、数据或成员谱系漂移。
    """

    base_plan = load_qwen_embedding_plan(qwen_base_plan_path)
    head_plan = load_qwen_head_challenger_plan(head_plan_path)
    if (
        base_plan.plan_sha256 != fusion_plan.qwen_base_plan_sha256
        or head_plan.plan_sha256 != fusion_plan.head_plan_sha256
        or base_plan.reference_csv_sha256 != fusion_plan.reference_csv_sha256
        or base_plan.train_manifest_sha256 != fusion_plan.train_manifest_sha256
        or base_plan.validation_manifest_sha256
        != fusion_plan.validation_manifest_sha256
        or base_plan.test_manifest_sha256 != fusion_plan.test_manifest_sha256
        or base_plan.split_anchor_model_id != fusion_plan.split_anchor_model_id
    ):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_plan_binding_mismatch"
        )
    text_evidence = load_qwen_embedding_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        split_anchor_package,
        sparse_plan_path,
        plan=base_plan,
        config=config,
        normalization_config=normalization_config,
    )
    cached = load_cached_qwen_training_evidence(
        qwen_source_package, plan=head_plan
    )
    by_key = {item.member_key: item for item in text_evidence.documents}
    if (
        len(by_key) != len(text_evidence.documents)
        or set(by_key) != {item.member_key for item in cached.members}
    ):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_members_mismatch"
        )
    documents = tuple(by_key[item.member_key] for item in cached.members)
    for document, member in zip(documents, cached.members, strict=True):
        if (
            document.component_id != member.component_id
            or document.tourism_label != member.tourism_label
        ):
            raise QwenSparseFusionArtifactError(
                "qwen_sparse_fusion_lineage_mismatch"
            )
    return QwenSparseFusionEvidence(
        documents=documents,
        members=cached.members,
        embeddings=cached.embeddings,
        reference_manifest_sha256=text_evidence.reference_manifest_sha256,
        split_manifest_sha256=text_evidence.split_manifest_sha256,
        split_anchor_package_manifest_sha256=(
            text_evidence.split_anchor_package_manifest_sha256
        ),
    )


def _run_identity(
    plan: QwenSparseFusionPlan,
    *,
    reference_manifest_sha256: str,
    split_manifest_sha256: str,
    runtime_versions: Mapping[str, str],
    code_version: str,
) -> str:
    """在训练前计算内容寻址运行身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": QWEN_SPARSE_FUSION_ALGORITHM_ID,
                "code_version": code_version,
                "plan_sha256": plan.plan_sha256,
                "reference_manifest_sha256": reference_manifest_sha256,
                "split_manifest_sha256": split_manifest_sha256,
                "runtime_versions": dict(runtime_versions),
            }
        )
    )[:32]


def _model_identity(run_id: str, result: QwenSparseFusionResult) -> str:
    """生成不依赖 joblib 字节的稳定融合模型身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "run_id": run_id,
                "selected_weight_id": result.selected_weight.weight_id,
                "qwen_head_candidate_id": (
                    result.full_training_qwen_head_candidate_id
                ),
                "fit_scope": "frozen_train_only",
            }
        )
    )[:32]


def _result_from_manifest(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> QwenSparseFusionPackageResult:
    """从已校验 manifest 构造去敏返回值。"""

    report = manifest["training_report"]
    artifacts = manifest["artifacts"]
    return QwenSparseFusionPackageResult(
        run_id=str(manifest["run_id"]),
        model_id=str(manifest["model_id"]),
        reused=reused,
        train_count=int(manifest["train_count"]),
        outer_fold_count=int(manifest["outer_fold_count"]),
        final_inner_fold_count=int(manifest["final_inner_fold_count"]),
        selected_weight=dict(report["selected_weight"]),
        outer_weight_counts=dict(report["outer_weight_counts"]),
        outer_qwen_head_counts=dict(report["outer_qwen_head_counts"]),
        acceptance_status=str(manifest["acceptance_status"]),
        acceptance_report=dict(report["acceptance"]),
        baseline_training_metrics=dict(report["baseline_training_metrics"]),
        candidate_training_metrics=dict(report["candidate_training_metrics"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        model_artifact_sha256=str(artifacts["model"]["sha256"]),
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
    expected_manifest_sha256: str,
    plan: QwenSparseFusionPlan,
) -> QwenSparseFusionPackageResult:
    """完整校验并复用既有不可变第二层运行包。"""

    manifest_path = package_dir / "training-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_package_manifest_unreadable"
        ) from exc
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-qwen-sparse-fusion"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != expected_run_id
        or manifest.get("algorithm_id") != QWEN_SPARSE_FUSION_ALGORITHM_ID
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
        or not isinstance(manifest.get("artifacts"), Mapping)
        or not isinstance(manifest.get("lineage"), Mapping)
        or manifest["lineage"].get("plan_sha256") != plan.plan_sha256
    ):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_package_manifest_invalid"
        )
    artifacts = manifest["artifacts"]
    expected_files = {
        "model": "model.joblib",
        "paired_oof": "paired-oof.json",
        "outer_selections": "outer-selections.json",
        "weight_scores": "weight-scores.json",
        "report": "training-report.json",
        "plan": "plan.yaml",
        "acceptance_policy": "acceptance-policy.yaml",
    }
    if set(artifacts) != set(expected_files):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_package_manifest_invalid"
        )
    for name, filename in expected_files.items():
        details = artifacts[name]
        artifact_path = package_dir / filename
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or not isinstance(details.get("sha256"), str)
            or artifact_path.is_symlink()
            or artifact_path.resolve().parent != package_dir.resolve()
            or _file_sha256(artifact_path) != details["sha256"]
        ):
            raise QwenSparseFusionArtifactError(
                "qwen_sparse_fusion_package_artifact_hash_mismatch"
            )
    report = _load_json(
        package_dir / "training-report.json",
        "qwen_sparse_fusion_package_report_invalid",
    )
    if report != manifest.get("training_report"):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_package_report_invalid"
        )
    return _result_from_manifest(manifest, manifest_bytes, reused=True)


def train_qwen_sparse_fusion_package(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    split_anchor_package: str | Path,
    sparse_plan_path: str | Path,
    sparse_package: str | Path,
    qwen_source_package: str | Path,
    head_package: str | Path,
    qwen_base_plan_path: str | Path,
    head_plan_path: str | Path,
    fusion_plan_path: str | Path,
    acceptance_policy_path: str | Path,
    artifact_root: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> QwenSparseFusionPackageResult:
    """执行第二层 nested OOF、验收并原子封存不可变运行包。"""

    version = code_version.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_code_version_invalid"
        )
    plan = load_qwen_sparse_fusion_plan(fusion_plan_path)
    head_plan = load_qwen_head_challenger_plan(head_plan_path)
    policy = load_model_acceptance_policy(acceptance_policy_path)
    if (
        head_plan.plan_sha256 != plan.head_plan_sha256
        or policy.policy_sha256 != plan.acceptance_policy_sha256
        or policy.baseline_model_id != plan.sparse_model_id
        or policy.reference_csv_sha256 != plan.reference_csv_sha256
        or policy.train_manifest_sha256 != plan.train_manifest_sha256
        or policy.validation_manifest_sha256 != plan.validation_manifest_sha256
        or policy.test_manifest_sha256 != plan.test_manifest_sha256
        or policy.evidence_scope != "train_nested_group_oof"
        or policy.paired_outer_folds is not True
    ):
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_acceptance_binding_mismatch"
        )
    _validate_head_failure_package(head_package, plan=plan)
    sparse_spec = _load_sparse_spec(
        sparse_package, sparse_plan_path, plan=plan
    )
    evidence = load_qwen_sparse_fusion_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        split_anchor_package,
        sparse_plan_path,
        qwen_source_package,
        fusion_plan=plan,
        config=config,
        normalization_config=normalization_config,
        qwen_base_plan_path=qwen_base_plan_path,
        head_plan_path=head_plan_path,
    )
    runtime_versions = _runtime_versions()
    run_id = _run_identity(
        plan,
        reference_manifest_sha256=evidence.reference_manifest_sha256,
        split_manifest_sha256=evidence.split_manifest_sha256,
        runtime_versions=runtime_versions,
        code_version=version,
    )
    root = Path(artifact_root).expanduser().resolve()
    package_dir = root / run_id
    if package_dir.exists():
        if expected_existing_manifest_sha256 is None:
            raise QwenSparseFusionArtifactError(
                "qwen_sparse_fusion_existing_package_requires_manifest_hash"
            )
        return _validate_existing_package(
            package_dir,
            expected_run_id=run_id,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            plan=plan,
        )
    result = fit_qwen_sparse_fusion_nested(
        evidence.documents,
        evidence.members,
        evidence.embeddings,
        sparse_spec=sparse_spec,
        head_plan=head_plan,
        fusion_plan=plan,
    )
    model_id = _model_identity(run_id, result)
    acceptance = evaluate_model_acceptance(
        result.paired_outer_oof, policy, candidate_model_id=model_id
    )
    weight_counts: dict[str, int] = {}
    head_counts: dict[str, int] = {}
    for selection in result.outer_selections:
        weight_key = f"qwen_weight={selection.selected_qwen_weight:g}"
        weight_counts[weight_key] = weight_counts.get(weight_key, 0) + 1
        head_counts[selection.qwen_head_candidate_id] = (
            head_counts.get(selection.qwen_head_candidate_id, 0) + 1
        )
    report = {
        "artifact_kind": "formal-cleaning-qwen-sparse-fusion-training-report",
        "selected_weight": asdict(result.selected_weight),
        "full_training_qwen_head_candidate_id": (
            result.full_training_qwen_head_candidate_id
        ),
        "outer_weight_counts": dict(sorted(weight_counts.items())),
        "outer_qwen_head_counts": dict(sorted(head_counts.items())),
        "baseline_training_metrics": dict(result.baseline_training_metrics),
        "candidate_training_metrics": dict(result.candidate_training_metrics),
        "acceptance": dict(acceptance),
        "paired_outer_folds": True,
        "sparse_search": False,
        "validation_labels_read": False,
        "test_probabilities_present": False,
        "diagnostic_cutoff_is_routing_threshold": False,
        "threshold_status": "UNSET",
        "audit_status": "UNSET",
    }
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.", dir=root)
    )
    try:
        joblib.dump(result.selected_model, temporary / "model.joblib")
        (temporary / "paired-oof.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-sparse-fusion-paired-oof",
                    "baseline_model_id": plan.sparse_model_id,
                    "candidate_model_id": model_id,
                    "records": [asdict(item) for item in result.paired_outer_oof],
                    "paired_outer_folds": True,
                    "platform_used": False,
                }
            )
        )
        (temporary / "outer-selections.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-sparse-fusion-outer-selections",
                    "model_id": model_id,
                    "records": [asdict(item) for item in result.outer_selections],
                    "platform_used": False,
                }
            )
        )
        (temporary / "weight-scores.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-sparse-fusion-weight-scores",
                    "model_id": model_id,
                    "qwen_head_candidate_id": (
                        result.full_training_qwen_head_candidate_id
                    ),
                    "records": [
                        asdict(item) for item in result.full_training_weight_scores
                    ],
                    "platform_used": False,
                }
            )
        )
        (temporary / "training-report.json").write_bytes(
            _canonical_bytes(report)
        )
        shutil.copyfile(fusion_plan_path, temporary / "plan.yaml")
        shutil.copyfile(
            acceptance_policy_path, temporary / "acceptance-policy.yaml"
        )
        artifacts = {
            "model": _artifact_details(temporary, "model.joblib"),
            "paired_oof": _artifact_details(temporary, "paired-oof.json"),
            "outer_selections": _artifact_details(
                temporary, "outer-selections.json"
            ),
            "weight_scores": _artifact_details(
                temporary, "weight-scores.json"
            ),
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
            "artifact_kind": "formal-cleaning-qwen-sparse-fusion",
            "artifact_status": "immutable",
            "run_id": run_id,
            "model_id": model_id,
            "algorithm_id": QWEN_SPARSE_FUSION_ALGORITHM_ID,
            "train_count": len(evidence.documents),
            "outer_fold_count": result.outer_fold_count,
            "final_inner_fold_count": result.final_inner_fold_count,
            "selected_weight_id": result.selected_weight.weight_id,
            "acceptance_status": acceptance["status"],
            "validation_status": validation_status,
            "test_status": "locked_not_opened",
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
                "reference_manifest_sha256": (
                    evidence.reference_manifest_sha256
                ),
                "train_manifest_sha256": plan.train_manifest_sha256,
                "validation_manifest_sha256": plan.validation_manifest_sha256,
                "test_manifest_sha256": plan.test_manifest_sha256,
                "split_manifest_sha256": evidence.split_manifest_sha256,
                "split_anchor_model_id": plan.split_anchor_model_id,
                "split_anchor_package_manifest_sha256": (
                    evidence.split_anchor_package_manifest_sha256
                ),
                "qwen_source_run_id": plan.qwen_source_run_id,
                "qwen_source_model_id": plan.qwen_source_model_id,
                "qwen_source_package_manifest_sha256": (
                    plan.qwen_source_package_manifest_sha256
                ),
                "qwen_source_embeddings_sha256": (
                    plan.qwen_source_embeddings_sha256
                ),
                "head_run_id": plan.head_run_id,
                "head_model_id": plan.head_model_id,
                "head_package_manifest_sha256": (
                    plan.head_package_manifest_sha256
                ),
                "head_plan_sha256": plan.head_plan_sha256,
                "sparse_run_id": plan.sparse_run_id,
                "sparse_model_id": plan.sparse_model_id,
                "sparse_package_manifest_sha256": (
                    plan.sparse_package_manifest_sha256
                ),
                "sparse_candidate_id": plan.sparse_candidate_id,
                "sparse_plan_sha256": plan.sparse_plan_sha256,
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
            raise QwenSparseFusionArtifactError(
                "qwen_sparse_fusion_package_publish_failed"
            ) from exc
        temporary = None
        return _result_from_manifest(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def render_qwen_sparse_fusion_result(
    result: QwenSparseFusionPackageResult, *, output_format: str = "human"
) -> str:
    """渲染稳定机器 JSON 或面向研究者的中文第二层摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise QwenSparseFusionArtifactError(
            "qwen_sparse_fusion_output_format_invalid"
        )
    acceptance = result.acceptance_report
    baseline = acceptance["baseline_metrics"]
    candidate = acceptance["candidate_metrics"]
    deltas = acceptance["candidate_minus_baseline"]
    decision = (
        "通过训练侧验收；停止扩展并另行进行一次验证方向复核"
        if result.acceptance_status == "passed"
        else "未通过训练侧验收；按 Issue #45 进入英文 instruction＋head-tail"
    )
    weights = ", ".join(
        f"{key}×{value}" for key, value in result.outer_weight_counts.items()
    )
    return "\n".join(
        [
            "Sparse＋Qwen3-Embedding-4B 无泄漏融合训练结果",
            "================================================",
            f"运行 ID：{result.run_id}",
            f"模型 ID：{result.model_id}",
            f"artifact：{'复用既有包' if result.reused else '新建并封存'}",
            f"训练证据：n={result.train_count}；同外层 paired nested group OOF "
            f"外{result.outer_fold_count}/内{result.final_inner_fold_count}折",
            "",
            "全训练端最终选择",
            f"  Qwen logit 权重：{result.selected_weight['qwen_weight']:g}",
            f"  权重 ID：{result.selected_weight['weight_id']}",
            f"  外层权重分布：{weights}",
            "  sparse 网格：未重新搜索；Qwen头：每个外层训练端内重选",
            "",
            "UGC 安全优先验收（固定0.5仅作诊断）",
            "  related→unrelated："
            f"sparse {baseline['related_to_unrelated_rate'] * 100:.2f}% → "
            f"fusion {candidate['related_to_unrelated_rate'] * 100:.2f}% "
            f"({deltas['related_to_unrelated_rate'] * 100:+.2f}个百分点)",
            "  log loss："
            f"{baseline['log_loss']:.4f} → {candidate['log_loss']:.4f} "
            f"({deltas['log_loss']:+.4f})",
            "  unrelated PR-AUC："
            f"{baseline['pr_auc_unrelated']:.4f} → "
            f"{candidate['pr_auc_unrelated']:.4f} "
            f"({deltas['pr_auc_unrelated']:+.4f})",
            "  Brier："
            f"{baseline['brier_score']:.4f} → {candidate['brier_score']:.4f} "
            f"({deltas['brier_score']:+.4f})",
            f"  结论：{decision}",
            "",
            f"验证：{result.validation_status}",
            "锁定测试：locked_not_opened（未生成测试概率）",
            "路由阈值：UNSET；审计策略：UNSET；自动清洗决定：未生成。",
        ]
    )
