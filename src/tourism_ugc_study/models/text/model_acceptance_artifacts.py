"""模型验收配对证据的读取、baseline 绑定与不可变报告持久化。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .model_acceptance import (
    ModelAcceptanceError,
    PairedOofObservation,
    evaluate_model_acceptance,
    load_model_acceptance_policy,
)


_EVIDENCE_FIELDS = frozenset(
    {
        "artifact_kind",
        "baseline_model_id",
        "candidate_model_id",
        "reference_csv_sha256",
        "train_manifest_sha256",
        "scope",
        "paired_outer_folds",
        "test_members_read",
        "test_probabilities_present",
        "platform_used",
        "records",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "member_key",
        "component_id",
        "tourism_label",
        "baseline_p_unrelated",
        "candidate_p_unrelated",
    }
)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    """生成带结尾换行的规范 JSON。"""

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


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelAcceptanceError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelAcceptanceError(reason_code)
    return value


def evaluate_model_acceptance_artifact(
    policy_path: str | Path,
    evidence_path: str | Path,
    output_path: str | Path,
) -> Mapping[str, Any]:
    """校验配对 nested OOF artifact 并排他封存验收报告。

    Args:
        policy_path: 已冻结的 UGC 安全优先策略 YAML。
        evidence_path: challenger 产生的去敏配对训练侧 OOF JSON。
        output_path: 尚不存在的聚合验收报告 JSON。

    Returns:
        加入输入证据摘要后的聚合验收报告。

    Raises:
        ModelAcceptanceError: 输入谱系、字段、测试边界或排他写入失败。
    """

    policy = load_model_acceptance_policy(policy_path)
    evidence_file = Path(evidence_path).expanduser().resolve()
    output_file = Path(output_path).expanduser().resolve()
    if output_file.exists():
        raise ModelAcceptanceError("model_acceptance_output_exists")
    evidence = _load_json(evidence_file, "model_acceptance_evidence_unreadable")
    if set(evidence) != _EVIDENCE_FIELDS:
        raise ModelAcceptanceError("model_acceptance_evidence_contract_invalid")
    if (
        evidence.get("artifact_kind")
        != "formal-cleaning-paired-nested-oof-comparison"
        or evidence.get("baseline_model_id") != policy.baseline_model_id
        or evidence.get("reference_csv_sha256") != policy.reference_csv_sha256
        or evidence.get("train_manifest_sha256") != policy.train_manifest_sha256
        or evidence.get("scope") != "train_nested_group_oof"
        or evidence.get("paired_outer_folds") is not True
        or evidence.get("test_members_read") is not False
        or evidence.get("test_probabilities_present") is not False
        or evidence.get("platform_used") is not False
    ):
        raise ModelAcceptanceError("model_acceptance_evidence_lineage_invalid")
    candidate_id = evidence.get("candidate_model_id")
    raw_records = evidence.get("records")
    if not isinstance(candidate_id, str) or not isinstance(raw_records, list):
        raise ModelAcceptanceError("model_acceptance_evidence_contract_invalid")
    observations: list[PairedOofObservation] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping) or set(raw_record) != _RECORD_FIELDS:
            raise ModelAcceptanceError("model_acceptance_evidence_record_invalid")
        try:
            observations.append(
                PairedOofObservation(
                    member_key=str(raw_record["member_key"]),
                    component_id=str(raw_record["component_id"]),
                    tourism_label=str(raw_record["tourism_label"]),
                    baseline_p_unrelated=float(raw_record["baseline_p_unrelated"]),
                    candidate_p_unrelated=float(raw_record["candidate_p_unrelated"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelAcceptanceError(
                "model_acceptance_evidence_record_invalid"
            ) from exc
    report = dict(
        evaluate_model_acceptance(
            observations,
            policy,
            candidate_model_id=candidate_id,
        )
    )
    try:
        evidence_sha256 = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
    except OSError as exc:
        raise ModelAcceptanceError("model_acceptance_evidence_unreadable") from exc
    report["evidence_sha256"] = evidence_sha256
    payload = _canonical_bytes(report)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_file.with_name(f".{output_file.name}.tmp")
    if temporary.exists():
        raise ModelAcceptanceError("model_acceptance_temporary_exists")
    try:
        temporary.write_bytes(payload)
        temporary.replace(output_file)
    except OSError as exc:
        raise ModelAcceptanceError("model_acceptance_output_write_failed") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return report
