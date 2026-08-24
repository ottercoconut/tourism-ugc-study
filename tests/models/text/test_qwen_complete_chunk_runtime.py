"""Qwen完整连续分块的零省略和确定性聚合测试。"""

from __future__ import annotations

import numpy as np

from tourism_ugc_study.models.text.model_retraining_config import (
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.qwen_complete_chunk_runtime import (
    LocalQwenCompleteChunkEncoder,
    continuous_token_ranges,
)


class _CharacterTokenizer:
    """用Unicode字符模拟可逆tokenizer。"""

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        values = [ord(character) for character in text]
        return ([1] + values) if add_special_tokens else values

    def decode(
        self,
        values: list[int],
        *,
        skip_special_tokens: bool,
        clean_up_tokenization_spaces: bool,
    ) -> str:
        del clean_up_tokenization_spaces
        return "".join(
            chr(value)
            for value in values
            if not (skip_special_tokens and value == 999)
        )

    def num_special_tokens_to_add(self, *, pair: bool) -> int:
        del pair
        return 1


class _FakeModel:
    """产生由视图长度决定的确定性归一化向量。"""

    def __init__(self) -> None:
        self.tokenizer = _CharacterTokenizer()

    def encode(self, views: list[str], **kwargs: object) -> np.ndarray:
        del kwargs
        matrix = np.zeros((len(views), 2560), dtype=np.float32)
        for index, view in enumerate(views):
            matrix[index, 0] = float(len(view))
            matrix[index, 1] = float(index + 1)
        matrix /= np.linalg.norm(matrix, axis=1)[:, None]
        return matrix


def _fake_encoder() -> LocalQwenCompleteChunkEncoder:
    """绕过公开权重校验，仅测试分块领域算法。"""

    encoder = object.__new__(LocalQwenCompleteChunkEncoder)
    encoder._retraining_plan = load_model_retraining_plan(  # type: ignore[attr-defined]
        "configs/cleaning-model-retraining.yaml"
    )
    encoder._model = _FakeModel()  # type: ignore[attr-defined]
    encoder._batch_size = 1  # type: ignore[attr-defined]
    return encoder


def test_continuous_ranges_cover_every_token_exactly_once() -> None:
    """初始分块必须无重叠、无空洞并包含最后一个token。"""

    ranges = continuous_token_ranges(5001, 2000)
    assert ranges == ((0, 2000), (2000, 4000), (4000, 5001))
    covered = [index for start, end in ranges for index in range(start, end)]
    assert covered == list(range(5001))


def test_complete_chunk_encoding_omits_no_long_text_tokens() -> None:
    """超过单视图预算的正文仍须保留全部原始token。"""

    encoder = _fake_encoder()
    result = encoder.encode_document("青" * 5000)
    diagnostics = result.diagnostics
    assert diagnostics.encoded_view_count >= 3
    assert diagnostics.retained_content_token_count == 5000
    assert diagnostics.omitted_token_count == 0
    assert diagnostics.encoded_view_token_count_max <= 2048
    assert result.embedding.shape == (2560,)
    assert np.isclose(np.linalg.norm(result.embedding), 1.0, atol=1e-6)


def test_complete_chunk_aggregation_is_deterministic() -> None:
    """相同文本和模型运行必须产生逐值相同的聚合向量。"""

    encoder = _fake_encoder()
    first = encoder.encode_document("旅" * 4200)
    second = encoder.encode_document("旅" * 4200)
    np.testing.assert_array_equal(first.embedding, second.embedding)
    assert first.diagnostics == second.diagnostics


def test_content_that_looks_like_special_token_is_not_dropped() -> None:
    """正文token即使被tokenizer视为特殊值，也必须按字面内容保留。"""

    encoder = _fake_encoder()
    text = ("青" * 2100) + chr(999) + ("岛" * 2100)
    result = encoder.encode_document(text)
    assert result.diagnostics.original_content_token_count == len(text)
    assert result.diagnostics.retained_content_token_count == len(text)
    assert result.diagnostics.omitted_token_count == 0
