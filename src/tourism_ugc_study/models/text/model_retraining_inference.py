"""未标注完整人口的只读构建、纯预测三段路由与checkpoint。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from tourism_ugc_study.cleaning.reference_projection import (
    ReferenceProjectionError,
    load_reference_text_projection,
)
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_embeddings import COMPLETE_CHUNK_ENCODING_ALGORITHM_ID
from .model_retraining_routing_artifacts import (
    load_retraining_routing_policy_package,
)
from .model_retraining_snapshot import RetrainingDocument
from .model_retraining_snapshot_artifacts import load_retraining_snapshot_package
from .qwen_complete_chunk_runtime import LocalQwenCompleteChunkEncoder


class ModelRetrainingInferenceError(RuntimeError):
    """全量人口、纯预测、checkpoint或三段动作失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、身份、概率或路径的稳定失败码。"""

        super().__init__("formal model retraining inference failed")
        self.reason_code = reason_code


POPULATION_INFERENCE_ALGORITHM_ID = (
    "complete-population-pure-predict-checkpoint-v1"
)


@dataclass(frozen=True)
class RetrainingInferenceMember:
    """一个尚无人工标签且需新模型覆盖的私有人口成员。"""

    member_key: str
    source_post_id: int
    source_version: int
    component_id: str
    normalized_model_text: str
    normalized_sha256: str

    @property
    def identity(self) -> tuple[int, int]:
        """返回候选人口的稳定帖子身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class ScoredRoutingMember:
    """唯一冻结策略对一条未标注记录的纯预测及临时动作。"""

    member_key: str
    source_post_id: int
    source_version: int
    component_id: str
    normalized_model_text: str
    normalized_sha256: str
    p_unrelated: float
    provisional_action: str

    @property
    def identity(self) -> tuple[int, int]:
        """返回稳定帖子身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class InferencePopulation:
    """精确排除1,300条人工成员后的完整推理人口。"""

    members: tuple[RetrainingInferenceMember, ...]
    candidate_count: int
    training_member_count: int
    inference_count: int
    component_count: int
    projection_member_sha256: str
    population_binding_sha256: str
    leakage_output_sha256: str


@dataclass(frozen=True)
class InferenceScoringResult:
    """已完成纯预测的人口、动作数量和运行身份。"""

    inference_id: str
    policy_id: str
    candidate_name: str
    count: int
    action_counts: Mapping[str, int]
    fit_call_count: int
    resumed_record_count: int
    manifest_sha256: str
    records_sha256: str
    reused: bool
    status: str
    audit_status: str


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    """以URI只读和query-only模式打开派生数据库。"""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_database_readonly_open_failed"
        ) from exc


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
    """流式计算运行包文件摘要。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同目录原子发布checkpoint或最终文件。"""

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
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingInferenceError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingInferenceError(reason_code)
    return value


def _member_key(
    plan: ModelRetrainingPlan, source_post_id: int, source_version: int
) -> str:
    """生成推理人口内稳定、去标识的成员键。"""

    return hashlib.sha256(
        (
            f"{plan.plan_id}|inference-member|{source_post_id}|{source_version}"
        ).encode("utf-8")
    ).hexdigest()[:24]


def build_unlabeled_inference_population(
    derived_db: str | Path,
    training_documents: Sequence[RetrainingDocument],
    *,
    plan: ModelRetrainingPlan,
    normalization_config: TextCleaningConfig,
) -> InferencePopulation:
    """只读重建13,858条候选并精确跳过1,300条人工训练成员。"""

    training_identities = {item.identity for item in training_documents}
    if len(training_identities) != plan.expected_training_count:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_training_members_invalid"
        )
    connection = _readonly_connection(derived_db)
    try:
        build = connection.execute(
            """
            SELECT candidate_build_id, input_post_count, component_count,
                   output_sha256, seal_status
            FROM text_leakage_builds WHERE leakage_build_id = ?
            """,
            (plan.leakage_build_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT source_post_id, source_version, component_id
            FROM text_leakage_members
            WHERE leakage_build_id = ?
            ORDER BY source_post_id, source_version
            """,
            (plan.leakage_build_id,),
        ).fetchall()
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    except sqlite3.Error as exc:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_leakage_contract_invalid"
        ) from exc
    finally:
        connection.close()
    if (
        build is None
        or str(build["candidate_build_id"]) != plan.candidate_build_id
        or str(build["seal_status"]) != "finalized"
        or int(build["input_post_count"]) != 13858
        or len(rows) != 13858
        or query_only != 1
    ):
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_leakage_contract_invalid"
        )
    try:
        component_by_identity = {
            (int(row["source_post_id"]), int(row["source_version"])): str(
                row["component_id"]
            )
            for row in rows
        }
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_leakage_members_invalid"
        ) from exc
    if (
        len(component_by_identity) != len(rows)
        or not training_identities.issubset(component_by_identity)
    ):
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_leakage_members_invalid"
        )
    try:
        projection = load_reference_text_projection(
            derived_db,
            candidate_build_id=plan.candidate_build_id,
            config=normalization_config,
        )
    except ReferenceProjectionError as exc:
        raise ModelRetrainingInferenceError(exc.reason_code) from exc
    if set(projection.by_identity) != set(component_by_identity):
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_projection_membership_mismatch"
        )
    members: list[RetrainingInferenceMember] = []
    for identity in sorted(component_by_identity):
        if identity in training_identities:
            continue
        projected = projection.by_identity[identity]
        if projected.structure_status != "usable":
            raise ModelRetrainingInferenceError(
                "model_retraining_inference_structure_invalid"
            )
        members.append(
            RetrainingInferenceMember(
                member_key=_member_key(plan, *identity),
                source_post_id=identity[0],
                source_version=identity[1],
                component_id=component_by_identity[identity],
                normalized_model_text=projected.normalized_model_text,
                normalized_sha256=projected.normalized_sha256,
            )
        )
    expected = len(component_by_identity) - len(training_identities)
    if (
        expected != 12558
        or len(members) != expected
        or len({item.member_key for item in members}) != len(members)
        or len({item.identity for item in members}) != len(members)
    ):
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_population_count_mismatch"
        )
    binding = [
        {
            "member_key": item.member_key,
            "component_id": item.component_id,
            "normalized_sha256": item.normalized_sha256,
        }
        for item in members
    ]
    return InferencePopulation(
        members=tuple(members),
        candidate_count=len(component_by_identity),
        training_member_count=len(training_identities),
        inference_count=len(members),
        component_count=len({item.component_id for item in members}),
        projection_member_sha256=projection.member_sha256,
        population_binding_sha256=hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        leakage_output_sha256=str(build["output_sha256"]),
    )


def _action(probability: float, *, T_keep: float, T_exclude: float) -> str:
    """把有限概率完整路由到三个互斥动作之一。"""

    if not np.isfinite(probability) or not 0 <= probability <= 1:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_probability_invalid"
        )
    if probability <= T_keep:
        return "auto_keep"
    if probability >= T_exclude:
        return "auto_exclude"
    return "manual_review"


def _receipt_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> InferenceScoringResult:
    """从已验证manifest生成公开摘要。"""

    return InferenceScoringResult(
        inference_id=str(manifest["inference_id"]),
        policy_id=str(manifest["policy_id"]),
        candidate_name=str(manifest["candidate_name"]),
        count=int(manifest["count"]),
        action_counts=dict(manifest["action_counts"]),
        fit_call_count=int(manifest["fit_call_count"]),
        resumed_record_count=int(manifest["resumed_record_count"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        records_sha256=str(manifest["artifacts"]["records"]["sha256"]),
        reused=reused,
        status=str(manifest["status"]),
        audit_status=str(manifest["audit_status"]),
    )


def _validate_existing(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> InferenceScoringResult:
    """严格验证完成的人口评分包。"""

    manifest_path = directory / "inference-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_inference_manifest_invalid"
    )
    records = manifest.get("artifacts", {}).get("records", {})
    population = manifest.get("artifacts", {}).get("population", {})
    action_counts = manifest.get("action_counts", {})
    audit_status = manifest.get("audit_status")
    automatic_authorized = manifest.get("automatic_routing_authorized")
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-population-scoring"
        or manifest.get("status") != "UNLABELED_POPULATION_SCORED"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("algorithm_id") != POPULATION_INFERENCE_ALGORITHM_ID
        or manifest.get("count") != 12558
        or manifest.get("fit_call_count") != 0
        or manifest.get("source_database_write_count") != 0
        or audit_status
        not in {
            "PENDING_NEW_BLIND_AUDIT",
            "HISTORICAL_AUDIT_POST_HOC_ONLY",
        }
        or not isinstance(automatic_authorized, bool)
        or automatic_authorized
        != (audit_status == "HISTORICAL_AUDIT_POST_HOC_ONLY")
        or not isinstance(action_counts, Mapping)
        or sum(int(value) for value in action_counts.values()) != 12558
        or records.get("filename") != "scored-records.json"
        or _file_sha256(directory / records["filename"]) != records.get("sha256")
        or population.get("filename") != "population-summary.json"
        or _file_sha256(directory / population["filename"])
        != population.get("sha256")
    ):
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_manifest_invalid"
        )
    return _receipt_result(manifest, manifest_path.read_bytes(), reused=True)


def score_unlabeled_population_package(
    derived_db: str | Path,
    snapshot_package: str | Path,
    policy_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    normalization_config: TextCleaningConfig,
    expected_snapshot_manifest_sha256: str,
    expected_policy_manifest_sha256: str,
    code_version: str,
    encoder: LocalQwenCompleteChunkEncoder | None,
    expected_existing_manifest_sha256: str | None = None,
) -> InferenceScoringResult:
    """对尚未标注的完整人口执行可恢复且绝不fit的纯预测。"""

    snapshot, snapshot_manifest = load_retraining_snapshot_package(
        snapshot_package,
        plan=plan,
        expected_manifest_sha256=expected_snapshot_manifest_sha256,
    )
    model, policy_manifest = load_retraining_routing_policy_package(
        policy_package,
        plan=plan,
        expected_manifest_sha256=expected_policy_manifest_sha256,
    )
    if len(code_version) != 40:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_code_version_invalid"
        )
    population = build_unlabeled_inference_population(
        derived_db,
        snapshot.documents,
        plan=plan,
        normalization_config=normalization_config,
    )
    if model.candidate_name in {"qwen_linear_svc", "logit_fusion"} and encoder is None:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_encoder_required"
        )
    selected = policy_manifest["selected"]
    T_keep = float(selected["T_keep"])
    T_exclude = float(selected["T_exclude"])
    inference_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{policy_manifest['policy_id']}|"
            f"{population.population_binding_sha256}|"
            f"{POPULATION_INFERENCE_ALGORITHM_ID}|{code_version}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / inference_id
    final_manifest = directory / "inference-manifest.json"
    if final_manifest.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingInferenceError(
                "model_retraining_inference_existing_requires_manifest_hash"
            )
        return _validate_existing(
            directory,
            plan=plan,
            expected_manifest_sha256=expected_existing_manifest_sha256,
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        checkpoint = directory / "checkpoint-records"
        checkpoint.mkdir(exist_ok=True)
    except OSError as exc:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_directory_create_failed"
        ) from exc
    state_path = directory / "checkpoint-state.json"
    if state_path.exists():
        state = dict(
            _load_json(
                state_path, "model_retraining_inference_checkpoint_invalid"
            )
        )
        if (
            state.get("inference_id") != inference_id
            or state.get("policy_manifest_sha256")
            != expected_policy_manifest_sha256
            or state.get("population_binding_sha256")
            != population.population_binding_sha256
            or state.get("algorithm_id") != POPULATION_INFERENCE_ALGORITHM_ID
            or state.get("code_version") != code_version
            or not isinstance(state.get("completed_count"), int)
            or not isinstance(state.get("rolling_sha256"), str)
        ):
            raise ModelRetrainingInferenceError(
                "model_retraining_inference_checkpoint_invalid"
            )
    else:
        state = {
            "artifact_kind": "formal-cleaning-model-retraining-inference-checkpoint",
            "inference_id": inference_id,
            "policy_manifest_sha256": expected_policy_manifest_sha256,
            "population_binding_sha256": population.population_binding_sha256,
            "algorithm_id": POPULATION_INFERENCE_ALGORITHM_ID,
            "code_version": code_version,
            "completed_count": 0,
            "rolling_sha256": "0" * 64,
        }
        _atomic_write(state_path, _canonical_bytes(state))
    completed_count = int(state["completed_count"])
    if completed_count < 0 or completed_count > population.inference_count:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_checkpoint_invalid"
        )
    records: list[ScoredRoutingMember] = []
    rolling = "0" * 64
    for index in range(completed_count):
        member = population.members[index]
        path = checkpoint / f"{index:05d}-{member.member_key}.json"
        receipt = _load_json(
            path, "model_retraining_inference_checkpoint_invalid"
        )
        try:
            record = ScoredRoutingMember(**receipt)
        except TypeError as exc:
            raise ModelRetrainingInferenceError(
                "model_retraining_inference_checkpoint_invalid"
            ) from exc
        if (
            record.member_key != member.member_key
            or record.identity != member.identity
            or record.normalized_sha256 != member.normalized_sha256
            or record.component_id != member.component_id
            or hashlib.sha256(
                record.normalized_model_text.encode("utf-8")
            ).hexdigest()
            != member.normalized_sha256
            or record.provisional_action
            != _action(record.p_unrelated, T_keep=T_keep, T_exclude=T_exclude)
        ):
            raise ModelRetrainingInferenceError(
                "model_retraining_inference_checkpoint_invalid"
            )
        receipt_sha = _file_sha256(path)
        rolling = hashlib.sha256(f"{rolling}|{receipt_sha}".encode()).hexdigest()
        records.append(record)
    if rolling != state["rolling_sha256"]:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_checkpoint_invalid"
        )
    for index in range(completed_count, population.inference_count):
        member = population.members[index]
        embeddings: np.ndarray | None = None
        if encoder is not None and model.candidate_name in {
            "qwen_linear_svc",
            "logit_fusion",
        }:
            encoded = encoder.encode_document(member.normalized_model_text)
            if encoded.diagnostics.omitted_token_count != 0:
                raise ModelRetrainingInferenceError(
                    "model_retraining_inference_omitted_tokens_nonzero"
                )
            embeddings = encoded.embedding[np.newaxis, :]
        try:
            probability = float(
                model.predict_p_unrelated(
                    [member.normalized_model_text], embeddings
                )[0]
            )
        except (IndexError, ValueError) as exc:
            raise ModelRetrainingInferenceError(
                "model_retraining_inference_prediction_failed"
            ) from exc
        record = ScoredRoutingMember(
            member_key=member.member_key,
            source_post_id=member.source_post_id,
            source_version=member.source_version,
            component_id=member.component_id,
            normalized_model_text=member.normalized_model_text,
            normalized_sha256=member.normalized_sha256,
            p_unrelated=probability,
            provisional_action=_action(
                probability, T_keep=T_keep, T_exclude=T_exclude
            ),
        )
        payload = _canonical_bytes(asdict(record))
        path = checkpoint / f"{index:05d}-{member.member_key}.json"
        _atomic_write(path, payload)
        receipt_sha = _sha256_bytes(payload)
        rolling = hashlib.sha256(f"{rolling}|{receipt_sha}".encode()).hexdigest()
        records.append(record)
        state["completed_count"] = index + 1
        state["rolling_sha256"] = rolling
        _atomic_write(state_path, _canonical_bytes(state))
    if (
        len(records) != population.inference_count
        or len({item.identity for item in records}) != len(records)
        or any(
            item.provisional_action
            not in {"auto_keep", "manual_review", "auto_exclude"}
            for item in records
        )
    ):
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_output_invalid"
        )
    records_bytes = _canonical_bytes([asdict(item) for item in records])
    _atomic_write(directory / "scored-records.json", records_bytes)
    action_counts = dict(
        sorted(Counter(item.provisional_action for item in records).items())
    )
    population_summary_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-model-retraining-inference-population",
            "candidate_count": population.candidate_count,
            "training_member_count": population.training_member_count,
            "inference_count": population.inference_count,
            "component_count": population.component_count,
            "projection_member_sha256": population.projection_member_sha256,
            "population_binding_sha256": population.population_binding_sha256,
            "leakage_output_sha256": population.leakage_output_sha256,
        }
    )
    _atomic_write(directory / "population-summary.json", population_summary_bytes)
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-population-scoring",
        "status": "UNLABELED_POPULATION_SCORED",
        "inference_id": inference_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "algorithm_id": POPULATION_INFERENCE_ALGORITHM_ID,
        "qwen_projection_algorithm_id": (
            COMPLETE_CHUNK_ENCODING_ALGORITHM_ID
            if model.candidate_name in {"qwen_linear_svc", "logit_fusion"}
            else None
        ),
        "code_version": code_version,
        "policy_id": str(policy_manifest["policy_id"]),
        "policy_manifest_sha256": expected_policy_manifest_sha256,
        "snapshot_id": str(snapshot_manifest["snapshot_id"]),
        "snapshot_manifest_sha256": expected_snapshot_manifest_sha256,
        "candidate_name": model.candidate_name,
        "candidate_id": model.candidate_id,
        "T_keep": T_keep,
        "T_exclude": T_exclude,
        "count": len(records),
        "training_member_count_skipped": snapshot.count,
        "action_counts": action_counts,
        "fit_call_count": 0,
        "resumed_record_count": completed_count,
        "platform_used": False,
        "source_database_write_count": 0,
        "historical_test_reopened": False,
        "automatic_routing_authorized": bool(
            policy_manifest["automatic_routing_authorized"]
        ),
        "audit_status": str(policy_manifest["audit_status"]),
        "acceptance_basis": policy_manifest.get("acceptance_basis"),
        "artifacts": {
            "records": {
                "filename": "scored-records.json",
                "sha256": _sha256_bytes(records_bytes),
            },
            "population": {
                "filename": "population-summary.json",
                "sha256": _sha256_bytes(population_summary_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(final_manifest, manifest_bytes)
    return _receipt_result(manifest, manifest_bytes, reused=False)


def load_inference_scoring_package(
    package: str | Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> tuple[tuple[ScoredRoutingMember, ...], Mapping[str, Any]]:
    """严格读取人口评分供盲审抽样和最终派生决定。"""

    try:
        directory = Path(package).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_package_unavailable"
        ) from exc
    _validate_existing(
        directory,
        plan=plan,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    manifest = _load_json(
        directory / "inference-manifest.json",
        "model_retraining_inference_manifest_invalid",
    )
    try:
        raw = json.loads(
            (directory / "scored-records.json").read_text(encoding="utf-8")
        )
        records = tuple(ScoredRoutingMember(**item) for item in raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_records_invalid"
        ) from exc
    if (
        len(records) != 12558
        or len({item.identity for item in records}) != len(records)
    ):
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_records_invalid"
        )
    return records, manifest


def render_inference_scoring_result(
    result: InferenceScoringResult, *, output_format: str
) -> str:
    """输出机器JSON或人口纯预测可读摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_output_format_invalid"
        )
    return "\n".join(
        [
            "# 未标注完整人口纯预测结果",
            "",
            f"推理ID：{result.inference_id}",
            f"策略ID：{result.policy_id}；候选={result.candidate_name}",
            f"人口：{result.count}；artifact={'严格复用' if result.reused else '新建并封存'}",
            (
                "三段动作："
                + " / ".join(
                    f"{key}={value}" for key, value in result.action_counts.items()
                )
            ),
            f"checkpoint恢复：{result.resumed_record_count}条",
            "fit调用：0；源数据库写入：0；1,300条人工成员已跳过。",
            "动作仍为provisional；完成新150+150盲审前不形成正式自动决定。",
            f"状态：{result.status}",
        ]
    )
