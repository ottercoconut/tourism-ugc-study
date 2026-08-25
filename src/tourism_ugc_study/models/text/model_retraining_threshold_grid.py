"""既有概率上的完整双阈值事后探索与聚合结果封存。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.stats import beta

from .model_retraining_audit import load_audit_assessment_package
from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_inference import load_inference_scoring_package
from .model_retraining_training import RetrainingOofObservation
from .model_retraining_training_artifacts import (
    load_retraining_training_package,
)


class ModelRetrainingThresholdGridError(RuntimeError):
    """阈值全网格输入、计算或artifact封存失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、身份或逐条概率的稳定失败码。"""

        super().__init__("formal threshold grid exploration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class AuditThresholdObservation:
    """一条审计标签及其既有冻结概率。"""

    p_unrelated: float
    tourism_label: str
    component_id: str


@dataclass(frozen=True)
class ThresholdGridExplorationResult:
    """完整阈值网格、非冻结推荐和运行包摘要。"""

    exploration_id: str
    point_count: int
    recommended_T_keep: float
    recommended_T_exclude: float
    recommended_population_manual_count: int
    recommended_population_manual_rate: float
    grid_filename: str
    grid_sha256: str
    manifest_sha256: str
    reused: bool
    status: str


GRID_COLUMNS = (
    "T_keep",
    "T_exclude",
    "population_auto_keep_count",
    "population_manual_review_count",
    "population_auto_exclude_count",
    "population_auto_keep_rate",
    "population_manual_review_rate",
    "population_auto_exclude_rate",
    "wave_b_auto_keep_count",
    "wave_b_manual_review_count",
    "wave_b_auto_exclude_count",
    "wave_b_auto_keep_adverse_count",
    "wave_b_auto_exclude_adverse_count",
    "wave_b_auto_keep_raw_risk",
    "wave_b_auto_exclude_raw_risk",
    "wave_b_auto_keep_weighted_risk",
    "wave_b_auto_exclude_weighted_risk",
    "wave_b_weighted_manual_rate",
    "wave_b_auto_keep_cp95_upper",
    "wave_b_auto_exclude_cp95_upper",
    "wave_b_minimum_support_passed",
    "wave_b_raw_risk_gate_passed",
    "wave_b_weighted_risk_gate_passed",
    "audit_auto_keep_count",
    "audit_manual_review_count",
    "audit_auto_exclude_count",
    "audit_auto_keep_adverse_count",
    "audit_auto_exclude_adverse_count",
    "audit_auto_keep_raw_risk",
    "audit_auto_exclude_raw_risk",
    "audit_auto_keep_cp95_upper",
    "audit_auto_exclude_cp95_upper",
    "audit_keep_tail_nested_in_original_sample",
    "audit_exclude_tail_nested_in_original_sample",
    "audit_both_tails_supported",
    "audit_descriptive_risk_gate_passed",
    "combined_exploratory_eligible",
    "is_recommended",
)


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
    """计算字节串SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算文件SHA-256并封装稳定失败语义。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """以同目录临时文件原子发布结果，避免半成品被误读。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_artifact_write_failed"
        ) from exc


def _load_json(path: Path) -> Mapping[str, Any]:
    """读取公开manifest并拒绝非对象JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_manifest_invalid"
        ) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_manifest_invalid"
        )
    return value


def _risk(
    mask: np.ndarray,
    adverse: np.ndarray,
    weights: np.ndarray | None = None,
) -> float:
    """计算一个尾部的原始或加权不利事件率。"""

    count = int(np.sum(mask))
    if count == 0:
        return float("nan")
    if weights is None:
        return float(np.mean(adverse[mask]))
    denominator = float(np.sum(weights[mask]))
    if denominator <= 0:
        return float("nan")
    return float(np.sum(weights[mask & adverse]) / denominator)


def _weighted_rate(mask: np.ndarray, weights: np.ndarray) -> float:
    """计算Wave B设计权重下某动作的人口比例。"""

    denominator = float(np.sum(weights))
    if denominator <= 0:
        return float("nan")
    return float(np.sum(weights[mask]) / denominator)


def _cp_upper(events: int, count: int, confidence_level: float) -> float:
    """计算原始二项率的单侧Clopper–Pearson上界。"""

    if count < 1:
        return float("nan")
    if events == count:
        return 1.0
    return float(beta.ppf(confidence_level, events + 1, count - events))


def _finite_or_none(value: float) -> float | None:
    """把无支持时的NaN转为可规范JSON表达的空值。"""

    return float(value) if np.isfinite(value) else None


def evaluate_threshold_grid(
    population_probabilities: Sequence[float],
    wave_b_rows: Sequence[RetrainingOofObservation],
    audit_rows: Sequence[AuditThresholdObservation],
    *,
    keep_thresholds: Sequence[float],
    exclude_thresholds: Sequence[float],
    original_T_keep: float,
    original_T_exclude: float,
    minimum_raw_tail_count: int,
    maximum_auto_keep_unrelated_rate: float,
    maximum_auto_exclude_related_rate: float,
    confidence_level: float,
) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
    """计算所有阈值组的人口数量、Wave B风险和300条审计诊断。

    Args:
        population_probabilities: 既有纯推理对完整未标注人口保存的概率。
        wave_b_rows: 唯一候选在Wave B成员上的分组折外概率和设计权重。
        audit_rows: 最后300条人工标签及其既有概率。
        keep_thresholds: 要枚举的自动保留上界。
        exclude_thresholds: 要枚举的自动排除下界。
        original_T_keep: 生成300条尾部样本时使用的保留阈值。
        original_T_exclude: 生成300条尾部样本时使用的排除阈值。
        minimum_raw_tail_count: Wave B每个自动尾部的最低原始支持。
        maximum_auto_keep_unrelated_rate: 保留端探索性风险参照。
        maximum_auto_exclude_related_rate: 排除端探索性风险参照。
        confidence_level: 单侧Clopper–Pearson置信水平。

    Returns:
        全阈值点与按预声明规则产生的非冻结推荐摘要。

    Raises:
        ModelRetrainingThresholdGridError: 输入数量、概率、标签或阈值非法。

    Notes:
        300条来自原始自动尾部。只有新保留尾是原保留尾子集且新排除尾是原
        排除尾子集时，两个尾部的审计诊断才同时具有原抽样范围支持。推荐仅是
        事后探索，不自动创建policy或部署授权。
    """

    population = np.asarray(population_probabilities, dtype=float)
    if (
        population.size < 1
        or not np.all(np.isfinite(population))
        or np.any((population < 0) | (population > 1))
        or len(wave_b_rows) != 360
        or len(audit_rows) != 300
    ):
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_input_invalid"
        )
    wave_probabilities = np.asarray(
        [item.p_unrelated for item in wave_b_rows], dtype=float
    )
    wave_labels = np.asarray(
        [item.tourism_label for item in wave_b_rows], dtype=object
    )
    wave_weights = np.asarray(
        [item.analysis_weight for item in wave_b_rows], dtype=float
    )
    audit_probabilities = np.asarray(
        [item.p_unrelated for item in audit_rows], dtype=float
    )
    audit_labels = np.asarray(
        [item.tourism_label for item in audit_rows], dtype=object
    )
    if (
        not np.all(np.isfinite(wave_probabilities))
        or not np.all(np.isfinite(wave_weights))
        or np.any(wave_weights <= 0)
        or not np.all(np.isfinite(audit_probabilities))
        or not set(wave_labels).issubset({"related", "unrelated", "uncertain"})
        or not set(audit_labels).issubset({"related", "unrelated", "uncertain"})
    ):
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_evidence_invalid"
        )
    wave_keep_adverse = np.isin(wave_labels, ["unrelated", "uncertain"])
    wave_exclude_adverse = np.isin(wave_labels, ["related", "uncertain"])
    audit_keep_adverse = np.isin(audit_labels, ["unrelated", "uncertain"])
    audit_exclude_adverse = np.isin(audit_labels, ["related", "uncertain"])
    points: list[dict[str, Any]] = []
    for T_keep in keep_thresholds:
        for T_exclude in exclude_thresholds:
            if not (0 <= T_keep < T_exclude <= 1):
                raise ModelRetrainingThresholdGridError(
                    "model_retraining_threshold_grid_threshold_invalid"
                )
            population_keep = population <= T_keep
            population_exclude = population >= T_exclude
            population_manual = ~(population_keep | population_exclude)
            wave_keep = wave_probabilities <= T_keep
            wave_exclude = wave_probabilities >= T_exclude
            wave_manual = ~(wave_keep | wave_exclude)
            audit_keep = audit_probabilities <= T_keep
            audit_exclude = audit_probabilities >= T_exclude
            audit_manual = ~(audit_keep | audit_exclude)
            wave_keep_count = int(np.sum(wave_keep))
            wave_exclude_count = int(np.sum(wave_exclude))
            audit_keep_count = int(np.sum(audit_keep))
            audit_exclude_count = int(np.sum(audit_exclude))
            wave_keep_events = int(np.sum(wave_keep & wave_keep_adverse))
            wave_exclude_events = int(
                np.sum(wave_exclude & wave_exclude_adverse)
            )
            audit_keep_events = int(np.sum(audit_keep & audit_keep_adverse))
            audit_exclude_events = int(
                np.sum(audit_exclude & audit_exclude_adverse)
            )
            wave_keep_raw = _risk(wave_keep, wave_keep_adverse)
            wave_exclude_raw = _risk(wave_exclude, wave_exclude_adverse)
            wave_keep_weighted = _risk(
                wave_keep, wave_keep_adverse, wave_weights
            )
            wave_exclude_weighted = _risk(
                wave_exclude, wave_exclude_adverse, wave_weights
            )
            audit_keep_raw = _risk(audit_keep, audit_keep_adverse)
            audit_exclude_raw = _risk(audit_exclude, audit_exclude_adverse)
            minimum_support = (
                wave_keep_count >= minimum_raw_tail_count
                and wave_exclude_count >= minimum_raw_tail_count
            )
            raw_gate = (
                minimum_support
                and wave_keep_raw <= maximum_auto_keep_unrelated_rate
                and wave_exclude_raw <= maximum_auto_exclude_related_rate
            )
            weighted_gate = (
                minimum_support
                and wave_keep_weighted <= maximum_auto_keep_unrelated_rate
                and wave_exclude_weighted
                <= maximum_auto_exclude_related_rate
            )
            keep_nested = T_keep <= original_T_keep
            exclude_nested = T_exclude >= original_T_exclude
            audit_supported = (
                keep_nested
                and exclude_nested
                and audit_keep_count >= minimum_raw_tail_count
                and audit_exclude_count >= minimum_raw_tail_count
            )
            audit_gate = (
                audit_supported
                and audit_keep_raw <= maximum_auto_keep_unrelated_rate
                and audit_exclude_raw
                <= maximum_auto_exclude_related_rate
            )
            population_count = int(population.size)
            point = {
                "T_keep": float(T_keep),
                "T_exclude": float(T_exclude),
                "population_auto_keep_count": int(np.sum(population_keep)),
                "population_manual_review_count": int(np.sum(population_manual)),
                "population_auto_exclude_count": int(np.sum(population_exclude)),
                "population_auto_keep_rate": float(
                    np.mean(population_keep)
                ),
                "population_manual_review_rate": float(
                    np.mean(population_manual)
                ),
                "population_auto_exclude_rate": float(
                    np.mean(population_exclude)
                ),
                "wave_b_auto_keep_count": wave_keep_count,
                "wave_b_manual_review_count": int(np.sum(wave_manual)),
                "wave_b_auto_exclude_count": wave_exclude_count,
                "wave_b_auto_keep_adverse_count": wave_keep_events,
                "wave_b_auto_exclude_adverse_count": wave_exclude_events,
                "wave_b_auto_keep_raw_risk": _finite_or_none(wave_keep_raw),
                "wave_b_auto_exclude_raw_risk": _finite_or_none(
                    wave_exclude_raw
                ),
                "wave_b_auto_keep_weighted_risk": _finite_or_none(
                    wave_keep_weighted
                ),
                "wave_b_auto_exclude_weighted_risk": _finite_or_none(
                    wave_exclude_weighted
                ),
                "wave_b_weighted_manual_rate": _weighted_rate(
                    wave_manual, wave_weights
                ),
                "wave_b_auto_keep_cp95_upper": _finite_or_none(
                    _cp_upper(
                        wave_keep_events, wave_keep_count, confidence_level
                    )
                ),
                "wave_b_auto_exclude_cp95_upper": _finite_or_none(
                    _cp_upper(
                        wave_exclude_events,
                        wave_exclude_count,
                        confidence_level,
                    )
                ),
                "wave_b_minimum_support_passed": minimum_support,
                "wave_b_raw_risk_gate_passed": raw_gate,
                "wave_b_weighted_risk_gate_passed": weighted_gate,
                "audit_auto_keep_count": audit_keep_count,
                "audit_manual_review_count": int(np.sum(audit_manual)),
                "audit_auto_exclude_count": audit_exclude_count,
                "audit_auto_keep_adverse_count": audit_keep_events,
                "audit_auto_exclude_adverse_count": audit_exclude_events,
                "audit_auto_keep_raw_risk": _finite_or_none(audit_keep_raw),
                "audit_auto_exclude_raw_risk": _finite_or_none(
                    audit_exclude_raw
                ),
                "audit_auto_keep_cp95_upper": _finite_or_none(
                    _cp_upper(
                        audit_keep_events, audit_keep_count, confidence_level
                    )
                ),
                "audit_auto_exclude_cp95_upper": _finite_or_none(
                    _cp_upper(
                        audit_exclude_events,
                        audit_exclude_count,
                        confidence_level,
                    )
                ),
                "audit_keep_tail_nested_in_original_sample": keep_nested,
                "audit_exclude_tail_nested_in_original_sample": exclude_nested,
                "audit_both_tails_supported": audit_supported,
                "audit_descriptive_risk_gate_passed": audit_gate,
                "combined_exploratory_eligible": bool(
                    raw_gate and weighted_gate and audit_gate
                ),
                "is_recommended": False,
            }
            if sum(
                point[key]
                for key in (
                    "population_auto_keep_count",
                    "population_manual_review_count",
                    "population_auto_exclude_count",
                )
            ) != population_count:
                raise ModelRetrainingThresholdGridError(
                    "model_retraining_threshold_grid_population_partition_invalid"
                )
            points.append(point)
    eligible = [item for item in points if item["combined_exploratory_eligible"]]
    if not eligible:
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_no_supported_recommendation"
        )
    recommendation = min(
        eligible,
        key=lambda item: (
            item["population_manual_review_rate"],
            item["wave_b_auto_exclude_raw_risk"],
            item["wave_b_auto_keep_raw_risk"],
            item["audit_auto_exclude_raw_risk"],
            item["audit_auto_keep_raw_risk"],
            item["T_keep"],
            item["T_exclude"],
        ),
    )
    recommendation["is_recommended"] = True
    summary = {
        "artifact_kind": "formal-cleaning-threshold-grid-summary",
        "status": "POST_HOC_THRESHOLD_GRID_COMPLETE",
        "point_count": len(points),
        "eligible_point_count": len(eligible),
        "recommendation": recommendation,
        "recommendation_is_policy_freeze": False,
        "automatic_routing_authorized": False,
        "audit_evidence_role": "post_hoc_threshold_diagnostic_only",
        "audit_sampling_support_rule": (
            "T_keep<=original_T_keep_and_T_exclude>=original_T_exclude"
        ),
        "fit_call_count": 0,
        "predict_call_count": 0,
    }
    return tuple(points), summary


def _csv_bytes(points: Sequence[Mapping[str, Any]]) -> bytes:
    """把全网格导出为UTF-8 BOM、固定列序CSV。"""

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(GRID_COLUMNS),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(points)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> ThresholdGridExplorationResult:
    """从已验证manifest生成公开摘要。"""

    recommendation = manifest["recommendation"]
    return ThresholdGridExplorationResult(
        exploration_id=str(manifest["exploration_id"]),
        point_count=int(manifest["point_count"]),
        recommended_T_keep=float(recommendation["T_keep"]),
        recommended_T_exclude=float(recommendation["T_exclude"]),
        recommended_population_manual_count=int(
            recommendation["population_manual_review_count"]
        ),
        recommended_population_manual_rate=float(
            recommendation["population_manual_review_rate"]
        ),
        grid_filename=str(manifest["artifacts"]["grid_csv"]["filename"]),
        grid_sha256=str(manifest["artifacts"]["grid_csv"]["sha256"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_existing(
    directory: Path,
    *,
    expected_manifest_sha256: str,
    plan: ModelRetrainingPlan,
) -> ThresholdGridExplorationResult:
    """严格复用既有阈值网格运行包。"""

    manifest_path = directory / "threshold-grid-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_manifest_hash_mismatch"
        )
    manifest = _load_json(manifest_path)
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-threshold-grid-exploration"
        or manifest.get("status") != "POST_HOC_THRESHOLD_GRID_COMPLETE"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("point_count")
        != len(plan.keep_thresholds) * len(plan.exclude_thresholds)
        or manifest.get("fit_call_count") != 0
        or manifest.get("predict_call_count") != 0
        or manifest.get("policy_frozen") is not False
    ):
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_manifest_invalid"
        )
    for key, filename in {
        "grid_json": "threshold-grid.json",
        "grid_csv": "threshold-grid.csv",
        "summary": "threshold-grid-summary.json",
    }.items():
        details = manifest.get("artifacts", {}).get(key, {})
        if (
            details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingThresholdGridError(
                "model_retraining_threshold_grid_artifact_hash_mismatch"
            )
    return _result(manifest, manifest_path.read_bytes(), reused=True)


def explore_threshold_grid_package(
    inference_package: str | Path,
    training_package: str | Path,
    audit_assessment_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_inference_manifest_sha256: str,
    expected_training_manifest_sha256: str,
    expected_audit_manifest_sha256: str,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
) -> ThresholdGridExplorationResult:
    """复用既有概率封存完整阈值网格，不执行fit或predict。

    Args:
        inference_package: 12,558条既有概率运行包。
        training_package: 三候选训练包，用于读取Wave B分组OOF概率。
        audit_assessment_package: 最后300条人工标签判读包。
        artifact_root: 新阈值探索运行包根目录。
        plan: 已冻结的重训计划与阈值网格。
        expected_inference_manifest_sha256: 推理manifest外部摘要。
        expected_training_manifest_sha256: 训练manifest外部摘要。
        expected_audit_manifest_sha256: 审计判读manifest外部摘要。
        code_version: 当前40位Git提交身份。
        expected_existing_manifest_sha256: 严格复用既有运行时的摘要。

    Returns:
        完整网格运行包公开摘要。

    Raises:
        ModelRetrainingThresholdGridError: 输入不兼容、连接不完整或输出冲突。
    """

    if len(code_version) != 40:
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_code_version_invalid"
        )
    inference, inference_manifest = load_inference_scoring_package(
        inference_package,
        plan=plan,
        expected_manifest_sha256=expected_inference_manifest_sha256,
    )
    _, oof, training_manifest = load_retraining_training_package(
        training_package,
        plan=plan,
        expected_manifest_sha256=expected_training_manifest_sha256,
    )
    audit_labels, audit_manifest = load_audit_assessment_package(
        audit_assessment_package,
        plan=plan,
        expected_manifest_sha256=expected_audit_manifest_sha256,
    )
    candidate_id = str(inference_manifest["candidate_id"])
    wave_b = tuple(
        item
        for item in oof
        if item.candidate_id == candidate_id and item.evidence_origin == "wave_b"
    )
    by_member = {item.member_key: item for item in inference}
    audit_rows: list[AuditThresholdObservation] = []
    seen: set[str] = set()
    for label in audit_labels:
        member_key = str(label["member_key"])
        scored = by_member.get(member_key)
        if (
            scored is None
            or member_key in seen
            or str(label["component_id"]) != scored.component_id
        ):
            raise ModelRetrainingThresholdGridError(
                "model_retraining_threshold_grid_audit_binding_invalid"
            )
        seen.add(member_key)
        audit_rows.append(
            AuditThresholdObservation(
                p_unrelated=scored.p_unrelated,
                tourism_label=str(label["tourism_label"]),
                component_id=scored.component_id,
            )
        )
    if len(seen) != 300 or len(wave_b) != 360:
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_evidence_count_invalid"
        )
    points, summary = evaluate_threshold_grid(
        [item.p_unrelated for item in inference],
        wave_b,
        audit_rows,
        keep_thresholds=plan.keep_thresholds,
        exclude_thresholds=plan.exclude_thresholds,
        original_T_keep=float(inference_manifest["T_keep"]),
        original_T_exclude=float(inference_manifest["T_exclude"]),
        minimum_raw_tail_count=plan.minimum_raw_tail_count,
        maximum_auto_keep_unrelated_rate=(
            plan.maximum_auto_keep_unrelated_rate
        ),
        maximum_auto_exclude_related_rate=(
            plan.maximum_auto_exclude_related_rate
        ),
        confidence_level=plan.confidence_level,
    )
    identity = _canonical_bytes(
        {
            "plan_id": plan.plan_id,
            "inference_manifest_sha256": expected_inference_manifest_sha256,
            "training_manifest_sha256": expected_training_manifest_sha256,
            "audit_manifest_sha256": expected_audit_manifest_sha256,
            "candidate_id": candidate_id,
            "grid": {
                "keep": list(plan.keep_thresholds),
                "exclude": list(plan.exclude_thresholds),
            },
            "code_version": code_version,
        }
    )
    exploration_id = _sha256_bytes(identity)[:32]
    directory = Path(artifact_root).expanduser().resolve() / exploration_id
    if directory.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingThresholdGridError(
                "model_retraining_threshold_grid_existing_requires_manifest_hash"
            )
        return _validate_existing(
            directory,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            plan=plan,
        )
    grid_json_bytes = _canonical_bytes(points)
    grid_csv_bytes = _csv_bytes(points)
    summary_bytes = _canonical_bytes(summary)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ModelRetrainingThresholdGridError(
            "model_retraining_threshold_grid_directory_create_failed"
        ) from exc
    _atomic_write(directory / "threshold-grid.json", grid_json_bytes)
    _atomic_write(directory / "threshold-grid.csv", grid_csv_bytes)
    _atomic_write(directory / "threshold-grid-summary.json", summary_bytes)
    recommendation = summary["recommendation"]
    manifest = {
        "artifact_kind": "formal-cleaning-threshold-grid-exploration",
        "status": "POST_HOC_THRESHOLD_GRID_COMPLETE",
        "exploration_id": exploration_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "code_version": code_version,
        "candidate_id": candidate_id,
        "candidate_name": str(inference_manifest["candidate_name"]),
        "inference_id": str(inference_manifest["inference_id"]),
        "inference_manifest_sha256": expected_inference_manifest_sha256,
        "training_run_id": str(training_manifest["training_run_id"]),
        "training_manifest_sha256": expected_training_manifest_sha256,
        "assessment_id": str(audit_manifest["assessment_id"]),
        "audit_manifest_sha256": expected_audit_manifest_sha256,
        "original_T_keep": float(inference_manifest["T_keep"]),
        "original_T_exclude": float(inference_manifest["T_exclude"]),
        "population_count": len(inference),
        "wave_b_count": len(wave_b),
        "audit_count": len(audit_rows),
        "point_count": len(points),
        "eligible_point_count": int(summary["eligible_point_count"]),
        "recommendation": recommendation,
        "recommendation_is_policy_freeze": False,
        "policy_frozen": False,
        "automatic_routing_authorized": False,
        "audit_evidence_role": "post_hoc_threshold_diagnostic_only",
        "platform_used": False,
        "fit_call_count": 0,
        "predict_call_count": 0,
        "source_database_write_count": 0,
        "artifacts": {
            "grid_json": {
                "filename": "threshold-grid.json",
                "sha256": _sha256_bytes(grid_json_bytes),
            },
            "grid_csv": {
                "filename": "threshold-grid.csv",
                "sha256": _sha256_bytes(grid_csv_bytes),
            },
            "summary": {
                "filename": "threshold-grid-summary.json",
                "sha256": _sha256_bytes(summary_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(directory / "threshold-grid-manifest.json", manifest_bytes)
    return _result(manifest, manifest_bytes, reused=False)


def render_threshold_grid_result(
    result: ThresholdGridExplorationResult, *, output_format: str
) -> str:
    """输出机器JSON或简洁的人类可读探索摘要。"""

    payload = {
        "exploration_id": result.exploration_id,
        "point_count": result.point_count,
        "recommended_T_keep": result.recommended_T_keep,
        "recommended_T_exclude": result.recommended_T_exclude,
        "recommended_population_manual_count": (
            result.recommended_population_manual_count
        ),
        "recommended_population_manual_rate": (
            result.recommended_population_manual_rate
        ),
        "grid_filename": result.grid_filename,
        "grid_sha256": result.grid_sha256,
        "manifest_sha256": result.manifest_sha256,
        "reused": result.reused,
        "status": result.status,
        "fit_call_count": 0,
        "predict_call_count": 0,
        "policy_frozen": False,
    }
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "\n".join(
        [
            "# 双阈值完整事后探索",
            "",
            f"探索ID：{result.exploration_id}",
            f"阈值组：{result.point_count}；artifact："
            f"{'严格复用' if result.reused else '新建并封存'}",
            f"非冻结推荐：T_keep={result.recommended_T_keep:.2f} / "
            f"T_exclude={result.recommended_T_exclude:.2f}",
            "推荐对应全人口人工处理："
            f"{result.recommended_population_manual_count} "
            f"({result.recommended_population_manual_rate:.2%})",
            f"完整CSV：{result.grid_filename}",
            "fit调用：0；predict调用：0；没有创建新policy或部署授权。",
            "最后300条已转为事后阈值诊断，不能同时充当新策略独立放行证据。",
            f"状态：{result.status}",
        ]
    )
