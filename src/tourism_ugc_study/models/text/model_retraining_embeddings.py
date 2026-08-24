"""1,300条Qwen完整分块编码的checkpoint、严格复用与封存。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_snapshot_artifacts import load_retraining_snapshot_package
from .qwen_complete_chunk_runtime import LocalQwenCompleteChunkEncoder
from .qwen_embedding_config import QwenEmbeddingPlan
from .qwen_embedding_runtime import QwenModelSnapshot


COMPLETE_CHUNK_ENCODING_ALGORITHM_ID = (
    "qwen-complete-contiguous-chunks-preserve-content-tokens-v2"
)


class ModelRetrainingEmbeddingError(RuntimeError):
    """完整编码输入、checkpoint或不可变包失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、身份或私有路径的稳定失败码。"""

        super().__init__("formal model retraining embedding failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RetrainingEmbeddingPackageResult:
    """不含向量、正文或源身份的编码运行摘要。"""

    encoding_id: str
    snapshot_id: str
    count: int
    encoded_view_count: int
    multi_view_count: int
    omitted_token_count: int
    original_token_count_max: int
    maximum_view_count: int
    boundary_adjusted_view_count: int
    manifest_sha256: str
    embeddings_sha256: str
    reused: bool
    resumed_record_count: int
    status: str


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
    """计算字节流SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算文件摘要并统一读取失败。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子替换checkpoint或最终manifest。"""

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
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingEmbeddingError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingEmbeddingError(reason_code)
    return value


def _validate_embedding(path: Path, dimension: int) -> np.ndarray:
    """读取一个checkpoint向量并验证维数、有限性与L2范数。"""

    try:
        value = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_checkpoint_invalid"
        ) from exc
    embedding = np.asarray(value, dtype=np.float32)
    if (
        embedding.shape != (dimension,)
        or not np.isfinite(embedding).all()
        or not np.isclose(np.linalg.norm(embedding), 1.0, atol=1e-5)
    ):
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_checkpoint_invalid"
        )
    return embedding


def _result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> RetrainingEmbeddingPackageResult:
    """从已验证manifest生成公开结果。"""

    diagnostics = manifest["diagnostics"]
    return RetrainingEmbeddingPackageResult(
        encoding_id=str(manifest["encoding_id"]),
        snapshot_id=str(manifest["snapshot_id"]),
        count=int(manifest["count"]),
        encoded_view_count=int(diagnostics["encoded_view_count"]),
        multi_view_count=int(diagnostics["multi_view_count"]),
        omitted_token_count=int(diagnostics["omitted_token_count"]),
        original_token_count_max=int(diagnostics["original_token_count_max"]),
        maximum_view_count=int(diagnostics["maximum_view_count"]),
        boundary_adjusted_view_count=int(
            diagnostics["boundary_adjusted_view_count"]
        ),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        embeddings_sha256=str(manifest["artifacts"]["embeddings"]["sha256"]),
        reused=reused,
        resumed_record_count=int(manifest.get("resumed_record_count", 0)),
        status=str(manifest["status"]),
    )


def _validate_final(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> RetrainingEmbeddingPackageResult:
    """严格复用最终包，拒绝只凭目录存在跳过编码。"""

    manifest_path = directory / "encoding-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_embedding_manifest_invalid"
    )
    embeddings = manifest.get("artifacts", {}).get("embeddings", {})
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-qwen-embeddings"
        or manifest.get("status") != "QWEN_COMPLETE_ENCODING_FROZEN"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("algorithm_id")
        != COMPLETE_CHUNK_ENCODING_ALGORITHM_ID
        or manifest.get("count") != plan.expected_training_count
        or manifest.get("omitted_token_count_required") != 0
        or manifest.get("diagnostics", {}).get("omitted_token_count") != 0
        or embeddings.get("filename") != "qwen-complete-embeddings.npz"
        or _file_sha256(directory / embeddings["filename"])
        != embeddings.get("sha256")
    ):
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_manifest_invalid"
        )
    return _result(manifest, manifest_path.read_bytes(), reused=True)


def load_retraining_embeddings_package(
    package: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
    expected_member_keys: list[str],
) -> tuple[np.ndarray, Mapping[str, Any]]:
    """严格读取与训练快照同序的完整Qwen向量矩阵。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_package_unavailable"
        ) from exc
    _validate_final(
        directory,
        plan=plan,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    manifest = _load_json(
        directory / "encoding-manifest.json",
        "model_retraining_embedding_manifest_invalid",
    )
    try:
        with np.load(
            directory / "qwen-complete-embeddings.npz", allow_pickle=False
        ) as loaded:
            matrix = np.asarray(loaded["embeddings"], dtype=np.float32)
            member_keys = [str(value) for value in loaded["member_keys"]]
    except (OSError, ValueError, KeyError) as exc:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_matrix_invalid"
        ) from exc
    if (
        matrix.shape
        != (plan.expected_training_count, plan.qwen_embedding_dimension)
        or member_keys != expected_member_keys
        or not np.isfinite(matrix).all()
        or not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    ):
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_matrix_invalid"
        )
    return matrix, manifest


def encode_retraining_embeddings_package(
    snapshot_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    base_plan: QwenEmbeddingPlan,
    encoder: LocalQwenCompleteChunkEncoder,
    model_snapshot: QwenModelSnapshot,
    expected_snapshot_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
    show_progress: bool = True,
) -> RetrainingEmbeddingPackageResult:
    """逐记录编码1,300条完整正文，支持严格checkpoint续跑。

    checkpoint只保存模型向量、去标识成员键和长度诊断；每次恢复都重新验证
    计划、快照、成员顺序及每个向量文件哈希。最终包不含标签拟合。
    """

    snapshot, snapshot_manifest = load_retraining_snapshot_package(
        snapshot_package,
        plan=plan,
        expected_manifest_sha256=expected_snapshot_manifest_sha256,
    )
    if len(code_version) != 40:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_code_version_invalid"
        )
    encoding_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{snapshot_manifest['snapshot_id']}|"
            f"{model_snapshot.snapshot_sha256}|"
            f"{COMPLETE_CHUNK_ENCODING_ALGORITHM_ID}|{code_version}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / encoding_id
    final_manifest = directory / "encoding-manifest.json"
    if final_manifest.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingEmbeddingError(
                "model_retraining_embedding_existing_requires_manifest_hash"
            )
        return _validate_final(
            directory,
            plan=plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        checkpoint_directory = directory / "checkpoint-records"
        checkpoint_directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_directory_create_failed"
        ) from exc
    state_path = directory / "checkpoint-state.json"
    state: dict[str, Any]
    if state_path.exists():
        loaded = _load_json(
            state_path, "model_retraining_embedding_checkpoint_invalid"
        )
        if (
            loaded.get("encoding_id") != encoding_id
            or loaded.get("plan_sha256") != plan.plan_sha256
            or loaded.get("snapshot_manifest_sha256")
            != expected_snapshot_manifest_sha256
            or loaded.get("member_binding_sha256")
            != snapshot.member_binding_sha256
            or loaded.get("algorithm_id")
            != COMPLETE_CHUNK_ENCODING_ALGORITHM_ID
            or loaded.get("code_version") != code_version
            or not isinstance(loaded.get("records"), list)
        ):
            raise ModelRetrainingEmbeddingError(
                "model_retraining_embedding_checkpoint_invalid"
            )
        state = dict(loaded)
    else:
        state = {
            "artifact_kind": "formal-cleaning-model-retraining-embedding-checkpoint",
            "encoding_id": encoding_id,
            "plan_sha256": plan.plan_sha256,
            "snapshot_manifest_sha256": expected_snapshot_manifest_sha256,
            "member_binding_sha256": snapshot.member_binding_sha256,
            "algorithm_id": COMPLETE_CHUNK_ENCODING_ALGORITHM_ID,
            "code_version": code_version,
            "records": [],
        }
        _atomic_write(state_path, _canonical_bytes(state))
    recorded = list(state["records"])
    if len(recorded) > snapshot.count:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_checkpoint_invalid"
        )
    embeddings: list[np.ndarray] = []
    diagnostics: list[Mapping[str, Any]] = []
    for index, receipt in enumerate(recorded):
        document = snapshot.documents[index]
        filename = f"{index:04d}-{document.member_key}.npy"
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("member_key") != document.member_key
            or receipt.get("normalized_sha256") != document.normalized_sha256
            or receipt.get("filename") != filename
            or _file_sha256(checkpoint_directory / filename)
            != receipt.get("sha256")
        ):
            raise ModelRetrainingEmbeddingError(
                "model_retraining_embedding_checkpoint_invalid"
            )
        embeddings.append(
            _validate_embedding(
                checkpoint_directory / filename,
                plan.qwen_embedding_dimension,
            )
        )
        receipt_diagnostics = receipt.get("diagnostics")
        if not isinstance(receipt_diagnostics, Mapping):
            raise ModelRetrainingEmbeddingError(
                "model_retraining_embedding_checkpoint_invalid"
            )
        diagnostics.append(dict(receipt_diagnostics))
    resumed_count = len(recorded)
    for index in range(resumed_count, snapshot.count):
        document = snapshot.documents[index]
        result = encoder.encode_document(
            document.normalized_model_text, show_progress=show_progress
        )
        if result.diagnostics.omitted_token_count != 0:
            raise ModelRetrainingEmbeddingError(
                "model_retraining_embedding_omitted_tokens_nonzero"
            )
        filename = f"{index:04d}-{document.member_key}.npy"
        target = checkpoint_directory / filename
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.", dir=checkpoint_directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with temporary.open("wb") as stream:
                np.save(stream, result.embedding, allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            raise ModelRetrainingEmbeddingError(
                "model_retraining_embedding_checkpoint_write_failed"
            ) from exc
        finally:
            if temporary.exists():
                temporary.unlink()
        receipt = {
            "member_key": document.member_key,
            "normalized_sha256": document.normalized_sha256,
            "filename": filename,
            "sha256": _file_sha256(target),
            "diagnostics": asdict(result.diagnostics),
        }
        recorded.append(receipt)
        embeddings.append(result.embedding)
        diagnostics.append(receipt["diagnostics"])
        state["records"] = recorded
        _atomic_write(state_path, _canonical_bytes(state))

    matrix = np.stack(embeddings).astype(np.float32, copy=False)
    if (
        matrix.shape
        != (snapshot.count, plan.qwen_embedding_dimension)
        or not np.isfinite(matrix).all()
        or not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    ):
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_matrix_invalid"
        )
    embeddings_path = directory / "qwen-complete-embeddings.npz"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".qwen-complete-embeddings.", dir=directory
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as stream:
            np.savez_compressed(
                stream,
                embeddings=matrix,
                member_keys=np.asarray(
                    [item.member_key for item in snapshot.documents], dtype="U24"
                ),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, embeddings_path)
    except OSError as exc:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    aggregate_diagnostics = {
        "encoded_view_count": sum(int(item["encoded_view_count"]) for item in diagnostics),
        "multi_view_count": sum(int(item["encoded_view_count"]) > 1 for item in diagnostics),
        "omitted_token_count": sum(int(item["omitted_token_count"]) for item in diagnostics),
        "original_token_count_max": max(int(item["original_input_token_count"]) for item in diagnostics),
        "maximum_view_count": max(int(item["encoded_view_count"]) for item in diagnostics),
        "boundary_adjusted_view_count": sum(int(item["boundary_adjusted_view_count"]) for item in diagnostics),
        "content_window_token_budget": min(int(item["content_window_token_budget"]) for item in diagnostics),
        "encoded_view_token_count_max": max(int(item["encoded_view_token_count_max"]) for item in diagnostics),
    }
    if aggregate_diagnostics["omitted_token_count"] != 0:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_omitted_tokens_nonzero"
        )
    diagnostics_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-model-retraining-encoding-diagnostics",
            "encoding_id": encoding_id,
            "count": snapshot.count,
            "aggregate": aggregate_diagnostics,
            "records": diagnostics,
        }
    )
    _atomic_write(directory / "encoding-diagnostics.json", diagnostics_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-qwen-embeddings",
        "status": "QWEN_COMPLETE_ENCODING_FROZEN",
        "encoding_id": encoding_id,
        "snapshot_id": str(snapshot_manifest["snapshot_id"]),
        "snapshot_manifest_sha256": expected_snapshot_manifest_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "count": snapshot.count,
        "model_snapshot_sha256": model_snapshot.snapshot_sha256,
        "model_weights_sha256": model_snapshot.weights_sha256,
        "projection": "continuous_complete_chunks",
        "algorithm_id": COMPLETE_CHUNK_ENCODING_ALGORITHM_ID,
        "aggregation": "arithmetic_mean_then_l2_normalize",
        "omitted_token_count_required": 0,
        "fit_call_count": 0,
        "platform_used": False,
        "resumed_record_count": resumed_count,
        "diagnostics": aggregate_diagnostics,
        "artifacts": {
            "embeddings": {
                "filename": embeddings_path.name,
                "sha256": _file_sha256(embeddings_path),
            },
            "diagnostics": {
                "filename": "encoding-diagnostics.json",
                "sha256": _sha256_bytes(diagnostics_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(final_manifest, manifest_bytes)
    return _result(manifest, manifest_bytes, reused=False)


def render_retraining_embedding_result(
    result: RetrainingEmbeddingPackageResult, *, output_format: str
) -> str:
    """输出机器JSON或可读的完整分块编码摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingEmbeddingError(
            "model_retraining_embedding_output_format_invalid"
        )
    return "\n".join(
        [
            "# Qwen完整连续分块编码结果",
            "",
            f"编码ID：{result.encoding_id}",
            f"训练快照：{result.snapshot_id}；成员={result.count}",
            f"artifact：{'严格复用' if result.reused else '新建并封存'}；恢复记录={result.resumed_record_count}",
            (
                f"编码视图：{result.encoded_view_count}；多视图记录={result.multi_view_count}；"
                f"单记录最多视图={result.maximum_view_count}"
            ),
            (
                f"原输入token最大值={result.original_token_count_max}；"
                f"边界调整视图={result.boundary_adjusted_view_count}"
            ),
            f"省略token：{result.omitted_token_count}（必须为0）",
            "fit调用：0；平台未使用；历史测试未重开。",
            f"状态：{result.status}",
        ]
    )
