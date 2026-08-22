"""700 条权威 CSV 到不可变正式 baseline 运行包的集成边界。"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
from sklearn.pipeline import Pipeline

from tourism_ugc_study.cleaning.config import StableCleaningConfig
from tourism_ugc_study.cleaning.reference_evidence import (
    REFERENCE_FIELDS,
    ReferenceValidationResult,
    validate_reference_evidence,
)

from .formal_baseline import (
    BaselineDocument,
    BaselineProbability,
    BaselineSplitPlan,
    FormalBaselineError,
    FrozenBaselineModel,
    SigmoidCalibrator,
    build_global_split_plan,
    component_split_names,
    fit_formal_baseline,
    split_counts,
)


BASELINE_ALGORITHM_ID = "char-tfidf-linear-svm-oof-sigmoid-v1"


class FormalTrainingError(RuntimeError):
    """训练输入、谱系或不可变运行包不满足契约。

    Attributes:
        reason_code: 不包含正文、作者或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化去敏失败。

        Args:
            reason_code: 供 CLI、测试和运行 manifest 使用的稳定失败码。
        """

        super().__init__("formal baseline training failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class BaselineEvidenceBundle:
    """权威 CSV、数据库身份与泄漏分组核验后的训练输入。

    Attributes:
        documents: 不含平台字段的确定标签训练投影。
        reference: 参考 CSV/manifest 校验摘要。
        leakage_build_id: 显式泄漏分组身份。
        leakage_manifest_sha256: 泄漏分组成员摘要。
        candidate_build_id: 700 条抽样时使用的候选构建身份。
        uncertain_count: 最终契约下恒为零。
    """

    documents: tuple[BaselineDocument, ...]
    reference: ReferenceValidationResult
    leakage_build_id: str
    leakage_manifest_sha256: str
    candidate_build_id: str
    uncertain_count: int


@dataclass(frozen=True)
class FormalTrainingPackageResult:
    """不可变 baseline 运行包的去敏摘要。

    Attributes:
        model_id: 内容寻址模型身份。
        status: 固定为 ``frozen``。
        reused: 是否复用已存在且通过完整性校验的相同运行包。
        reference_count: 权威完成 CSV 总记录数。
        determinate_count: 实际进入二分类切分的确定标签数。
        uncertain_count: 未进入训练的 ``uncertain`` 数量。
        split_counts: 训练、验证和锁定测试集合数量。
        calibration_fold_count: 实际分组折外折数。
        validation_metrics: 仅基于验证集的总体诊断指标。
        artifact_sha256: 模型 pipeline 文件摘要。
        calibration_artifact_sha256: Sigmoid 校准器文件摘要。
        package_manifest_sha256: 完整训练运行 manifest 摘要。
        test_status: 固定为 ``locked_not_opened``。
        threshold_status: 固定为 ``UNSET``。
    """

    model_id: str
    status: str
    reused: bool
    reference_count: int
    determinate_count: int
    uncertain_count: int
    split_counts: Mapping[str, int]
    calibration_fold_count: int
    validation_metrics: Mapping[str, Any]
    artifact_sha256: str
    calibration_artifact_sha256: str
    package_manifest_sha256: str
    test_status: str
    threshold_status: str


def _canonical_bytes(value: object) -> bytes:
    """把 JSON 对象编码为唯一规范字节。

    Args:
        value: 可 JSON 序列化对象。

    Returns:
        排序键、两空格缩进并以换行结尾的 UTF-8 字节。
    """

    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(
        "utf-8"
    ) + b"\n"


def _sha256_bytes(value: bytes) -> str:
    """计算内存字节的 SHA-256。

    Args:
        value: 待摘要字节。

    Returns:
        小写十六进制 SHA-256。
    """

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256。

    Args:
        path: 待读取文件。

    Returns:
        小写十六进制 SHA-256。
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    """以只读、query-only 模式打开派生数据库。

    Args:
        path: 已存在的派生 SQLite 路径。

    Returns:
        禁止写操作并使用行名访问的 SQLite 连接。
    """

    resolved = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def _reference_labels(path: Path) -> tuple[dict[str, str], ...]:
    """读取已由证据校验器确认字段和哈希的完成 CSV。

    Args:
        path: 完成 CSV 路径。

    Returns:
        保留原始字符串的逐行字典。

    Raises:
        FormalTrainingError: 字段顺序在两次读取间发生变化。
    """

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != REFERENCE_FIELDS:
                raise FormalTrainingError("training_reference_field_contract_changed")
            return tuple(dict(row) for row in reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise FormalTrainingError("training_reference_reread_failed") from exc


def _leakage_member_manifest(
    connection: sqlite3.Connection, leakage_build_id: str
) -> str:
    """从成员子行重建泄漏分组摘要。

    Args:
        connection: 已启用只读模式的派生库连接。
        leakage_build_id: 待复验的封存泄漏构建身份。

    Returns:
        与泄漏分组创建器相同的规范成员 SHA-256。
    """

    members = [
        {
            "component_id": str(row["component_id"]),
            "source_post_id": int(row["source_post_id"]),
            "source_version": int(row["source_version"]),
            "author_edge_used": bool(row["author_edge_used"]),
            "exact_edge_used": bool(row["exact_edge_used"]),
            "confirmed_near_edge_used": bool(row["confirmed_near_edge_used"]),
        }
        for row in connection.execute(
            """
            SELECT component_id, source_post_id, source_version,
                   author_edge_used, exact_edge_used, confirmed_near_edge_used
            FROM text_leakage_members WHERE leakage_build_id = ?
            ORDER BY source_post_id, source_version
            """,
            (leakage_build_id,),
        )
    ]
    compact = json.dumps(
        members, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(compact)


def load_baseline_evidence(
    csv_path: str | Path,
    manifest_path: str | Path,
    derived_db: str | Path,
    *,
    leakage_build_id: str,
    config: StableCleaningConfig,
) -> BaselineEvidenceBundle:
    """校验700条权威证据并绑定泄漏分量，生成平台无关训练投影。

    Args:
        csv_path: 唯一最终700条不重复参考 CSV。
        manifest_path: 与最终 CSV 唯一配对的 finalized manifest。
        derived_db: 只读派生 SQLite。
        leakage_build_id: 显式且已封存的泄漏分组身份。
        config: 严格校验的稳定清洗配置。

    Returns:
        完成哈希、身份、文本、样本和泄漏分组校验的训练证据。

    Raises:
        FormalTrainingError: 泄漏构建不完整、候选构建不一致或成员连接缺失。
        ReferenceEvidenceError: 最终参考证据不满足唯一权威契约。
    """

    reference = validate_reference_evidence(
        csv_path,
        manifest_path,
        derived_db,
        expected_label_guide_version=config.label_guide_version,
        expected_normalization_rule_id=str(
            config.artifacts["normalization_version_lock"]
        ),
    )
    try:
        verified_csv_path = Path(csv_path).expanduser().resolve(strict=True)
        rows = _reference_labels(verified_csv_path)
        reread_hash = _file_sha256(verified_csv_path)
    except OSError as exc:
        raise FormalTrainingError("training_reference_reread_failed") from exc
    if reread_hash != reference.csv_sha256:
        raise FormalTrainingError("training_reference_changed_after_validation")
    try:
        connection = _readonly_connection(derived_db)
    except (OSError, sqlite3.Error) as exc:
        raise FormalTrainingError("training_database_readonly_open_failed") from exc
    try:
        leakage = connection.execute(
            """
            SELECT candidate_build_id, output_sha256, seal_status, input_post_count
            FROM text_leakage_builds WHERE leakage_build_id = ?
            """,
            (leakage_build_id,),
        ).fetchone()
        if leakage is None or str(leakage["seal_status"]) != "finalized":
            raise FormalTrainingError("finalized_leakage_build_required")
        candidate_build_id = reference.candidate_build_id
        if str(leakage["candidate_build_id"]) != candidate_build_id:
            raise FormalTrainingError("training_leakage_candidate_build_mismatch")
        leakage_count = connection.execute(
            """
            SELECT COUNT(*) FROM text_leakage_members WHERE leakage_build_id = ?
            """,
            (leakage_build_id,),
        ).fetchone()[0]
        if int(leakage_count) != int(leakage["input_post_count"]):
            raise FormalTrainingError("training_leakage_build_incomplete")
        if _leakage_member_manifest(connection, leakage_build_id) != str(
            leakage["output_sha256"]
        ):
            raise FormalTrainingError("training_leakage_manifest_mismatch")
        database_rows = connection.execute(
            """
            SELECT c.source_post_id, c.source_version, i.captured_at_sort,
                   r.normalized_model_text, l.component_id
            FROM text_candidate_corpus_members AS c
            JOIN text_deterministic_results AS r ON r.task_id = c.task_id
            JOIN source_post_inventory AS i ON i.source_post_id = c.source_post_id
            JOIN text_leakage_members AS l
              ON l.leakage_build_id = ?
             AND l.source_post_id = c.source_post_id
             AND l.source_version = c.source_version
            WHERE c.build_id = ?
            ORDER BY c.source_post_id, c.source_version
            """,
            (leakage_build_id, candidate_build_id),
        ).fetchall()
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise FormalTrainingError("training_leakage_database_contract_invalid") from exc
    finally:
        connection.close()
    by_identity = {
        (str(row["source_post_id"]), str(row["source_version"])): row
        for row in database_rows
    }
    documents: list[BaselineDocument] = []
    for item in rows:
        identity = (item["source_post_id"].strip(), item["source_version"].strip())
        database_row = by_identity.get(identity)
        if database_row is None:
            raise FormalTrainingError("training_reference_not_in_leakage_build")
        label = item["tourism_label"].strip()
        documents.append(
            BaselineDocument(
                source_post_id=int(item["source_post_id"]),
                source_version=int(item["source_version"]),
                captured_at_sort=str(database_row["captured_at_sort"] or ""),
                normalized_model_text=str(database_row["normalized_model_text"]),
                tourism_label=label,
                component_id=str(database_row["component_id"]),
                task_id=item["task_id"].strip(),
            )
        )
    return BaselineEvidenceBundle(
        documents=tuple(documents),
        reference=reference,
        leakage_build_id=leakage_build_id,
        leakage_manifest_sha256=str(leakage["output_sha256"]),
        candidate_build_id=candidate_build_id,
        uncertain_count=0,
    )


def _model_identity(
    evidence: BaselineEvidenceBundle,
    split_plan: BaselineSplitPlan,
    config: StableCleaningConfig,
    code_version: str,
) -> str:
    """计算不依赖本机路径或运行时间的内容寻址模型身份。

    Args:
        evidence: 已校验训练证据。
        split_plan: 拟合前冻结的切分计划。
        config: 稳定配置。
        code_version: 当前不可变代码身份。

    Returns:
        32位十六进制模型 ID。
    """

    payload = {
        "algorithm_id": BASELINE_ALGORITHM_ID,
        "code_version": code_version,
        "config_sha256": config.sha256,
        "reference_csv_sha256": evidence.reference.csv_sha256,
        "reference_manifest_sha256": evidence.reference.manifest_sha256,
        "leakage_manifest_sha256": evidence.leakage_manifest_sha256,
        "split_manifest_sha256": split_plan.manifest_sha256,
    }
    return _sha256_bytes(_canonical_bytes(payload))[:32]


def _probability_rows(rows: Sequence[BaselineProbability]) -> list[dict[str, Any]]:
    """把概率证据转换为稳定 JSON 行。

    Args:
        rows: 折外或验证概率记录。

    Returns:
        按身份排序的 JSON 对象列表。
    """

    return [
        asdict(item)
        for item in sorted(rows, key=lambda value: (value.source_post_id, value.source_version))
    ]


def _runtime_versions() -> Mapping[str, str]:
    """收集参与 baseline 训练的精确依赖版本。

    Returns:
        Python包名到已安装版本的排序映射。
    """

    return {
        name: importlib.metadata.version(name)
        for name in ("joblib", "numpy", "scikit-learn", "scipy")
    }


def _validate_existing_package(
    package_dir: Path,
    *,
    expected_model_id: str,
) -> FormalTrainingPackageResult:
    """验证并复用内容寻址的既有不可变运行包。

    Args:
        package_dir: 以模型 ID 命名的运行包目录。
        expected_model_id: 当前输入计算出的模型身份。

    Returns:
        与首次训练相同的去敏结果，``reused`` 为 ``True``。

    Raises:
        FormalTrainingError: manifest、身份或任一文件摘要不一致。
    """

    manifest_path = package_dir / "training-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FormalTrainingError("baseline_package_manifest_unreadable") from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("model_id") != expected_model_id
        or manifest.get("artifact_status") != "immutable"
        or manifest_bytes != _canonical_bytes(manifest)
    ):
        raise FormalTrainingError("baseline_package_manifest_invalid")
    artifacts = manifest.get("artifacts")
    expected_artifacts = {
        "model": "model.joblib",
        "calibrator": "calibrator.joblib",
        "split_manifest": "split-manifest.json",
        "development_probabilities": "development-probabilities.json",
        "validation_metrics": "validation-metrics.json",
    }
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(expected_artifacts):
        raise FormalTrainingError("baseline_package_artifacts_missing")
    for logical_name, details in artifacts.items():
        if not isinstance(details, Mapping):
            raise FormalTrainingError("baseline_package_artifact_invalid")
        if details.get("filename") != expected_artifacts[str(logical_name)]:
            raise FormalTrainingError("baseline_package_artifact_filename_invalid")
        path = package_dir / expected_artifacts[str(logical_name)]
        if not path.is_file() or _file_sha256(path) != details.get("sha256"):
            raise FormalTrainingError("baseline_package_artifact_hash_mismatch")
    return FormalTrainingPackageResult(
        model_id=expected_model_id,
        status="frozen",
        reused=True,
        reference_count=int(manifest["reference_count"]),
        determinate_count=int(manifest["determinate_count"]),
        uncertain_count=int(manifest["uncertain_count"]),
        split_counts=dict(manifest["split_counts"]),
        calibration_fold_count=int(manifest["calibration_fold_count"]),
        validation_metrics=dict(manifest["validation_metrics"]),
        artifact_sha256=str(artifacts["model"]["sha256"]),
        calibration_artifact_sha256=str(artifacts["calibrator"]["sha256"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        test_status="locked_not_opened",
        threshold_status="UNSET",
    )


def _publish_package(temporary: Path, destination: Path) -> None:
    """把完整临时目录原子发布为不可变模型运行包。

    Args:
        temporary: 已完整写入并复核的同文件系统临时目录。
        destination: 以模型 ID 命名且不得已存在的最终目录。

    Raises:
        FormalTrainingError: 目标已存在或目录重命名失败。
    """

    if destination.exists():
        raise FormalTrainingError("baseline_package_publish_race")
    try:
        temporary.rename(destination)
    except OSError as exc:
        raise FormalTrainingError("baseline_package_publish_failed") from exc


def train_formal_baseline_package(
    csv_path: str | Path,
    manifest_path: str | Path,
    derived_db: str | Path,
    artifact_root: str | Path,
    *,
    leakage_build_id: str,
    config: StableCleaningConfig,
    code_version: str,
) -> FormalTrainingPackageResult:
    """训练并排他封存当前正式 baseline，但不打开测试集或选择阈值。

    Args:
        csv_path: 唯一最终700条不重复参考 CSV。
        manifest_path: 与最终 CSV 唯一配对的 finalized manifest。
        derived_db: 只读派生 SQLite。
        artifact_root: 模型运行包的本地父目录。
        leakage_build_id: 显式封存泄漏分组身份。
        config: 严格校验且阈值保持 ``UNSET`` 的稳定配置。
        code_version: 当前 Git SHA 或等价不可变代码身份。

    Returns:
        模型身份、验证指标、artifact 哈希和复用状态。

    Raises:
        FormalTrainingError: 输入、切分、拟合、写入或既有运行包不满足契约。
        FormalBaselineError: 数据无法形成合规切分或模型。

    Notes:
        运行包只含训练集拟合模型、折外校准器、训练折外概率、验证概率及锁定
        测试成员。锁定测试集不在本函数中预测，两个路由阈值仍为 ``UNSET``。
    """

    if not code_version.strip():
        raise FormalTrainingError("baseline_code_version_required")
    if config.routing["T_keep"] != "UNSET" or config.routing["T_exclude"] != "UNSET":
        raise FormalTrainingError("baseline_training_requires_unset_thresholds")
    evidence = load_baseline_evidence(
        csv_path,
        manifest_path,
        derived_db,
        leakage_build_id=leakage_build_id,
        config=config,
    )
    split_plan = build_global_split_plan(
        evidence.documents,
        random_seed=config.random_seed,
        temporal_test_fraction=float(config.split["temporal_test_fraction"]),
        validation_fraction=float(config.split["validation_fraction"]),
    )
    if any(len(names) != 1 for names in component_split_names(split_plan).values()):
        raise FormalTrainingError("baseline_component_leakage_detected")
    model_id = _model_identity(evidence, split_plan, config, code_version.strip())
    root = Path(artifact_root).expanduser().resolve()
    package_dir = root / model_id
    if package_dir.exists():
        return _validate_existing_package(package_dir, expected_model_id=model_id)
    result = fit_formal_baseline(
        evidence.documents,
        split_plan=split_plan,
        config=config,
    )
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{model_id}.", dir=root)
    )
    try:
        assert temporary is not None
        model_path = temporary / "model.joblib"
        calibration_path = temporary / "calibrator.joblib"
        split_path = temporary / "split-manifest.json"
        probability_path = temporary / "development-probabilities.json"
        metrics_path = temporary / "validation-metrics.json"
        joblib.dump(result.model.pipeline, model_path)
        joblib.dump(result.model.calibrator, calibration_path)
        split_payload = {
            "assignments": [asdict(item) for item in result.split_plan.assignments],
            "manifest_sha256": result.split_plan.manifest_sha256,
            "train_manifest_sha256": result.split_plan.train_manifest_sha256,
            "validation_manifest_sha256": result.split_plan.validation_manifest_sha256,
            "test_manifest_sha256": result.split_plan.test_manifest_sha256,
            "test_candidate_manifest_sha256": (
                result.split_plan.test_candidate_manifest_sha256
            ),
            "test_candidate_count": result.split_plan.test_candidate_count,
        }
        split_path.write_bytes(_canonical_bytes(split_payload))
        probabilities_payload = {
            "positive_class": "unrelated",
            "threshold_status": "UNSET",
            "train_oof": _probability_rows(result.train_oof_probabilities),
            "validation": _probability_rows(result.validation_probabilities),
        }
        probability_path.write_bytes(_canonical_bytes(probabilities_payload))
        metrics_path.write_bytes(_canonical_bytes(result.validation_metrics))
        artifact_details = {
            "model": {"filename": model_path.name, "sha256": _file_sha256(model_path)},
            "calibrator": {
                "filename": calibration_path.name,
                "sha256": _file_sha256(calibration_path),
            },
            "split_manifest": {
                "filename": split_path.name,
                "sha256": _file_sha256(split_path),
            },
            "development_probabilities": {
                "filename": probability_path.name,
                "sha256": _file_sha256(probability_path),
            },
            "validation_metrics": {
                "filename": metrics_path.name,
                "sha256": _file_sha256(metrics_path),
            },
        }
        counts = split_counts(result.split_plan)
        manifest_payload = {
            "artifact_kind": "formal-cleaning-baseline",
            "artifact_status": "immutable",
            "model_id": model_id,
            "algorithm_id": BASELINE_ALGORITHM_ID,
            "feature_contract": "normalized_model_text",
            "positive_class": "unrelated",
            "platform_used": False,
            "fit_scope": "train_only",
            "calibration_scope": "train_grouped_out_of_fold_only",
            "calibration_method": "sigmoid",
            "calibration_fold_count": result.calibration_fold_count,
            "test_status": "locked_not_opened",
            "threshold_status": "UNSET",
            "reference_count": evidence.reference.row_count,
            "determinate_count": len(evidence.documents),
            "uncertain_count": evidence.uncertain_count,
            "split_counts": counts,
            "validation_metrics": dict(result.validation_metrics),
            "lineage": {
                "code_version": code_version.strip(),
                "config_sha256": config.sha256,
                "normalization_version_lock": config.artifacts[
                    "normalization_version_lock"
                ],
                "reference_csv_sha256": evidence.reference.csv_sha256,
                "reference_manifest_sha256": evidence.reference.manifest_sha256,
                "reference_member_sha256": evidence.reference.member_manifest_sha256,
                "probability_estimation_status": (
                    evidence.reference.probability_estimation_status
                ),
                "candidate_build_id": evidence.candidate_build_id,
                "leakage_build_id": evidence.leakage_build_id,
                "leakage_manifest_sha256": evidence.leakage_manifest_sha256,
                "split_manifest_sha256": result.split_plan.manifest_sha256,
                "test_manifest_sha256": result.split_plan.test_manifest_sha256,
                "random_seed": config.random_seed,
                "runtime_versions": _runtime_versions(),
            },
            "artifacts": artifact_details,
        }
        manifest_path_out = temporary / "training-manifest.json"
        manifest_bytes = _canonical_bytes(manifest_payload)
        manifest_path_out.write_bytes(manifest_bytes)
        for details in artifact_details.values():
            path = temporary / str(details["filename"])
            if _file_sha256(path) != details["sha256"]:
                raise FormalTrainingError("baseline_temporary_artifact_hash_mismatch")
        _publish_package(temporary, package_dir)
        temporary = None
        return FormalTrainingPackageResult(
            model_id=model_id,
            status="frozen",
            reused=False,
            reference_count=evidence.reference.row_count,
            determinate_count=len(evidence.documents),
            uncertain_count=evidence.uncertain_count,
            split_counts=counts,
            calibration_fold_count=result.calibration_fold_count,
            validation_metrics=dict(result.validation_metrics),
            artifact_sha256=str(artifact_details["model"]["sha256"]),
            calibration_artifact_sha256=str(
                artifact_details["calibrator"]["sha256"]
            ),
            package_manifest_sha256=_sha256_bytes(manifest_bytes),
            test_status="locked_not_opened",
            threshold_status="UNSET",
        )
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def load_frozen_baseline_model(package_dir: str | Path) -> FrozenBaselineModel:
    """校验运行包摘要后加载冻结模型和校准器。

    Args:
        package_dir: 包含 ``training-manifest.json`` 的模型目录。

    Returns:
        可用于后续无训练推理入口的冻结 baseline。

    Raises:
        FormalTrainingError: manifest、artifact 摘要或对象类型不符合契约。
    """

    directory = Path(package_dir).expanduser().resolve(strict=True)
    try:
        manifest = json.loads(
            (directory / "training-manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FormalTrainingError("baseline_package_manifest_unreadable") from exc
    if not isinstance(manifest, Mapping):
        raise FormalTrainingError("baseline_package_manifest_invalid")
    _validate_existing_package(directory, expected_model_id=str(manifest.get("model_id", "")))
    pipeline = joblib.load(directory / str(manifest["artifacts"]["model"]["filename"]))
    calibrator = joblib.load(
        directory / str(manifest["artifacts"]["calibrator"]["filename"])
    )
    if not isinstance(calibrator, SigmoidCalibrator) or not isinstance(pipeline, Pipeline):
        raise FormalTrainingError("baseline_package_object_type_invalid")
    return FrozenBaselineModel(pipeline, calibrator)
