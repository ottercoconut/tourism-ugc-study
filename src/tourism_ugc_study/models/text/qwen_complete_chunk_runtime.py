"""Qwen3-Embedding完整连续分块、记录级聚合与无遗漏诊断。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

import numpy as np

from .model_retraining_config import ModelRetrainingPlan
from .qwen_embedding_config import QwenEmbeddingPlan
from .qwen_inference_execution import QwenInferenceExecution
from .qwen_embedding_runtime import (
    LocalQwenEmbeddingEncoder,
    QwenEmbeddingRuntimeError,
)


@dataclass(frozen=True)
class CompleteChunkDiagnostics:
    """单条正文的完整token覆盖与编码视图诊断。"""

    original_content_token_count: int
    original_input_token_count: int
    encoded_view_count: int
    content_window_token_budget: int
    retained_content_token_count: int
    omitted_token_count: int
    boundary_adjusted_view_count: int
    encoded_view_token_count_max: int
    max_length: int


@dataclass(frozen=True)
class CompleteChunkEncodingResult:
    """单条正文的L2聚合向量与去敏长度诊断。"""

    embedding: np.ndarray
    diagnostics: CompleteChunkDiagnostics


def continuous_token_ranges(
    token_count: int, token_budget: int
) -> tuple[tuple[int, int], ...]:
    """生成覆盖全部token且无重叠、无间隙的初始区间。

    Args:
        token_count: 正文token总数。
        token_budget: 每个视图的最大正文token预算。

    Returns:
        从0开始、以``token_count``结束的左闭右开区间。

    Raises:
        QwenEmbeddingRuntimeError: 数量或预算无效。
    """

    if token_count < 1 or token_budget < 1:
        raise QwenEmbeddingRuntimeError(
            "qwen_complete_chunk_token_range_invalid"
        )
    return tuple(
        (start, min(start + token_budget, token_count))
        for start in range(0, token_count, token_budget)
    )


class LocalQwenCompleteChunkEncoder(LocalQwenEmbeddingEncoder):
    """只读编码完整正文、逐块平均并重新L2规范化。

    该类不读取标签且没有``fit``接口。正文token区间严格首尾相接；若解码后加
    instruction使某个候选块超出2048 tokens，块尾会缩短，余下token从同一位置
    进入下一块，因而不会复用head-tail方案的中段省略。
    """

    def __init__(
        self,
        model_dir: str | Path,
        *,
        base_plan: QwenEmbeddingPlan,
        retraining_plan: ModelRetrainingPlan,
        inference_execution: QwenInferenceExecution | None = None,
    ) -> None:
        """绑定冻结算法；可额外传入已验证、独立记账的推理设备配置。

        默认行为保持原MPS契约。运行配置只能改变设备与同帖视图batch，不能
        修改原训练计划摘要、dtype、窗口、指令或模型。缓存会绑定新增运行身份。
        """

        if (
            base_plan.plan_sha256 != retraining_plan.qwen_base_plan_sha256
            or base_plan.encoder.repository != retraining_plan.qwen_repository
            or base_plan.encoder.revision != retraining_plan.qwen_revision
            or base_plan.encoder.embedding_dimension
            != retraining_plan.qwen_embedding_dimension
            or base_plan.encoder.max_length != retraining_plan.qwen_max_length
            or base_plan.execution.device != retraining_plan.qwen_device
            or base_plan.execution.batch_size != retraining_plan.qwen_batch_size
        ):
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_plan_binding_mismatch"
            )
        runtime_plan = replace(
            base_plan,
            encoder=replace(
                base_plan.encoder,
                instruction=retraining_plan.qwen_instruction,
            ),
        )
        self.inference_execution_identity = None
        if inference_execution is not None:
            if (inference_execution.parameter_dtype != base_plan.execution.parameter_dtype
                    or inference_execution.output_dtype != base_plan.execution.output_dtype):
                raise QwenEmbeddingRuntimeError("qwen_inference_dtype_override_forbidden")
            runtime_plan = replace(runtime_plan, execution=replace(
                runtime_plan.execution, device=inference_execution.device,
                batch_size=inference_execution.batch_size))
            self.inference_execution_identity = inference_execution.identity()
        super().__init__(model_dir, plan=runtime_plan)
        self._retraining_plan = retraining_plan

    def _largest_fitting_view(
        self,
        content_ids: Sequence[int],
        *,
        start: int,
        proposed_end: int,
        prompt: str,
    ) -> tuple[str, int, int]:
        """二分寻找从``start``起实际不超限的最大连续token块。"""

        if self._model is None:
            raise QwenEmbeddingRuntimeError("qwen_embedding_model_load_failed")
        tokenizer = self._model.tokenizer
        low = start + 1
        high = proposed_end
        best_text = ""
        best_end = start
        best_encoded_count = 0
        while low <= high:
            end = (low + high) // 2
            decoded = tokenizer.decode(
                content_ids[start:end],
                # ``content_ids``本身未添加BOS/EOS；若UGC正文恰好含形似特殊
                # token的字面串，也必须保留，不能借``skip_special_tokens``静默
                # 删除。模型真正添加的特殊token只在prompt+view再次编码时产生。
                skip_special_tokens=False,
                clean_up_tokenization_spaces=False,
            )
            if not decoded.strip():
                low = end + 1
                continue
            encoded_count = len(
                tokenizer.encode(prompt + decoded, add_special_tokens=True)
            )
            if encoded_count <= self._retraining_plan.qwen_max_length:
                best_text = decoded
                best_end = end
                best_encoded_count = encoded_count
                low = end + 1
            else:
                high = end - 1
        if not best_text or best_end <= start:
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_view_fit_failed"
            )
        return best_text, best_end, best_encoded_count

    def encode_document(
        self, text: str, *, show_progress: bool = False
    ) -> CompleteChunkEncodingResult:
        """完整编码一条正文并验证原token区间零省略。

        Args:
            text: 已冻结的规范化模型正文。
            show_progress: 是否显示当前记录的视图编码进度。

        Returns:
            一个记录级聚合向量和无正文诊断。

        Raises:
            QwenEmbeddingRuntimeError: token预算、覆盖或输出违反冻结契约。
        """

        if not isinstance(text, str) or not text.strip():
            raise QwenEmbeddingRuntimeError("qwen_embedding_text_invalid")
        self._ensure_loaded()
        if self._model is None:
            raise QwenEmbeddingRuntimeError("qwen_embedding_model_load_failed")
        tokenizer = self._model.tokenizer
        prompt = self._retraining_plan.qwen_prompt_template.format(
            instruction=self._retraining_plan.qwen_instruction
        )
        try:
            prompt_ids = tokenizer.encode(prompt, add_special_tokens=False)
            special_count = int(tokenizer.num_special_tokens_to_add(pair=False))
            content_ids = tokenizer.encode(text, add_special_tokens=False)
            original_input_count = len(
                tokenizer.encode(prompt + text, add_special_tokens=True)
            )
        except Exception as exc:
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_token_budget_failed"
            ) from exc
        content_budget = (
            self._retraining_plan.qwen_max_length
            - len(prompt_ids)
            - special_count
        )
        if content_budget < 32 or not content_ids:
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_token_budget_invalid"
            )

        views: list[str] = []
        ranges: list[tuple[int, int]] = []
        encoded_counts: list[int] = []
        boundary_adjustments = 0
        start = 0
        while start < len(content_ids):
            proposed_end = min(start + content_budget, len(content_ids))
            view, end, encoded_count = self._largest_fitting_view(
                content_ids,
                start=start,
                proposed_end=proposed_end,
                prompt=prompt,
            )
            boundary_adjustments += int(end < proposed_end)
            views.append(view)
            ranges.append((start, end))
            encoded_counts.append(encoded_count)
            start = end
        if (
            not ranges
            or ranges[0][0] != 0
            or ranges[-1][1] != len(content_ids)
            or any(left[1] != right[0] for left, right in zip(ranges, ranges[1:]))
            or sum(end - begin for begin, end in ranges) != len(content_ids)
            or max(encoded_counts) > self._retraining_plan.qwen_max_length
        ):
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_token_coverage_invalid"
            )
        try:
            values = self._model.encode(
                views,
                prompt=prompt,
                batch_size=self._batch_size,
                show_progress_bar=show_progress,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        except Exception as exc:
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_encode_failed"
            ) from exc
        view_embeddings = np.asarray(values, dtype=np.float32)
        if (
            view_embeddings.shape
            != (len(views), self._retraining_plan.qwen_embedding_dimension)
            or not np.isfinite(view_embeddings).all()
        ):
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_output_invalid"
            )
        aggregated = np.mean(view_embeddings, axis=0, dtype=np.float32)
        norm = float(np.linalg.norm(aggregated))
        if not np.isfinite(norm) or norm <= 0.0:
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_output_invalid"
            )
        embedding = np.asarray(aggregated / norm, dtype=np.float32)
        if (
            embedding.shape != (self._retraining_plan.qwen_embedding_dimension,)
            or not np.isfinite(embedding).all()
            or not np.isclose(np.linalg.norm(embedding), 1.0, atol=1e-6)
        ):
            raise QwenEmbeddingRuntimeError(
                "qwen_complete_chunk_output_not_normalized"
            )
        return CompleteChunkEncodingResult(
            embedding=embedding,
            diagnostics=CompleteChunkDiagnostics(
                original_content_token_count=len(content_ids),
                original_input_token_count=original_input_count,
                encoded_view_count=len(views),
                content_window_token_budget=content_budget,
                retained_content_token_count=sum(
                    end - begin for begin, end in ranges
                ),
                omitted_token_count=0,
                boundary_adjusted_view_count=boundary_adjustments,
                encoded_view_token_count_max=max(encoded_counts),
                max_length=self._retraining_plan.qwen_max_length,
            ),
        )
