"""私有训练证据到不可变 sparse challenger 运行包的集成边界。"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib

from tourism_ugc_study.cleaning.config import StableCleaningConfig
from tourism_ugc_study.cleaning.reference_evidence import (
    REFERENCE_FIELDS,
    validate_reference_evidence,
)
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .model_acceptance import (
    ModelAcceptancePolicy,
    evaluate_model_acceptance,
    load_model_acceptance_policy,
)
from .sparse_challenger import (
    ChallengerDocument,
    FrozenSparseCandidateModel,
    SparseChallengerResult,
    fit_sparse_challenger_nested,
)
from .sparse_challenger_config import (
    SparseChallengerPlan,
    load_sparse_challenger_plan,
)


SPARSE_CHALLENGER_ALGORITHM_ID = "nested-group-sparse-challenger-ugc-safety-v1"


class SparseChallengerArtifactError(RuntimeError):
    """challenger 输入谱系或不可变运行包失败时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、帖子身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: 供 CLI、测试和运行 manifest 使用的失败码。
        """

        super().__init__("formal sparse challenger artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class SparseChallengerEvidenceBundle:
    """仅含冻结训练成员的 challenger 输入。

    Attributes:
        documents: 442 条训练侧文本、标签与泄漏分量投影。
        reference_manifest_sha256: 当前最终参考 manifest 摘要。
        baseline_split_manifest_sha256: baseline 全局切分摘要。
        baseline_package_manifest_sha256: baseline 运行 manifest 文件摘要。
        train_count: 训练成员数。
        validation_count: 只从切分元数据读取的验证成员数。
        test_count: 只从切分元数据读取的锁定测试成员数。
    """

    documents: tuple[ChallengerDocument, ...]
    reference_manifest_sha256: str
    baseline_split_manifest_sha256: str
    baseline_package_manifest_sha256: str
    train_count: int
    validation_count: int
    test_count: int


@dataclass(frozen=True)
class SparseChallengerPackageResult:
    """不可变 challenger 训练运行包的去敏摘要。

    Attributes:
        run_id: 训练前可确定的内容寻址运行身份。
        candidate_model_id: 加入最终候选规范后的模型身份。
        status: 固定为 ``frozen``。
        reused: 是否完整复用既有不可变运行包。
        selected_candidate_id: 最终唯一候选规范身份。
        selected_family: 最终唯一候选模型族。
        baseline_fallback: 是否因无安全候选而回退 baseline anchor。
        train_count: 训练侧配对 OOF 记录数。
        outer_fold_count: 实际外层折数。
        inner_fold_count: 最终选择实际内层折数。
        acceptance_status: ``passed`` 或 ``failed_retain_baseline``。
        acceptance_report: 不含成员信息的 UGC 安全验收聚合报告。
        package_manifest_sha256: 完整运行 manifest 摘要。
        model_artifact_sha256: 私有候选模型 artifact 摘要。
        paired_oof_sha256: 私有逐成员配对 OOF 摘要。
        validation_status: ``pending_directional_check`` 或 ``not_allowed``。
        test_status: 固定为 ``locked_not_opened``。
        threshold_status: 固定为 ``UNSET``。
    """

    run_id: str
    candidate_model_id: str
    status: str
    reused: bool
    selected_candidate_id: str
    selected_family: str
    baseline_fallback: bool
    train_count: int
    outer_fold_count: int
    inner_fold_count: int
    acceptance_status: str
    acceptance_report: Mapping[str, Any]
    package_manifest_sha256: str
    model_artifact_sha256: str
    paired_oof_sha256: str
    validation_status: str
    test_status: str
    threshold_status: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序键、禁止 NaN 且以换行结束的规范 JSON。"""

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
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SparseChallengerArtifactError(
            "sparse_challenger_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SparseChallengerArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise SparseChallengerArtifactError(reason_code)
    return value


def _runtime_versions() -> Mapping[str, str]:
    """记录参与 challenger 拟合的精确依赖版本。"""

    return {
        name: importlib.metadata.version(name)
        for name in ("joblib", "numpy", "scikit-learn", "scipy")
    }


def load_verified_baseline_split_bindings(
    baseline_package: Path,
    plan: SparseChallengerPlan,
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[tuple[int, int], tuple[str, str]],
]:
    """校验 baseline 包及切分谱系，返回全部成员的分量与集合映射。

    Args:
        baseline_package: 当前不可变 baseline 包目录。
        plan: 绑定三集合 manifest 的 challenger 计划。

    Returns:
        baseline manifest、split manifest，以及身份到
        ``(component_id, split_name)`` 的只读映射。

    Raises:
        SparseChallengerArtifactError: 任一 artifact、成员摘要或泄漏边界漂移。
    """

    manifest_path = baseline_package / "training-manifest.json"
    manifest = _load_json(
        manifest_path, "sparse_challenger_baseline_manifest_unreadable"
    )
    if (
        manifest.get("artifact_kind") != "formal-cleaning-baseline"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("model_id") != plan.baseline_model_id
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
    ):
        raise SparseChallengerArtifactError(
            "sparse_challenger_baseline_manifest_invalid"
        )
    lineage = manifest.get("lineage")
    artifacts = manifest.get("artifacts")
    if not isinstance(lineage, Mapping) or not isinstance(artifacts, Mapping):
        raise SparseChallengerArtifactError(
            "sparse_challenger_baseline_manifest_invalid"
        )
    if lineage.get("reference_csv_sha256") != plan.reference_csv_sha256:
        raise SparseChallengerArtifactError(
            "sparse_challenger_reference_binding_mismatch"
        )
    split_details = artifacts.get("split_manifest")
    if (
        not isinstance(split_details, Mapping)
        or split_details.get("filename") != "split-manifest.json"
    ):
        raise SparseChallengerArtifactError(
            "sparse_challenger_baseline_split_artifact_invalid"
        )
    split_path = baseline_package / str(split_details.get("filename", ""))
    if _file_sha256(split_path) != split_details.get("sha256"):
        raise SparseChallengerArtifactError(
            "sparse_challenger_baseline_split_artifact_invalid"
        )
    split = _load_json(
        split_path, "sparse_challenger_baseline_split_artifact_invalid"
    )
    if (
        split.get("train_manifest_sha256") != plan.train_manifest_sha256
        or split.get("validation_manifest_sha256")
        != plan.validation_manifest_sha256
        or split.get("test_manifest_sha256") != plan.test_manifest_sha256
        or lineage.get("test_manifest_sha256") != plan.test_manifest_sha256
    ):
        raise SparseChallengerArtifactError(
            "sparse_challenger_split_binding_mismatch"
        )
    raw_assignments = split.get("assignments")
    if not isinstance(raw_assignments, list):
        raise SparseChallengerArtifactError(
            "sparse_challenger_baseline_split_artifact_invalid"
        )
    assignment_bindings: dict[tuple[int, int], tuple[str, str]] = {}
    seen_identities: set[tuple[int, int]] = set()
    component_splits: dict[str, set[str]] = {}
    normalized_assignments: list[dict[str, Any]] = []
    for raw in raw_assignments:
        if not isinstance(raw, Mapping) or set(raw) != {
            "source_post_id",
            "source_version",
            "component_id",
            "split_name",
        }:
            raise SparseChallengerArtifactError(
                "sparse_challenger_baseline_split_artifact_invalid"
            )
        try:
            identity = (int(raw["source_post_id"]), int(raw["source_version"]))
            component_id = str(raw["component_id"])
            split_name = str(raw["split_name"])
        except (TypeError, ValueError) as exc:
            raise SparseChallengerArtifactError(
                "sparse_challenger_baseline_split_artifact_invalid"
            ) from exc
        if (
            identity in seen_identities
            or not component_id
            or split_name not in {"train", "validation", "test"}
        ):
            raise SparseChallengerArtifactError(
                "sparse_challenger_baseline_split_artifact_invalid"
            )
        seen_identities.add(identity)
        component_splits.setdefault(component_id, set()).add(split_name)
        normalized = {
            "source_post_id": identity[0],
            "source_version": identity[1],
            "component_id": component_id,
            "split_name": split_name,
        }
        normalized_assignments.append(normalized)
        assignment_bindings[identity] = (component_id, split_name)
    if any(len(names) != 1 for names in component_splits.values()):
        raise SparseChallengerArtifactError(
            "sparse_challenger_component_leakage_detected"
        )
    def assignment_sha256(split_name: str | None) -> str:
        """按 baseline 原始契约重建全部或单集合成员摘要。"""

        projection = [
            item
            for item in normalized_assignments
            if split_name is None or item["split_name"] == split_name
        ]
        compact = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _sha256_bytes(compact)

    expected_hashes = {
        "manifest_sha256": assignment_sha256(None),
        "train_manifest_sha256": assignment_sha256("train"),
        "validation_manifest_sha256": assignment_sha256("validation"),
        "test_manifest_sha256": assignment_sha256("test"),
    }
    if any(split.get(field) != value for field, value in expected_hashes.items()):
        raise SparseChallengerArtifactError(
            "sparse_challenger_split_manifest_mismatch"
        )
    return manifest, split, assignment_bindings


def load_sparse_challenger_evidence(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    baseline_package: str | Path,
    *,
    plan: SparseChallengerPlan,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
) -> SparseChallengerEvidenceBundle:
    """校验最终证据与 baseline 切分，只物化冻结训练成员。

    Args:
        csv_path: 唯一最终700条参考 CSV。
        reference_manifest_path: 与 CSV 唯一配对的 finalized manifest。
        derived_db: 只读派生 SQLite，仅供参考证据完整性校验。
        baseline_package: 当前冻结 baseline 运行包。
        plan: 绑定当前 baseline 与三集合 manifest 的 challenger 计划。
        config: 稳定清洗配置。
        normalization_config: 冻结文本规范化配置。

    Returns:
        不含验证或测试记录对象的442条训练侧输入及谱系摘要。

    Raises:
        SparseChallengerArtifactError: baseline、切分、CSV 或成员投影漂移。
        ReferenceEvidenceError: 最终参考证据未通过既有权威校验。

    Notes:
        baseline 切分文件只用于绑定成员元数据；验证和测试文本、标签不会进入
        返回值，也不会生成验证/测试概率。
    """

    try:
        baseline_dir = Path(baseline_package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SparseChallengerArtifactError(
            "sparse_challenger_baseline_package_unavailable"
        ) from exc
    baseline_manifest, split, assignment_bindings = (
        load_verified_baseline_split_bindings(baseline_dir, plan)
    )
    train_components = {
        identity: component_id
        for identity, (component_id, split_name) in assignment_bindings.items()
        if split_name == "train"
    }
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
    lineage = baseline_manifest["lineage"]
    if (
        reference.csv_sha256 != plan.reference_csv_sha256
        or reference.manifest_sha256 != lineage.get("reference_manifest_sha256")
    ):
        raise SparseChallengerArtifactError(
            "sparse_challenger_reference_binding_mismatch"
        )
    try:
        verified_csv = Path(csv_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SparseChallengerArtifactError(
            "sparse_challenger_reference_reread_failed"
        ) from exc
    documents: list[ChallengerDocument] = []
    seen: set[tuple[int, int]] = set()
    try:
        with verified_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise SparseChallengerArtifactError(
                    "sparse_challenger_reference_fields_invalid"
                )
            for row in reader:
                identity = (
                    int(row["source_post_id"].strip()),
                    int(row["source_version"].strip()),
                )
                component_id = train_components.get(identity)
                if component_id is None:
                    continue
                if identity in seen:
                    raise SparseChallengerArtifactError(
                        "sparse_challenger_train_identity_duplicate"
                    )
                seen.add(identity)
                label = row["tourism_label"].strip()
                text = row["normalized_model_text"]
                if label not in {"related", "unrelated"} or not text.strip():
                    raise SparseChallengerArtifactError(
                        "sparse_challenger_train_record_invalid"
                    )
                member_payload = (
                    f"{plan.plan_sha256}:{identity[0]}:{identity[1]}"
                ).encode("utf-8")
                documents.append(
                    ChallengerDocument(
                        member_key=hashlib.sha256(member_payload).hexdigest(),
                        component_id=component_id,
                        normalized_model_text=text,
                        tourism_label=label,
                    )
                )
    except (OSError, UnicodeError, csv.Error, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SparseChallengerArtifactError):
            raise
        raise SparseChallengerArtifactError(
            "sparse_challenger_reference_reread_failed"
        ) from exc
    if seen != set(train_components):
        raise SparseChallengerArtifactError(
            "sparse_challenger_train_members_incomplete"
        )
    counts = baseline_manifest.get("split_counts")
    if not isinstance(counts, Mapping):
        raise SparseChallengerArtifactError(
            "sparse_challenger_baseline_manifest_invalid"
        )
    split_counts = {
        name: sum(
            1
            for item in split["assignments"]
            if item.get("split_name") == name
        )
        for name in ("train", "validation", "test")
    }
    if any(int(counts.get(name, -1)) != split_counts[name] for name in split_counts):
        raise SparseChallengerArtifactError(
            "sparse_challenger_split_count_mismatch"
        )
    return SparseChallengerEvidenceBundle(
        documents=tuple(sorted(documents, key=lambda item: item.member_key)),
        reference_manifest_sha256=reference.manifest_sha256,
        baseline_split_manifest_sha256=str(split["manifest_sha256"]),
        baseline_package_manifest_sha256=_file_sha256(
            baseline_dir / "training-manifest.json"
        ),
        train_count=split_counts["train"],
        validation_count=split_counts["validation"],
        test_count=split_counts["test"],
    )


def _run_identity(
    evidence: SparseChallengerEvidenceBundle,
    plan: SparseChallengerPlan,
    code_version: str,
) -> str:
    """计算训练前可确定且不依赖路径或时间的运行身份。"""

    payload = {
        "algorithm_id": SPARSE_CHALLENGER_ALGORITHM_ID,
        "code_version": code_version,
        "plan_sha256": plan.plan_sha256,
        "reference_manifest_sha256": evidence.reference_manifest_sha256,
        "baseline_split_manifest_sha256": evidence.baseline_split_manifest_sha256,
        "baseline_package_manifest_sha256": evidence.baseline_package_manifest_sha256,
    }
    return _sha256_bytes(_canonical_bytes(payload))[:32]


def _candidate_model_identity(
    run_id: str, result: SparseChallengerResult
) -> str:
    """把运行身份与最终选择规范绑定为候选模型身份。"""

    return _sha256_bytes(
        _canonical_bytes(
            {
                "run_id": run_id,
                "selected_candidate": asdict(result.selected_spec),
                "baseline_fallback": result.baseline_fallback,
            }
        )
    )[:32]


def _artifact_details(directory: Path, filenames: Mapping[str, str]) -> dict[str, Any]:
    """计算一组已写文件的摘要映射。"""

    return {
        name: {"filename": filename, "sha256": _file_sha256(directory / filename)}
        for name, filename in filenames.items()
    }


def _result_from_manifest(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> SparseChallengerPackageResult:
    """从已完整校验的 manifest 构造去敏结果。"""

    artifacts = manifest["artifacts"]
    return SparseChallengerPackageResult(
        run_id=str(manifest["run_id"]),
        candidate_model_id=str(manifest["candidate_model_id"]),
        status="frozen",
        reused=reused,
        selected_candidate_id=str(manifest["selected_candidate"]["candidate_id"]),
        selected_family=str(manifest["selected_candidate"]["family"]),
        baseline_fallback=bool(manifest["baseline_fallback"]),
        train_count=int(manifest["train_count"]),
        outer_fold_count=int(manifest["outer_fold_count"]),
        inner_fold_count=int(manifest["final_inner_fold_count"]),
        acceptance_status=str(manifest["acceptance_status"]),
        acceptance_report=dict(manifest["acceptance_summary"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        model_artifact_sha256=str(artifacts["model"]["sha256"]),
        paired_oof_sha256=str(artifacts["paired_outer_oof"]["sha256"]),
        validation_status=str(manifest["validation_status"]),
        test_status="locked_not_opened",
        threshold_status="UNSET",
    )


def _validate_existing_package(
    package_dir: Path, *, expected_run_id: str
) -> SparseChallengerPackageResult:
    """完整校验并复用内容寻址的既有 challenger 包。"""

    manifest_path = package_dir / "training-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SparseChallengerArtifactError(
            "sparse_challenger_package_manifest_unreadable"
        ) from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind") != "formal-cleaning-sparse-challenger"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != expected_run_id
        or manifest_bytes != _canonical_bytes(manifest)
    ):
        raise SparseChallengerArtifactError(
            "sparse_challenger_package_manifest_invalid"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "model",
        "paired_outer_oof",
        "outer_selections",
        "full_training_scores",
        "acceptance_report",
    }:
        raise SparseChallengerArtifactError(
            "sparse_challenger_package_artifacts_invalid"
        )
    for details in artifacts.values():
        if not isinstance(details, Mapping):
            raise SparseChallengerArtifactError(
                "sparse_challenger_package_artifacts_invalid"
            )
        path = package_dir / str(details.get("filename", ""))
        if _file_sha256(path) != details.get("sha256"):
            raise SparseChallengerArtifactError(
                "sparse_challenger_package_artifact_hash_mismatch"
            )
    return _result_from_manifest(manifest, manifest_bytes, reused=True)


def _write_training_artifacts(
    directory: Path,
    result: SparseChallengerResult,
    *,
    plan: SparseChallengerPlan,
    candidate_model_id: str,
    policy: ModelAcceptancePolicy,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """写入模型、配对证据、选择过程、候选评分与验收报告。"""

    filenames = {
        "model": "selected-model.joblib",
        "paired_outer_oof": "paired-outer-oof.json",
        "outer_selections": "outer-selections.json",
        "full_training_scores": "full-training-scores.json",
        "acceptance_report": "acceptance-report.json",
    }
    joblib.dump(result.selected_model, directory / filenames["model"])
    paired_payload = {
        "artifact_kind": "formal-cleaning-paired-nested-oof-comparison",
        "baseline_model_id": plan.baseline_model_id,
        "candidate_model_id": candidate_model_id,
        "reference_csv_sha256": plan.reference_csv_sha256,
        "train_manifest_sha256": plan.train_manifest_sha256,
        "scope": "train_nested_group_oof",
        "paired_outer_folds": True,
        "test_members_read": False,
        "test_probabilities_present": False,
        "platform_used": False,
        "records": [asdict(item) for item in result.paired_outer_oof],
    }
    (directory / filenames["paired_outer_oof"]).write_bytes(
        _canonical_bytes(paired_payload)
    )
    (directory / filenames["outer_selections"]).write_bytes(
        _canonical_bytes([asdict(item) for item in result.outer_selections])
    )
    (directory / filenames["full_training_scores"]).write_bytes(
        _canonical_bytes(
            {
                "baseline": asdict(result.full_training_baseline_score),
                "candidates": [
                    asdict(item) for item in result.full_training_candidate_scores
                ],
                "selection_rule": "related_safety_then_log_loss_then_pr_auc",
                "selected_candidate": asdict(result.selected_spec),
                "baseline_fallback": result.baseline_fallback,
            }
        )
    )
    acceptance = dict(
        evaluate_model_acceptance(
            result.paired_outer_oof,
            policy,
            candidate_model_id=candidate_model_id,
        )
    )
    (directory / filenames["acceptance_report"]).write_bytes(
        _canonical_bytes(acceptance)
    )
    return _artifact_details(directory, filenames), acceptance


def train_sparse_challenger_package(
    csv_path: str | Path,
    reference_manifest_path: str | Path,
    derived_db: str | Path,
    baseline_package: str | Path,
    plan_path: str | Path,
    policy_path: str | Path,
    artifact_root: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    code_version: str,
) -> SparseChallengerPackageResult:
    """训练并封存首轮 sparse challenger，同时执行训练侧安全验收。

    Args:
        csv_path: 当前唯一最终参考 CSV。
        reference_manifest_path: 与 CSV 配对的 finalized manifest。
        derived_db: 只读派生 SQLite。
        baseline_package: 当前 baseline 不可变运行包。
        plan_path: 54候选预登记 YAML。
        policy_path: UGC 安全优先验收策略 YAML。
        artifact_root: 私有 challenger 运行包父目录。
        config: 稳定清洗配置。
        normalization_config: 冻结规范化配置。
        code_version: 当前 Git 完整提交身份。

    Returns:
        唯一候选、训练侧验收和 artifact 摘要；不含私有路径或正文。

    Raises:
        SparseChallengerArtifactError: 谱系、写入或既有包不满足契约。
        SparseChallengerError: 嵌套分组拟合失败。
        ModelAcceptanceError: 配对安全验收证据失败。

    Notes:
        训练侧验收通过只允许后续唯一候选做验证方向性复核；本函数不预测
        验证或测试集合，不选择路由阈值，也不生成自动清洗决定。
    """

    version = code_version.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise SparseChallengerArtifactError(
            "sparse_challenger_code_version_invalid"
        )
    plan = load_sparse_challenger_plan(plan_path)
    policy = load_model_acceptance_policy(policy_path)
    if (
        policy.policy_sha256 != plan.acceptance_policy_sha256
        or policy.baseline_model_id != plan.baseline_model_id
        or policy.reference_csv_sha256 != plan.reference_csv_sha256
        or policy.train_manifest_sha256 != plan.train_manifest_sha256
        or policy.validation_manifest_sha256 != plan.validation_manifest_sha256
        or policy.test_manifest_sha256 != plan.test_manifest_sha256
    ):
        raise SparseChallengerArtifactError(
            "sparse_challenger_acceptance_binding_mismatch"
        )
    evidence = load_sparse_challenger_evidence(
        csv_path,
        reference_manifest_path,
        derived_db,
        baseline_package,
        plan=plan,
        config=config,
        normalization_config=normalization_config,
    )
    run_id = _run_identity(evidence, plan, version)
    root = Path(artifact_root).expanduser().resolve()
    package_dir = root / run_id
    if package_dir.exists():
        return _validate_existing_package(package_dir, expected_run_id=run_id)
    result = fit_sparse_challenger_nested(evidence.documents, plan=plan)
    candidate_model_id = _candidate_model_identity(run_id, result)
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.", dir=root)
    )
    try:
        artifact_details, acceptance = _write_training_artifacts(
            temporary,
            result,
            plan=plan,
            candidate_model_id=candidate_model_id,
            policy=policy,
        )
        validation_status = (
            "pending_directional_check"
            if acceptance["all_gates_passed"]
            else "not_allowed"
        )
        manifest_payload = {
            "artifact_kind": "formal-cleaning-sparse-challenger",
            "artifact_status": "immutable",
            "run_id": run_id,
            "candidate_model_id": candidate_model_id,
            "algorithm_id": SPARSE_CHALLENGER_ALGORITHM_ID,
            "selected_candidate": asdict(result.selected_spec),
            "baseline_fallback": result.baseline_fallback,
            "train_count": evidence.train_count,
            "outer_fold_count": result.outer_fold_count,
            "final_inner_fold_count": result.final_inner_fold_count,
            "acceptance_status": acceptance["status"],
            "acceptance_summary": acceptance,
            "validation_status": validation_status,
            "test_status": "locked_not_opened",
            "test_member_metadata_binding_only": True,
            "test_probabilities_present": False,
            "threshold_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
            "lineage": {
                "code_version": version,
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "acceptance_policy_sha256": policy.policy_sha256,
                "baseline_model_id": plan.baseline_model_id,
                "baseline_package_manifest_sha256": (
                    evidence.baseline_package_manifest_sha256
                ),
                "reference_csv_sha256": plan.reference_csv_sha256,
                "reference_manifest_sha256": evidence.reference_manifest_sha256,
                "train_manifest_sha256": plan.train_manifest_sha256,
                "validation_manifest_sha256": plan.validation_manifest_sha256,
                "test_manifest_sha256": plan.test_manifest_sha256,
                "split_manifest_sha256": evidence.baseline_split_manifest_sha256,
                "random_seed": plan.random_seed,
                "runtime_versions": _runtime_versions(),
            },
            "artifacts": artifact_details,
        }
        manifest_bytes = _canonical_bytes(manifest_payload)
        (temporary / "training-manifest.json").write_bytes(manifest_bytes)
        for details in artifact_details.values():
            if _file_sha256(temporary / details["filename"]) != details["sha256"]:
                raise SparseChallengerArtifactError(
                    "sparse_challenger_temporary_artifact_hash_mismatch"
                )
        try:
            temporary.rename(package_dir)
        except OSError as exc:
            raise SparseChallengerArtifactError(
                "sparse_challenger_package_publish_failed"
            ) from exc
        temporary = None
        return _result_from_manifest(manifest_payload, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def load_frozen_sparse_challenger_model(
    package_dir: str | Path,
) -> FrozenSparseCandidateModel:
    """校验不可变运行包后加载唯一候选模型。

    Args:
        package_dir: 包含 challenger ``training-manifest.json`` 的目录。

    Returns:
        只在训练集合拟合的候选模型；是否获准验证仍由 manifest 决定。

    Raises:
        SparseChallengerArtifactError: manifest、文件摘要或对象类型非法。
    """

    try:
        directory = Path(package_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SparseChallengerArtifactError(
            "sparse_challenger_package_unavailable"
        ) from exc
    manifest = _load_json(
        directory / "training-manifest.json",
        "sparse_challenger_package_manifest_unreadable",
    )
    _validate_existing_package(
        directory, expected_run_id=str(manifest.get("run_id", ""))
    )
    try:
        model = joblib.load(
            directory / str(manifest["artifacts"]["model"]["filename"])
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SparseChallengerArtifactError(
            "sparse_challenger_model_unreadable"
        ) from exc
    if not isinstance(model, FrozenSparseCandidateModel):
        raise SparseChallengerArtifactError(
            "sparse_challenger_model_type_invalid"
        )
    return model


def render_sparse_challenger_result(
    result: SparseChallengerPackageResult,
    *,
    output_format: str = "human",
) -> str:
    """把训练结果渲染为稳定 JSON 或安全优先的中文报告。

    Args:
        result: 已封存或复用的 challenger 去敏摘要。
        output_format: ``human`` 或 ``json``。

    Returns:
        不含正文、身份或路径的可读结果。

    Raises:
        SparseChallengerArtifactError: 输出格式未知。
    """

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise SparseChallengerArtifactError(
            "sparse_challenger_output_format_invalid"
        )
    report = result.acceptance_report
    baseline_metrics = report["baseline_metrics"]
    candidate_metrics = report["candidate_metrics"]
    deltas = report["candidate_minus_baseline"]
    decision = (
        "通过训练侧验收，可进行唯一候选的验证方向性复核"
        if result.acceptance_status == "passed"
        else "未通过训练侧验收，保留现有 baseline"
    )
    lines = [
        "Sparse challenger 训练结果",
        "==========================",
        f"运行 ID：{result.run_id}",
        f"候选模型 ID：{result.candidate_model_id}",
        f"artifact：{'复用既有包' if result.reused else '新建并封存'}",
        "",
        "唯一候选",
        f"  模型族：{result.selected_family}",
        f"  候选参数 ID：{result.selected_candidate_id}",
        f"  baseline 回退：{'是' if result.baseline_fallback else '否'}",
        f"  nested group OOF：n={result.train_count}，外层{result.outer_fold_count}折，"
        f"最终选择内层{result.inner_fold_count}折",
        "",
        "UGC 安全优先验收（固定0.5仅作诊断）",
        "  related→unrelated："
        f"baseline {baseline_metrics['related_to_unrelated_rate'] * 100:.2f}% → "
        f"candidate {candidate_metrics['related_to_unrelated_rate'] * 100:.2f}% "
        f"({deltas['related_to_unrelated_rate'] * 100:+.2f}个百分点)",
        "  log loss："
        f"baseline {baseline_metrics['log_loss']:.4f} → "
        f"candidate {candidate_metrics['log_loss']:.4f} "
        f"({deltas['log_loss']:+.4f})",
        "  unrelated PR-AUC："
        f"baseline {baseline_metrics['pr_auc_unrelated']:.4f} → "
        f"candidate {candidate_metrics['pr_auc_unrelated']:.4f} "
        f"({deltas['pr_auc_unrelated']:+.4f})",
        f"  结论：{decision}",
        "",
        f"验证：{result.validation_status}",
        "锁定测试：locked_not_opened（未生成测试概率）",
        "路由阈值：UNSET；自动清洗决定：未生成。",
    ]
    return "\n".join(lines)
