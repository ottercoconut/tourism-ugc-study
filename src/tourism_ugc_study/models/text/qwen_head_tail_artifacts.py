"""英文 head-tail Qwen 表示的证据集成、评测与不可变运行包。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np

from tourism_ugc_study.cleaning.config import StableCleaningConfig
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .formal_baseline import evaluate_binary_probabilities
from .model_acceptance import evaluate_model_acceptance, load_model_acceptance_policy
from .qwen_embedding_artifacts import (
    QwenEmbeddingEvidenceBundle,
    load_qwen_embedding_evidence,
    load_sparse_comparator_oof,
)
from .qwen_embedding_config import QwenEmbeddingPlan, load_qwen_embedding_plan
from .qwen_embedding_runtime import (
    LocalQwenHeadTailEncoder,
    QwenExecutionReceipt,
    QwenModelSnapshot,
    validate_qwen_model_directory,
)
from .qwen_head_challenger import (
    QwenHeadMember,
    fit_qwen_head_challenger_nested,
)
from .qwen_head_challenger_artifacts import (
    pair_qwen_head_with_sparse_comparator,
)
from .qwen_head_challenger_config import load_qwen_head_challenger_plan
from .qwen_head_tail_config import QwenHeadTailPlan, load_qwen_head_tail_plan


QWEN_HEAD_TAIL_ALGORITHM_ID = (
    "qwen3-embedding-english-head-tail-mrl-head-nested-oof-v1"
)


class QwenHeadTailArtifactError(RuntimeError):
    """第三层谱系、聚合诊断或 artifact 失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、成员身份或本机路径的稳定失败码。"""

        super().__init__("qwen head tail artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenHeadTailPackageResult:
    """不可变第三层运行包的去敏摘要。"""

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
    encoding_diagnostics: Mapping[str, Any]
    length_subgroup_diagnostics: Mapping[str, Any]
    package_manifest_sha256: str
    model_artifact_sha256: str
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
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenHeadTailArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise QwenHeadTailArtifactError(reason_code)
    return value


def _artifact_details(path: Path, filename: str) -> Mapping[str, str]:
    """返回 manifest 使用的文件名与摘要。"""

    return {"filename": filename, "sha256": _file_sha256(path / filename)}


def _runtime_versions() -> Mapping[str, str]:
    """记录第三层编码与训练实际使用的精确依赖版本。"""

    names = (
        "joblib",
        "numpy",
        "scikit-learn",
        "scipy",
        "torch",
        "transformers",
        "sentence-transformers",
        "tokenizers",
    )
    try:
        return {name: importlib.metadata.version(name) for name in names}
    except importlib.metadata.PackageNotFoundError as exc:
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_runtime_dependency_missing"
        ) from exc


def _validate_layer2_failure_package(
    package: str | Path, *, plan: QwenHeadTailPlan
) -> None:
    """确认第三层只在第二层已失败且未读验证时启动。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_layer2_package_unavailable"
        ) from exc
    manifest_path = directory / "training-manifest.json"
    if _file_sha256(manifest_path) != plan.layer2_package_manifest_sha256:
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_layer2_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "qwen_head_tail_layer2_manifest_invalid"
    )
    if (
        manifest.get("artifact_kind") != "formal-cleaning-qwen-sparse-fusion"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != plan.layer2_run_id
        or manifest.get("model_id") != plan.layer2_model_id
        or manifest.get("acceptance_status") != "failed_retain_baseline"
        or manifest.get("validation_status") != "not_allowed"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
    ):
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_layer2_manifest_invalid"
        )


def _subgroup_metrics(
    labels: Sequence[str], probabilities: Sequence[float], mask: np.ndarray
) -> Mapping[str, Any]:
    """计算一个长度分组的聚合指标，类别不足时失败关闭为描述计数。"""

    selected = np.asarray(mask, dtype=bool)
    if selected.ndim != 1 or len(selected) != len(labels):
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_length_mask_invalid"
        )
    subgroup_labels = [label for label, keep in zip(labels, selected) if keep]
    subgroup_probabilities = [
        float(value)
        for value, keep in zip(probabilities, selected)
        if keep
    ]
    counts = {
        "count": len(subgroup_labels),
        "related_count": sum(label == "related" for label in subgroup_labels),
        "unrelated_count": sum(
            label == "unrelated" for label in subgroup_labels
        ),
    }
    if not subgroup_labels or set(subgroup_labels) != {"related", "unrelated"}:
        return {"status": "insufficient_class_support", **counts}
    return {
        "status": "evaluated",
        **evaluate_binary_probabilities(
            subgroup_labels, subgroup_probabilities
        ),
    }


def length_subgroup_diagnostics(
    labels: Sequence[str],
    probabilities: Sequence[float],
    *,
    over_limit_mask: np.ndarray,
    middle_omitted_mask: np.ndarray,
) -> Mapping[str, Any]:
    """报告单视图内、head-tail溢出和中段省略三组训练 OOF 指标。

    Args:
        labels: 与训练 OOF 同顺序的人工标签。
        probabilities: 同顺序的候选 OOF 无关概率。
        over_limit_mask: 原英文 prompt 输入是否超过2048 tokens。
        middle_omitted_mask: 两个窗口是否仍未覆盖中间 tokens。

    Returns:
        三个互有关联、只含聚合计数与指标的长度诊断。

    Raises:
        QwenHeadTailArtifactError: 掩码形状、包含关系或输入数量非法。
    """

    over = np.asarray(over_limit_mask, dtype=bool)
    middle = np.asarray(middle_omitted_mask, dtype=bool)
    if (
        len(labels) != len(probabilities)
        or over.shape != (len(labels),)
        or middle.shape != (len(labels),)
        or np.any(middle & ~over)
    ):
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_length_mask_invalid"
        )
    return {
        "within_single_view": _subgroup_metrics(labels, probabilities, ~over),
        "head_tail_overflow": _subgroup_metrics(labels, probabilities, over),
        "middle_omitted": _subgroup_metrics(labels, probabilities, middle),
        "subgroups_are_population_estimates": False,
        "diagnostic_cutoff_is_routing_threshold": False,
    }


def _runtime_plan(
    base_plan: QwenEmbeddingPlan, projection_plan: QwenHeadTailPlan
) -> QwenEmbeddingPlan:
    """仅替换 instruction，保留公开权重与执行身份供运行时校验。"""

    return replace(
        base_plan,
        encoder=replace(
            base_plan.encoder, instruction=projection_plan.instruction
        ),
    )


def _run_identity(
    evidence: QwenEmbeddingEvidenceBundle,
    plan: QwenHeadTailPlan,
    head_plan_sha256: str,
    snapshot: QwenModelSnapshot,
    execution: QwenExecutionReceipt,
    runtime_versions: Mapping[str, str],
    code_version: str,
) -> str:
    """在编码和拟合前计算内容寻址运行身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": QWEN_HEAD_TAIL_ALGORITHM_ID,
                "code_version": code_version,
                "plan_sha256": plan.plan_sha256,
                "head_plan_sha256": head_plan_sha256,
                "reference_manifest_sha256": evidence.reference_manifest_sha256,
                "split_manifest_sha256": evidence.split_manifest_sha256,
                "encoder_revision": snapshot.revision,
                "weights_sha256": snapshot.weights_sha256,
                "snapshot_sha256": snapshot.snapshot_sha256,
                "execution": asdict(execution),
                "runtime_versions": dict(runtime_versions),
            }
        )
    )[:32]


def _model_identity(run_id: str, candidate_id: str, plan_id: str) -> str:
    """生成不依赖 joblib 字节的稳定第三层模型身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "run_id": run_id,
                "selected_candidate_id": candidate_id,
                "representation_plan_id": plan_id,
                "fit_scope": "frozen_train_only",
            }
        )
    )[:32]


def _result_from_manifest(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> QwenHeadTailPackageResult:
    """从已校验 manifest 构造去敏返回值。"""

    report = manifest["training_report"]
    artifacts = manifest["artifacts"]
    return QwenHeadTailPackageResult(
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
        encoding_diagnostics=dict(report["encoding_diagnostics"]),
        length_subgroup_diagnostics=dict(
            report["length_subgroup_diagnostics"]
        ),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        model_artifact_sha256=str(artifacts["model"]["sha256"]),
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
    expected_manifest_sha256: str,
    plan: QwenHeadTailPlan,
) -> QwenHeadTailPackageResult:
    """完整校验并复用既有不可变第三层运行包。"""

    manifest_path = package_dir / "training-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_package_manifest_unreadable"
        ) from exc
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-qwen-english-head-tail"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != expected_run_id
        or manifest.get("algorithm_id") != QWEN_HEAD_TAIL_ALGORITHM_ID
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
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_package_manifest_invalid"
        )
    artifacts = manifest["artifacts"]
    expected_files = {
        "model": "model.joblib",
        "embeddings": "train-embeddings.npz",
        "training_oof": "training-oof.json",
        "paired_oof": "paired-oof.json",
        "candidate_scores": "candidate-scores.json",
        "report": "training-report.json",
        "plan": "plan.yaml",
        "head_plan": "head-plan.yaml",
        "acceptance_policy": "acceptance-policy.yaml",
    }
    if set(artifacts) != set(expected_files):
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_package_manifest_invalid"
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
            raise QwenHeadTailArtifactError(
                "qwen_head_tail_package_artifact_hash_mismatch"
            )
    report = _load_json(
        package_dir / "training-report.json",
        "qwen_head_tail_package_report_invalid",
    )
    if report != manifest.get("training_report"):
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_package_report_invalid"
        )
    return _result_from_manifest(manifest, manifest_bytes, reused=True)


def train_qwen_head_tail_package(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    split_anchor_package: str | Path,
    sparse_plan_path: str | Path,
    sparse_package: str | Path,
    layer2_package: str | Path,
    qwen_base_plan_path: str | Path,
    head_plan_path: str | Path,
    projection_plan_path: str | Path,
    acceptance_policy_path: str | Path,
    model_dir: str | Path,
    artifact_root: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
    show_progress: bool = True,
) -> QwenHeadTailPackageResult:
    """执行第三层重新编码、nested分类头验收并原子封存运行包。"""

    version = code_version.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise QwenHeadTailArtifactError("qwen_head_tail_code_version_invalid")
    projection_plan = load_qwen_head_tail_plan(projection_plan_path)
    base_plan = load_qwen_embedding_plan(qwen_base_plan_path)
    head_plan = load_qwen_head_challenger_plan(head_plan_path)
    policy = load_model_acceptance_policy(acceptance_policy_path)
    if (
        base_plan.plan_sha256 != projection_plan.qwen_base_plan_sha256
        or head_plan.plan_sha256 != projection_plan.head_plan_sha256
        or policy.policy_sha256 != projection_plan.acceptance_policy_sha256
        or policy.baseline_model_id != projection_plan.sparse_model_id
        or policy.reference_csv_sha256 != projection_plan.reference_csv_sha256
        or policy.train_manifest_sha256 != projection_plan.train_manifest_sha256
        or policy.validation_manifest_sha256
        != projection_plan.validation_manifest_sha256
        or policy.test_manifest_sha256 != projection_plan.test_manifest_sha256
        or policy.paired_outer_folds is not False
    ):
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_plan_binding_mismatch"
        )
    _validate_layer2_failure_package(layer2_package, plan=projection_plan)
    runtime_plan = _runtime_plan(base_plan, projection_plan)
    snapshot = validate_qwen_model_directory(model_dir, plan=runtime_plan)
    encoder = LocalQwenHeadTailEncoder(
        model_dir,
        base_plan=base_plan,
        projection_plan=projection_plan,
    )
    execution = encoder.execution_receipt
    evidence = load_qwen_embedding_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        split_anchor_package,
        sparse_plan_path,
        plan=base_plan,
        config=config,
        normalization_config=normalization_config,
    )
    comparator_oof = load_sparse_comparator_oof(
        sparse_package, plan=base_plan
    )
    runtime_versions = _runtime_versions()
    run_id = _run_identity(
        evidence,
        projection_plan,
        head_plan.plan_sha256,
        snapshot,
        execution,
        runtime_versions,
        version,
    )
    root = Path(artifact_root).expanduser().resolve()
    package_dir = root / run_id
    if package_dir.exists():
        if expected_existing_manifest_sha256 is None:
            raise QwenHeadTailArtifactError(
                "qwen_head_tail_existing_package_requires_manifest_hash"
            )
        return _validate_existing_package(
            package_dir,
            expected_run_id=run_id,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            plan=projection_plan,
        )
    encoded = encoder.encode_head_tail_with_diagnostics(
        [item.normalized_model_text for item in evidence.documents],
        show_progress=show_progress,
    )
    members = tuple(
        QwenHeadMember(
            member_key=item.member_key,
            component_id=item.component_id,
            tourism_label=item.tourism_label,
        )
        for item in evidence.documents
    )
    result = fit_qwen_head_challenger_nested(
        members, encoded.embeddings, plan=head_plan
    )
    model_id = _model_identity(
        run_id, result.selected_spec.candidate_id, projection_plan.plan_id
    )
    paired = pair_qwen_head_with_sparse_comparator(
        result.oof_probabilities, comparator_oof
    )
    acceptance = evaluate_model_acceptance(
        paired, policy, candidate_model_id=model_id
    )
    # ``result.oof_probabilities`` 已按 member_key 排序，而编码掩码保持证据顺序；
    # 先按成员键回填，避免长度分组与概率发生隐蔽错位。
    probability_by_key = {
        item.member_key: item.p_unrelated for item in result.oof_probabilities
    }
    ordered_probabilities = np.asarray(
        [probability_by_key[item.member_key] for item in evidence.documents],
        dtype=float,
    )
    labels = [item.tourism_label for item in evidence.documents]
    subgroup_diagnostics = length_subgroup_diagnostics(
        labels,
        ordered_probabilities,
        over_limit_mask=encoded.original_over_limit_mask,
        middle_omitted_mask=encoded.middle_omitted_mask,
    )
    selection_counts: dict[str, int] = {}
    for selection in result.outer_selections:
        selection_counts[selection.selected_candidate_id] = (
            selection_counts.get(selection.selected_candidate_id, 0) + 1
        )
    report = {
        "artifact_kind": "formal-cleaning-qwen-head-tail-training-report",
        "selected_candidate": asdict(result.selected_spec),
        "outer_selection_counts": dict(sorted(selection_counts.items())),
        "training_metrics": dict(result.training_metrics),
        "encoding_diagnostics": asdict(encoded.diagnostics),
        "length_subgroup_diagnostics": subgroup_diagnostics,
        "acceptance": dict(acceptance),
        "representation": {
            "instruction_language": "english",
            "projection": projection_plan.projection,
            "aggregation": projection_plan.aggregation,
            "middle_is_fully_covered": False,
        },
        "paired_outer_folds": False,
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
        with (temporary / "train-embeddings.npz").open("wb") as stream:
            np.savez_compressed(
                stream,
                embeddings=np.asarray(encoded.embeddings, dtype=np.float32),
                member_keys=np.asarray(
                    [item.member_key for item in evidence.documents], dtype=str
                ),
            )
        (temporary / "training-oof.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-head-tail-nested-oof",
                    "model_id": model_id,
                    "records": [asdict(item) for item in result.oof_probabilities],
                    "platform_used": False,
                }
            )
        )
        (temporary / "paired-oof.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-qwen-head-tail-paired-oof",
                    "baseline_model_id": projection_plan.sparse_model_id,
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
                    "artifact_kind": "formal-cleaning-qwen-head-tail-candidate-scores",
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
        shutil.copyfile(projection_plan_path, temporary / "plan.yaml")
        shutil.copyfile(head_plan_path, temporary / "head-plan.yaml")
        shutil.copyfile(
            acceptance_policy_path, temporary / "acceptance-policy.yaml"
        )
        artifacts = {
            "model": _artifact_details(temporary, "model.joblib"),
            "embeddings": _artifact_details(
                temporary, "train-embeddings.npz"
            ),
            "training_oof": _artifact_details(temporary, "training-oof.json"),
            "paired_oof": _artifact_details(temporary, "paired-oof.json"),
            "candidate_scores": _artifact_details(
                temporary, "candidate-scores.json"
            ),
            "report": _artifact_details(temporary, "training-report.json"),
            "plan": _artifact_details(temporary, "plan.yaml"),
            "head_plan": _artifact_details(temporary, "head-plan.yaml"),
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
            "artifact_kind": "formal-cleaning-qwen-english-head-tail",
            "artifact_status": "immutable",
            "run_id": run_id,
            "model_id": model_id,
            "algorithm_id": QWEN_HEAD_TAIL_ALGORITHM_ID,
            "train_count": len(evidence.documents),
            "candidate_count": len(head_plan.candidates),
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
                "plan_id": projection_plan.plan_id,
                "plan_sha256": projection_plan.plan_sha256,
                "head_plan_sha256": head_plan.plan_sha256,
                "acceptance_policy_sha256": policy.policy_sha256,
                "reference_csv_sha256": projection_plan.reference_csv_sha256,
                "reference_manifest_sha256": evidence.reference_manifest_sha256,
                "train_manifest_sha256": projection_plan.train_manifest_sha256,
                "validation_manifest_sha256": (
                    projection_plan.validation_manifest_sha256
                ),
                "test_manifest_sha256": projection_plan.test_manifest_sha256,
                "split_manifest_sha256": evidence.split_manifest_sha256,
                "split_anchor_model_id": projection_plan.split_anchor_model_id,
                "split_anchor_package_manifest_sha256": (
                    evidence.split_anchor_package_manifest_sha256
                ),
                "sparse_run_id": projection_plan.sparse_run_id,
                "sparse_model_id": projection_plan.sparse_model_id,
                "sparse_package_manifest_sha256": (
                    projection_plan.sparse_package_manifest_sha256
                ),
                "sparse_paired_oof_sha256": (
                    projection_plan.sparse_paired_oof_sha256
                ),
                "layer2_run_id": projection_plan.layer2_run_id,
                "layer2_model_id": projection_plan.layer2_model_id,
                "layer2_package_manifest_sha256": (
                    projection_plan.layer2_package_manifest_sha256
                ),
                "encoder_repository": snapshot.repository,
                "encoder_revision": snapshot.revision,
                "encoder_weights_sha256": snapshot.weights_sha256,
                "encoder_snapshot_sha256": snapshot.snapshot_sha256,
                "execution": asdict(execution),
                "encoding_diagnostics": asdict(encoded.diagnostics),
                "random_seed": projection_plan.random_seed,
                "runtime_versions": runtime_versions,
            },
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "training-manifest.json").write_bytes(manifest_bytes)
        try:
            temporary.rename(package_dir)
        except OSError as exc:
            raise QwenHeadTailArtifactError(
                "qwen_head_tail_package_publish_failed"
            ) from exc
        temporary = None
        return _result_from_manifest(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def render_qwen_head_tail_result(
    result: QwenHeadTailPackageResult, *, output_format: str = "human"
) -> str:
    """渲染稳定机器 JSON 或面向研究者的中文第三层摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_output_format_invalid"
        )
    acceptance = result.acceptance_report
    baseline = acceptance["baseline_metrics"]
    candidate = acceptance["candidate_metrics"]
    deltas = acceptance["candidate_minus_baseline"]
    selected = result.selected_candidate
    encoding = result.encoding_diagnostics
    decision = (
        "通过训练侧验收；停止扩展并另行进行一次验证方向复核"
        if result.acceptance_status == "passed"
        else "未通过训练侧验收；停止 Issue #45 搜索并保留 sparse"
    )
    outer_counts = ", ".join(
        f"{key[:8]}…×{value}"
        for key, value in result.outer_selection_counts.items()
    )
    return "\n".join(
        [
            "Qwen3-Embedding-4B 英文 head-tail 第三层训练结果",
            "================================================",
            f"运行 ID：{result.run_id}",
            f"模型 ID：{result.model_id}",
            f"artifact：{'复用既有包' if result.reused else '新建并封存'}",
            f"训练证据：n={result.train_count}；候选={result.candidate_count}；"
            f"nested group OOF 外{result.outer_fold_count}/内{result.final_inner_fold_count}折",
            "",
            "英文 instruction 与长度投影",
            f"  原输入超过2048：{encoding['original_over_limit_count']}；"
            f"编码视图：{encoding['encoded_view_count']}",
            f"  head-tail双视图：{encoding['two_view_count']}；"
            f"仍省略中段：{encoding['middle_omitted_count']}",
            f"  每视图内容预算：{encoding['content_window_token_budget']} tokens；"
            f"编码视图超限：{encoding['encoded_view_over_limit_count']}",
            "  边界：双视图缓解首部截断，不声称覆盖被省略的超长中段",
            "",
            "全训练端最终选择",
            f"  模型族：{selected['family']}；MRL维度：{selected['embedding_dimension']}",
            f"  C：{selected['C']:g}；class_weight："
            f"{selected['class_weight'] or 'none'}",
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
