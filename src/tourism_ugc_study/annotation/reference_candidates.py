"""冻结字符3–5 gram TF-IDF的完整无序帖子对候选计算。"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from .reference_contract import (
    DuplicateCandidate,
    PairIdentity,
    ReferenceDatasetError,
    ReferencePost,
    canonical_sha256,
)


DUPLICATE_SIMILARITY_THRESHOLD = 0.80
DUPLICATE_NGRAM_RANGE = (3, 5)
DUPLICATE_ALGORITHM_ID = "normalized-char-3-5gram-tfidf-cosine-all-pairs"


@dataclass(frozen=True)
class CandidateComputation:
    """完整全对计算的结果和可复验摘要。

    Attributes:
        candidates: 精确重复或相似度达到阈值的稳定候选边。
        examined_pair_count: 实际参与余弦比较的无序帖子对数。
        expected_pair_count: 输入规模按 ``n × (n-1) / 2`` 得到的理论对数。
        input_member_sha256: 输入身份、文本哈希的稳定摘要。
        candidate_sha256: 候选边及冻结相似度表示的稳定摘要。
        algorithm_identity: 算法、阈值、精度和依赖版本身份。
    """

    candidates: tuple[DuplicateCandidate, ...]
    examined_pair_count: int
    expected_pair_count: int
    input_member_sha256: str
    candidate_sha256: str
    algorithm_identity: dict[str, object]


def duplicate_algorithm_identity() -> dict[str, object]:
    """返回完整冻结的重复候选算法身份。

    Returns:
        不含平台或旅游标签的向量器参数、计算精度和依赖版本。
    """

    return {
        "algorithm_id": DUPLICATE_ALGORITHM_ID,
        "analyzer": "char",
        "ngram_range": list(DUPLICATE_NGRAM_RANGE),
        "threshold": DUPLICATE_SIMILARITY_THRESHOLD,
        "threshold_role": "candidate_only",
        "lowercase": False,
        "norm": "l2",
        "dtype": "float64",
        "use_idf": True,
        "smooth_idf": True,
        "sublinear_tf": False,
        "numpy": importlib.metadata.version("numpy"),
        "scikit_learn": importlib.metadata.version("scikit-learn"),
    }


def _vectorize(posts: Sequence[ReferencePost]):
    """按冻结参数拟合参考集自身的字符 TF-IDF 矩阵。

    Args:
        posts: 身份唯一且文本非空的帖子序列。

    Returns:
        行顺序与输入一致、L2 归一化的稀疏 float64 矩阵。

    Raises:
        ReferenceDatasetError: 输入文本无法形成3–5字符词表。
    """

    vectorizer = TfidfVectorizer(
        analyzer="char",
        ngram_range=DUPLICATE_NGRAM_RANGE,
        lowercase=False,
        norm="l2",
        dtype=np.float64,
        use_idf=True,
        smooth_idf=True,
        sublinear_tf=False,
    )
    try:
        return vectorizer.fit_transform([post.normalized_model_text for post in posts])
    except ValueError as exc:
        raise ReferenceDatasetError("duplicate_tfidf_vocabulary_empty") from exc


def compute_duplicate_candidates(
    posts: Sequence[ReferencePost],
) -> CandidateComputation:
    """对全部无序帖子对计算余弦相似度并输出候选。

    该函数没有阻塞、近邻或平台预筛。即使精确哈希已经相同，该帖子对仍会
    参与余弦计算，因此700条输入的 ``examined_pair_count`` 必为244,650。
    阈值只控制候选输出，绝不自动形成删除决定。

    Args:
        posts: 已按冻结规范化规则生成文本的帖子。
    Returns:
        候选、全对计数、输入/输出哈希和算法身份。

    Raises:
        ReferenceDatasetError: 输入少于两条、身份重复、文本为空，或实际比较数
            不等于理论全对数。
    """

    ordered = sorted(posts, key=lambda item: item.identity)
    if len(ordered) < 2:
        raise ReferenceDatasetError("duplicate_candidate_input_too_small")
    identities = [post.identity for post in ordered]
    if len(identities) != len(set(identities)):
        raise ReferenceDatasetError("duplicate_candidate_identity_not_unique")
    if any(not post.normalized_model_text for post in ordered):
        raise ReferenceDatasetError("duplicate_candidate_text_blank")
    matrix = _vectorize(ordered)
    similarities = (matrix @ matrix.T).toarray()
    candidates: list[DuplicateCandidate] = []
    examined = 0
    for left_index in range(len(ordered) - 1):
        for right_index in range(left_index + 1, len(ordered)):
            examined += 1
            left = ordered[left_index]
            right = ordered[right_index]
            similarity = float(similarities[left_index, right_index])
            exact = left.normalized_sha256 == right.normalized_sha256
            if exact or similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
                candidates.append(
                    DuplicateCandidate(
                        pair=PairIdentity.of(left.identity, right.identity),
                        similarity=similarity,
                        exact_normalized_hash_match=exact,
                        candidate_reason_code=(
                            "exact_normalized_hash" if exact else "char_tfidf_similarity"
                        ),
                    )
                )
    expected = len(ordered) * (len(ordered) - 1) // 2
    if examined != expected:
        raise ReferenceDatasetError("duplicate_all_pairs_incomplete")
    input_payload = [
        {"identity": post.identity.as_list(), "normalized_sha256": post.normalized_sha256}
        for post in ordered
    ]
    candidate_payload = [
        {
            "pair": candidate.pair.as_list(),
            "similarity": format(candidate.similarity, ".17g"),
            "exact": candidate.exact_normalized_hash_match,
            "reason_code": candidate.candidate_reason_code,
        }
        for candidate in candidates
    ]
    return CandidateComputation(
        candidates=tuple(candidates),
        examined_pair_count=examined,
        expected_pair_count=expected,
        input_member_sha256=canonical_sha256(input_payload),
        candidate_sha256=canonical_sha256(candidate_payload),
        algorithm_identity=duplicate_algorithm_identity(),
    )
