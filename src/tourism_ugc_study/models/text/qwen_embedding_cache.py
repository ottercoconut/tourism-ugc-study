"""冻结Qwen完整分块向量的跨批次内容寻址缓存。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_embeddings import COMPLETE_CHUNK_ENCODING_ALGORITHM_ID
from .qwen_complete_chunk_runtime import LocalQwenCompleteChunkEncoder
from .qwen_embedding_runtime import QwenModelSnapshot


QWEN_EMBEDDING_CACHE_ALGORITHM_ID = (
    "qwen-complete-chunk-content-addressed-vector-cache-v1"
)


class QwenEmbeddingCacheError(RuntimeError):
    """向量缓存身份、内容或持久化违反契约时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、帖子身份或私有路径的稳定失败码。

        Args:
            reason_code: CLI、运行包和测试共享的稳定失败原因。
        """

        super().__init__("qwen embedding cache failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenEmbeddingCacheLookup:
    """一次缓存命中或首次编码的去敏结果。"""

    embedding: np.ndarray
    cache_namespace_id: str
    cache_key: str
    cache_hit: bool
    receipt_sha256: str


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
    """流式计算缓存文件SHA-256并隐藏本机路径。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise QwenEmbeddingCacheError(
            "qwen_embedding_cache_file_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path) -> Mapping[str, Any]:
    """读取缓存回执并统一损坏语义。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenEmbeddingCacheError(
            "qwen_embedding_cache_receipt_invalid"
        ) from exc
    if not isinstance(value, Mapping):
        raise QwenEmbeddingCacheError("qwen_embedding_cache_receipt_invalid")
    return value


def _npy_bytes(embedding: np.ndarray) -> bytes:
    """把确定float32向量序列化为禁止pickle的NumPy字节。"""

    stream = io.BytesIO()
    try:
        np.save(stream, embedding, allow_pickle=False)
    except (TypeError, ValueError) as exc:
        raise QwenEmbeddingCacheError(
            "qwen_embedding_cache_vector_serialize_failed"
        ) from exc
    return stream.getvalue()


class QwenEmbeddingCache:
    """跨批次复用同一规范正文的Qwen完整分块向量。

    缓存命名空间同时绑定Qwen权重快照、重训计划和完整分块算法。条目只以
    ``normalized_sha256``寻址，不保存帖子身份；同一帖子正文不变时命中，正文
    或任一模型身份变化时进入不同条目或命名空间，禁止错误复用。
    """

    def __init__(
        self,
        cache_root: str | Path,
        *,
        plan: ModelRetrainingPlan,
        model_snapshot: QwenModelSnapshot,
        encoder: LocalQwenCompleteChunkEncoder,
    ) -> None:
        """绑定缓存根、冻结计划、权重快照和只读编码器。

        Args:
            cache_root: Git忽略的共享向量缓存根目录。
            plan: 冻结的1,300条模型计划，包含instruction和向量维度。
            model_snapshot: 已逐文件校验的Qwen公开权重身份。
            encoder: 完整连续分块、零省略的只读Qwen编码器。

        Raises:
            QwenEmbeddingCacheError: 权重、维度或最大长度与计划不一致，或
                缓存目录无法安全创建。
        """

        if (
            model_snapshot.repository != plan.qwen_repository
            or model_snapshot.revision != plan.qwen_revision
            or model_snapshot.embedding_dimension
            != plan.qwen_embedding_dimension
            or model_snapshot.max_length != plan.qwen_max_length
        ):
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_model_binding_mismatch"
            )
        namespace_payload = {
            "algorithm_id": QWEN_EMBEDDING_CACHE_ALGORITHM_ID,
            "encoding_algorithm_id": COMPLETE_CHUNK_ENCODING_ALGORITHM_ID,
            "plan_sha256": plan.plan_sha256,
            "repository": model_snapshot.repository,
            "revision": model_snapshot.revision,
            "weights_sha256": model_snapshot.weights_sha256,
            "snapshot_sha256": model_snapshot.snapshot_sha256,
            "embedding_dimension": model_snapshot.embedding_dimension,
            "max_length": model_snapshot.max_length,
        }
        # 缺省仍使用历史命名空间；显式CUDA执行必须与MPS及不同依赖版本隔离。
        self.execution_identity = getattr(encoder, "inference_execution_identity", None)
        if self.execution_identity is not None:
            namespace_payload["inference_execution_profile"] = self.execution_identity
        self.namespace_id = _sha256_bytes(_canonical_bytes(namespace_payload))[:32]
        self._namespace_payload = namespace_payload
        self._dimension = model_snapshot.embedding_dimension
        self._encoder = encoder
        try:
            self._directory = (
                Path(cache_root).expanduser().resolve() / self.namespace_id
            )
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_directory_unavailable"
            ) from exc

    def _entry_directory(self, normalized_sha256: str) -> Path:
        """把规范正文摘要映射到分片后的条目目录。"""

        if (
            len(normalized_sha256) != 64
            or any(character not in "0123456789abcdef" for character in normalized_sha256)
        ):
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_normalized_sha256_invalid"
            )
        return (
            self._directory
            / normalized_sha256[:2]
            / normalized_sha256
        )

    def _validate_embedding(self, embedding: np.ndarray) -> np.ndarray:
        """要求缓存或新编码向量有限、定维且逐条L2规范化。"""

        vector = np.asarray(embedding, dtype=np.float32)
        if (
            vector.shape != (self._dimension,)
            or not np.isfinite(vector).all()
            or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5)
        ):
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_vector_invalid"
            )
        return np.ascontiguousarray(vector)

    def _load_entry(
        self, entry: Path, *, normalized_sha256: str
    ) -> QwenEmbeddingCacheLookup:
        """完整校验既有条目及向量字节后返回缓存命中。"""

        receipt_path = entry / "receipt.json"
        vector_path = entry / "embedding.npy"
        if not receipt_path.is_file() or not vector_path.is_file():
            raise QwenEmbeddingCacheError("qwen_embedding_cache_entry_incomplete")
        receipt = _load_json(receipt_path)
        if (
            receipt.get("artifact_kind")
            != "formal-cleaning-qwen-content-addressed-embedding"
            or receipt.get("inference_execution_profile") != self.execution_identity
            or receipt.get("cache_namespace_id") != self.namespace_id
            or receipt.get("normalized_sha256") != normalized_sha256
            or receipt.get("algorithm_id")
            != QWEN_EMBEDDING_CACHE_ALGORITHM_ID
            or receipt.get("encoding_algorithm_id")
            != COMPLETE_CHUNK_ENCODING_ALGORITHM_ID
            or receipt.get("plan_sha256")
            != self._namespace_payload["plan_sha256"]
            or receipt.get("qwen_snapshot_sha256")
            != self._namespace_payload["snapshot_sha256"]
            or receipt.get("embedding_dimension") != self._dimension
            or receipt.get("omitted_token_count") != 0
            or receipt.get("embedding_filename") != "embedding.npy"
            or receipt.get("embedding_file_sha256")
            != _file_sha256(vector_path)
        ):
            raise QwenEmbeddingCacheError("qwen_embedding_cache_receipt_invalid")
        try:
            embedding = self._validate_embedding(
                np.load(vector_path, allow_pickle=False)
            )
        except (OSError, ValueError, TypeError) as exc:
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_vector_invalid"
            ) from exc
        if receipt.get("embedding_value_sha256") != _sha256_bytes(
            embedding.tobytes(order="C")
        ):
            raise QwenEmbeddingCacheError("qwen_embedding_cache_vector_invalid")
        try:
            receipt_bytes = receipt_path.read_bytes()
        except OSError as exc:
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_receipt_invalid"
            ) from exc
        return QwenEmbeddingCacheLookup(
            embedding=embedding,
            cache_namespace_id=self.namespace_id,
            cache_key=f"{self.namespace_id}:{normalized_sha256}",
            cache_hit=True,
            receipt_sha256=_sha256_bytes(receipt_bytes),
        )

    def get_or_encode(
        self, text: str, *, normalized_sha256: str
    ) -> QwenEmbeddingCacheLookup:
        """读取同正文向量，缺失时只编码一次并原子发布。

        Args:
            text: 已由冻结规范化规则生成的模型正文。
            normalized_sha256: ``text``的SHA-256，作为跨帖子、跨批次缓存键。

        Returns:
            向量、命名空间、缓存键、命中状态和回执摘要。

        Raises:
            QwenEmbeddingCacheError: 正文摘要不符、既有条目不完整、向量损坏、
                编码省略token或条目无法原子发布。

        Notes:
            已存在但损坏的条目不会被静默重算覆盖；必须先显式排查缓存损坏。
            不同帖子只要规范正文完全一致即可安全共享向量。
        """

        if (
            not isinstance(text, str)
            or not text.strip()
            or _sha256_bytes(text.encode("utf-8")) != normalized_sha256
        ):
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_text_binding_mismatch"
            )
        entry = self._entry_directory(normalized_sha256)
        if entry.exists():
            return self._load_entry(entry, normalized_sha256=normalized_sha256)
        encoded = self._encoder.encode_document(text)
        if encoded.diagnostics.omitted_token_count != 0:
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_omitted_tokens_nonzero"
            )
        embedding = self._validate_embedding(encoded.embedding)
        vector_bytes = _npy_bytes(embedding)
        receipt = {
            "artifact_kind": "formal-cleaning-qwen-content-addressed-embedding",
            "inference_execution_profile": self.execution_identity,
            "cache_namespace_id": self.namespace_id,
            "normalized_sha256": normalized_sha256,
            "algorithm_id": QWEN_EMBEDDING_CACHE_ALGORITHM_ID,
            "encoding_algorithm_id": COMPLETE_CHUNK_ENCODING_ALGORITHM_ID,
            "plan_sha256": self._namespace_payload["plan_sha256"],
            "qwen_snapshot_sha256": self._namespace_payload["snapshot_sha256"],
            "embedding_dimension": self._dimension,
            "omitted_token_count": 0,
            "embedding_filename": "embedding.npy",
            "embedding_file_sha256": _sha256_bytes(vector_bytes),
            "embedding_value_sha256": _sha256_bytes(
                embedding.tobytes(order="C")
            ),
        }
        receipt_bytes = _canonical_bytes(receipt)
        try:
            entry.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{normalized_sha256}.", dir=entry.parent)
            )
            try:
                (temporary / "embedding.npy").write_bytes(vector_bytes)
                (temporary / "receipt.json").write_bytes(receipt_bytes)
                try:
                    os.replace(temporary, entry)
                except OSError:
                    # 并发进程可能已发布同一内容键；只接受随后可完整验证的条目。
                    if not entry.exists():
                        raise
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        except OSError as exc:
            raise QwenEmbeddingCacheError(
                "qwen_embedding_cache_entry_write_failed"
            ) from exc
        loaded = self._load_entry(entry, normalized_sha256=normalized_sha256)
        return QwenEmbeddingCacheLookup(
            embedding=loaded.embedding,
            cache_namespace_id=loaded.cache_namespace_id,
            cache_key=loaded.cache_key,
            cache_hit=False,
            receipt_sha256=loaded.receipt_sha256,
        )
