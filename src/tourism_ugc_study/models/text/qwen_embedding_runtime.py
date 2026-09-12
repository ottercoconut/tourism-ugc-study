"""Qwen3-Embedding 权重准备、身份校验与本地只读编码运行时。"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

import numpy as np

from .qwen_embedding_config import QwenEmbeddingPlan
from .qwen_head_tail_config import QwenHeadTailPlan


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
    snapshot_sha256: str
    embedding_dimension: int
    max_length: int
    reused: bool


@dataclass(frozen=True)
class QwenExecutionReceipt:
    """不含路径的实际本地编码执行身份。"""

    device: str
    batch_size: int
    parameter_dtype: str
    output_dtype: str
    python_version: str
    operating_system: str
    operating_system_release: str
    macos_version: str
    machine: str
    hardware_model: str


@dataclass(frozen=True)
class QwenEncodingDiagnostics:
    """不含正文或成员身份的 token 截断诊断。"""

    count: int
    truncated_count: int
    truncated_rate: float
    token_count_max: int
    token_count_p95: float
    max_length: int


@dataclass(frozen=True)
class QwenEncodingResult:
    """语义向量及其聚合截断诊断。"""

    embeddings: np.ndarray
    diagnostics: QwenEncodingDiagnostics


@dataclass(frozen=True)
class QwenHeadTailEncodingDiagnostics:
    """不含正文或成员身份的英文双视图长度诊断。"""

    count: int
    original_over_limit_count: int
    original_over_limit_rate: float
    encoded_view_count: int
    two_view_count: int
    middle_omitted_count: int
    middle_omitted_rate: float
    middle_omitted_token_count_total: int
    original_token_count_max: int
    original_token_count_p95: float
    content_window_token_budget: int
    boundary_adjusted_view_count: int
    retained_content_token_count_total: int
    max_length: int
    encoded_view_over_limit_count: int


@dataclass(frozen=True)
class QwenHeadTailEncodingResult:
    """英文 head-tail 聚合向量、长度分组掩码与去敏诊断。"""

    embeddings: np.ndarray
    original_over_limit_mask: np.ndarray
    middle_omitted_mask: np.ndarray
    diagnostics: QwenHeadTailEncodingDiagnostics


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
    expected_files = dict(plan.encoder.snapshot_files)
    actual_files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(directory).parts
    }
    if actual_files != set(expected_files):
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_snapshot_incomplete")
    actual_hashes: dict[str, str] = {}
    for relative, expected_sha256 in expected_files.items():
        path = directory / relative
        actual_sha256 = _file_sha256(path)
        actual_hashes[relative] = actual_sha256
        if path.is_symlink() or actual_sha256 != expected_sha256:
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_model_snapshot_hash_mismatch"
            )
    snapshot_sha256 = hashlib.sha256(
        json.dumps(
            expected_files, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if snapshot_sha256 != plan.encoder.snapshot_sha256:
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_model_snapshot_hash_mismatch"
        )
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        weight_index = json.loads(
            (directory / "model.safetensors.index.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid") from exc
    if not isinstance(config, dict) or not isinstance(weight_index, dict):
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid")
    try:
        hidden_size = int(config.get("hidden_size", -1))
    except (TypeError, ValueError) as exc:
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_model_config_invalid"
        ) from exc
    if (
        not isinstance(config, dict)
        or config.get("model_type") != "qwen3"
        or hidden_size != plan.encoder.embedding_dimension
    ):
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid")
    try:
        modules = json.loads((directory / "modules.json").read_text("utf-8"))
        pooling = json.loads(
            (directory / "1_Pooling/config.json").read_text("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid") from exc
    if (
        not isinstance(modules, list)
        or [item.get("type") for item in modules if isinstance(item, dict)]
        != [
            "sentence_transformers.models.Transformer",
            "sentence_transformers.models.Pooling",
            "sentence_transformers.models.Normalize",
        ]
        or not isinstance(pooling, dict)
        or pooling.get("pooling_mode_lasttoken") is not True
        or any(
            pooling.get(field) is not False
            for field in (
                "pooling_mode_cls_token",
                "pooling_mode_mean_tokens",
                "pooling_mode_max_tokens",
                "pooling_mode_mean_sqrt_len_tokens",
                "pooling_mode_weightedmean_tokens",
            )
        )
    ):
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid")
    weight_files = dict(plan.encoder.weight_files)
    weight_map = weight_index.get("weight_map")
    if (
        not isinstance(weight_map, dict)
        or not weight_map
        or set(weight_map.values()) != set(weight_files)
    ):
        raise QwenEmbeddingRuntimeError("qwen_embedding_model_config_invalid")
    actual_weight_files = {
        relative: actual_hashes[relative] for relative in weight_files
    }
    weights_sha256 = hashlib.sha256(
        json.dumps(
            actual_weight_files, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if weights_sha256 != plan.encoder.weights_sha256:
        raise QwenEmbeddingRuntimeError("qwen_embedding_weights_hash_mismatch")
    return QwenModelSnapshot(
        repository=plan.encoder.repository,
        revision=plan.encoder.revision,
        weights_sha256=weights_sha256,
        snapshot_sha256=snapshot_sha256,
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
    ) -> None:
        """记录冻结运行参数并校验模型目录。

        Args:
            model_dir: 仓库外的本地模型目录。
            plan: 冻结编码器与文本投影。
            设备、batch 与 dtype 均来自计划，不允许 CLI 覆盖。
        """

        validate_qwen_model_directory(model_dir, plan=plan)
        self._model_dir = Path(model_dir).expanduser().resolve()
        self._plan = plan
        self._requested_device = plan.execution.device
        self._batch_size = plan.execution.batch_size
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
        selected = self._requested_device
        if selected == "mps" and not torch.backends.mps.is_available():
            raise QwenEmbeddingRuntimeError("qwen_embedding_mps_unavailable")
        if selected.startswith("cuda") and not torch.cuda.is_available():
            raise QwenEmbeddingRuntimeError("qwen_embedding_cuda_unavailable")
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
        try:
            parameter_dtype = str(next(model.parameters()).dtype).removeprefix("torch.")
        except (StopIteration, AttributeError) as exc:
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_parameter_dtype_invalid"
            ) from exc
        if parameter_dtype != self._plan.execution.parameter_dtype:
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_parameter_dtype_invalid"
            )

    @property
    def execution_receipt(self) -> QwenExecutionReceipt:
        """返回实际设备、dtype 与主机运行身份。"""

        self._ensure_loaded()
        try:
            process = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                check=True,
                capture_output=True,
                text=True,
            )
            hardware_model = process.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            hardware_model = platform.processor().strip()
        if not hardware_model:
            raise QwenEmbeddingRuntimeError(
                "qwen_embedding_hardware_identity_unavailable"
            )
        return QwenExecutionReceipt(
            device=self.device,
            batch_size=self._batch_size,
            parameter_dtype=self._plan.execution.parameter_dtype,
            output_dtype=self._plan.execution.output_dtype,
            python_version=sys.version.split()[0],
            operating_system=platform.system(),
            operating_system_release=platform.release(),
            macos_version=platform.mac_ver()[0],
            machine=platform.machine(),
            hardware_model=hardware_model,
        )

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
        return self.encode_with_diagnostics(
            texts, show_progress=show_progress
        ).embeddings

    def encode_with_diagnostics(
        self,
        texts: Sequence[str],
        *,
        show_progress: bool = False,
    ) -> QwenEncodingResult:
        """编码文本并返回不含正文的截断统计。"""

        if not texts or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise QwenEmbeddingRuntimeError("qwen_embedding_text_invalid")
        self._ensure_loaded()
        prompt = f"Instruct: {self._plan.encoder.instruction}\nQuery: "
        try:
            tokenized = self._model.tokenizer(
                [prompt + text for text in texts],
                truncation=False,
                add_special_tokens=True,
                return_length=True,
            )
            token_counts = np.asarray(tokenized["length"], dtype=int)
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
        if not np.isfinite(norms).all() or np.any(norms <= 0.0):
            raise QwenEmbeddingRuntimeError("qwen_embedding_output_invalid")
        # Sentence Transformers 会在模型计算 dtype 中归一化；MPS 的半精度路径
        # 转回 float32 后可出现约 1e-3 的范数偏移。这里以 float32 再归一化，
        # 使后续线性头和 CPU/MPS 运行具有同一明确输入契约。
        embeddings = embeddings / norms[:, np.newaxis]
        if not np.allclose(
            np.linalg.norm(embeddings, axis=1), 1.0, atol=1e-6, rtol=1e-6
        ):
            raise QwenEmbeddingRuntimeError("qwen_embedding_output_not_normalized")
        truncated = token_counts > self._plan.encoder.max_length
        diagnostics = QwenEncodingDiagnostics(
            count=len(texts),
            truncated_count=int(np.sum(truncated)),
            truncated_rate=float(np.mean(truncated)),
            token_count_max=int(np.max(token_counts)),
            token_count_p95=float(np.percentile(token_counts, 95)),
            max_length=self._plan.encoder.max_length,
        )
        return QwenEncodingResult(embeddings=embeddings, diagnostics=diagnostics)


class LocalQwenHeadTailEncoder(LocalQwenEmbeddingEncoder):
    """复用同一公开权重的英文 instruction 与 head-tail 双视图编码器。

    短文本编码一次；超过单视图上限的文本分别编码最大可容纳的头部和尾部
    token 窗口，再对两个归一化向量取算术平均并重新 L2 归一化。该投影不
    声称覆盖超长文本中间被省略的 token，并以聚合统计明确披露该边界。
    """

    def __init__(
        self,
        model_dir: str | Path,
        *,
        base_plan: QwenEmbeddingPlan,
        projection_plan: QwenHeadTailPlan,
    ) -> None:
        """绑定基础权重计划与第三层文本投影计划。

        Args:
            model_dir: 已由基础计划逐文件校验的仓库外模型目录。
            base_plan: 首次4B运行使用的完整公开权重与执行计划。
            projection_plan: 固定英文 instruction 和双视图算法的第三层计划。

        Raises:
            QwenEmbeddingRuntimeError: 两计划的模型、长度或执行身份漂移。
        """

        if (
            base_plan.plan_sha256 != projection_plan.qwen_base_plan_sha256
            or base_plan.encoder.max_length != projection_plan.max_length
            or base_plan.execution.device != projection_plan.device
            or base_plan.execution.batch_size != projection_plan.batch_size
            or base_plan.execution.parameter_dtype
            != projection_plan.parameter_dtype
            or base_plan.execution.output_dtype != projection_plan.output_dtype
        ):
            raise QwenEmbeddingRuntimeError(
                "qwen_head_tail_plan_binding_mismatch"
            )
        modified_encoder = replace(
            base_plan.encoder, instruction=projection_plan.instruction
        )
        runtime_plan = replace(base_plan, encoder=modified_encoder)
        super().__init__(model_dir, plan=runtime_plan)
        self._projection_plan = projection_plan

    def encode_head_tail_with_diagnostics(
        self,
        texts: Sequence[str],
        *,
        show_progress: bool = False,
    ) -> QwenHeadTailEncodingResult:
        """按冻结英文 prompt 编码短文本或 head-tail 双视图。

        Args:
            texts: 已冻结的单通道规范化文本。
            show_progress: 是否显示本地视图批处理进度。

        Returns:
            每条文档一个2560维聚合向量、长度分组掩码与聚合诊断。

        Raises:
            QwenEmbeddingRuntimeError: 文本、token预算、视图或输出违反契约。
        """

        if not texts or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise QwenEmbeddingRuntimeError("qwen_embedding_text_invalid")
        self._ensure_loaded()
        if self._model is None:
            raise QwenEmbeddingRuntimeError("qwen_embedding_model_load_failed")
        prompt = self._projection_plan.prompt_template.format(
            instruction=self._projection_plan.instruction
        )
        tokenizer = self._model.tokenizer
        try:
            prompt_ids = tokenizer.encode(prompt, add_special_tokens=False)
            special_count = int(tokenizer.num_special_tokens_to_add(pair=False))
        except Exception as exc:
            raise QwenEmbeddingRuntimeError(
                "qwen_head_tail_token_budget_failed"
            ) from exc
        content_budget = (
            self._projection_plan.max_length - len(prompt_ids) - special_count
        )
        if content_budget < 32:
            raise QwenEmbeddingRuntimeError(
                "qwen_head_tail_token_budget_invalid"
            )
        views: list[str] = []
        view_document_indices: list[int] = []
        original_counts: list[int] = []
        over_limit: list[bool] = []
        middle_omitted: list[bool] = []
        omitted_token_counts: list[int] = []
        retained_token_counts: list[int] = []
        boundary_adjusted_view_count = 0

        def fitting_view(
            token_ids: Sequence[int], *, keep_tail: bool
        ) -> tuple[str, int, int]:
            """二分寻找加 prompt 后实际不超限的最大头部或尾部窗口。"""

            low = 1
            high = len(token_ids)
            best_text = ""
            best_kept = 0
            best_encoded_count = 0
            while low <= high:
                kept = (low + high) // 2
                selected_ids = (
                    token_ids[-kept:] if keep_tail else token_ids[:kept]
                )
                decoded = tokenizer.decode(
                    selected_ids,
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                )
                if not decoded.strip():
                    high = kept - 1
                    continue
                encoded_count = len(
                    tokenizer.encode(
                        prompt + decoded, add_special_tokens=True
                    )
                )
                if encoded_count <= self._projection_plan.max_length:
                    best_text = decoded
                    best_kept = kept
                    best_encoded_count = encoded_count
                    low = kept + 1
                else:
                    high = kept - 1
            if not best_text or best_kept < 1:
                raise QwenEmbeddingRuntimeError(
                    "qwen_head_tail_view_fit_failed"
                )
            return best_text, best_kept, best_encoded_count

        try:
            for document_index, text in enumerate(texts):
                full_ids = tokenizer.encode(
                    prompt + text, add_special_tokens=True
                )
                content_ids = tokenizer.encode(text, add_special_tokens=False)
                full_count = len(full_ids)
                is_over_limit = full_count > self._projection_plan.max_length
                original_counts.append(full_count)
                over_limit.append(is_over_limit)
                if not is_over_limit:
                    views.append(text)
                    view_document_indices.append(document_index)
                    retained_token_counts.append(len(content_ids))
                    middle_omitted.append(False)
                    omitted_token_counts.append(0)
                    continue
                head_ids = content_ids[:content_budget]
                tail_ids = content_ids[-content_budget:]
                head, head_kept, _head_encoded = fitting_view(
                    head_ids, keep_tail=False
                )
                tail, tail_kept, _tail_encoded = fitting_view(
                    tail_ids, keep_tail=True
                )
                boundary_adjusted_view_count += int(
                    head_kept < len(head_ids)
                ) + int(tail_kept < len(tail_ids))
                views.extend((head, tail))
                view_document_indices.extend((document_index, document_index))
                retained_token_counts.extend((head_kept, tail_kept))
                omitted = max(0, len(content_ids) - head_kept - tail_kept)
                middle_omitted.append(omitted > 0)
                omitted_token_counts.append(omitted)
            encoded_view_counts = np.asarray(
                tokenizer(
                    [prompt + view for view in views],
                    truncation=False,
                    add_special_tokens=True,
                    return_length=True,
                )["length"],
                dtype=int,
            )
            if np.any(encoded_view_counts > self._projection_plan.max_length):
                raise QwenEmbeddingRuntimeError(
                    "qwen_head_tail_encoded_view_over_limit"
                )
            values = self._model.encode(
                views,
                prompt=prompt,
                batch_size=self._batch_size,
                show_progress_bar=show_progress,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        except QwenEmbeddingRuntimeError:
            raise
        except Exception as exc:
            raise QwenEmbeddingRuntimeError(
                "qwen_head_tail_encode_failed"
            ) from exc
        view_embeddings = np.asarray(values, dtype=np.float32)
        expected_shape = (
            len(views),
            self._plan.encoder.embedding_dimension,
        )
        if view_embeddings.shape != expected_shape or not np.isfinite(
            view_embeddings
        ).all():
            raise QwenEmbeddingRuntimeError("qwen_head_tail_output_invalid")
        aggregated = np.zeros(
            (len(texts), self._plan.encoder.embedding_dimension),
            dtype=np.float32,
        )
        view_counts = np.zeros(len(texts), dtype=int)
        for view_index, document_index in enumerate(view_document_indices):
            aggregated[document_index] += view_embeddings[view_index]
            view_counts[document_index] += 1
        if np.any((view_counts < 1) | (view_counts > 2)):
            raise QwenEmbeddingRuntimeError("qwen_head_tail_view_count_invalid")
        aggregated /= view_counts[:, np.newaxis]
        norms = np.linalg.norm(aggregated, axis=1)
        if not np.isfinite(norms).all() or np.any(norms <= 0.0):
            raise QwenEmbeddingRuntimeError("qwen_head_tail_output_invalid")
        aggregated /= norms[:, np.newaxis]
        if not np.allclose(
            np.linalg.norm(aggregated, axis=1), 1.0, atol=1e-6, rtol=1e-6
        ):
            raise QwenEmbeddingRuntimeError(
                "qwen_head_tail_output_not_normalized"
            )
        original_counts_array = np.asarray(original_counts, dtype=int)
        over_limit_mask = np.asarray(over_limit, dtype=bool)
        middle_omitted_mask = np.asarray(middle_omitted, dtype=bool)
        diagnostics = QwenHeadTailEncodingDiagnostics(
            count=len(texts),
            original_over_limit_count=int(np.sum(over_limit_mask)),
            original_over_limit_rate=float(np.mean(over_limit_mask)),
            encoded_view_count=len(views),
            two_view_count=int(np.sum(view_counts == 2)),
            middle_omitted_count=int(np.sum(middle_omitted_mask)),
            middle_omitted_rate=float(np.mean(middle_omitted_mask)),
            middle_omitted_token_count_total=int(sum(omitted_token_counts)),
            original_token_count_max=int(np.max(original_counts_array)),
            original_token_count_p95=float(
                np.percentile(original_counts_array, 95)
            ),
            content_window_token_budget=content_budget,
            boundary_adjusted_view_count=boundary_adjusted_view_count,
            retained_content_token_count_total=int(sum(retained_token_counts)),
            max_length=self._projection_plan.max_length,
            encoded_view_over_limit_count=0,
        )
        return QwenHeadTailEncodingResult(
            embeddings=aggregated,
            original_over_limit_mask=over_limit_mask,
            middle_omitted_mask=middle_omitted_mask,
            diagnostics=diagnostics,
        )
