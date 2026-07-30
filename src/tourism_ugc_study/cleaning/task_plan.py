"""与数据库无关的处理清单和输入变化影响规则。"""

from __future__ import annotations


POST_STAGES: tuple[str, ...] = ("text_deterministic", "text_relevance", "finalize")
IMAGE_STAGES: tuple[str, ...] = (
    "image_role",
    "image_fingerprint",
    "image_noise",
    "finalize",
)


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
