"""与数据库无关的处理清单和输入变化影响规则。"""

from __future__ import annotations

from typing import Mapping


POST_STAGES: tuple[str, ...] = ("text_deterministic", "text_relevance", "finalize")

_VERSION_COMPONENTS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("post", "text_deterministic"): (
        "text_deterministic",
        "text_normalization",
        "text_runtime",
    ),
    ("post", "text_relevance"): (
        "text_deterministic",
        "text_normalization",
        "text_runtime",
        "text_relevance",
    ),
    ("post", "finalize"): (
        "text_deterministic",
        "text_normalization",
        "text_runtime",
        "text_relevance",
        "finalize",
    ),
}


def post_affected_stages(changed_axes: set[str]) -> set[str]:
    """把帖子输入变化映射到最小重跑范围。"""

    affected: set[str] = set()
    if "text" in changed_axes:
        affected.update(POST_STAGES)
    if "author" in changed_axes:
        affected.update(("text_relevance", "finalize"))
    return affected


def stage_required(object_type: str, stage_name: str) -> int:
    """校验帖子任务身份并返回必需标记。"""

    if object_type != "post" or stage_name not in POST_STAGES:
        raise ValueError("unsupported_cleaning_task")
    return 1


def algorithm_affected_stages(object_type: str, changed_stages: set[str]) -> set[str]:
    """把算法版本变化传播到依赖该结果的全部下游处理。"""

    dependencies = {
        "post": {
            "text_deterministic": {"text_deterministic", "text_relevance", "finalize"},
            "text_relevance": {"text_relevance", "finalize"},
            "finalize": {"finalize"},
        },
    }
    affected: set[str] = set()
    for stage_name in changed_stages:
        affected.update(dependencies.get(object_type, {}).get(stage_name, {stage_name}))
    return affected


def effective_stage_version(
    algorithm_versions: Mapping[str, str | int],
    object_type: str,
    stage_name: str,
) -> str:
    """组合本处理及全部上游算法版本，形成可审计的有效处理版本。"""

    components = _VERSION_COMPONENTS.get((object_type, stage_name), (stage_name,))
    return ";".join(f"{name}={algorithm_versions[name]}" for name in components)
