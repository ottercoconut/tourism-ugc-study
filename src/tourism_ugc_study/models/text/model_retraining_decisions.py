"""训练人工标签、审计覆盖和通过尾部模型动作的最终派生决定。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .model_retraining_audit import load_audit_assessment_package
from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_inference import load_inference_scoring_package
from .model_retraining_snapshot_artifacts import load_retraining_snapshot_package


class ModelRetrainingDecisionError(RuntimeError):
    """人工覆盖、尾部降级或最终派生决定失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、身份、标签或路径的稳定失败码。"""

        super().__init__("formal model retraining decision failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class FinalCleaningDecision:
    """一条候选记录的最终派生动作与证据来源。"""

    source_post_id: int
    source_version: int
    component_id: str
    final_action: str
    decision_source: str
    policy_id: str | None
    p_unrelated: float | None

    @property
    def identity(self) -> tuple[int, int]:
        """返回稳定帖子身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class FinalDecisionPackageResult:
    """正式派生决定的公开计数、自动动作和只读保证。"""

    decision_run_id: str
    count: int
    action_counts: Mapping[str, int]
    source_counts: Mapping[str, int]
    enabled_automatic_actions: tuple[str, ...]
    source_database_write_count: int
    manifest_sha256: str
    decisions_sha256: str
    reused: bool
    status: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序、禁止NaN且以换行结束的规范JSON。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    """计算字节流摘要。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算最终决定文件摘要。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子发布最终决定或manifest。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingDecisionError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingDecisionError(reason_code)
    return value


def _result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> FinalDecisionPackageResult:
    """从已验证manifest生成公开摘要。"""

    return FinalDecisionPackageResult(
        decision_run_id=str(manifest["decision_run_id"]),
        count=int(manifest["count"]),
        action_counts=dict(manifest["action_counts"]),
        source_counts=dict(manifest["source_counts"]),
        enabled_automatic_actions=tuple(
            manifest["enabled_automatic_actions"]
        ),
        source_database_write_count=int(manifest["source_database_write_count"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        decisions_sha256=str(manifest["artifacts"]["decisions"]["sha256"]),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_existing(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> FinalDecisionPackageResult:
    """严格验证既有最终派生决定包。"""

    manifest_path = directory / "final-decision-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_decision_manifest_invalid"
    )
    decisions = manifest.get("artifacts", {}).get("decisions", {})
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-final-decisions"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("count") != 13858
        or manifest.get("fit_call_count") != 0
        or manifest.get("source_database_write_count") != 0
        or manifest.get("source_records_deleted") != 0
        or decisions.get("filename") != "final-decisions.json"
        or _file_sha256(directory / decisions["filename"])
        != decisions.get("sha256")
    ):
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_manifest_invalid"
        )
    return _result(manifest, manifest_path.read_bytes(), reused=True)


def resolve_final_action(
    provisional_action: str,
    *,
    enabled_automatic_actions: set[str],
    audited_label: str | None = None,
) -> tuple[str, str]:
    """应用人工优先和单尾失败降级规则。

    Args:
        provisional_action: 模型的三段临时动作。
        enabled_automatic_actions: 新盲审通过的自动尾部集合。
        audited_label: 审计成员的人工标签；非审计成员为``None``。

    Returns:
        最终动作和稳定证据来源。

    Raises:
        ModelRetrainingDecisionError: 动作、通过集合或人工标签非法。
    """

    if (
        provisional_action
        not in {"auto_keep", "manual_review", "auto_exclude"}
        or not enabled_automatic_actions.issubset(
            {"auto_keep", "auto_exclude"}
        )
        or audited_label
        not in {None, "related", "unrelated", "uncertain"}
    ):
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_routing_input_invalid"
        )
    if audited_label is not None:
        action = (
            "keep"
            if audited_label == "related"
            else "exclude"
            if audited_label == "unrelated"
            else "manual_review"
        )
        return action, "human_audit_label"
    if provisional_action in enabled_automatic_actions:
        return (
            "keep" if provisional_action == "auto_keep" else "exclude",
            "model_audit_released",
        )
    if provisional_action in {"auto_keep", "auto_exclude"}:
        return "manual_review", "model_tail_downgraded"
    return "manual_review", "model_middle_band"


def build_final_decisions_package(
    snapshot_package: str | Path,
    inference_package: str | Path,
    audit_assessment_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_snapshot_manifest_sha256: str,
    expected_inference_manifest_sha256: str,
    expected_audit_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> FinalDecisionPackageResult:
    """合并人工标签、审计覆盖和通过尾部动作，不写源数据库。"""

    snapshot, snapshot_manifest = load_retraining_snapshot_package(
        snapshot_package,
        plan=plan,
        expected_manifest_sha256=expected_snapshot_manifest_sha256,
    )
    scored, inference_manifest = load_inference_scoring_package(
        inference_package,
        plan=plan,
        expected_manifest_sha256=expected_inference_manifest_sha256,
    )
    audit_labels, audit_manifest = load_audit_assessment_package(
        audit_assessment_package,
        plan=plan,
        expected_manifest_sha256=expected_audit_manifest_sha256,
    )
    if len(code_version) != 40:
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_code_version_invalid"
        )
    audit_by_identity = {
        (int(item["source_post_id"]), int(item["source_version"])): item
        for item in audit_labels
    }
    if len(audit_by_identity) != 300:
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_audit_members_invalid"
        )
    enabled = set(str(item) for item in audit_manifest["enabled_automatic_actions"])
    if not enabled.issubset({"auto_keep", "auto_exclude"}):
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_audit_status_invalid"
        )
    policy_id = str(inference_manifest["policy_id"])
    decisions: list[FinalCleaningDecision] = []
    for document in snapshot.documents:
        decisions.append(
            FinalCleaningDecision(
                source_post_id=document.source_post_id,
                source_version=document.source_version,
                component_id=document.component_id,
                final_action=(
                    "keep" if document.tourism_label == "related" else "exclude"
                ),
                decision_source="human_training_label",
                policy_id=None,
                p_unrelated=None,
            )
        )
    for member in scored:
        audited = audit_by_identity.get(member.identity)
        action, source = resolve_final_action(
            member.provisional_action,
            enabled_automatic_actions=enabled,
            audited_label=(
                str(audited["tourism_label"]) if audited is not None else None
            ),
        )
        decisions.append(
            FinalCleaningDecision(
                source_post_id=member.source_post_id,
                source_version=member.source_version,
                component_id=member.component_id,
                final_action=action,
                decision_source=source,
                policy_id=policy_id,
                p_unrelated=member.p_unrelated,
            )
        )
    decisions.sort(key=lambda item: item.identity)
    if (
        len(decisions) != 13858
        or len({item.identity for item in decisions}) != len(decisions)
        or any(
            item.final_action not in {"keep", "exclude", "manual_review"}
            for item in decisions
        )
    ):
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_output_invalid"
        )
    decision_run_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{inference_manifest['inference_id']}|"
            f"{audit_manifest['assessment_id']}|final-decisions"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / decision_run_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingDecisionError(
                "model_retraining_decision_existing_requires_manifest_hash"
            )
        return _validate_existing(
            directory,
            plan=plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    decisions_bytes = _canonical_bytes([asdict(item) for item in decisions])
    action_counts = dict(sorted(Counter(item.final_action for item in decisions).items()))
    source_counts = dict(sorted(Counter(item.decision_source for item in decisions).items()))
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_directory_create_failed"
        ) from exc
    _atomic_write(directory / "final-decisions.json", decisions_bytes)
    status = (
        "BOTH_AUTOMATIC_ACTIONS_ENABLED"
        if len(enabled) == 2
        else "ONE_AUTOMATIC_ACTION_ENABLED"
        if len(enabled) == 1
        else "MANUAL_ONLY"
    )
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-final-decisions",
        "status": status,
        "decision_run_id": decision_run_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "snapshot_id": str(snapshot_manifest["snapshot_id"]),
        "snapshot_manifest_sha256": expected_snapshot_manifest_sha256,
        "inference_id": str(inference_manifest["inference_id"]),
        "inference_manifest_sha256": expected_inference_manifest_sha256,
        "assessment_id": str(audit_manifest["assessment_id"]),
        "audit_manifest_sha256": expected_audit_manifest_sha256,
        "policy_id": policy_id,
        "count": len(decisions),
        "training_human_count": snapshot.count,
        "audit_human_count": len(audit_labels),
        "enabled_automatic_actions": sorted(enabled),
        "action_counts": action_counts,
        "source_counts": source_counts,
        "fit_call_count": 0,
        "source_database_write_count": 0,
        "source_records_deleted": 0,
        "platform_used": False,
        "artifacts": {
            "decisions": {
                "filename": "final-decisions.json",
                "sha256": _sha256_bytes(decisions_bytes),
            }
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "final-decision-manifest.json", manifest_bytes)
    return _result(manifest, manifest_bytes, reused=False)


def render_final_decision_result(
    result: FinalDecisionPackageResult, *, output_format: str
) -> str:
    """输出机器JSON或最终派生清洗决定摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_output_format_invalid"
        )
    return "\n".join(
        [
            "# 最终派生清洗决定",
            "",
            f"决定运行ID：{result.decision_run_id}",
            f"候选人口：{result.count}",
            "动作：" + " / ".join(f"{key}={value}" for key, value in result.action_counts.items()),
            "证据来源：" + " / ".join(f"{key}={value}" for key, value in result.source_counts.items()),
            f"允许自动动作：{', '.join(result.enabled_automatic_actions) or '无'}",
            "fit调用：0；源数据库写入：0；源记录删除：0。",
            "所有结果只存在派生artifact，可按模型、阈值、审计和人工覆盖追溯。",
            f"状态：{result.status}",
        ]
    )
