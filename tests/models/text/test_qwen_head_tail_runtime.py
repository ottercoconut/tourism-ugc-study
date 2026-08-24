"""Qwen 英文 head-tail 双视图 token 投影与聚合测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from tourism_ugc_study.models.text.qwen_embedding_config import (
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    LocalQwenHeadTailEncoder,
)
from tourism_ugc_study.models.text.qwen_head_tail_config import (
    load_qwen_head_tail_plan,
)


ROOT = Path(__file__).resolve().parents[3]


class _CharacterTokenizer:
    """按 Unicode 字符计 token 的确定性测试 tokenizer。"""

    @staticmethod
    def encode(text, *, add_special_tokens):
        values = [ord(character) for character in text]
        return [0, *values] if add_special_tokens else values

    @staticmethod
    def decode(ids, **_kwargs):
        return "".join(chr(value) for value in ids if value)

    @staticmethod
    def num_special_tokens_to_add(*, pair):
        assert pair is False
        return 1

    def __call__(self, texts, **_kwargs):
        return {
            "length": [len(self.encode(text, add_special_tokens=True)) for text in texts]
        }


class _RoundTripExpandingTokenizer(_CharacterTokenizer):
    """模拟 token 窗口解码后重新分词长度增加的边界 tokenizer。"""

    @staticmethod
    def decode(ids, **_kwargs):
        return "".join(f"{chr(value)} " for value in ids if value)


class _FakeSentenceModel:
    """返回由视图长度决定、但始终归一化的2560维向量。"""

    def __init__(self) -> None:
        self.tokenizer = _CharacterTokenizer()

    @staticmethod
    def encode(texts, **_kwargs):
        matrix = np.zeros((len(texts), 2560), dtype=np.float32)
        for index, text in enumerate(texts):
            matrix[index, 0] = 1.0
            matrix[index, 1] = min(len(text), 10000) / 10000.0
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def _encoder() -> LocalQwenHeadTailEncoder:
    """绕过真实权重加载，只测试冻结 token 投影计算。"""

    base = load_qwen_embedding_plan(
        ROOT / "configs/cleaning-qwen-embedding-baseline.yaml"
    )
    projection = load_qwen_head_tail_plan(
        ROOT / "configs/cleaning-qwen-english-head-tail.yaml"
    )
    runtime_plan = replace(
        base, encoder=replace(base.encoder, instruction=projection.instruction)
    )
    encoder = object.__new__(LocalQwenHeadTailEncoder)
    encoder._plan = runtime_plan
    encoder._projection_plan = projection
    encoder._batch_size = 1
    encoder._model = _FakeSentenceModel()
    encoder._device = "cpu"
    return encoder


def test_head_tail_encoding_eliminates_encoded_view_overflow_and_reports_middle() -> None:
    encoder = _encoder()
    texts = ("短文本", "长" * 2500, "超长" * 3000)

    result = encoder.encode_head_tail_with_diagnostics(texts)

    assert result.embeddings.shape == (3, 2560)
    assert np.allclose(np.linalg.norm(result.embeddings, axis=1), 1.0)
    assert result.diagnostics.original_over_limit_count == 2
    assert result.diagnostics.two_view_count == 2
    assert result.diagnostics.encoded_view_count == 5
    assert result.diagnostics.encoded_view_over_limit_count == 0
    assert result.diagnostics.boundary_adjusted_view_count == 0
    assert result.diagnostics.middle_omitted_count == 1
    assert result.original_over_limit_mask.tolist() == [False, True, True]
    assert result.middle_omitted_mask.tolist() == [False, False, True]


def test_head_tail_encoding_shrinks_roundtrip_expanded_views() -> None:
    encoder = _encoder()
    encoder._model.tokenizer = _RoundTripExpandingTokenizer()

    result = encoder.encode_head_tail_with_diagnostics(("长" * 2500,))

    assert result.diagnostics.original_over_limit_count == 1
    assert result.diagnostics.encoded_view_count == 2
    assert result.diagnostics.boundary_adjusted_view_count == 2
    assert result.diagnostics.encoded_view_over_limit_count == 0
    assert result.diagnostics.retained_content_token_count_total < (
        2 * result.diagnostics.content_window_token_budget
    )
    assert result.diagnostics.middle_omitted_count == 1
