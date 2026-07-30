"""与数据库无关的处理清单和输入变化影响规则。"""

from __future__ import annotations

from typing import Mapping


POST_STAGES: tuple[str, ...] = ("text_deterministic", "text_relevance", "finalize")
IMAGE_STAGES: tuple[str, ...] = (
    "image_role",
    "image_fingerprint",
    "image_noise",
    "finalize",
)

_VERSION_COMPONENTS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("post", "text_deterministic"): ("text_deterministic", "text_normalization"),
    ("post", "text_relevance"): (
        "text_deterministic",
        "text_normalization",
        "text_relevance",
    ),
    ("post", "finalize"): (
        "text_deterministic",
        "text_normalization",
        "text_relevance",
        "finalize",
    ),
    ("image", "image_role"): ("image_role",),
    ("image", "image_fingerprint"): ("image_role", "image_fingerprint"),
    ("image", "image_noise"): ("image_role", "image_fingerprint", "image_noise"),
    ("image", "finalize"): (
        "image_role",
        "image_fingerprint",
        "image_noise",
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


def image_affected_stages(changed_axes: set[str]) -> set[str]:
    """把图片关系或文件变化映射到图片处理及其下游。"""

    if "relation" in changed_axes:
        return set(IMAGE_STAGES)
    if "file" in changed_axes:
        return {"image_fingerprint", "image_noise", "finalize"}
    return set()


def stage_required(object_type: str, stage_name: str) -> int:
    """图片文件相关任务允许因尚未落盘而阻塞，其余任务属于必需任务。"""

    if object_type == "image" and stage_name in {"image_fingerprint", "image_noise", "finalize"}:
        return 0
    return 1


def algorithm_affected_stages(object_type: str, changed_stages: set[str]) -> set[str]:
    """把算法版本变化传播到依赖该结果的全部下游处理。"""

    dependencies = {
        "post": {
            "text_deterministic": {"text_deterministic", "text_relevance", "finalize"},
            "text_relevance": {"text_relevance", "finalize"},
            "finalize": {"finalize"},
        },
        "image": {
            "image_role": set(IMAGE_STAGES),
            "image_fingerprint": {"image_fingerprint", "image_noise", "finalize"},
            "image_noise": {"image_noise", "finalize"},
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
