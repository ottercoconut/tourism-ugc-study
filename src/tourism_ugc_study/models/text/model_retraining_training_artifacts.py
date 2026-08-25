"""固定三候选重训证据、模型与OOF的不可变运行包。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import joblib

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_embeddings import load_retraining_embeddings_package
from .model_retraining_snapshot_artifacts import load_retraining_snapshot_package
from .model_retraining_training import (
    FrozenRetrainingCandidate,
    ModelRetrainingTrainingError,
    ModelRetrainingTrainingResult,
    RetrainingOofObservation,
    train_fixed_retraining_candidates,
)


class ModelRetrainingTrainingArtifactError(RuntimeError):
    """固定候选运行包构建或复用失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、身份、标签或路径的稳定失败码。"""

        super().__init__("formal model retraining artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ModelRetrainingPackageResult:
    """不含正文、向量或模型权重的三候选训练摘要。"""

    training_run_id: str
    snapshot_id: str
    encoding_id: str
    count: int
    outer_fold_count: int
    candidate_ids: Mapping[str, str]
    candidate_metrics: Mapping[str, Mapping[str, Any]]
    fit_call_count: int
    package_manifest_sha256: str
    models_sha256: str
    oof_sha256: str
    reused: bool
    status: str
    historical_test_status: str
    routing_threshold_status: str
    audit_status: str


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
    """流式计算运行包文件摘要。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子发布JSON文件。"""

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
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingTrainingArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingTrainingArtifactError(reason_code)
    return value


def _runtime_versions() -> Mapping[str, str]:
    """记录影响线性模型和持久化的精确依赖版本。"""

    try:
        return {
            name: importlib.metadata.version(name)
            for name in ("joblib", "numpy", "scikit-learn", "scipy")
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_dependency_missing"
        ) from exc


def _result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> ModelRetrainingPackageResult:
    """从已验证manifest生成去敏摘要。"""

    return ModelRetrainingPackageResult(
        training_run_id=str(manifest["training_run_id"]),
        snapshot_id=str(manifest["snapshot_id"]),
        encoding_id=str(manifest["encoding_id"]),
        count=int(manifest["count"]),
        outer_fold_count=int(manifest["outer_fold_count"]),
        candidate_ids=dict(manifest["candidate_ids"]),
        candidate_metrics=dict(manifest["candidate_metrics"]),
        fit_call_count=int(manifest["fit_call_count"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        models_sha256=str(manifest["artifacts"]["models"]["sha256"]),
        oof_sha256=str(manifest["artifacts"]["oof"]["sha256"]),
        reused=reused,
        status=str(manifest["status"]),
        historical_test_status=str(manifest["historical_test_status"]),
        routing_threshold_status=str(manifest["routing_threshold_status"]),
        audit_status=str(manifest["audit_status"]),
    )


def _validate_existing(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> ModelRetrainingPackageResult:
    """验证既有训练包和全部关键文件哈希。"""

    manifest_path = directory / "training-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_training_manifest_invalid"
    )
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-fixed-candidates"
        or manifest.get("status") != "FIXED_CANDIDATES_TRAINED"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("count") != plan.expected_training_count
        or manifest.get("historical_test_status") != "consumed_not_reopened"
        or manifest.get("component_cross_fold_violation_count") != 0
        or manifest.get("sampling_weights_entered_fit") is not False
        or manifest.get("platform_used") is not False
        or manifest.get("routing_threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
    ):
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_manifest_invalid"
        )
    for key, filename in {
        "models": "candidate-models.joblib",
        "oof": "oof-predictions.json",
        "folds": "fold-assignments.json",
        "summary": "training-summary.json",
    }.items():
        details = manifest.get("artifacts", {}).get(key, {})
        if (
            details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingTrainingArtifactError(
                "model_retraining_training_artifact_hash_mismatch"
            )
    return _result(manifest, manifest_path.read_bytes(), reused=True)


def train_retraining_candidates_package(
    snapshot_package: str | Path,
    embedding_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_snapshot_manifest_sha256: str,
    expected_embedding_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> ModelRetrainingPackageResult:
    """读取冻结快照和向量，训练并封存固定三候选。"""

    snapshot, snapshot_manifest = load_retraining_snapshot_package(
        snapshot_package,
        plan=plan,
        expected_manifest_sha256=expected_snapshot_manifest_sha256,
    )
    embeddings, embedding_manifest = load_retraining_embeddings_package(
        embedding_package,
        plan=plan,
        expected_manifest_sha256=expected_embedding_manifest_sha256,
        expected_member_keys=[item.member_key for item in snapshot.documents],
    )
    if len(code_version) != 40:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_code_version_invalid"
        )
    training_run_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{snapshot_manifest['snapshot_id']}|"
            f"{embedding_manifest['encoding_id']}|fixed-three-candidates"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / training_run_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingTrainingArtifactError(
                "model_retraining_training_existing_requires_manifest_hash"
            )
        return _validate_existing(
            directory,
            plan=plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    try:
        result = train_fixed_retraining_candidates(
            snapshot.documents, embeddings, plan=plan
        )
    except ModelRetrainingTrainingError:
        raise
    models_payload = {
        item.candidate_name: item for item in result.candidates
    }
    oof_bytes = _canonical_bytes(
        [asdict(item) for item in result.oof_observations]
    )
    folds_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-model-retraining-folds",
            "split_unit": "finalized_leakage_component",
            "fold_count": result.outer_fold_count,
            "component_cross_fold_violation_count": (
                result.component_cross_fold_violation_count
            ),
            "assignments": dict(sorted(result.fold_assignments.items())),
        }
    )
    summary = {
        "artifact_kind": "formal-cleaning-model-retraining-summary",
        "training_run_id": training_run_id,
        "count": snapshot.count,
        "candidate_ids": {
            item.candidate_name: item.candidate_id for item in result.candidates
        },
        "candidate_metrics": result.candidate_metrics,
        "outer_fold_count": result.outer_fold_count,
        "fit_call_count": result.fit_call_count,
        "sampling_weights_entered_fit": False,
        "platform_used": False,
        "historical_test_status": "consumed_not_reopened",
        "routing_threshold_status": "UNSET",
        "audit_status": "UNSET",
    }
    summary_bytes = _canonical_bytes(summary)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        models_path = directory / "candidate-models.joblib"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".candidate-models.", dir=directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            joblib.dump(models_payload, temporary, compress=3)
            os.replace(temporary, models_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    except (OSError, ValueError) as exc:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_artifact_write_failed"
        ) from exc
    _atomic_write(directory / "oof-predictions.json", oof_bytes)
    _atomic_write(directory / "fold-assignments.json", folds_bytes)
    _atomic_write(directory / "training-summary.json", summary_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-fixed-candidates",
        "status": "FIXED_CANDIDATES_TRAINED",
        "training_run_id": training_run_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "snapshot_id": str(snapshot_manifest["snapshot_id"]),
        "snapshot_manifest_sha256": expected_snapshot_manifest_sha256,
        "encoding_id": str(embedding_manifest["encoding_id"]),
        "embedding_manifest_sha256": expected_embedding_manifest_sha256,
        "count": snapshot.count,
        "outer_fold_count": result.outer_fold_count,
        "candidate_ids": summary["candidate_ids"],
        "candidate_metrics": result.candidate_metrics,
        "fit_call_count": result.fit_call_count,
        "component_cross_fold_violation_count": 0,
        "sampling_weights_entered_fit": False,
        "platform_used": False,
        "historical_test_status": "consumed_not_reopened",
        "historical_test_probabilities_are_test_evidence": False,
        "routing_threshold_status": "UNSET",
        "audit_status": "UNSET",
        "model_family_expansion_allowed": False,
        "runtime_versions": _runtime_versions(),
        "artifacts": {
            "models": {
                "filename": "candidate-models.joblib",
                "sha256": _file_sha256(models_path),
            },
            "oof": {
                "filename": "oof-predictions.json",
                "sha256": _sha256_bytes(oof_bytes),
            },
            "folds": {
                "filename": "fold-assignments.json",
                "sha256": _sha256_bytes(folds_bytes),
            },
            "summary": {
                "filename": "training-summary.json",
                "sha256": _sha256_bytes(summary_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "training-manifest.json", manifest_bytes)
    return _result(manifest, manifest_bytes, reused=False)


def load_retraining_training_package(
    package: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> tuple[
    Mapping[str, FrozenRetrainingCandidate],
    tuple[RetrainingOofObservation, ...],
    Mapping[str, Any],
]:
    """严格读取三候选模型和OOF证据供策略选择或纯预测。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_package_unavailable"
        ) from exc
    _validate_existing(
        directory,
        plan=plan,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    manifest = _load_json(
        directory / "training-manifest.json",
        "model_retraining_training_manifest_invalid",
    )
    try:
        models = joblib.load(directory / "candidate-models.joblib")
        raw_oof = json.loads(
            (directory / "oof-predictions.json").read_text(encoding="utf-8")
        )
        oof = tuple(RetrainingOofObservation(**item) for item in raw_oof)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_payload_invalid"
        ) from exc
    if (
        not isinstance(models, Mapping)
        or set(models)
        != {"qwen_linear_svc", "sparse_linear_svc", "logit_fusion"}
        or any(not isinstance(item, FrozenRetrainingCandidate) for item in models.values())
        or len(oof) != plan.expected_training_count * 3
    ):
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_payload_invalid"
        )
    return models, oof, manifest


def render_model_retraining_result(
    result: ModelRetrainingPackageResult, *, output_format: str
) -> str:
    """输出机器JSON或固定三候选可读摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_training_output_format_invalid"
        )
    lines = [
        "# 1300条标签固定三候选重训结果",
        "",
        f"训练运行ID：{result.training_run_id}",
        f"artifact：{'严格复用' if result.reused else '新建并封存'}",
        f"训练成员：{result.count}；leakage-group OOF={result.outer_fold_count}折",
        f"fit调用：{result.fit_call_count}（仅训练阶段）",
        "",
        "候选OOF诊断（0.5不是路由阈值）",
    ]
    for name in ("qwen_linear_svc", "sparse_linear_svc", "logit_fusion"):
        metrics = result.candidate_metrics[name]
        lines.append(
            (
                f"  {name} [{result.candidate_ids[name]}]："
                f"Accuracy={(metrics['confusion']['related_as_related'] + metrics['confusion']['unrelated_as_unrelated']) / metrics['count']:.2%}；"
                f"log loss={metrics['log_loss']:.4f}；"
                f"PR-AUC={metrics['pr_auc_unrelated']:.4f}；"
                f"Brier={metrics['brier_score']:.4f}"
            )
        )
    lines.extend(
        [
            "",
            "历史锁定测试：consumed_not_reopened；不产生新测试证据。",
            "路由阈值：UNSET；审计：UNSET；自动清洗决定：未生成。",
            f"状态：{result.status}",
        ]
    )
    return "\n".join(lines)
