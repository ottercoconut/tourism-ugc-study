"""冻结融合模型对未来新增记录执行可恢复、幂等的纯预测。"""

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

from tourism_ugc_study.cleaning.text_config import TextCleaningConfig
from tourism_ugc_study.cleaning.text_normalize import normalize_post_text

from .model_retraining_config import ModelRetrainingPlan
from .model_retraining_delivery_config import ModelRetrainingDeliveryPlan
from .model_retraining_routing_artifacts import (
    load_retraining_routing_policy_package,
)
from .model_retraining_snapshot_artifacts import load_retraining_snapshot_package
from .model_retraining_training import FrozenRetrainingCandidate
from .qwen_complete_chunk_runtime import LocalQwenCompleteChunkEncoder


INCREMENTAL_INFERENCE_ALGORITHM_ID = (
    "frozen-routing-new-batch-pure-predict-checkpoint-v1"
)
INCREMENTAL_INPUT_COLUMNS = (
    "source_post_id",
    "source_version",
    "component_id",
    "title",
    "body",
    "source_status",
)
MANUAL_TASK_COLUMNS = (
    "task_id",
    "sample_run_id",
    "normalized_model_text",
    "tourism_label",
)


class ModelRetrainingIncrementalError(RuntimeError):
    """新增批次契约、预测或封存失败时的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、身份或私有路径的稳定失败码。

        Args:
            reason_code: 供CLI、日志和测试稳定识别的失败原因。
        """

        super().__init__("formal incremental model inference failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class IncrementalInferenceMember:
    """一条通过结构门并按冻结规则规范化的新增记录。

    Attributes:
        member_key: 由计划、源身份和规范正文形成的去标识成员键。
        source_post_id: 私有源帖子稳定身份。
        source_version: 本批次绑定的源帖子版本。
        component_id: 更新后的泄漏分量身份，仅用于谱系与聚类审计。
        normalized_model_text: 与训练完全相同契约的规范模型文本。
        normalized_sha256: 规范模型文本摘要。
    """

    member_key: str
    source_post_id: int
    source_version: int
    component_id: str
    normalized_model_text: str
    normalized_sha256: str

    @property
    def identity(self) -> tuple[int, int]:
        """返回源帖子与版本组成的稳定身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class IncrementalScoredMember:
    """冻结模型对一条新增记录给出的概率与三段动作。"""

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
        """返回源帖子与版本组成的稳定身份。"""

        return self.source_post_id, self.source_version


@dataclass(frozen=True)
class IncrementalInferenceResult:
    """新增批次纯预测运行的公开去敏摘要。"""

    batch_id: str
    policy_id: str
    candidate_name: str
    count: int
    action_counts: Mapping[str, int]
    manual_task_count: int
    fit_call_count: int
    resumed_record_count: int
    manifest_sha256: str
    records_sha256: str
    manual_task_sha256: str
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
    """计算内存字节流SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算文件SHA-256并隐藏本机路径。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """在目标目录内原子发布私有运行文件。"""

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
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_artifact_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层JSON映射并统一失败语义。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRetrainingIncrementalError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelRetrainingIncrementalError(reason_code)
    return value


def _member_key(
    plan: ModelRetrainingPlan,
    *,
    source_post_id: int,
    source_version: int,
    normalized_sha256: str,
) -> str:
    """绑定计划、身份和正文生成稳定成员键。"""

    return hashlib.sha256(
        (
            f"{plan.plan_id}|new-batch-member|{source_post_id}|"
            f"{source_version}|{normalized_sha256}"
        ).encode("utf-8")
    ).hexdigest()[:24]


def load_incremental_input_csv(
    input_csv: str | Path,
    *,
    plan: ModelRetrainingPlan,
    normalization_config: TextCleaningConfig,
) -> tuple[IncrementalInferenceMember, ...]:
    """读取原始新增批次并在程序内执行冻结规范化与结构门。

    Args:
        input_csv: UTF-8或UTF-8 BOM CSV。列必须严格为源身份、泄漏分量、
            标题、正文和上游结构状态；不得包含标签、平台或旧模型概率。
        plan: 已冻结的1,300条重训计划，用于生成稳定成员键。
        normalization_config: 与训练投影一致的文本规范化配置。

    Returns:
        按输入顺序排列、正文已规范化且结构可用的新增记录。

    Raises:
        ModelRetrainingIncrementalError: 表头漂移、身份重复、字段非法、
            结构不可用或正文无法按冻结规则生成时失败关闭。

    Notes:
        ``component_id``不进入模型，只用于后续去重、谱系和聚类审计。
        本函数不读取平台、人工标签、旧概率或抽样字段。
    """

    try:
        resolved = Path(input_csv).expanduser().resolve(strict=True)
        with resolved.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != INCREMENTAL_INPUT_COLUMNS:
                raise ModelRetrainingIncrementalError(
                    "model_retraining_incremental_input_header_invalid"
                )
            rows = list(reader)
    except ModelRetrainingIncrementalError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_input_unreadable"
        ) from exc
    if not rows:
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_input_empty"
        )
    members: list[IncrementalInferenceMember] = []
    identities: set[tuple[int, int]] = set()
    for row in rows:
        try:
            source_post_id = int(row["source_post_id"])
            source_version = int(row["source_version"])
            component_id = str(row["component_id"]).strip()
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_input_identity_invalid"
            ) from exc
        identity = (source_post_id, source_version)
        if (
            source_post_id <= 0
            or source_version <= 0
            or not component_id
            or identity in identities
        ):
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_input_identity_invalid"
            )
        normalized = normalize_post_text(
            row.get("title", ""),
            row.get("body", ""),
            source_status=row.get("source_status", ""),
            config=normalization_config,
        )
        if normalized.structure_status != "usable":
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_structure_not_usable"
            )
        identities.add(identity)
        members.append(
            IncrementalInferenceMember(
                member_key=_member_key(
                    plan,
                    source_post_id=source_post_id,
                    source_version=source_version,
                    normalized_sha256=normalized.normalized_sha256,
                ),
                source_post_id=source_post_id,
                source_version=source_version,
                component_id=component_id,
                normalized_model_text=normalized.model_text,
                normalized_sha256=normalized.normalized_sha256,
            )
        )
    return tuple(members)


def _route_probability(
    probability: float, *, T_keep: float, T_exclude: float
) -> str:
    """按冻结双阈值把有限概率分配到唯一动作。"""

    if (
        not np.isfinite(probability)
        or not 0 <= probability <= 1
        or not 0 <= T_keep < T_exclude <= 1
    ):
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_probability_invalid"
        )
    if probability <= T_keep:
        return "auto_keep"
    if probability >= T_exclude:
        return "auto_exclude"
    return "manual_review"


def score_incremental_member(
    member: IncrementalInferenceMember,
    *,
    model: FrozenRetrainingCandidate,
    encoder: LocalQwenCompleteChunkEncoder | None,
    T_keep: float,
    T_exclude: float,
) -> IncrementalScoredMember:
    """对一条已规范化新增记录执行一次纯预测。

    Args:
        member: 已通过新增批次契约的一条记录。
        model: 唯一冻结候选；函数只调用其``predict_p_unrelated``。
        encoder: Qwen或融合候选所需的只读完整分块编码器。
        T_keep: 自动保留闭区间上界。
        T_exclude: 自动排除闭区间下界。

    Returns:
        保留身份、正文摘要、概率与三段动作的私有记录。

    Raises:
        ModelRetrainingIncrementalError: 缺少编码器、token有省略或预测无效。

    Notes:
        本函数没有``fit``路径。完整分块编码的``omitted_token_count``必须为0。
    """

    embeddings: np.ndarray | None = None
    if model.candidate_name in {"qwen_linear_svc", "logit_fusion"}:
        if encoder is None:
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_encoder_required"
            )
        encoded = encoder.encode_document(member.normalized_model_text)
        if encoded.diagnostics.omitted_token_count != 0:
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_omitted_tokens_nonzero"
            )
        embeddings = encoded.embedding[np.newaxis, :]
    try:
        probability = float(
            model.predict_p_unrelated(
                [member.normalized_model_text], embeddings
            )[0]
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_prediction_failed"
        ) from exc
    return IncrementalScoredMember(
        **asdict(member),
        p_unrelated=probability,
        provisional_action=_route_probability(
            probability, T_keep=T_keep, T_exclude=T_exclude
        ),
    )


def _manual_task_bytes(
    records: Sequence[IncrementalScoredMember], *, batch_id: str
) -> tuple[bytes, tuple[Mapping[str, Any], ...]]:
    """生成盲四列表及只留在私有包内的任务映射。"""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(MANUAL_TASK_COLUMNS), lineterminator="\n"
    )
    writer.writeheader()
    private_map: list[Mapping[str, Any]] = []
    for record in records:
        if record.provisional_action != "manual_review":
            continue
        task_id = hashlib.sha256(
            f"{batch_id}|manual-review|{record.member_key}".encode("utf-8")
        ).hexdigest()[:24]
        writer.writerow(
            {
                "task_id": task_id,
                "sample_run_id": batch_id,
                "normalized_model_text": record.normalized_model_text,
                "tourism_label": "",
            }
        )
        private_map.append(
            {
                "task_id": task_id,
                "member_key": record.member_key,
                "source_post_id": record.source_post_id,
                "source_version": record.source_version,
                "component_id": record.component_id,
                "normalized_sha256": record.normalized_sha256,
                "p_unrelated": record.p_unrelated,
                "provisional_action": record.provisional_action,
            }
        )
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), tuple(private_map)


def _result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> IncrementalInferenceResult:
    """从已校验manifest生成公开摘要。"""

    return IncrementalInferenceResult(
        batch_id=str(manifest["batch_id"]),
        policy_id=str(manifest["policy_id"]),
        candidate_name=str(manifest["candidate_name"]),
        count=int(manifest["count"]),
        action_counts=dict(manifest["action_counts"]),
        manual_task_count=int(manifest["manual_task_count"]),
        fit_call_count=int(manifest["fit_call_count"]),
        resumed_record_count=int(manifest["resumed_record_count"]),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        records_sha256=str(manifest["artifacts"]["records"]["sha256"]),
        manual_task_sha256=str(
            manifest["artifacts"]["manual_task"]["sha256"]
        ),
        reused=reused,
        status=str(manifest["status"]),
    )


def _validate_existing(
    directory: Path,
    *,
    plan: ModelRetrainingPlan,
    expected_manifest_sha256: str,
) -> IncrementalInferenceResult:
    """完整校验同批次既有包后才允许幂等复用。"""

    manifest_path = directory / "incremental-inference-manifest.json"
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_manifest_hash_mismatch"
        )
    manifest = _load_json(
        manifest_path, "model_retraining_incremental_manifest_invalid"
    )
    artifacts = manifest.get("artifacts", {})
    action_counts = manifest.get("action_counts", {})
    expected_files = {
        "records": "incremental-scored-records.json",
        "manual_task": "manual-review-tourism-relevance-annotation.csv",
        "manual_private_map": "manual-review-private-map.json",
        "input_receipt": "input-receipt.json",
    }
    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-retraining-incremental-inference"
        or manifest.get("status") != "NEW_BATCH_SCORED"
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("algorithm_id") != INCREMENTAL_INFERENCE_ALGORITHM_ID
        or manifest.get("fit_call_count") != 0
        or manifest.get("source_database_write_count") != 0
        or manifest.get("platform_used") is not False
        or not isinstance(action_counts, Mapping)
        or sum(int(value) for value in action_counts.values())
        != int(manifest.get("count", -1))
    ):
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_manifest_invalid"
        )
    for key, filename in expected_files.items():
        details = artifacts.get(key, {})
        if (
            details.get("filename") != filename
            or _file_sha256(directory / filename) != details.get("sha256")
        ):
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_artifact_hash_mismatch"
            )
    return _result(manifest, manifest_path.read_bytes(), reused=True)


def score_incremental_batch_package(
    input_csv: str | Path,
    snapshot_package: str | Path,
    policy_package: str | Path,
    artifact_root: str | Path,
    *,
    plan: ModelRetrainingPlan,
    delivery_plan: ModelRetrainingDeliveryPlan,
    normalization_config: TextCleaningConfig,
    expected_snapshot_manifest_sha256: str,
    expected_policy_manifest_sha256: str,
    code_version: str,
    encoder: LocalQwenCompleteChunkEncoder | None,
    expected_existing_manifest_sha256: str | None = None,
) -> IncrementalInferenceResult:
    """使用唯一冻结策略处理任意新增批次并同步生成中间层任务表。

    Args:
        input_csv: 严格六列的私有新增批次CSV。
        snapshot_package: 1,300条训练快照，用于拒绝训练成员重复进入预测。
        policy_package: 当前唯一冻结路由包。
        artifact_root: 私有运行包根目录。
        plan: 冻结重训计划。
        delivery_plan: 研究者确认的0.31/0.96交付配置。
        normalization_config: 冻结文本规范化配置。
        expected_snapshot_manifest_sha256: 训练快照manifest摘要。
        expected_policy_manifest_sha256: 冻结策略manifest摘要。
        code_version: 40位Git提交身份。
        encoder: 融合模型所需的本地Qwen完整分块编码器。
        expected_existing_manifest_sha256: 严格复用已有包时的外部摘要。

    Returns:
        不含正文和源身份的运行摘要。

    Raises:
        ModelRetrainingIncrementalError: 输入、策略、checkpoint、模型输出或
            artifact不满足冻结契约。

    Notes:
        该入口没有训练分支；``fit_call_count``恒为0。自动排除仅作为派生动作，
        不会回写或删除源记录。中间层表使用UTF-8 BOM四列契约。
    """

    if len(code_version) != 40:
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_code_version_invalid"
        )
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
    selected = policy_manifest.get("selected", {})
    if (
        policy_manifest.get("status") != "RESEARCHER_SELECTED_ROUTING_FROZEN"
        or policy_manifest.get("automatic_routing_authorized") is not True
        or selected.get("candidate_name")
        != delivery_plan.selection.candidate_name
        or selected.get("candidate_id") != delivery_plan.selection.candidate_id
        or float(selected.get("T_keep", -1)) != delivery_plan.selection.T_keep
        or float(selected.get("T_exclude", -1))
        != delivery_plan.selection.T_exclude
        or model.candidate_id != delivery_plan.selection.candidate_id
    ):
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_policy_binding_mismatch"
        )
    members = load_incremental_input_csv(
        input_csv, plan=plan, normalization_config=normalization_config
    )
    training_identities = {item.identity for item in snapshot.documents}
    if len(training_identities) != plan.expected_training_count or any(
        item.identity in training_identities for item in members
    ):
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_training_member_overlap"
        )
    binding = [
        {
            "member_key": item.member_key,
            "component_id": item.component_id,
            "normalized_sha256": item.normalized_sha256,
        }
        for item in members
    ]
    binding_sha256 = _sha256_bytes(_canonical_bytes(binding))
    batch_id = hashlib.sha256(
        (
            f"{plan.plan_id}|{policy_manifest['policy_id']}|{binding_sha256}|"
            f"{normalization_config.sha256}|{INCREMENTAL_INFERENCE_ALGORITHM_ID}|"
            f"{code_version}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    directory = Path(artifact_root).expanduser().resolve() / batch_id
    final_manifest = directory / "incremental-inference-manifest.json"
    if final_manifest.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_existing_requires_manifest_hash"
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
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_directory_create_failed"
        ) from exc
    state_path = directory / "checkpoint-state.json"
    if state_path.exists():
        state = dict(
            _load_json(
                state_path, "model_retraining_incremental_checkpoint_invalid"
            )
        )
        if (
            state.get("batch_id") != batch_id
            or state.get("policy_manifest_sha256")
            != expected_policy_manifest_sha256
            or state.get("binding_sha256") != binding_sha256
            or state.get("code_version") != code_version
            or not isinstance(state.get("completed_count"), int)
            or not isinstance(state.get("rolling_sha256"), str)
        ):
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_checkpoint_invalid"
            )
    else:
        state = {
            "artifact_kind": "formal-cleaning-incremental-inference-checkpoint",
            "batch_id": batch_id,
            "policy_manifest_sha256": expected_policy_manifest_sha256,
            "binding_sha256": binding_sha256,
            "algorithm_id": INCREMENTAL_INFERENCE_ALGORITHM_ID,
            "code_version": code_version,
            "completed_count": 0,
            "rolling_sha256": "0" * 64,
        }
        _atomic_write(state_path, _canonical_bytes(state))
    completed_count = int(state["completed_count"])
    if completed_count < 0 or completed_count > len(members):
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_checkpoint_invalid"
        )
    records: list[IncrementalScoredMember] = []
    rolling = "0" * 64
    T_keep = delivery_plan.selection.T_keep
    T_exclude = delivery_plan.selection.T_exclude
    for index in range(completed_count):
        member = members[index]
        path = checkpoint / f"{index:07d}-{member.member_key}.json"
        receipt = _load_json(
            path, "model_retraining_incremental_checkpoint_invalid"
        )
        try:
            record = IncrementalScoredMember(**receipt)
        except TypeError as exc:
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_checkpoint_invalid"
            ) from exc
        if (
            record.identity != member.identity
            or record.member_key != member.member_key
            or record.component_id != member.component_id
            or record.normalized_sha256 != member.normalized_sha256
            or _sha256_bytes(record.normalized_model_text.encode("utf-8"))
            != member.normalized_sha256
            or record.provisional_action
            != _route_probability(
                record.p_unrelated, T_keep=T_keep, T_exclude=T_exclude
            )
        ):
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_checkpoint_invalid"
            )
        receipt_sha = _file_sha256(path)
        rolling = hashlib.sha256(f"{rolling}|{receipt_sha}".encode()).hexdigest()
        records.append(record)
    if rolling != state["rolling_sha256"]:
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_checkpoint_invalid"
        )
    for index in range(completed_count, len(members)):
        record = score_incremental_member(
            members[index],
            model=model,
            encoder=encoder,
            T_keep=T_keep,
            T_exclude=T_exclude,
        )
        payload = _canonical_bytes(asdict(record))
        path = checkpoint / f"{index:07d}-{record.member_key}.json"
        _atomic_write(path, payload)
        receipt_sha = _sha256_bytes(payload)
        rolling = hashlib.sha256(f"{rolling}|{receipt_sha}".encode()).hexdigest()
        records.append(record)
        state["completed_count"] = index + 1
        state["rolling_sha256"] = rolling
        _atomic_write(state_path, _canonical_bytes(state))
    if (
        len(records) != len(members)
        or len({item.identity for item in records}) != len(records)
        or len({item.member_key for item in records}) != len(records)
    ):
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_output_invalid"
        )
    records_bytes = _canonical_bytes([asdict(item) for item in records])
    manual_bytes, private_map = _manual_task_bytes(records, batch_id=batch_id)
    private_map_bytes = _canonical_bytes(list(private_map))
    input_receipt_bytes = _canonical_bytes(
        {
            "artifact_kind": "formal-cleaning-incremental-input-receipt",
            "input_file_sha256": _file_sha256(
                Path(input_csv).expanduser().resolve(strict=True)
            ),
            "input_columns": list(INCREMENTAL_INPUT_COLUMNS),
            "count": len(members),
            "binding_sha256": binding_sha256,
            "normalization_config_sha256": normalization_config.sha256,
            "training_member_overlap_count": 0,
            "platform_used": False,
        }
    )
    _atomic_write(directory / "incremental-scored-records.json", records_bytes)
    _atomic_write(
        directory / "manual-review-tourism-relevance-annotation.csv",
        manual_bytes,
    )
    _atomic_write(directory / "manual-review-private-map.json", private_map_bytes)
    _atomic_write(directory / "input-receipt.json", input_receipt_bytes)
    action_counts = dict(
        sorted(Counter(item.provisional_action for item in records).items())
    )
    manifest = {
        "artifact_kind": "formal-cleaning-model-retraining-incremental-inference",
        "status": "NEW_BATCH_SCORED",
        "batch_id": batch_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "algorithm_id": INCREMENTAL_INFERENCE_ALGORITHM_ID,
        "code_version": code_version,
        "policy_id": str(policy_manifest["policy_id"]),
        "policy_manifest_sha256": expected_policy_manifest_sha256,
        "candidate_name": model.candidate_name,
        "candidate_id": model.candidate_id,
        "T_keep": T_keep,
        "T_exclude": T_exclude,
        "snapshot_id": str(snapshot_manifest["snapshot_id"]),
        "snapshot_manifest_sha256": expected_snapshot_manifest_sha256,
        "count": len(records),
        "action_counts": action_counts,
        "manual_task_count": len(private_map),
        "fit_call_count": 0,
        "predict_record_count": len(records),
        "encoded_record_count": (
            len(records)
            if model.candidate_name in {"qwen_linear_svc", "logit_fusion"}
            else 0
        ),
        "resumed_record_count": completed_count,
        "training_member_overlap_count": 0,
        "platform_used": False,
        "source_database_write_count": 0,
        "source_records_deleted": 0,
        "automatic_routing_authorized": True,
        "acceptance_basis": str(policy_manifest["acceptance_basis"]),
        "artifacts": {
            "records": {
                "filename": "incremental-scored-records.json",
                "sha256": _sha256_bytes(records_bytes),
            },
            "manual_task": {
                "filename": "manual-review-tourism-relevance-annotation.csv",
                "sha256": _sha256_bytes(manual_bytes),
                "encoding": "utf-8-sig",
                "columns": list(MANUAL_TASK_COLUMNS),
            },
            "manual_private_map": {
                "filename": "manual-review-private-map.json",
                "sha256": _sha256_bytes(private_map_bytes),
            },
            "input_receipt": {
                "filename": "input-receipt.json",
                "sha256": _sha256_bytes(input_receipt_bytes),
            },
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(final_manifest, manifest_bytes)
    return _result(manifest, manifest_bytes, reused=False)


def render_incremental_inference_result(
    result: IncrementalInferenceResult, *, output_format: str
) -> str:
    """输出机器JSON或新增批次纯预测的人类可读摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelRetrainingIncrementalError(
            "model_retraining_incremental_output_format_invalid"
        )
    return "\n".join(
        [
            "# 冻结模型新增批次纯预测结果",
            "",
            f"批次ID：{result.batch_id}",
            f"策略ID：{result.policy_id}；候选={result.candidate_name}",
            (
                f"记录：{result.count}；artifact="
                f"{'严格复用' if result.reused else '新建并封存'}"
            ),
            (
                "三段动作："
                + " / ".join(
                    f"{key}={value}" for key, value in result.action_counts.items()
                )
            ),
            f"人工中间层任务：{result.manual_task_count}条（UTF-8 BOM四列）",
            f"checkpoint恢复：{result.resumed_record_count}条",
            "fit调用：0；平台特征：未使用；源数据库写入和删除：0。",
            "使用冻结融合模型与0.31/0.96；未替换模型或重新训练。",
            f"状态：{result.status}",
        ]
    )
