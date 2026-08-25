"""Qwen完整分块向量跨批次内容寻址缓存测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tourism_ugc_study.models.text.model_retraining_config import (
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_cache import (
    QwenEmbeddingCache,
    QwenEmbeddingCacheError,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import QwenModelSnapshot


ROOT = Path(__file__).resolve().parents[3]


class _CountingEncoder:
    """记录真实编码调用次数的合成完整分块编码器。"""

    def __init__(self, dimension: int) -> None:
        """保存向量维度并初始化调用计数。"""

        self.dimension = dimension
        self.call_count = 0

    def encode_document(self, text: str) -> SimpleNamespace:
        """每次调用返回确定、零省略且L2规范化的向量。"""

        assert text
        self.call_count += 1
        vector = np.zeros(self.dimension, dtype=np.float32)
        vector[0] = 1.0
        return SimpleNamespace(
            embedding=vector,
            diagnostics=SimpleNamespace(omitted_token_count=0),
        )


def _snapshot(*, snapshot_sha256: str = "a" * 64) -> QwenModelSnapshot:
    """构造与冻结计划一致的合成公开权重身份。"""

    plan = load_model_retraining_plan(
        ROOT / "configs" / "cleaning-model-retraining.yaml"
    )
    return QwenModelSnapshot(
        repository=plan.qwen_repository,
        revision=plan.qwen_revision,
        weights_sha256="b" * 64,
        snapshot_sha256=snapshot_sha256,
        embedding_dimension=plan.qwen_embedding_dimension,
        max_length=plan.qwen_max_length,
        reused=True,
    )


def _text_sha256(text: str) -> str:
    """计算合成规范正文摘要。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_same_normalized_post_is_encoded_once_across_cache_instances(
    tmp_path: Path,
) -> None:
    """同一帖子正文跨批次再次出现时必须读取缓存而非再次编码。"""

    plan = load_model_retraining_plan(
        ROOT / "configs" / "cleaning-model-retraining.yaml"
    )
    text = "[TITLE]\n合成标题\n[BODY]\n合成正文"
    normalized_sha256 = _text_sha256(text)
    first_encoder = _CountingEncoder(plan.qwen_embedding_dimension)
    first_cache = QwenEmbeddingCache(
        tmp_path,
        plan=plan,
        model_snapshot=_snapshot(),
        encoder=first_encoder,  # type: ignore[arg-type]
    )
    first = first_cache.get_or_encode(
        text, normalized_sha256=normalized_sha256
    )
    second = first_cache.get_or_encode(
        text, normalized_sha256=normalized_sha256
    )
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first_encoder.call_count == 1
    assert np.array_equal(first.embedding, second.embedding)

    later_encoder = _CountingEncoder(plan.qwen_embedding_dimension)
    later_batch_cache = QwenEmbeddingCache(
        tmp_path,
        plan=plan,
        model_snapshot=_snapshot(),
        encoder=later_encoder,  # type: ignore[arg-type]
    )
    later = later_batch_cache.get_or_encode(
        text, normalized_sha256=normalized_sha256
    )
    assert later.cache_hit is True
    assert later_encoder.call_count == 0
    assert later.cache_key == first.cache_key


def test_changed_text_or_model_snapshot_uses_new_cache_entry(tmp_path: Path) -> None:
    """正文或Qwen快照变化时不得沿用旧向量。"""

    plan = load_model_retraining_plan(
        ROOT / "configs" / "cleaning-model-retraining.yaml"
    )
    encoder = _CountingEncoder(plan.qwen_embedding_dimension)
    cache = QwenEmbeddingCache(
        tmp_path,
        plan=plan,
        model_snapshot=_snapshot(),
        encoder=encoder,  # type: ignore[arg-type]
    )
    first_text = "[TITLE]\n标题\n[BODY]\n正文A"
    second_text = "[TITLE]\n标题\n[BODY]\n正文B"
    cache.get_or_encode(first_text, normalized_sha256=_text_sha256(first_text))
    changed = cache.get_or_encode(
        second_text, normalized_sha256=_text_sha256(second_text)
    )
    assert changed.cache_hit is False
    assert encoder.call_count == 2

    new_encoder = _CountingEncoder(plan.qwen_embedding_dimension)
    changed_snapshot_cache = QwenEmbeddingCache(
        tmp_path,
        plan=plan,
        model_snapshot=_snapshot(snapshot_sha256="c" * 64),
        encoder=new_encoder,  # type: ignore[arg-type]
    )
    changed_snapshot = changed_snapshot_cache.get_or_encode(
        first_text, normalized_sha256=_text_sha256(first_text)
    )
    assert changed_snapshot.cache_hit is False
    assert new_encoder.call_count == 1
    assert changed_snapshot.cache_namespace_id != cache.namespace_id


def test_corrupted_cache_fails_closed_without_reencoding(tmp_path: Path) -> None:
    """既有条目损坏时必须失败关闭，不能静默重算掩盖损坏。"""

    plan = load_model_retraining_plan(
        ROOT / "configs" / "cleaning-model-retraining.yaml"
    )
    encoder = _CountingEncoder(plan.qwen_embedding_dimension)
    cache = QwenEmbeddingCache(
        tmp_path,
        plan=plan,
        model_snapshot=_snapshot(),
        encoder=encoder,  # type: ignore[arg-type]
    )
    text = "[TITLE]\n标题\n[BODY]\n正文"
    normalized_sha256 = _text_sha256(text)
    cache.get_or_encode(text, normalized_sha256=normalized_sha256)
    entry = cache._entry_directory(normalized_sha256)
    (entry / "embedding.npy").write_bytes(b"corrupted")
    with pytest.raises(QwenEmbeddingCacheError):
        cache.get_or_encode(text, normalized_sha256=normalized_sha256)
    assert encoder.call_count == 1
