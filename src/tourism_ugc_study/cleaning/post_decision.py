"""帖子级文本清洗最终决定的纯领域规则。

本模块不读取数据库，也不负责选择“最新”证据。调用方必须显式传入同一
帖子版本的人工双轴证据或模型候选，以及正式发布所需的质量门状态。领域层
只生成可解释、可复算的 ``keep/review/exclude`` 决定，持久化与封存由独立
仓储模块完成。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Sequence


PostDecisionValue = Literal["keep", "review", "exclude"]
StructureLabel = Literal["usable", "invalid", "uncertain"]
TourismLabel = Literal["related", "unrelated", "uncertain", "not_applicable"]
ModelSuggestedAction = Literal[
    "high_risk_review", "manual_review", "low_risk_keep_candidate"
]


@dataclass(frozen=True)
class HumanTextEvidence:
    """一条由调用方显式选入决定构建的人工双轴证据。

    ``evidence_id`` 应引用追加式原始证据或仲裁记录。领域层允许收到多条
    证据，以便显式识别冲突；它不会按时间或输入顺序静默挑选其中一条。
    """

    evidence_id: str
    structure_label: StructureLabel
    tourism_label: TourismLabel


@dataclass(frozen=True)
class ModelDecisionEvidence:
    """正式文本模型对单个帖子给出的候选动作及其发布门状态。

    两个版本 manifest 分别表示决定构建要求的版本集合与模型实际绑定的
    版本集合；二者必须完全相同，避免用一个含糊的“兼容”标志掩盖快照、
    手册、配置或候选构建错配。模型只要使用测试集调参，也不能最终化。
    """

    evidence_id: str
    model_run_id: str
    suggested_action: ModelSuggestedAction
    run_mode: Literal["formal", "smoke"]
    seal_status: Literal["building", "finalized"]
    required_version_manifest_sha256: str
    model_version_manifest_sha256: str
    test_set_isolation_passed: bool
    low_risk_threshold_enabled: bool
    platform_audit_complete: bool
    text_keep_audit_passed: bool


@dataclass(frozen=True)
class PostDecisionRequest:
    """一个冻结帖子版本的全部显式决策输入。

    ``rule_version`` 是决定算法版本，不是文档中的开发阶段名称。人工证据
    可为空；模型证据也可为空，此时缺证必须进入 ``review``。
    """

    source_post_id: int
    source_version: int
    rule_version: str
    human_evidence: tuple[HumanTextEvidence, ...] = ()
    model_evidence: ModelDecisionEvidence | None = None


@dataclass(frozen=True)
class PostDecision:
    """可持久化的帖子决定、稳定理由链和内容哈希。"""

    source_post_id: int
    source_version: int
    decision: PostDecisionValue
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    rule_version: str
    decision_sha256: str


def _sha256(payload: object) -> str:
    """以稳定 JSON 编码计算领域对象哈希。"""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_human_evidence(evidence: HumanTextEvidence) -> None:
    """验证双轴适用性，拒绝把结构无效记录制造成旅游判断。"""

    if not evidence.evidence_id:
        raise ValueError("human evidence id is required")
    if evidence.structure_label not in {"usable", "invalid", "uncertain"}:
        raise ValueError("unsupported structure label")
    if evidence.tourism_label not in {
        "related",
        "unrelated",
        "uncertain",
        "not_applicable",
    }:
        raise ValueError("unsupported tourism label")
    if (evidence.structure_label == "invalid") != (
        evidence.tourism_label == "not_applicable"
    ):
        raise ValueError("tourism applicability conflicts with structure label")


def _validate_model_evidence(evidence: ModelDecisionEvidence) -> None:
    """验证模型候选的枚举和可追溯身份，不替质量门作默认值。"""

    if not evidence.evidence_id or not evidence.model_run_id:
        raise ValueError("model evidence id and model run id are required")
    if evidence.suggested_action not in {
        "high_risk_review",
        "manual_review",
        "low_risk_keep_candidate",
    }:
        raise ValueError("unsupported model suggested action")
    if evidence.run_mode not in {"formal", "smoke"}:
        raise ValueError("unsupported model run mode")
    if evidence.seal_status not in {"building", "finalized"}:
        raise ValueError("unsupported model seal status")
    for value in (
        evidence.required_version_manifest_sha256,
        evidence.model_version_manifest_sha256,
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("version manifest sha256 must be lowercase hexadecimal")


def _human_decision(
    evidence: Sequence[HumanTextEvidence],
) -> tuple[PostDecisionValue, tuple[str, ...]]:
    """合并显式人工证据；任何不唯一或不确定结果都保守转为复核。"""

    labels = {(item.structure_label, item.tourism_label) for item in evidence}
    if len(labels) != 1:
        return "review", ("human_evidence_conflict",)
    structure, tourism = next(iter(labels))
    if structure == "uncertain" or tourism == "uncertain":
        return "review", ("human_evidence_uncertain",)
    if structure == "invalid" and tourism == "not_applicable":
        return "exclude", ("structure_invalid",)
    if structure == "usable" and tourism == "unrelated":
        return "exclude", ("human_confirmed_tourism_unrelated",)
    if structure == "usable" and tourism == "related":
        return "keep", ("human_confirmed_usable_related",)
    # 前置验证已经封闭了枚举组合；保留此失败分支可防未来扩展时静默放行。
    raise ValueError("unsupported human evidence combination")


def _model_decision(
    evidence: ModelDecisionEvidence,
) -> tuple[PostDecisionValue, tuple[str, ...]]:
    """把模型候选经过正式门槛转为决定，且模型永不直接产生排除。"""

    invalid_reasons: list[str] = []
    if evidence.run_mode != "formal":
        invalid_reasons.append("model_not_formal")
    if evidence.seal_status != "finalized":
        invalid_reasons.append("model_not_finalized")
    if (
        evidence.required_version_manifest_sha256
        != evidence.model_version_manifest_sha256
    ):
        invalid_reasons.append("model_version_mismatch")
    if not evidence.test_set_isolation_passed:
        invalid_reasons.append("model_test_isolation_failed")

    # 高风险和中间区间的语义本来就是等待人工确认，模型质量门齐全也不能
    # 越权把它们变成排除或保留。
    if evidence.suggested_action in {"high_risk_review", "manual_review"}:
        return "review", (evidence.suggested_action, *invalid_reasons)

    if not evidence.low_risk_threshold_enabled:
        invalid_reasons.append("low_risk_threshold_disabled")
    if not evidence.platform_audit_complete:
        invalid_reasons.append("platform_audit_incomplete")
    if not evidence.text_keep_audit_passed:
        invalid_reasons.append("text_keep_audit_not_passed")
    if invalid_reasons:
        return "review", tuple(invalid_reasons)
    return "keep", ("formal_low_risk_candidate_gates_passed",)


def decide_post(request: PostDecisionRequest) -> PostDecision:
    """按人工优先规则为一个帖子版本生成确定性最终决定。

    失败语义如下：身份、枚举或双轴适用性不合法时抛出 ``ValueError``；
    合法但证据缺失、互相冲突或质量门未通过时返回 ``review``。决定哈希
    覆盖帖子身份、规则版本、决定、理由和全部显式证据 ID，因此证据链或
    规则变化一定产生新哈希。
    """

    if request.source_post_id <= 0 or request.source_version <= 0:
        raise ValueError("positive source post identity is required")
    if not request.rule_version:
        raise ValueError("post decision rule version is required")

    evidence_ids: list[str] = []
    for evidence in request.human_evidence:
        _validate_human_evidence(evidence)
        evidence_ids.append(evidence.evidence_id)
    if request.model_evidence is not None:
        _validate_model_evidence(request.model_evidence)
        evidence_ids.append(request.model_evidence.evidence_id)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("decision evidence ids must be unique")

    if request.human_evidence:
        decision, reasons = _human_decision(request.human_evidence)
    elif request.model_evidence is not None:
        decision, reasons = _model_decision(request.model_evidence)
    else:
        decision, reasons = "review", ("required_evidence_missing",)

    ordered_evidence_ids = tuple(sorted(evidence_ids))
    payload = {
        "decision": decision,
        "evidence_ids": ordered_evidence_ids,
        "reason_codes": reasons,
        "rule_version": request.rule_version,
        "source_post_id": request.source_post_id,
        "source_version": request.source_version,
    }
    return PostDecision(
        source_post_id=request.source_post_id,
        source_version=request.source_version,
        decision=decision,
        reason_codes=reasons,
        evidence_ids=ordered_evidence_ids,
        rule_version=request.rule_version,
        decision_sha256=_sha256(payload),
    )


def build_post_decisions(
    requests: Sequence[PostDecisionRequest],
) -> tuple[PostDecision, ...]:
    """批量生成按帖子身份排序的决定，并拒绝重复身份。

    输入顺序不影响结果；这使仓储层可直接对输出构建稳定 manifest。空输入
    合法，表示显式的空快照，而不是隐式读取其他运行。
    """

    identities = [
        (request.source_post_id, request.source_version) for request in requests
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate post decision identity")
    return tuple(
        decide_post(request)
        for request in sorted(
            requests,
            key=lambda item: (item.source_post_id, item.source_version),
        )
    )
