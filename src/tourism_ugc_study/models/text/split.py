"""按泄漏分量隔离且按平台较晚时段留出的数据切分。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from sklearn.model_selection import StratifiedGroupKFold


class SplitError(RuntimeError):
    """金标规模或分组结构无法形成合规三集合时抛出。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__("leakage-safe split failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class SplitDocument:
    """切分所需的标签、平台、时间和泄漏分量投影。"""

    source_post_id: int
    source_version: int
    platform_key: str
    captured_at_sort: str
    tourism_label: str
    component_id: str


@dataclass(frozen=True)
class SplitAssignment:
    """单条金标记录的固定集合归属。"""

    source_post_id: int
    source_version: int
    component_id: str
    split_name: str


@dataclass(frozen=True)
class SplitPlan:
    """无组件交叉的三集合清单及其规范哈希。"""

    assignments: tuple[SplitAssignment, ...]
    manifest_sha256: str


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _component_order_key(
    component_id: str,
    documents: Sequence[SplitDocument],
    platform: str,
    seed: int,
) -> tuple[str, str]:
    times = [item.captured_at_sort for item in documents if item.platform_key == platform]
    latest = max(times) if times else ""
    tie = _sha256([seed, "temporal-test", platform, component_id])
    return latest, tie


def _require_both_labels(documents: Sequence[SplitDocument], reason_code: str) -> None:
    if {item.tourism_label for item in documents} != {"related", "unrelated"}:
        raise SplitError(reason_code)


def build_split_plan(
    documents: Sequence[SplitDocument],
    *,
    random_seed: int,
    temporal_test_fraction: float,
    temporal_test_min_per_platform: int,
    validation_fraction: float,
) -> SplitPlan:
    """先留出各平台较晚分量，再用训练余集做分层分组验证切分。

    测试集在任何 C 或阈值选择前即被冻结。每个 component 只能进入一个集合；
    若某平台无法在至少留一条非测试记录的前提下满足最小测试量，函数明确失败。
    """

    identities = [(item.source_post_id, item.source_version) for item in documents]
    if len(identities) != len(set(identities)):
        raise SplitError("duplicate_gold_identity")
    if not documents:
        raise SplitError("empty_gold_dataset")
    _require_both_labels(documents, "gold_dataset_missing_class")
    by_component: dict[str, list[SplitDocument]] = defaultdict(list)
    by_platform: dict[str, list[SplitDocument]] = defaultdict(list)
    for document in documents:
        by_component[document.component_id].append(document)
        by_platform[document.platform_key].append(document)

    test_components: set[str] = set()
    for platform in sorted(by_platform):
        platform_documents = by_platform[platform]
        target = max(
            temporal_test_min_per_platform,
            math.ceil(len(platform_documents) * temporal_test_fraction),
        )
        if target >= len(platform_documents):
            raise SplitError("platform_temporal_test_capacity_insufficient")
        candidates = sorted(
            {
                item.component_id for item in platform_documents
            },
            key=lambda component: _component_order_key(
                component, by_component[component], platform, random_seed
            ),
            reverse=True,
        )
        selected_count = sum(
            item.platform_key == platform
            for component in test_components
            for item in by_component[component]
        )
        for component in candidates:
            if selected_count >= target:
                break
            if component not in test_components:
                test_components.add(component)
                selected_count += sum(
                    item.platform_key == platform for item in by_component[component]
                )
        if selected_count < target:
            raise SplitError("platform_temporal_test_minimum_not_met")

    test_documents = [
        item for item in documents if item.component_id in test_components
    ]
    remaining = [
        item for item in documents if item.component_id not in test_components
    ]
    _require_both_labels(test_documents, "test_set_missing_class")
    _require_both_labels(remaining, "development_set_missing_class")
    remaining_components = {item.component_id for item in remaining}
    if len(remaining_components) < 2:
        raise SplitError("insufficient_development_components")

    labels = np.asarray([item.tourism_label for item in remaining])
    groups = np.asarray([item.component_id for item in remaining])
    indices = np.arange(len(remaining))
    desired_splits = max(2, round(1.0 / validation_fraction))
    n_splits = min(desired_splits, len(remaining_components))
    splitter = StratifiedGroupKFold(
        n_splits=n_splits,
        shuffle=True,
        random_state=random_seed,
    )
    candidates: list[tuple[float, tuple[int, ...], tuple[int, ...]]] = []
    for train_indices, validation_indices in splitter.split(indices, labels, groups):
        train = [remaining[int(index)] for index in train_indices]
        validation = [remaining[int(index)] for index in validation_indices]
        if (
            {item.tourism_label for item in train} != {"related", "unrelated"}
            or {item.tourism_label for item in validation} != {"related", "unrelated"}
        ):
            continue
        fraction_error = abs(len(validation) / len(remaining) - validation_fraction)
        class_error = abs(
            Counter(item.tourism_label for item in validation)["unrelated"]
            / len(validation)
            - Counter(item.tourism_label for item in remaining)["unrelated"] / len(remaining)
        )
        candidates.append(
            (
                fraction_error + class_error,
                tuple(int(index) for index in train_indices),
                tuple(int(index) for index in validation_indices),
            )
        )
    if not candidates:
        raise SplitError("no_stratified_group_validation_split")
    _, train_indices, validation_indices = min(candidates, key=lambda item: item[0])
    train_components = {remaining[index].component_id for index in train_indices}
    validation_components = {remaining[index].component_id for index in validation_indices}
    if train_components & validation_components or (
        test_components & (train_components | validation_components)
    ):
        raise SplitError("component_leakage_detected")

    split_by_component = {
        **{component: "train" for component in train_components},
        **{component: "validation" for component in validation_components},
        **{component: "test" for component in test_components},
    }
    assignments = tuple(
        SplitAssignment(
            item.source_post_id,
            item.source_version,
            item.component_id,
            split_by_component[item.component_id],
        )
        for item in sorted(documents, key=lambda value: (value.source_post_id, value.source_version))
    )
    return SplitPlan(assignments, _sha256([item.__dict__ for item in assignments]))
