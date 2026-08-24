"""冻结策略后的150+150盲审抽样、导入、区间与单尾降级判读。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.stats import beta

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_inference import (
    ScoredRoutingMember,
    load_inference_scoring_package,
)


class ModelRetrainingAuditError(RuntimeError):
    """双尾盲审抽样、完成表或判读失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、身份、标签或概率的稳定失败码。"""

        super().__init__("formal model retraining audit failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class AuditTaskPackageResult:
    """不暴露动作分层和成员身份的盲审任务摘要。"""

    audit_id: str
    inference_id: str
    count: int
    auto_keep_count: int
    auto_exclude_count: int
    task_filename: str
    task_sha256: str
    manifest_sha256: str
    reused: bool
    status: str


@dataclass(frozen=True)
class AuditTailResult:
    """一个自动尾部的不利事件、区间与独立放行状态。"""

    action: str
    count: int
    adverse_event_count: int
    maximum_adverse_events: int
    passed: bool
    clopper_pearson_one_sided_upper: float
    component_bootstrap_lower: float
    component_bootstrap_upper: float


@dataclass(frozen=True)
class AuditAssessmentResult:
    """两个尾部的判读及最终可启用动作。"""

    assessment_id: str
    audit_id: str
    auto_keep: AuditTailResult
    auto_exclude: AuditTailResult
    enabled_automatic_actions: tuple[str, ...]
    failed_automatic_actions: tuple[str, ...]
    manifest_sha256: str
    labels_sha256: str
    reused: bool
    status: str
    resampling_allowed: bool


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
    """计算字节流SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算盲审文件摘要。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子发布盲审任务或判读文件。"""

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
        raise ModelRetrainingAuditError(
            "model_retraining_audit_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingAuditError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingAuditError(reason_code)
    return value


def _task_csv_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    """以UTF-8 BOM和固定四列生成一行一条的人工任务。"""

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "task_id",
            "sample_run_id",
            "normalized_model_text",
            "tourism_label",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _audit_task_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> AuditTaskPackageResult:
    """从已验证任务manifest生成公开摘要。"""

    return AuditTaskPackageResult(
        audit_id=str(manifest["audit_id"]),
        inference_id=str(manifest["inference_id"]),
        count=int(manifest["count"]),
        auto_keep_count=int(manifest["tail_counts"]["auto_keep"]),
        auto_exclude_count=int(manifest["tail_counts"]["auto_exclude"]),
        task_filename=str(manifest["artifacts"]["task"]["filename"]),
        task_sha256=str(manifest["artifacts"]["task"]["sha256"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_task_package(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> AuditTaskPackageResult:
    """严格验证既有盲审任务，阻止补抽或替换。"""

    manifest_path = directory / "audit-task-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_task_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_audit_task_manifest_invalid"
    )
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-blind-audit-task"
        or manifest.get("status") != "BLIND_AUDIT_TASK_FROZEN"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("count") != 300
        or manifest.get("tail_counts")
        != {"auto_exclude": 150, "auto_keep": 150}
        or manifest.get("replacement_or_dilution_allowed") is not False
        or manifest.get("model_outputs_present_in_task") is not False
    ):
        raise ModelRetrainingAuditError(
            "model_retraining_audit_task_manifest_invalid"
        )
    for key, filename in {
        "task": "tourism-relevance-routing-audit.csv",
        "private_map": "private-map.json",
    }.items():
        details = manifest.get("artifacts", {}).get(key, {})
        if (
            details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingAuditError(
                "model_retraining_audit_task_artifact_hash_mismatch"
            )
    return _audit_task_result(manifest, manifest_path.read_bytes(), reused=True)


def prepare_blind_audit_package(
    inference_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_inference_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> AuditTaskPackageResult:
    """从两个临时自动尾部各简单随机无放回抽取150条盲审任务。"""

    records, inference_manifest = load_inference_scoring_package(
        inference_package,
        plan=plan,
        expected_manifest_sha256=expected_inference_manifest_sha256,
    )
    if len(code_version) != 40:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_code_version_invalid"
        )
    tails = {
        action: [item for item in records if item.provisional_action == action]
        for action in ("auto_keep", "auto_exclude")
    }
    if any(
        len(items) < plan.audit_sample_count_per_tail for items in tails.values()
    ):
        raise ModelRetrainingAuditError(
            "model_retraining_audit_tail_population_insufficient"
        )
    audit_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{inference_manifest['inference_id']}|"
            "blind-audit-150-per-tail"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / audit_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingAuditError(
                "model_retraining_audit_existing_requires_manifest_hash"
            )
        return _validate_task_package(
            directory,
            plan=plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    selected: list[tuple[str, ScoredRoutingMember]] = []
    for action in ("auto_keep", "auto_exclude"):
        seed = int(
            hashlib.sha256(
                f"{plan.random_seed}|{audit_id}|{action}".encode()
            ).hexdigest()[:16],
            16,
        )
        generator = np.random.default_rng(seed)
        indices = generator.choice(
            len(tails[action]),
            size=plan.audit_sample_count_per_tail,
            replace=False,
        )
        selected.extend((action, tails[action][int(index)]) for index in indices)
    shuffle_seed = int(hashlib.sha256(f"{audit_id}|shuffle".encode()).hexdigest()[:16], 16)
    generator = np.random.default_rng(shuffle_seed)
    order = generator.permutation(len(selected))
    task_rows: list[Mapping[str, str]] = []
    private_rows: list[Mapping[str, Any]] = []
    for position in order:
        action, member = selected[int(position)]
        task_id = hashlib.sha256(
            f"{audit_id}|task|{member.member_key}".encode()
        ).hexdigest()[:24]
        task_rows.append(
            {
                "task_id": task_id,
                "sample_run_id": audit_id,
                "normalized_model_text": member.normalized_model_text,
                "tourism_label": "",
            }
        )
        private_rows.append(
            {
                "task_id": task_id,
                "member_key": member.member_key,
                "source_post_id": member.source_post_id,
                "source_version": member.source_version,
                "component_id": member.component_id,
                "normalized_sha256": member.normalized_sha256,
                "provisional_action": action,
                "p_unrelated": member.p_unrelated,
            }
        )
    if (
        len(task_rows) != 300
        or len({item["task_id"] for item in task_rows}) != 300
        or Counter(item["provisional_action"] for item in private_rows)
        != {"auto_keep": 150, "auto_exclude": 150}
    ):
        raise ModelRetrainingAuditError(
            "model_retraining_audit_sample_invalid"
        )
    task_bytes = _task_csv_bytes(task_rows)
    private_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-model-retraining-blind-audit-private-map",
            "audit_id": audit_id,
            "records": private_rows,
        }
    )
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_directory_create_failed"
        ) from exc
    _atomic_write(directory / "tourism-relevance-routing-audit.csv", task_bytes)
    _atomic_write(directory / "private-map.json", private_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-blind-audit-task",
        "status": "BLIND_AUDIT_TASK_FROZEN",
        "audit_id": audit_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "inference_id": str(inference_manifest["inference_id"]),
        "inference_manifest_sha256": expected_inference_manifest_sha256,
        "count": 300,
        "tail_counts": {"auto_keep": 150, "auto_exclude": 150},
        "sampling_method": "simple_random_without_replacement_within_action_tail",
        "task_columns": list(plan.audit_task_columns),
        "task_encoding": "utf-8-sig",
        "model_outputs_present_in_task": False,
        "selection_reason_present_in_task": False,
        "replacement_or_dilution_allowed": False,
        "fit_call_count": 0,
        "automatic_routing_authorized": False,
        "artifacts": {
            "task": {
                "filename": "tourism-relevance-routing-audit.csv",
                "sha256": _sha256_bytes(task_bytes),
            },
            "private_map": {
                "filename": "private-map.json",
                "sha256": _sha256_bytes(private_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "audit-task-manifest.json", manifest_bytes)
    return _audit_task_result(manifest, manifest_bytes, reused=False)


def _read_completed_csv(path: str | Path) -> list[Mapping[str, str]]:
    """读取严格四列、300行且标签终止的完成表。"""

    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != [
                "task_id",
                "sample_run_id",
                "normalized_model_text",
                "tourism_label",
            ]:
                raise ValueError
            rows = [dict(item) for item in reader]
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_completed_csv_invalid"
        ) from exc
    if len(rows) != 300:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_completed_count_invalid"
        )
    return rows


def _one_sided_upper(events: int, count: int, confidence_level: float) -> float:
    """计算二项不利事件率的单侧Clopper–Pearson上界。"""

    if events == count:
        return 1.0
    return float(beta.ppf(confidence_level, events + 1, count - events))


def _component_bootstrap_interval(
    rows: Sequence[Mapping[str, Any]],
    *,
    plan: ModelRetrainingPlan,
    seed_suffix: str,
) -> tuple[float, float]:
    """按component聚类重抽不利事件率并给出双侧百分位区间。"""

    by_component: dict[str, list[int]] = {}
    for row in rows:
        by_component.setdefault(str(row["component_id"]), []).append(
            int(row["adverse"])
        )
    components = sorted(by_component)
    generator = np.random.default_rng(
        int(
            hashlib.sha256(
                f"{plan.random_seed}|audit|{seed_suffix}".encode()
            ).hexdigest()[:16],
            16,
        )
    )
    rates: list[float] = []
    for _ in range(plan.bootstrap_repetitions):
        sampled = generator.choice(components, size=len(components), replace=True)
        values = [event for component in sampled for event in by_component[str(component)]]
        rates.append(float(np.mean(values)))
    alpha = (1 - plan.confidence_level) / 2
    return float(np.quantile(rates, alpha)), float(np.quantile(rates, 1 - alpha))


def _tail_result(
    action: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    plan: ModelRetrainingPlan,
) -> AuditTailResult:
    """按固定3/7事件门判读单个尾部。"""

    maximum = (
        plan.audit_auto_exclude_maximum_adverse_events
        if action == "auto_exclude"
        else plan.audit_auto_keep_maximum_adverse_events
    )
    events = sum(int(item["adverse"]) for item in rows)
    lower, upper = _component_bootstrap_interval(
        rows, plan=plan, seed_suffix=action
    )
    return AuditTailResult(
        action=action,
        count=len(rows),
        adverse_event_count=events,
        maximum_adverse_events=maximum,
        passed=events <= maximum,
        clopper_pearson_one_sided_upper=_one_sided_upper(
            events, len(rows), plan.confidence_level
        ),
        component_bootstrap_lower=lower,
        component_bootstrap_upper=upper,
    )


def assess_audit_labels(
    labels: Sequence[Mapping[str, Any]], *, plan: ModelRetrainingPlan
) -> tuple[AuditTailResult, AuditTailResult]:
    """纯计算判读300条审计标签，供artifact和边界测试共同复用。"""

    by_action = {
        action: [item for item in labels if item["provisional_action"] == action]
        for action in ("auto_keep", "auto_exclude")
    }
    if any(len(rows) != 150 for rows in by_action.values()):
        raise ModelRetrainingAuditError(
            "model_retraining_audit_tail_count_invalid"
        )
    keep = _tail_result("auto_keep", by_action["auto_keep"], plan=plan)
    exclude = _tail_result(
        "auto_exclude", by_action["auto_exclude"], plan=plan
    )
    return keep, exclude


def _assessment_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> AuditAssessmentResult:
    """从判读manifest生成公开摘要。"""

    return AuditAssessmentResult(
        assessment_id=str(manifest["assessment_id"]),
        audit_id=str(manifest["audit_id"]),
        auto_keep=AuditTailResult(**manifest["tails"]["auto_keep"]),
        auto_exclude=AuditTailResult(**manifest["tails"]["auto_exclude"]),
        enabled_automatic_actions=tuple(manifest["enabled_automatic_actions"]),
        failed_automatic_actions=tuple(manifest["failed_automatic_actions"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        labels_sha256=str(manifest["artifacts"]["labels"]["sha256"]),
        reused=reused,
        status=str(manifest["status"]),
        resampling_allowed=bool(manifest["resampling_allowed"]),
    )


def assess_blind_audit_package(
    completed_csv: str | Path,
    task_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_task_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> AuditAssessmentResult:
    """导入一次完成表，独立判读两个尾部且禁止失败后稀释。"""

    try:
        task_directory = Path(task_package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_task_package_unavailable"
        ) from exc
    _validate_task_package(
        task_directory,
        plan=plan,
        expected_manifest_sha256=expected_task_manifest_sha256,
    )
    if len(code_version) != 40:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_code_version_invalid"
        )
    task_manifest = _load_json(
        task_directory / "audit-task-manifest.json",
        "model_retraining_audit_task_manifest_invalid",
    )
    private = _load_json(
        task_directory / "private-map.json",
        "model_retraining_audit_private_map_invalid",
    )
    private_rows = private.get("records")
    if not isinstance(private_rows, list) or len(private_rows) != 300:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_private_map_invalid"
        )
    by_task = {str(item["task_id"]): item for item in private_rows}
    completed = _read_completed_csv(completed_csv)
    labels: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for row in completed:
        task_id = row["task_id"]
        mapped = by_task.get(task_id)
        if (
            mapped is None
            or task_id in seen
            or row["sample_run_id"] != task_manifest["audit_id"]
            or row["tourism_label"]
            not in {"related", "unrelated", "uncertain"}
            or hashlib.sha256(
                row["normalized_model_text"].encode("utf-8")
            ).hexdigest()
            != mapped["normalized_sha256"]
        ):
            raise ModelRetrainingAuditError(
                "model_retraining_audit_completed_binding_invalid"
            )
        seen.add(task_id)
        action = str(mapped["provisional_action"])
        label = row["tourism_label"]
        adverse = (
            label in {"unrelated", "uncertain"}
            if action == "auto_keep"
            else label in {"related", "uncertain"}
        )
        labels.append(
            {
                "task_id": task_id,
                "member_key": mapped["member_key"],
                "source_post_id": mapped["source_post_id"],
                "source_version": mapped["source_version"],
                "component_id": mapped["component_id"],
                "provisional_action": action,
                "tourism_label": label,
                "adverse": bool(adverse),
            }
        )
    if set(by_task) != seen:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_completed_membership_invalid"
        )
    keep, exclude = assess_audit_labels(labels, plan=plan)
    enabled = tuple(
        action
        for action, passed in (
            ("auto_keep", keep.passed),
            ("auto_exclude", exclude.passed),
        )
        if passed
    )
    failed = tuple(
        action
        for action in ("auto_keep", "auto_exclude")
        if action not in enabled
    )
    assessment_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{task_manifest['audit_id']}|"
            f"{_file_sha256(Path(completed_csv))}|assessment"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / assessment_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingAuditError(
                "model_retraining_audit_assessment_existing_requires_manifest_hash"
            )
        manifest_path = directory / "audit-assessment-manifest.json"
        if _file_sha256(manifest_path) != expected_existing_manifest_sha256:
            raise ModelRetrainingAuditError(
                "model_retraining_audit_assessment_manifest_hash_mismatch"
            )
        manifest = _load_json(
            manifest_path,
            "model_retraining_audit_assessment_manifest_invalid",
        )
        if (
            manifest.get("artifact_kind")
            != "formal-cleaning-model-retraining-audit-assessment"
            or manifest.get("plan_id") != plan.plan_id
            or manifest.get("audit_id") != task_manifest["audit_id"]
            or manifest.get("count") != 300
            or manifest.get("resampling_allowed") is not False
            or manifest.get("threshold_modification_allowed") is not False
            or manifest.get("fit_call_count") != 0
        ):
            raise ModelRetrainingAuditError(
                "model_retraining_audit_assessment_manifest_invalid"
            )
        for key, filename in {
            "labels": "audit-labels.json",
            "results": "audit-results.json",
        }.items():
            details = manifest.get("artifacts", {}).get(key, {})
            if (
                details.get("filename") != filename
                or _file_sha256(directory / filename) != details.get("sha256")
            ):
                raise ModelRetrainingAuditError(
                    "model_retraining_audit_assessment_artifact_hash_mismatch"
                )
        return _assessment_result(
            manifest, manifest_path.read_bytes(), reused=True
        )
    labels_bytes = _canonical_bytes(labels)
    results_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-model-retraining-audit-results",
            "audit_id": task_manifest["audit_id"],
            "tails": {"auto_keep": asdict(keep), "auto_exclude": asdict(exclude)},
            "intervals_are_hard_gates": False,
            "enabled_automatic_actions": list(enabled),
            "failed_automatic_actions": list(failed),
        }
    )
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_directory_create_failed"
        ) from exc
    _atomic_write(directory / "audit-labels.json", labels_bytes)
    _atomic_write(directory / "audit-results.json", results_bytes)
    status = (
        "BOTH_TAILS_RELEASED"
        if len(enabled) == 2
        else "ONE_TAIL_RELEASED"
        if len(enabled) == 1
        else "ALL_AUTOMATIC_ACTIONS_DOWNGRADED"
    )
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-audit-assessment",
        "status": status,
        "assessment_id": assessment_id,
        "audit_id": str(task_manifest["audit_id"]),
        "task_manifest_sha256": expected_task_manifest_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "completed_csv_sha256": _file_sha256(Path(completed_csv)),
        "count": 300,
        "tails": {"auto_keep": asdict(keep), "auto_exclude": asdict(exclude)},
        "enabled_automatic_actions": list(enabled),
        "failed_automatic_actions": list(failed),
        "intervals_are_hard_gates": False,
        "resampling_allowed": False,
        "threshold_modification_allowed": False,
        "fit_call_count": 0,
        "source_database_write_count": 0,
        "artifacts": {
            "labels": {
                "filename": "audit-labels.json",
                "sha256": _sha256_bytes(labels_bytes),
            },
            "results": {
                "filename": "audit-results.json",
                "sha256": _sha256_bytes(results_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "audit-assessment-manifest.json", manifest_bytes)
    return _assessment_result(manifest, manifest_bytes, reused=False)


def load_audit_assessment_package(
    package: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
    """严格读取双尾判读及300条人工覆盖供最终决定使用。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_assessment_package_unavailable"
        ) from exc
    manifest_path = directory / "audit-assessment-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_assessment_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_audit_assessment_manifest_invalid"
    )
    labels_details = manifest.get("artifacts", {}).get("labels", {})
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-audit-assessment"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("count") != 300
        or manifest.get("resampling_allowed") is not False
        or labels_details.get("filename") != "audit-labels.json"
        or _file_sha256(directory / "audit-labels.json")
        != labels_details.get("sha256")
    ):
        raise ModelRetrainingAuditError(
            "model_retraining_audit_assessment_manifest_invalid"
        )
    try:
        labels = json.loads(
            (directory / "audit-labels.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_labels_invalid"
        ) from exc
    if not isinstance(labels, list) or len(labels) != 300:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_labels_invalid"
        )
    return tuple(labels), manifest


def render_audit_task_result(
    result: AuditTaskPackageResult, *, output_format: str
) -> str:
    """输出盲审任务摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingAuditError(
            "model_retraining_audit_output_format_invalid"
        )
    return "\n".join(
        [
            "# 新双尾盲审任务",
            "",
            f"审计ID：{result.audit_id}",
            f"任务：{result.task_filename}；UTF-8 BOM四列；共{result.count}条",
            f"抽样：auto_keep={result.auto_keep_count} / auto_exclude={result.auto_exclude_count}",
            "人工只填写tourism_label；表中不含模型、概率、动作或入选原因。",
            "任务已一次冻结；不得补抽、替换或追加以稀释失败。",
            f"状态：{result.status}",
        ]
    )


def render_audit_assessment_result(
    result: AuditAssessmentResult, *, output_format: str
) -> str:
    """输出两个尾部独立放行/降级摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingAuditError(
            "model_retraining_audit_output_format_invalid"
        )
    return "\n".join(
        [
            "# 新双尾盲审判读",
            "",
            f"判读ID：{result.assessment_id}",
            (
                f"auto_keep：不利事件 {result.auto_keep.adverse_event_count}/150；"
                f"门≤{result.auto_keep.maximum_adverse_events}；"
                f"{'通过' if result.auto_keep.passed else '降为人工'}"
            ),
            (
                f"auto_exclude：不利事件 {result.auto_exclude.adverse_event_count}/150；"
                f"门≤{result.auto_exclude.maximum_adverse_events}；"
                f"{'通过' if result.auto_exclude.passed else '降为人工'}"
            ),
            f"允许自动动作：{', '.join(result.enabled_automatic_actions) or '无'}",
            f"降级动作：{', '.join(result.failed_automatic_actions) or '无'}",
            "Clopper–Pearson与component bootstrap已报告但不作硬门；禁止补抽。",
            f"状态：{result.status}",
        ]
    )
