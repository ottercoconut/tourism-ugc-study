"""1300条训练快照的内容寻址封存与严格复用。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_snapshot import RetrainingSnapshot


class ModelRetrainingSnapshotArtifactError(RuntimeError):
    """训练快照写入、复用或摘要校验失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不暴露正文、身份或路径的稳定失败码。"""

        super().__init__("formal model retraining snapshot artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RetrainingSnapshotPackageResult:
    """不含正文、源身份或本机路径的训练快照封存摘要。"""

    snapshot_id: str
    manifest_sha256: str
    records_sha256: str
    count: int
    related_count: int
    unrelated_count: int
    component_count: int
    historical_test_consumed_count: int
    reused: bool
    status: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序、紧凑、拒绝NaN且以换行结束的规范JSON。"""

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
    """计算字节流SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算已封存文件SHA-256。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子发布一个不可变artifact文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> RetrainingSnapshotPackageResult:
    """从已验证manifest生成公开摘要。"""

    return RetrainingSnapshotPackageResult(
        snapshot_id=str(manifest["snapshot_id"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        records_sha256=str(manifest["artifacts"]["records"]["sha256"]),
        count=int(manifest["count"]),
        related_count=int(manifest["related_count"]),
        unrelated_count=int(manifest["unrelated_count"]),
        component_count=int(manifest["component_count"]),
        historical_test_consumed_count=int(
            manifest["historical_test_consumed_count"]
        ),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_existing(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> RetrainingSnapshotPackageResult:
    """验证既有包全部哈希与训练边界。"""

    manifest_path = directory / "snapshot-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_manifest_hash_mismatch"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_manifest_invalid"
        ) from exc
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(artifacts, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-snapshot"
        or manifest.get("status") != "TRAINING_SNAPSHOT_FROZEN"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("plan_sha256") != plan.plan_sha256
        or manifest.get("count") != plan.expected_training_count
        or manifest.get("labels_entered_fit") is True
        or manifest.get("old_locked_test_reopened") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_manifest_invalid"
        )
    for key, filename in {
        "records": "training-records.json",
        "summary": "snapshot-summary.json",
    }.items():
        details = artifacts.get(key)
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingSnapshotArtifactError(
                "model_retraining_snapshot_artifact_hash_mismatch"
            )
    return _result(manifest, manifest_path.read_bytes(), reused=True)


def freeze_retraining_snapshot_package(
    snapshot: RetrainingSnapshot,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> RetrainingSnapshotPackageResult:
    """封存包含私有文本的训练记录及去敏manifest。

    Args:
        snapshot: 已通过全部成员与分量校验的1,300条快照。
        artifact_root: 被Git忽略的结果根目录。
        plan: 冻结Issue #49计划。
        code_version: 执行时完整Git commit。
        expected_existing_manifest_sha256: 严格复用既有运行时必填哈希。

    Returns:
        去敏的运行身份与文件摘要。

    Raises:
        ModelRetrainingSnapshotArtifactError: 输入、目标冲突或不可变性失败。
    """

    if (
        snapshot.count != plan.expected_training_count
        or snapshot.related_count != plan.expected_related_count
        or snapshot.unrelated_count != plan.expected_unrelated_count
        or len(code_version) != 40
        or any(character not in "0123456789abcdef" for character in code_version)
    ):
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_package_input_invalid"
        )
    records_payload = [asdict(item) for item in snapshot.documents]
    records_bytes = _canonical_bytes(records_payload)
    records_sha256 = _sha256_bytes(records_bytes)
    snapshot_id = hashlib.sha256(
        f"{plan.plan_id}|{snapshot.member_binding_sha256}|{records_sha256}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / snapshot_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingSnapshotArtifactError(
                "model_retraining_snapshot_existing_requires_manifest_hash"
            )
        return _validate_existing(
            directory,
            plan=plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    summary = {
        "artifact_kind": "formal-cleaning-model-retraining-snapshot-summary",
        "snapshot_id": snapshot_id,
        "count": snapshot.count,
        "related_count": snapshot.related_count,
        "unrelated_count": snapshot.unrelated_count,
        "component_count": snapshot.component_count,
        "origin_counts": dict(snapshot.origin_counts),
        "historical_test_consumed_count": snapshot.historical_test_consumed_count,
        "sampling_weights_enter_fit": False,
        "platform_used": False,
    }
    summary_bytes = _canonical_bytes(summary)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-snapshot",
        "status": "TRAINING_SNAPSHOT_FROZEN",
        "snapshot_id": snapshot_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "count": snapshot.count,
        "related_count": snapshot.related_count,
        "unrelated_count": snapshot.unrelated_count,
        "component_count": snapshot.component_count,
        "origin_counts": dict(snapshot.origin_counts),
        "historical_test_consumed_count": snapshot.historical_test_consumed_count,
        "member_binding_sha256": snapshot.member_binding_sha256,
        "leakage_output_sha256": snapshot.leakage_output_sha256,
        "labels_entered_fit": False,
        "old_locked_test_reopened": False,
        "platform_used": False,
        "artifacts": {
            "records": {
                "filename": "training-records.json",
                "sha256": records_sha256,
            },
            "summary": {
                "filename": "snapshot-summary.json",
                "sha256": _sha256_bytes(summary_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_directory_create_failed"
        ) from exc
    _atomic_write(directory / "training-records.json", records_bytes)
    _atomic_write(directory / "snapshot-summary.json", summary_bytes)
    _atomic_write(directory / "snapshot-manifest.json", manifest_bytes)
    return _result(manifest, manifest_bytes, reused=False)


def render_retraining_snapshot_result(
    result: RetrainingSnapshotPackageResult, *, output_format: str
) -> str:
    """把快照结果渲染为机器JSON或不含私有信息的中文摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_output_format_invalid"
        )
    return "\n".join(
        [
            "# 1300条模型重训快照",
            "",
            f"快照ID：{result.snapshot_id}",
            f"artifact：{'严格复用' if result.reused else '新建并封存'}",
            (
                f"成员：{result.count}；related={result.related_count}；"
                f"unrelated={result.unrelated_count}；"
                f"leakage components={result.component_count}"
            ),
            f"旧测试已消费成员：{result.historical_test_consumed_count}（只可训练，不再测试）",
            "抽样权重和平台均不进入fit；旧锁定测试未重开。",
            f"状态：{result.status}",
        ]
    )
