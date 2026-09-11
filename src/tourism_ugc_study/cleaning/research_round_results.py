"""将完整人口与新模型结果一一合并为待人工审查的候选决定。

纯领域计算：不加载模型、不读取数据库、不作人工终审、不按重复关系删除。
人工证据没有模型概率，模型结果必须同时匹配身份、规范正文和泄漏分量。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence


def merge_candidates(records: Sequence[Mapping[str, Any]],
                     scored: Sequence[Mapping[str, Any]], *,
                     T_keep: float, T_exclude: float) -> list[dict[str, Any]]:
    """覆盖全部输入且只消费一次预测；缺失、重复、错配或非法概率均失败。

阈值由已验证的冻结策略提供，只用于验收原动作，不重新选择阈值。输出decision
只有keep/exclude/manual_review且始终是候选；本轮人工审查状态仍为pending。
    """
    if not 0 <= T_keep < T_exclude <= 1:
        raise ValueError("round_result_threshold_invalid")
    by_id: dict[tuple[int, int], Mapping[str, Any]] = {}
    for row in scored:
        identity = (row["source_post_id"], row["source_version"])
        if identity in by_id:
            raise ValueError("round_prediction_duplicate")
        by_id[identity] = row
    identities: set[tuple[int, int]] = set()
    post_ids: set[int] = set()
    result = []
    for original in records:
        row = dict(original)
        identity = (row["source_post_id"], row["source_version"])
        if identity in identities or row["source_post_id"] in post_ids:
            raise ValueError("round_candidate_identity_duplicate")
        identities.add(identity)
        post_ids.add(row["source_post_id"])
        if row["human_final_review_status"] != "pending":
            raise ValueError("round_premature_final_review")
        prior = row["prior_human_label"]
        if prior is not None:
            if prior not in ("related", "unrelated") or identity in by_id:
                raise ValueError("round_human_prediction_overlap")
            if not row["prior_human_origin"] or not row["prior_human_evidence_sha256"]:
                raise ValueError("round_human_evidence_missing")
            row.update(decision="keep" if prior == "related" else "exclude",
                       decision_origin=row["prior_human_origin"], p_unrelated=None,
                       decision_evidence_sha256=row["prior_human_evidence_sha256"])
        else:
            prediction = by_id.pop(identity, None)
            if prediction is None:
                raise ValueError("round_prediction_missing")
            for key in ("component_id", "normalized_sha256", "normalized_model_text"):
                if row[key] != prediction[key]:
                    raise ValueError("round_prediction_text_or_component_mismatch")
            probability = prediction["p_unrelated"]
            if isinstance(probability, bool) or not isinstance(probability, (float, int)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("round_prediction_probability_invalid")
            action = "auto_keep" if probability <= T_keep else "auto_exclude" if probability >= T_exclude else "manual_review"
            if prediction["provisional_action"] != action:
                raise ValueError("round_prediction_action_mismatch")
            row.update(decision={"auto_keep": "keep", "auto_exclude": "exclude", "manual_review": "manual_review"}[action],
                       decision_origin="frozen_model", p_unrelated=probability,
                       decision_evidence_sha256=prediction["evidence_manifest_sha256"])
        result.append(row)
    if by_id:
        raise ValueError("round_unexpected_prediction")
    cluster_decisions: dict[str, set[str]] = defaultdict(set)
    for row in result:
        cluster_decisions[row["exact_cluster_id"]].add(row["decision"])
    for row in result:
        row["exact_cluster_decision_conflict"] = len(cluster_decisions[row["exact_cluster_id"]]) > 1
    return sorted(result, key=lambda row: row["source_post_id"])


def candidate_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """生成仅含计数的汇总；keep按帖子计数，不冒充去重后最终样本量。"""
    return {"record_count": len(records),
            "decision_counts": dict(Counter(row["decision"] for row in records)),
            "decision_origin_counts": dict(Counter(row["decision_origin"] for row in records)),
            "platform_decision_counts": {platform: dict(Counter(row["decision"] for row in records if row["platform_key"] == platform))
                                         for platform in sorted({row["platform_key"] for row in records})},
            "keep_exact_cluster_count": len({r["exact_cluster_id"] for r in records if r["decision"] == "keep"}),
            "exact_cluster_decision_conflict_count": len({r["exact_cluster_id"] for r in records if r["exact_cluster_decision_conflict"]}),
            "human_final_review_pending_count": sum(r["human_final_review_status"] == "pending" for r in records),
            "final_keep_published_count": 0}
