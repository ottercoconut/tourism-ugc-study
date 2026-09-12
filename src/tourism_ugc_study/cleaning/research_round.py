"""新研究人口的版本、确定性规范化、人工证据与泄漏分组计算。

不加载判别模型，不修改输入，也不把近重复候选或同作者关系作为删除真值。
人工标签只匹配同一源身份和完全相同的规范正文；变化正文不能继承旧标签。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from tourism_ugc_study.annotation.leakage_groups import LeakagePost, build_leakage_plan

from .text_config import TextCleaningConfig
from .text_duplicates import TextDocument, build_duplicate_plan
from .text_normalize import normalize_post_text


def canonical_hash(value: Any) -> str:
    """以排序且禁止NaN的规范JSON绑定输入，返回完整SHA-256。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def prepare_records(
    current: Sequence[Mapping[str, Any]], historical: Mapping[int, Mapping[str, Any]],
    versions: Mapping[int, int], human: Mapping[int, Mapping[str, str]],
    normalization: TextCleaningConfig,
) -> list[dict[str, Any]]:
    """逐条形成完整的新轮输入，不沿用旧模型概率。

    源版本在标题、正文、结构状态或平台内作者身份变化时递增；来源身份复用但
    平台帖子键冲突时失败。采集时间/互动刷新不造成文本版本变化。输入结构失败
    时停止准备，原记录不删除。规范正文完全相同的训练成员即使原始格式变化，
    也继续走人工证据路径，不利用新版本绕过训练隔离。
    """
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    fields = ("title", "content_text", "status", "author_platform_id")
    for row in sorted(current, key=lambda item: item["id"]):
        post_id = int(row["id"])
        if post_id <= 0 or post_id in seen:
            raise ValueError("round_source_identity_invalid")
        seen.add(post_id)
        old = historical.get(post_id)
        if old and (old["platform_key"], old["platform_post_id"] or old["canonical_url"]) != (
            row["platform_key"], row["platform_post_id"] or row["canonical_url"]
        ):
            raise ValueError("round_source_identity_reused")
        changed = bool(old and any(row.get(key) != old.get(key) for key in fields))
        if old is not None and (post_id not in versions or versions[post_id] < 1):
            raise ValueError("round_historical_version_missing")
        version = versions.get(post_id, 0) + 1 if (old is None or changed) else versions[post_id]
        normalized = normalize_post_text(row.get("title"), row.get("content_text"),
                                         source_status=row.get("status"), config=normalization)
        if normalized.structure_status != "usable":
            raise ValueError("round_structure_not_usable:" + normalized.structure_reason_code)
        prior = human.get(post_id)
        if prior is not None and prior["tourism_label"] not in ("related", "unrelated"):
            raise ValueError("round_human_label_invalid")
        prior_matches = prior is not None and prior["normalized_sha256"] == normalized.normalized_sha256
        author = str(row.get("author_platform_id") or "").strip()
        records.append({
            "source_post_id": post_id, "source_version": version,
            "input_change": "new" if old is None else "changed" if changed else "unchanged",
            "raw_input_sha256": canonical_hash({key: row.get(key) for key in fields}),
            "platform_key": row["platform_key"],
            "author_sha256": canonical_hash([row["platform_key"], author]),
            "author_identity_present": bool(author),
            "title": row.get("title") or "", "body": row.get("content_text") or "",
            "source_status": row.get("status") or "",
            "normalized_title": normalized.normalized_title,
            "normalized_body": normalized.normalized_body,
            "normalized_model_text": normalized.model_text,
            "normalized_sha256": normalized.normalized_sha256,
            "exact_canonical_sha256": normalized.exact_canonical_sha256,
            "prior_human_label": prior["tourism_label"] if prior_matches else None,
            "prior_human_origin": prior["origin"] if prior_matches else None,
            "prior_human_evidence_sha256": prior["evidence_sha256"] if prior_matches else None,
            "human_final_review_status": "pending",
        })
    return records


def attach_duplicate_groups(records: list[dict[str, Any]], normalization: TextCleaningConfig) -> dict[str, Any]:
    """复用既有去重与泄漏纯函数，原位补充分组但不删任何帖子。

    本轮历史库不存在人工确认近重复边，调用方必须先核实这一前提。当前近重复
    候选仅供审查，不进入泄漏真值；同作者边也不自动成为分析去重关系。
    """
    documents = [TextDocument(r["source_post_id"], r["source_version"], r["platform_key"],
                              r["normalized_title"], r["normalized_body"], r["exact_canonical_sha256"])
                 for r in records]
    duplicates = build_duplicate_plan(documents, normalization.near_duplicate)
    exact: dict[int, tuple[str, int, int]] = {}
    for cluster in duplicates.exact_clusters:
        for member in cluster.members:
            exact[member.source_post_id] = (cluster.cluster_id, cluster.representative.source_post_id, len(cluster.members))
    leakage = build_leakage_plan([
        LeakagePost(r["source_post_id"], r["source_version"], r["author_sha256"],
                    r["author_identity_present"], exact[r["source_post_id"]][0]) for r in records
    ], [])
    by_id = {member.source_post_id: member for member in leakage.members}
    for row in records:
        cluster, representative, size = exact[row["source_post_id"]]
        row.update({"exact_cluster_id": cluster, "exact_representative_id": representative,
                    "exact_cluster_size": size, "component_id": by_id[row["source_post_id"]].component_id})
    return {"near_pairs": [asdict(pair) for pair in duplicates.near_pairs],
            "duplicate_output_sha256": duplicates.output_sha256,
            "leakage_output_sha256": leakage.output_sha256,
            "exact_cluster_count": len(duplicates.exact_clusters),
            "exact_duplicate_cluster_count": sum(len(c.members) > 1 for c in duplicates.exact_clusters),
            "exact_duplicate_extra_records": len(records) - len(duplicates.exact_clusters),
            "component_count": len({member.component_id for member in leakage.members}),
            "near_candidate_count": len(duplicates.near_pairs),
            "library_versions": dict(duplicates.library_versions)}
