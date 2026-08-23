"""Qwen3-Embedding 权重准备、身份校验与本地只读编码运行时。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .qwen_embedding_config import QwenEmbeddingPlan


class QwenEmbeddingRuntimeError(RuntimeError):
    """本地模型目录、依赖或编码输出非法时抛出的去敏异常。

    Attributes:
        reason_code: 不包含帖子正文或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: CLI、测试和 manifest 共享的失败码。
        """

        super().__init__("qwen embedding local runtime failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenModelSnapshot:
    """经校验的本地权重身份，不暴露本机目录。"""

    repository: str
    revision: str
    weights_sha256: str
    embedding_dimension: int
    max_length: int
    reused: bool


def _file_sha256(path: Path) -> str:
    """流式计算大文件 SHA-256。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_model_file_unreadable"
        ) from exc
    return digest.hexdigest()


def validate_qwen_model_directory(
    model_dir: str | Path,
    *,
    plan: QwenEmbeddingPlan,
    reused: bool = True,
) -> QwenModelSnapshot:
    """校验本地模型快照与预登记权重身份。

    Args:
        model_dir: ``snapshot_download`` 生成的本地目录。
        plan: 冻结 Qwen 语义 baseline 计划。
        reused: 摘要中是否标记为复用既有目录。

    Returns:
        不含路径的本地快照身份。

    Raises:
        QwenEmbeddingRuntimeError: 目录、配置或主权重摘要不匹配。
    """

    try:
        directory = Path(model_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_model_directory_unavailable"
        ) from exc
    if not directory.is_dir():
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_model_directory_unavailable"
        )
    required = {
        "config.json",
        "modules.json",
        "tokenizer.json",
        "tokenizer_config.json",
        plan.encoder.weights_filename,
    }
    if any(not (directory / name).is_file() for name in required):
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_snapshot_incomplete")
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid") from exc
    if (
        not isinstance(config, dict)
        or config.get("model_type") != "qwen3"
        or int(config.get("hidden_size", -1)) != plan.encoder.embedding_dimension
    ):
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid")
    weights_sha256 = _file_sha256(directory / plan.encoder.weights_filename)
    if weights_sha256 != plan.encoder.weights_sha256:
        raise QwenEmbeddingRuntimeError("qwen_embedding_weights_hash_mismatch")
    return QwenModelSnapshot(
        repository=plan.encoder.repository,
        revision=plan.encoder.revision,
        weights_sha256=weights_sha256,
        embedding_dimension=plan.encoder.embedding_dimension,
        max_length=plan.encoder.max_length,
        reused=reused,
    )


def prepare_qwen_model_directory(
    model_dir: str | Path,
    *,
    plan: QwenEmbeddingPlan,
) -> QwenModelSnapshot:
    """按完整 revision 下载并原子发布本地模型目录。

    Args:
        model_dir: 模型权重目标目录，建议位于仓库外的相邻目录。
        plan: 冻结模型仓库、revision 和权重摘要。

    Returns:
        已校验模型快照；既有合法目录只读复用。

    Raises:
        QwenEmbeddingRuntimeError: 依赖缺失、下载失败、目标冲突或摘要不符。

    Notes:
        本函数只下载公开模型文件，不读取任何 UGC。目标已存在但非法时失败
        关闭，绝不覆盖或删除用户文件。
    """

    target = Path(model_dir).expanduser().resolve()
    if target.exists():
        return validate_qwen_model_directory(target, plan=plan, reused=True)
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_download_dependency_missing"
        ) from exc
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
        )
    except OSError as exc:
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_model_target_unavailable"
        ) from exc
    published = False
    try:
        try:
            snapshot_download(
                repo_id=plan.encoder.repository,
                revision=plan.encoder.revision,
                local_dir=temporary,
            )
        except Exception as exc:  # 上游客户端具有多种网络与缓存异常类型。
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_model_download_failed"
            ) from exc
        snapshot = validate_qwen_model_directory(
            temporary, plan=plan, reused=False
        )
        try:
            temporary.rename(target)
        except OSError as exc:
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_model_publish_failed"
            ) from exc
        published = True
        return snapshot
    finally:
        if not published and temporary.exists():
            shutil.rmtree(temporary)


class LocalQwenEmbeddingEncoder:
    """延迟加载 Sentence Transformers 的本地冻结编码器。

    编码器只从已经过哈希校验的本地目录加载，禁止远程回退和远程代码；
    ``encode`` 不持有标签，也不提供任何拟合接口。
    """

    def __init__(
        self,
        model_dir: str | Path,
        *,
        plan: QwenEmbeddingPlan,
        device: str = "auto",
        batch_size: int = 4,
    ) -> None:
        """记录冻结运行参数并校验模型目录。

        Args:
            model_dir: 仓库外的本地模型目录。
            plan: 冻结编码器与文本投影。
            device: ``auto``、``mps`` 或 ``cpu``。
            batch_size: 仅影响吞吐的正整数，不改变成员或模型选择。
        """

        if device not in {"auto", "mps", "cpu"}:
            raise QwenEmbeddingRuntimeError("qwen_embedding_device_invalid")
        if isinstance(batch_size, bool) or batch_size < 1:
            raise QwenEmbeddingRuntimeError("qwen_embedding_batch_size_invalid")
        validate_qwen_model_directory(model_dir, plan=plan)
        self._model_dir = Path(model_dir).expanduser().resolve()
        self._plan = plan
        self._requested_device = device
        self._batch_size = int(batch_size)
        self._model = None
        self._device: str | None = None

    @property
    def device(self) -> str:
        """返回实际设备；首次访问会加载本地模型。"""

        self._ensure_loaded()
        if self._device is None:
            raise QwenEmbeddingRuntimeError("qwen_embedding_model_load_failed")
        return self._device

    def _ensure_loaded(self) -> None:
        """首次编码时加载依赖与模型，后续复用同一只读实例。"""

        if self._model is not None:
            return
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_runtime_dependency_missing"
            ) from exc
        if self._requested_device == "auto":
            selected = "mps" if torch.backends.mps.is_available() else "cpu"
        else:
            selected = self._requested_device
        if selected == "mps" and not torch.backends.mps.is_available():
            raise QwenEmbeddingRuntimeError("qwen_embedding_mps_unavailable")
        try:
            model = SentenceTransformer(
                str(self._model_dir),
                device=selected,
                local_files_only=True,
                trust_remote_code=False,
            )
            model.max_seq_length = self._plan.encoder.max_length
        except Exception as exc:  # 上游加载异常需统一去敏。
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_model_load_failed"
            ) from exc
        self._model = model
        self._device = selected

    def encode(
        self,
        texts: Sequence[str],
        *,
        show_progress: bool = False,
    ) -> np.ndarray:
        """用固定任务指令生成 L2 归一化语义向量。

        Args:
            texts: 已冻结的单通道规范化文本。
            show_progress: 是否显示本地批处理进度。

        Returns:
            与输入等长的有限 ``float32`` 二维矩阵。

        Raises:
            QwenEmbeddingRuntimeError: 文本或编码输出违反冻结契约。
        """

        if not texts or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise QwenEmbeddingRuntimeError("qwen_embedding_text_invalid")
        self._ensure_loaded()
        prompt = f"Instruct: {self._plan.encoder.instruction}\nQuery: "
        try:
            values = self._model.encode(
                list(texts),
                prompt=prompt,
                batch_size=self._batch_size,
                show_progress_bar=show_progress,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        except Exception as exc:  # 编码正文不得进入异常文本。
            raise QwenEmbeddingRuntimeError("qwen_embedding_encode_failed") from exc
        embeddings = np.asarray(values, dtype=np.float32)
        if (
            embeddings.shape
            != (len(texts), self._plan.encoder.embedding_dimension)
            or not np.isfinite(embeddings).all()
        ):
            raise QwenEmbeddingRuntimeError("qwen_embedding_output_invalid")
        norms = np.linalg.norm(embeddings, axis=1)
        if not np.allclose(norms, 1.0, atol=1e-4, rtol=1e-4):
            raise QwenEmbeddingRuntimeError("qwen_embedding_output_not_normalized")
        return embeddings
