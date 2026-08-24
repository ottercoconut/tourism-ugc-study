"""Qwen 缓存向量分类头 challenger 的证据加载与不可变运行包。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np

from .model_acceptance import (
    PairedOofObservation,
    evaluate_model_acceptance,
    load_model_acceptance_policy,
)
from .qwen_embedding_artifacts import load_sparse_comparator_oof
from .qwen_embedding_config import QwenEmbeddingPlan, load_qwen_embedding_plan
from .qwen_head_challenger import (
    FrozenQwenHeadModel,
    QwenHeadChallengerError,
    QwenHeadMember,
    QwenHeadOofProbability,
    fit_qwen_head_challenger_nested,
)
from .qwen_head_challenger_config import (
    QwenHeadChallengerPlan,
    load_qwen_head_challenger_plan,
)


QWEN_HEAD_CHALLENGER_ALGORITHM_ID = (
    "qwen3-embedding-mrl-linear-head-nested-group-oof-v1"
)


class QwenHeadChallengerArtifactError(RuntimeError):
    """分类头 challenger 谱系或 artifact 失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含文本、成员身份或本机路径的稳定失败码。"""

        super().__init__("qwen head challenger artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class CachedQwenTrainingEvidence:
    """从封存4B包读取且不含正文的训练成员与向量。"""

    members: tuple[QwenHeadMember, ...]
    embeddings: np.ndarray
    source_manifest_sha256: str
    source_embeddings_sha256: str
    source_training_oof_sha256: str


@dataclass(frozen=True)
class QwenHeadChallengerPackageResult:
    """不可变第一层运行包的去敏摘要。"""

    run_id: str
    model_id: str
    reused: bool
    train_count: int
    candidate_count: int
    outer_fold_count: int
    final_inner_fold_count: int
    selected_candidate: Mapping[str, Any]
    outer_selection_counts: Mapping[str, int]
    acceptance_status: str
    acceptance_report: Mapping[str, Any]
    training_metrics: Mapping[str, Any]
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
        raise QwenHeadChallengerArtifactError(
            "qwen_head_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenHeadChallengerArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise QwenHeadChallengerArtifactError(reason_code)
    return value


def _artifact_details(path: Path, filename: str) -> Mapping[str, str]:
    """返回 manifest 使用的文件名与摘要。"""

    return {"filename": filename, "sha256": _file_sha256(path / filename)}


def _runtime_versions() -> Mapping[str, str]:
    """记录分类头训练实际使用的精确依赖版本。"""

    try:
        return {
            name: importlib.metadata.version(name)
            for name in ("joblib", "numpy", "scikit-learn", "scipy")
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise QwenHeadChallengerArtifactError(
            "qwen_head_runtime_dependency_missing"
        ) from exc


def load_cached_qwen_training_evidence(
    source_package: str | Path,
    *,
    plan: QwenHeadChallengerPlan,
) -> CachedQwenTrainingEvidence:
    """校验4B baseline 包并读取训练 embedding 与去标识 OOF 成员。

    Args:
        source_package: 已封存4B baseline 运行目录。
        plan: 绑定该运行、manifest 和两个输入 artifact 的第一层计划。

    Returns:
        以 embedding 文件顺序对齐的442条成员与2560维向量。

    Raises:
        QwenHeadChallengerArtifactError: 路径、哈希、谱系或成员对齐漂移。

    Notes:
        本函数不读取正文、验证概率或测试成员。NPZ 禁止 pickle，避免把
        不可信对象反序列化进训练进程。
    """

    try:
        directory = Path(source_package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_package_unavailable"
        ) from exc
    manifest_path = directory / "training-manifest.json"
    if _file_sha256(manifest_path) != plan.source_package_manifest_sha256:
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "qwen_head_source_manifest_invalid"
    )
    artifacts = manifest.get("artifacts")
    lineage = manifest.get("lineage")
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-qwen-embedding-baseline"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != plan.source_run_id
        or manifest.get("model_id") != plan.source_model_id
        or manifest.get("train_count") != 442
        or manifest.get("validation_status") != "not_allowed"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("platform_used") is not False
        or not isinstance(artifacts, Mapping)
        or not isinstance(lineage, Mapping)
        or lineage.get("plan_sha256") != plan.source_plan_sha256
    ):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_manifest_invalid"
        )
    embedding_details = artifacts.get("embeddings")
    oof_details = artifacts.get("training_oof")
    if (
        not isinstance(embedding_details, Mapping)
        or not isinstance(oof_details, Mapping)
        or embedding_details.get("sha256") != plan.source_embeddings_sha256
        or oof_details.get("sha256") != plan.source_training_oof_sha256
        or embedding_details.get("filename") != "train-embeddings.npz"
        or oof_details.get("filename") != "training-oof.json"
    ):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_artifacts_invalid"
        )
    embedding_path = directory / "train-embeddings.npz"
    oof_path = directory / "training-oof.json"
    if (
        _file_sha256(embedding_path) != plan.source_embeddings_sha256
        or _file_sha256(oof_path) != plan.source_training_oof_sha256
    ):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_artifact_hash_mismatch"
        )
    oof_payload = _load_json(oof_path, "qwen_head_source_oof_invalid")
    raw_records = oof_payload.get("records")
    if (
        oof_payload.get("artifact_kind")
        != "formal-cleaning-qwen-group-oof"
        or oof_payload.get("model_id") != plan.source_model_id
        or oof_payload.get("platform_used") is not False
        or not isinstance(raw_records, list)
        or len(raw_records) != 442
    ):
        raise QwenHeadChallengerArtifactError("qwen_head_source_oof_invalid")
    by_key: dict[str, QwenHeadMember] = {}
    try:
        for raw in raw_records:
            if not isinstance(raw, Mapping):
                raise TypeError
            member = QwenHeadMember(
                member_key=str(raw["member_key"]),
                component_id=str(raw["component_id"]),
                tourism_label=str(raw["tourism_label"]),
            )
            if (
                not member.member_key
                or member.member_key in by_key
                or not member.component_id
                or member.tourism_label not in {"related", "unrelated"}
                or not np.isfinite(float(raw["p_unrelated"]))
                or int(raw["fold_index"]) < 0
            ):
                raise ValueError
            by_key[member.member_key] = member
    except (KeyError, TypeError, ValueError) as exc:
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_oof_invalid"
        ) from exc
    try:
        with np.load(embedding_path, allow_pickle=False) as payload:
            if set(payload.files) != {"embeddings", "member_keys"}:
                raise ValueError
            embeddings = np.asarray(payload["embeddings"], dtype=np.float32)
            member_keys = [str(value) for value in payload["member_keys"].tolist()]
    except (OSError, ValueError, TypeError) as exc:
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_embeddings_invalid"
        ) from exc
    if (
        embeddings.shape != (442, 2560)
        or len(member_keys) != 442
        or len(set(member_keys)) != 442
        or set(member_keys) != set(by_key)
        or not np.isfinite(embeddings).all()
        or not np.allclose(
            np.linalg.norm(embeddings, axis=1), 1.0, atol=1e-5, rtol=1e-5
        )
    ):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_source_embeddings_invalid"
        )
    return CachedQwenTrainingEvidence(
        members=tuple(by_key[key] for key in member_keys),
        embeddings=embeddings,
        source_manifest_sha256=plan.source_package_manifest_sha256,
        source_embeddings_sha256=plan.source_embeddings_sha256,
        source_training_oof_sha256=plan.source_training_oof_sha256,
    )


def pair_qwen_head_with_sparse_comparator(
    qwen_oof: Sequence[QwenHeadOofProbability],
    sparse_oof: Sequence[PairedOofObservation],
) -> tuple[PairedOofObservation, ...]:
    """把第一层 nested OOF 与已封存 sparse candidate OOF 逐成员配对。"""

    qwen_by_key = {item.member_key: item for item in qwen_oof}
    sparse_by_key = {item.member_key: item for item in sparse_oof}
    if (
        len(qwen_by_key) != len(qwen_oof)
        or len(sparse_by_key) != len(sparse_oof)
        or set(qwen_by_key) != set(sparse_by_key)
    ):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_comparator_members_mismatch"
        )
    paired: list[PairedOofObservation] = []
    for member_key in sorted(qwen_by_key):
        qwen = qwen_by_key[member_key]
        sparse = sparse_by_key[member_key]
        if (
            qwen.component_id != sparse.component_id
            or qwen.tourism_label != sparse.tourism_label
        ):
            raise QwenHeadChallengerArtifactError(
                "qwen_head_comparator_lineage_mismatch"
            )
        paired.append(
            PairedOofObservation(
                member_key=member_key,
                component_id=qwen.component_id,
                tourism_label=qwen.tourism_label,
                baseline_p_unrelated=sparse.candidate_p_unrelated,
                candidate_p_unrelated=qwen.p_unrelated,
            )
        )
    return tuple(paired)


def _run_identity(
    plan: QwenHeadChallengerPlan,
    *,
    acceptance_policy_sha256: str,
    comparator_paired_oof_sha256: str,
    runtime_versions: Mapping[str, str],
    code_version: str,
) -> str:
    """在拟合前计算内容寻址运行身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": QWEN_HEAD_CHALLENGER_ALGORITHM_ID,
                "code_version": code_version,
                "plan_sha256": plan.plan_sha256,
                "source_package_manifest_sha256": (
                    plan.source_package_manifest_sha256
                ),
                "source_embeddings_sha256": plan.source_embeddings_sha256,
                "acceptance_policy_sha256": acceptance_policy_sha256,
                "comparator_paired_oof_sha256": comparator_paired_oof_sha256,
                "runtime_versions": dict(runtime_versions),
            }
        )
    )[:32]


def _model_identity(run_id: str, candidate_id: str) -> str:
    """生成不依赖 joblib 字节的稳定模型身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "run_id": run_id,
                "selected_candidate_id": candidate_id,
                "fit_scope": "frozen_train_only",
            }
        )
    )[:32]


def _result_from_manifest(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> QwenHeadChallengerPackageResult:
    """从已校验 manifest 构造去敏返回值。"""

    report = manifest["training_report"]
    artifacts = manifest["artifacts"]
    return QwenHeadChallengerPackageResult(
        run_id=str(manifest["run_id"]),
        model_id=str(manifest["model_id"]),
        reused=reused,
        train_count=int(manifest["train_count"]),
        candidate_count=int(manifest["candidate_count"]),
        outer_fold_count=int(manifest["outer_fold_count"]),
        final_inner_fold_count=int(manifest["final_inner_fold_count"]),
        selected_candidate=dict(report["selected_candidate"]),
        outer_selection_counts=dict(report["outer_selection_counts"]),
        acceptance_status=str(manifest["acceptance_status"]),
        acceptance_report=dict(report["acceptance"]),
        training_metrics=dict(report["training_metrics"]),
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
    plan: QwenHeadChallengerPlan,
) -> QwenHeadChallengerPackageResult:
    """完整校验并复用既有不可变第一层运行包。"""

    manifest_path = package_dir / "training-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise QwenHeadChallengerArtifactError(
            "qwen_head_package_manifest_unreadable"
        ) from exc
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-qwen-head-challenger"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != expected_run_id
        or manifest.get("algorithm_id") != QWEN_HEAD_CHALLENGER_ALGORITHM_ID
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
        raise QwenHeadChallengerArtifactError(
            "qwen_head_package_manifest_invalid"
        )
    artifacts = manifest["artifacts"]
    expected_files = {
        "model": "model.joblib",
        "training_oof": "training-oof.json",
        "paired_oof": "paired-oof.json",
        "candidate_scores": "candidate-scores.json",
        "report": "training-report.json",
        "plan": "plan.yaml",
        "acceptance_policy": "acceptance-policy.yaml",
    }
    if set(artifacts) != set(expected_files):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_package_manifest_invalid"
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
            raise QwenHeadChallengerArtifactError(
                "qwen_head_package_artifact_hash_mismatch"
            )
    report = _load_json(
        package_dir / "training-report.json", "qwen_head_package_report_invalid"
    )
    if report != manifest.get("training_report"):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_package_report_invalid"
        )
    return _result_from_manifest(manifest, manifest_bytes, reused=True)


def train_qwen_head_challenger_package(
    source_package: str | Path,
    comparator_package: str | Path,
    base_plan_path: str | Path,
    plan_path: str | Path,
    acceptance_policy_path: str | Path,
    artifact_root: str | Path,
    *,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> QwenHeadChallengerPackageResult:
    """执行第一层 nested OOF、验收并原子封存不可变运行包。

    入口只接收已有训练 artifact，不接收原始正文、验证、测试、平台、阈值
    或审计参数。训练侧通过只表示可另行进行一次方向复核。
    """

    version = code_version.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_code_version_invalid"
        )
    plan = load_qwen_head_challenger_plan(plan_path)
    base_plan: QwenEmbeddingPlan = load_qwen_embedding_plan(base_plan_path)
    policy = load_model_acceptance_policy(acceptance_policy_path)
    if (
        base_plan.plan_sha256 != plan.source_plan_sha256
        or base_plan.acceptance_policy_sha256 != policy.policy_sha256
        or policy.baseline_model_id != base_plan.comparator_model_id
        or policy.reference_csv_sha256 != base_plan.reference_csv_sha256
        or policy.train_manifest_sha256 != base_plan.train_manifest_sha256
        or policy.validation_manifest_sha256
        != base_plan.validation_manifest_sha256
        or policy.test_manifest_sha256 != base_plan.test_manifest_sha256
    ):
        raise QwenHeadChallengerArtifactError(
            "qwen_head_plan_binding_mismatch"
        )
    evidence = load_cached_qwen_training_evidence(source_package, plan=plan)
    sparse_oof = load_sparse_comparator_oof(comparator_package, plan=base_plan)
    runtime_versions = _runtime_versions()
    run_id = _run_identity(
        plan,
        acceptance_policy_sha256=policy.policy_sha256,
        comparator_paired_oof_sha256=base_plan.comparator_paired_oof_sha256,
        runtime_versions=runtime_versions,
        code_version=version,
    )
    root = Path(artifact_root).expanduser().resolve()
    package_dir = root / run_id
    if package_dir.exists():
        if expected_existing_manifest_sha256 is None:
            raise QwenHeadChallengerArtifactError(
                "qwen_head_existing_package_requires_manifest_hash"
            )
        return _validate_existing_package(
            package_dir,
            expected_run_id=run_id,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            plan=plan,
        )
    result = fit_qwen_head_challenger_nested(
        evidence.members, evidence.embeddings, plan=plan
    )
    model_id = _model_identity(run_id, result.selected_spec.candidate_id)
    paired = pair_qwen_head_with_sparse_comparator(
        result.oof_probabilities, sparse_oof
    )
    acceptance = evaluate_model_acceptance(
        paired, policy, candidate_model_id=model_id
    )
    selection_counts: dict[str, int] = {}
    for selection in result.outer_selections:
        selection_counts[selection.selected_candidate_id] = (
            selection_counts.get(selection.selected_candidate_id, 0) + 1
        )
    selected_candidate = asdict(result.selected_spec)
    report = {
        "artifact_kind": "formal-cleaning-qwen-head-training-report",
        "selected_candidate": selected_candidate,
        "outer_selection_counts": dict(sorted(selection_counts.items())),
        "training_metrics": dict(result.training_metrics),
        "acceptance": dict(acceptance),
        "candidate_count": len(plan.candidates),
        "selection_protocol": "nested_group_oof_ugc_safety_first",
        "internal_safety_anchor_candidate_id": plan.safety_anchor_candidate_id,
        "comparator_paired_outer_folds": False,
        "diagnostic_cutoff_is_routing_threshold": False,
        "validation_labels_read": False,
        "test_probabilities_present": False,
        "threshold_status": "UNSET",
        "audit_status": "UNSET",
    }
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.", dir=root)
    )
    try:
        joblib.dump(result.selected_model, temporary / "model.joblib")
        (temporary / "training-oof.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-head-nested-oof",
                    "model_id": model_id,
                    "records": [asdict(item) for item in result.oof_probabilities],
                    "platform_used": False,
                }
            )
        )
        (temporary / "paired-oof.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-head-paired-oof",
                    "baseline_model_id": base_plan.comparator_model_id,
                    "candidate_model_id": model_id,
                    "records": [asdict(item) for item in paired],
                    "paired_outer_folds": False,
                    "common_members_and_group_scheme": True,
                    "platform_used": False,
                }
            )
        )
        (temporary / "candidate-scores.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-head-candidate-scores",
                    "model_id": model_id,
                    "safety_anchor": asdict(result.full_training_anchor_score),
                    "candidates": [
                        asdict(item)
                        for item in result.full_training_candidate_scores
                    ],
                    "outer_selections": [
                        asdict(item) for item in result.outer_selections
                    ],
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
            "model": _artifact_details(temporary, "model.joblib"),
            "training_oof": _artifact_details(temporary, "training-oof.json"),
            "paired_oof": _artifact_details(temporary, "paired-oof.json"),
            "candidate_scores": _artifact_details(
                temporary, "candidate-scores.json"
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
            "artifact_kind": "formal-cleaning-qwen-head-challenger",
            "artifact_status": "immutable",
            "run_id": run_id,
            "model_id": model_id,
            "algorithm_id": QWEN_HEAD_CHALLENGER_ALGORITHM_ID,
            "train_count": len(evidence.members),
            "candidate_count": len(plan.candidates),
            "outer_fold_count": result.outer_fold_count,
            "final_inner_fold_count": result.final_inner_fold_count,
            "selected_candidate_id": result.selected_spec.candidate_id,
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
                "source_run_id": plan.source_run_id,
                "source_model_id": plan.source_model_id,
                "source_package_manifest_sha256": (
                    evidence.source_manifest_sha256
                ),
                "source_embeddings_sha256": evidence.source_embeddings_sha256,
                "source_training_oof_sha256": (
                    evidence.source_training_oof_sha256
                ),
                "base_plan_sha256": base_plan.plan_sha256,
                "acceptance_policy_sha256": policy.policy_sha256,
                "comparator_run_id": base_plan.comparator_run_id,
                "comparator_model_id": base_plan.comparator_model_id,
                "comparator_paired_oof_sha256": (
                    base_plan.comparator_paired_oof_sha256
                ),
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
            raise QwenHeadChallengerArtifactError(
                "qwen_head_package_publish_failed"
            ) from exc
        temporary = None
        return _result_from_manifest(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def render_qwen_head_challenger_result(
    result: QwenHeadChallengerPackageResult, *, output_format: str = "human"
) -> str:
    """渲染稳定机器 JSON 或面向研究者的中文第一层摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise QwenHeadChallengerArtifactError(
            "qwen_head_output_format_invalid"
        )
    acceptance = result.acceptance_report
    baseline = acceptance["baseline_metrics"]
    candidate = acceptance["candidate_metrics"]
    deltas = acceptance["candidate_minus_baseline"]
    selected = result.selected_candidate
    decision = (
        "通过训练侧验收；停止扩展并另行进行一次验证方向复核"
        if result.acceptance_status == "passed"
        else "未通过训练侧验收；按 Issue #45 进入第二层无泄漏融合"
    )
    class_weight = selected["class_weight"] or "none"
    outer_counts = ", ".join(
        f"{key[:8]}…×{value}"
        for key, value in result.outer_selection_counts.items()
    )
    return "\n".join(
        [
            "Qwen3-Embedding-4B 分类头 challenger 训练结果",
            "============================================",
            f"运行 ID：{result.run_id}",
            f"模型 ID：{result.model_id}",
            f"artifact：{'复用既有包' if result.reused else '新建并封存'}",
            f"训练证据：n={result.train_count}；候选={result.candidate_count}；"
            f"nested group OOF 外{result.outer_fold_count}/内{result.final_inner_fold_count}折",
            "",
            "全训练端最终选择",
            f"  模型族：{selected['family']}；MRL维度：{selected['embedding_dimension']}",
            f"  C：{selected['C']:g}；class_weight：{class_weight}",
            f"  候选参数 ID：{selected['candidate_id']}",
            f"  外层选择分布：{outer_counts}",
            "",
            "UGC 安全优先验收（固定0.5仅作诊断）",
            "  related→unrelated："
            f"sparse {baseline['related_to_unrelated_rate'] * 100:.2f}% → "
            f"candidate {candidate['related_to_unrelated_rate'] * 100:.2f}% "
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
