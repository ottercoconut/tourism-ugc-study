"""最终700条不重复建模参考集的稳定领域契约。

本模块只定义不可变数据结构、稳定身份和公开枚举，不读取文件、数据库或配置。
候选计算、人工证据解析、候补调度和 artifact 持久化分别由独立模块负责。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


FINAL_REFERENCE_CONTRACT = "final-nonduplicate-model-reference"
FINAL_REFERENCE_STATUS = "finalized"
FINAL_REFERENCE_ROW_COUNT = 700
FINAL_PROBABILITY_COUNT = 500
FINAL_TARGETED_COUNT = 200
ALLOWED_LABELS = frozenset({"related", "unrelated"})
ALLOWED_SAMPLE_FRAMES = frozenset({"probability", "targeted"})
FINAL_REFERENCE_FIELDS: tuple[str, ...] = (
    "task_id",
    "source_post_id",
    "source_version",
    "normalized_model_text",
    "tourism_label",
    "sample_frame",
    "selection_reason_code",
    "selection_rank",
    "inclusion_probability",
    "analysis_weight",
    "evidence_origin",
    "duplicate_component_id",
)
SUPPLEMENTAL_LABEL_FIELDS: tuple[str, ...] = (
    "task_id",
    "source_post_id",
    "source_version",
    "selection_rank",
    "normalized_model_text",
    "tourism_label",
    "review_status",
)


class ReferenceDatasetError(RuntimeError):
    """参考集生成或验证失败时抛出的去敏异常。

    Attributes:
        reason_code: 不包含正文、作者身份或私有路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化只公开稳定失败码的异常。

        Args:
            reason_code: 供 CLI、测试和 manifest 使用的稳定失败码。
        """

        super().__init__("reference dataset operation failed")
        self.reason_code = reason_code


def canonical_json_bytes(value: object) -> bytes:
    """把 JSON 对象编码为稳定 UTF-8 字节。

    Args:
        value: 可 JSON 序列化且不含非有限浮点数的对象。

    Returns:
        键排序、无多余空白的 UTF-8 字节。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    """计算对象规范 JSON 的 SHA-256。

    Args:
        value: 可 JSON 序列化对象。

    Returns:
        小写十六进制 SHA-256。
    """

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def text_sha256(text: str) -> str:
    """计算规范化模型文本的精确内容哈希。

    Args:
        text: 已由冻结规范化规则生成的完整模型文本。

    Returns:
        UTF-8 文本的小写十六进制 SHA-256。
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, order=True)
class SourceIdentity:
    """源帖子版本的稳定复合身份。

    Attributes:
        source_post_id: 派生库中的正整数帖子身份。
        source_version: 与文本快照绑定的正整数版本。
    """

    source_post_id: int
    source_version: int

    def __post_init__(self) -> None:
        """拒绝非正整数或布尔型身份。

        Raises:
            ReferenceDatasetError: 任一身份不是规范正整数。
        """

        if (
            isinstance(self.source_post_id, bool)
            or isinstance(self.source_version, bool)
            or not isinstance(self.source_post_id, int)
            or not isinstance(self.source_version, int)
            or self.source_post_id <= 0
            or self.source_version <= 0
        ):
            raise ReferenceDatasetError("source_identity_invalid")

    def as_list(self) -> list[int]:
        """返回用于规范哈希的有序整数列表。

        Returns:
            ``[source_post_id, source_version]``。
        """

        return [self.source_post_id, self.source_version]


@dataclass(frozen=True)
class ReferencePost:
    """候选计算和参考集组装使用的最小帖子投影。

    Attributes:
        identity: 稳定帖子版本身份。
        task_id: 人工任务身份。
        normalized_model_text: 冻结规则生成的规范化模型文本。
        tourism_label: 已最终确认标签；待补充标注时为 ``None``。
        sample_frame: ``probability``、``targeted`` 或候补阶段的 ``None``。
        selection_reason_code: 不含平台的稳定选择原因。
        selection_rank: 样本框内或全局候补队列中的正整数秩。
        inclusion_probability: 合法时的纳入概率；不可证明时为 ``None``。
        analysis_weight: 合法时的分析权重；不可证明时为 ``None``。
        evidence_origin: ``existing_representative`` 或 ``supplemental_annotation``。
        structure_usable: 结构门是否允许进入标注参考集。
    """

    identity: SourceIdentity
    task_id: str
    normalized_model_text: str
    tourism_label: str | None
    sample_frame: str | None
    selection_reason_code: str
    selection_rank: int
    inclusion_probability: float | None
    analysis_weight: float | None
    evidence_origin: str
    structure_usable: bool = True

    @property
    def normalized_sha256(self) -> str:
        """返回规范化文本的精确哈希。

        Returns:
            文本 UTF-8 字节的 SHA-256。
        """

        return text_sha256(self.normalized_model_text)

    @property
    def completeness(self) -> int:
        """计算平台无关的文本完整度决胜量。

        Returns:
            去除所有 Unicode 空白后的字符数。该值只用于重复组代表决胜，
            不读取旅游标签或平台。
        """

        return sum(not character.isspace() for character in self.normalized_model_text)


@dataclass(frozen=True, order=True)
class PairIdentity:
    """无向帖子对的规范身份。

    Attributes:
        left: 排序后较小的源身份。
        right: 排序后较大的源身份。
    """

    left: SourceIdentity
    right: SourceIdentity

    def __post_init__(self) -> None:
        """强制无向边只使用严格递增端点。

        Raises:
            ReferenceDatasetError: 端点相同或顺序不规范。
        """

        if self.left >= self.right:
            raise ReferenceDatasetError("pair_identity_not_canonical")

    @classmethod
    def of(cls, first: SourceIdentity, second: SourceIdentity) -> "PairIdentity":
        """把任意两个不同身份规范化为无向边。

        Args:
            first: 第一个端点。
            second: 第二个端点。

        Returns:
            端点严格递增的无向帖子对。

        Raises:
            ReferenceDatasetError: 两个端点相同。
        """

        if first == second:
            raise ReferenceDatasetError("pair_identity_self_edge")
        left, right = sorted((first, second))
        return cls(left, right)

    def as_list(self) -> list[list[int]]:
        """返回用于规范哈希的嵌套身份列表。

        Returns:
            左右端点的稳定整数列表。
        """

        return [self.left.as_list(), self.right.as_list()]


@dataclass(frozen=True)
class DuplicateCandidate:
    """需要人工复核或可自动成组的重复候选边。

    Attributes:
        pair: 无向帖子对。
        similarity: 冻结字符 TF-IDF 余弦相似度。
        exact_normalized_hash_match: 是否为规范化文本精确哈希相同。
        candidate_reason_code: ``exact_normalized_hash`` 或
            ``char_tfidf_similarity``。
    """

    pair: PairIdentity
    similarity: float
    exact_normalized_hash_match: bool
    candidate_reason_code: str


@dataclass(frozen=True)
class DuplicateDecision:
    """人工最终确认的无向重复关系证据。

    Attributes:
        evidence_id: 人工证据的稳定身份。
        pair: 已确认的无向帖子对。
        decision: ``duplicate`` 或 ``not_duplicate``。
        evidence_origin: 常规阈值候选或人工显式低阈值补入。
    """

    evidence_id: str
    pair: PairIdentity
    decision: str
    evidence_origin: str


@dataclass(frozen=True)
class FinalReferenceRow:
    """最终权威 CSV 的一行。

    Attributes:
        post: 已具备最终标签和样本框身份的帖子。
        duplicate_component_id: 由确认重复边稳定计算的分量身份。
    """

    post: ReferencePost
    duplicate_component_id: str

    def as_csv_row(self) -> dict[str, Any]:
        """转换为冻结字段契约的 CSV 行。

        Returns:
            字段顺序由 ``FINAL_REFERENCE_FIELDS`` 约束的映射；空概率证据使用
            空字符串，避免把不可用权重伪装成零。
        """

        return {
            "task_id": self.post.task_id,
            "source_post_id": self.post.identity.source_post_id,
            "source_version": self.post.identity.source_version,
            "normalized_model_text": self.post.normalized_model_text,
            "tourism_label": self.post.tourism_label or "",
            "sample_frame": self.post.sample_frame or "",
            "selection_reason_code": self.post.selection_reason_code,
            "selection_rank": self.post.selection_rank,
            "inclusion_probability": (
                "" if self.post.inclusion_probability is None else self.post.inclusion_probability
            ),
            "analysis_weight": (
                "" if self.post.analysis_weight is None else self.post.analysis_weight
            ),
            "evidence_origin": self.post.evidence_origin,
            "duplicate_component_id": self.duplicate_component_id,
        }
