"""最终不重复建模参考集的分阶段应用服务。

本模块组合配置无关的领域模块，但不解析命令行。每个公开函数对应一个可恢复
阶段，并只通过不可变 artifact 交换证据。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .reference_artifacts import (
    ArtifactPairResult,
    file_sha256,
    load_candidate_population,
    load_legacy_reference_input,
    seal_label_resolution_artifact,
    validate_duplicate_decision_artifact,
    validate_label_resolution_artifact,
    write_candidate_review_artifacts,
    write_final_reference_artifacts,
    write_label_conflict_review_artifacts,
    write_replacement_queue_artifacts,
    write_supplemental_label_tasks,
)
from .reference_candidates import compute_duplicate_candidates
from .reference_contract import (
    ALLOWED_LABELS,
    ReferenceDatasetError,
    SourceIdentity,
    SUPPLEMENTAL_LABEL_FIELDS,
    canonical_sha256,
)
from .reference_human_evidence import (
    build_duplicate_component_plan,
    find_duplicate_label_conflicts,
)
from .reference_replacements import (
    ReplacementQueueItem,
    build_replacement_queue,
    compute_replacement_duplicate_candidates,
    find_replacement_label_conflicts,
    prepare_replacement_annotation_plan,
    select_replacements,
)


@dataclass(frozen=True)
class ReferenceWorkflowContext:
    """所有阶段共享但不含输出路径的冻结输入身份。

    Attributes:
        legacy_csv: 现有700条完成 CSV。
        legacy_manifest: 与旧 CSV 绑定的 manifest。
        derived_db: 只读派生数据库。
        label_guide_id: 标签手册身份。
        normalization_rule_id: 规范化规则身份。
        random_seed: 全局候补队列固定种子。
    """

    legacy_csv: Path
    legacy_manifest: Path
    derived_db: Path
    label_guide_id: str
    normalization_rule_id: str
    random_seed: int


def build_duplicate_review_stage(
    context: ReferenceWorkflowContext,
    *,
    output_csv: str | Path,
    output_manifest: str | Path,
) -> ArtifactPairResult:
    """校验现有700条并生成完整全对近重复人工复核 artifact。

    Args:
        context: 旧证据、只读数据库和冻结规则身份。
        output_csv: 私有候选人工复核 CSV。
        output_manifest: 配对候选 manifest。

    Returns:
        候选 CSV/manifest 的哈希、行数和状态。
    """

    legacy = load_legacy_reference_input(
        context.legacy_csv,
        context.legacy_manifest,
        context.derived_db,
        expected_label_guide_id=context.label_guide_id,
        normalization_rule_id=context.normalization_rule_id,
    )
    computation = compute_duplicate_candidates(legacy.posts)
    return write_candidate_review_artifacts(
        computation,
        legacy.posts,
        output_csv,
        output_manifest,
        input_hashes={
            "legacy_csv_sha256": legacy.csv_sha256,
            "legacy_manifest_sha256": legacy.manifest_sha256,
        },
    )


def build_replacement_queue_stage(
    context: ReferenceWorkflowContext,
    *,
    duplicate_decisions_csv: str | Path,
    duplicate_decisions_manifest: str | Path,
    label_resolutions_csv: str | Path,
    label_resolutions_manifest: str | Path,
    output_csv: str | Path,
    output_manifest: str | Path,
) -> ArtifactPairResult:
    """消费人工重复结论并冻结全局、无标签候补队列。

    Args:
        context: 旧证据、只读数据库和冻结规则身份。
        duplicate_decisions_csv: 已 finalized 的人工重复决定 CSV。
        duplicate_decisions_manifest: 与决定 CSV 唯一配对的 finalized manifest。
        label_resolutions_csv: 重复分量标签冲突完成 CSV；无冲突时为空表。
        label_resolutions_manifest: 与冲突确认 CSV 配对的 finalized manifest。
        output_csv: 私有候补队列 CSV。
        output_manifest: 配对候补队列 manifest。

    Returns:
        候补队列 artifact 的哈希、行数和状态。

    Raises:
        ReferenceDatasetError: 任一近重复候选尚未最终确认或标签冲突未关闭。
    """

    legacy = load_legacy_reference_input(
        context.legacy_csv,
        context.legacy_manifest,
        context.derived_db,
        expected_label_guide_id=context.label_guide_id,
        normalization_rule_id=context.normalization_rule_id,
    )
    candidates = compute_duplicate_candidates(legacy.posts)
    decisions = validate_duplicate_decision_artifact(
        duplicate_decisions_csv,
        duplicate_decisions_manifest,
        expected_input_member_sha256=candidates.input_member_sha256,
        expected_candidate_sha256=candidates.candidate_sha256,
        expected_input_hashes={
            "legacy_csv_sha256": legacy.csv_sha256,
            "legacy_manifest_sha256": legacy.manifest_sha256,
        },
    )
    resolution_artifact = validate_label_resolution_artifact(
        label_resolutions_csv,
        label_resolutions_manifest,
        expected_duplicate_decision_sha256=file_sha256(
            duplicate_decisions_manifest
        ),
        expected_component_member_sha256=candidates.input_member_sha256,
        expected_label_guide_id=context.label_guide_id,
    )
    plan = build_duplicate_component_plan(
        legacy.posts,
        candidates.candidates,
        decisions,
        label_resolutions=resolution_artifact.resolutions,
        require_all_candidates_finalized=True,
    )
    population = load_candidate_population(
        context.derived_db, candidate_build_id=legacy.candidate_build_id
    )
    queue = build_replacement_queue(
        population,
        blocked_identities=[post.identity for post in legacy.posts],
        random_seed=context.random_seed,
    )
    return write_replacement_queue_artifacts(
        queue,
        plan.representatives,
        output_csv,
        output_manifest,
        candidate_build_id=legacy.candidate_build_id,
        candidate_build_sha256=legacy.candidate_build_sha256,
        duplicate_decision_sha256=plan.decision_sha256,
    )


def build_label_conflict_review_stage(
    context: ReferenceWorkflowContext,
    *,
    duplicate_decisions_csv: str | Path,
    duplicate_decisions_manifest: str | Path,
    output_csv: str | Path,
    output_manifest: str | Path,
) -> ArtifactPairResult:
    """在重复决定 finalized 后生成标签冲突人工任务。

    Args:
        context: 旧证据、只读数据库和冻结规则身份。
        duplicate_decisions_csv: 初始700条 finalized 重复决定 CSV。
        duplicate_decisions_manifest: 与决定 CSV 配对的 finalized manifest。
        output_csv: 私有冲突标签待处理 CSV。
        output_manifest: 配对 pending manifest。

    Returns:
        冲突任务 artifact 的哈希、数量和状态。
    """

    legacy = load_legacy_reference_input(
        context.legacy_csv,
        context.legacy_manifest,
        context.derived_db,
        expected_label_guide_id=context.label_guide_id,
        normalization_rule_id=context.normalization_rule_id,
    )
    candidates = compute_duplicate_candidates(legacy.posts)
    decisions = validate_duplicate_decision_artifact(
        duplicate_decisions_csv,
        duplicate_decisions_manifest,
        expected_input_member_sha256=candidates.input_member_sha256,
        expected_candidate_sha256=candidates.candidate_sha256,
        expected_input_hashes={
            "legacy_csv_sha256": legacy.csv_sha256,
            "legacy_manifest_sha256": legacy.manifest_sha256,
        },
    )
    conflicts = find_duplicate_label_conflicts(
        legacy.posts, candidates.candidates, decisions
    )
    return write_label_conflict_review_artifacts(
        conflicts,
        output_csv,
        output_manifest,
        duplicate_decision_sha256=file_sha256(duplicate_decisions_manifest),
        component_member_sha256=candidates.input_member_sha256,
        label_guide_id=context.label_guide_id,
    )


def build_replacement_duplicate_review_stage(
    context: ReferenceWorkflowContext,
    *,
    duplicate_decisions_csv: str | Path,
    duplicate_decisions_manifest: str | Path,
    label_resolutions_csv: str | Path,
    label_resolutions_manifest: str | Path,
    max_queue_rank: int,
    output_csv: str | Path,
    output_manifest: str | Path,
) -> ArtifactPairResult:
    """在补充标签前生成候补前缀的重复人工复核 artifact。

    Args:
        context: 旧证据、只读数据库和冻结规则身份。
        duplicate_decisions_csv: 初始700条 finalized 重复决定 CSV。
        duplicate_decisions_manifest: 与初始决定配对的 finalized manifest。
        label_resolutions_csv: 初始分量标签冲突完成 CSV。
        label_resolutions_manifest: 与冲突 CSV 配对的 finalized manifest。
        max_queue_rank: 本轮联合全对检查的候补最大冻结秩。
        output_csv: 候补重复待处理 CSV。
        output_manifest: 配对 pending manifest。

    Returns:
        候补重复候选 artifact 的哈希、计数和状态。
    """

    legacy = load_legacy_reference_input(
        context.legacy_csv,
        context.legacy_manifest,
        context.derived_db,
        expected_label_guide_id=context.label_guide_id,
        normalization_rule_id=context.normalization_rule_id,
    )
    initial_candidates = compute_duplicate_candidates(legacy.posts)
    initial_decisions = validate_duplicate_decision_artifact(
        duplicate_decisions_csv,
        duplicate_decisions_manifest,
        expected_input_member_sha256=initial_candidates.input_member_sha256,
        expected_candidate_sha256=initial_candidates.candidate_sha256,
        expected_input_hashes={
            "legacy_csv_sha256": legacy.csv_sha256,
            "legacy_manifest_sha256": legacy.manifest_sha256,
        },
    )
    resolution_artifact = validate_label_resolution_artifact(
        label_resolutions_csv,
        label_resolutions_manifest,
        expected_duplicate_decision_sha256=file_sha256(
            duplicate_decisions_manifest
        ),
        expected_component_member_sha256=initial_candidates.input_member_sha256,
        expected_label_guide_id=context.label_guide_id,
    )
    initial_plan = build_duplicate_component_plan(
        legacy.posts,
        initial_candidates.candidates,
        initial_decisions,
        label_resolutions=resolution_artifact.resolutions,
    )
    population = load_candidate_population(
        context.derived_db, candidate_build_id=legacy.candidate_build_id
    )
    queue = build_replacement_queue(
        population,
        blocked_identities=[post.identity for post in legacy.posts],
        random_seed=context.random_seed,
    )
    computation = compute_replacement_duplicate_candidates(
        initial_plan.representatives,
        queue,
        max_queue_rank=max_queue_rank,
        lineage_posts=legacy.posts,
    )
    batch_posts = [
        item.post
        for item in queue.items
        if item.queue_rank <= max_queue_rank and item.post.structure_usable
    ]
    return write_candidate_review_artifacts(
        computation,
        [*legacy.posts, *batch_posts],
        output_csv,
        output_manifest,
        input_hashes={
            "initial_duplicate_decision_sha256": initial_plan.decision_sha256,
            "replacement_queue_sha256": queue.queue_sha256,
        },
        reviewed_max_queue_rank=max_queue_rank,
    )


def build_replacement_label_conflict_review_stage(
    context: ReferenceWorkflowContext,
    *,
    duplicate_decisions_csv: str | Path,
    duplicate_decisions_manifest: str | Path,
    label_resolutions_csv: str | Path,
    label_resolutions_manifest: str | Path,
    replacement_duplicate_decisions_csv: str | Path,
    replacement_duplicate_decisions_manifest: str | Path,
    max_queue_rank: int,
    prior_supplemental_labels_csv: str | Path | None,
    prior_supplemental_labels_manifest: str | Path | None,
    output_csv: str | Path,
    output_manifest: str | Path,
) -> ArtifactPairResult:
    """生成扩展候补前缀后出现的旅游标签冲突任务。

    Args:
        context: 旧证据、只读数据库和冻结规则身份。
        duplicate_decisions_csv: 初始700条 finalized 重复决定。
        duplicate_decisions_manifest: 初始决定配对 manifest。
        label_resolutions_csv: 初始重复分量冲突完成 CSV。
        label_resolutions_manifest: 初始冲突配对 finalized manifest。
        replacement_duplicate_decisions_csv: 当前候补前缀重复决定。
        replacement_duplicate_decisions_manifest: 候补决定配对 finalized manifest。
        max_queue_rank: 当前已完成重复复核的冻结前缀末秩。
        prior_supplemental_labels_csv: 可选前轮累计补充标签 CSV。
        prior_supplemental_labels_manifest: 前轮配对 finalized manifest。
        output_csv: 候补标签冲突待处理 CSV。
        output_manifest: 配对 pending manifest。

    Returns:
        冲突任务 artifact 摘要；无冲突时为规范空任务。
    """

    legacy = load_legacy_reference_input(
        context.legacy_csv,
        context.legacy_manifest,
        context.derived_db,
        expected_label_guide_id=context.label_guide_id,
        normalization_rule_id=context.normalization_rule_id,
    )
    initial_candidates = compute_duplicate_candidates(legacy.posts)
    initial_decisions = validate_duplicate_decision_artifact(
        duplicate_decisions_csv,
        duplicate_decisions_manifest,
        expected_input_member_sha256=initial_candidates.input_member_sha256,
        expected_candidate_sha256=initial_candidates.candidate_sha256,
        expected_input_hashes={
            "legacy_csv_sha256": legacy.csv_sha256,
            "legacy_manifest_sha256": legacy.manifest_sha256,
        },
    )
    initial_resolution = validate_label_resolution_artifact(
        label_resolutions_csv,
        label_resolutions_manifest,
        expected_duplicate_decision_sha256=file_sha256(
            duplicate_decisions_manifest
        ),
        expected_component_member_sha256=initial_candidates.input_member_sha256,
        expected_label_guide_id=context.label_guide_id,
    )
    initial_plan = build_duplicate_component_plan(
        legacy.posts,
        initial_candidates.candidates,
        initial_decisions,
        label_resolutions=initial_resolution.resolutions,
    )
    population = load_candidate_population(
        context.derived_db, candidate_build_id=legacy.candidate_build_id
    )
    queue = build_replacement_queue(
        population,
        blocked_identities=[post.identity for post in legacy.posts],
        random_seed=context.random_seed,
    )
    replacement_computation = compute_replacement_duplicate_candidates(
        initial_plan.representatives,
        queue,
        max_queue_rank=max_queue_rank,
        lineage_posts=legacy.posts,
    )
    replacement_decisions = validate_duplicate_decision_artifact(
        replacement_duplicate_decisions_csv,
        replacement_duplicate_decisions_manifest,
        expected_input_member_sha256=replacement_computation.input_member_sha256,
        expected_candidate_sha256=replacement_computation.candidate_sha256,
        expected_input_hashes={
            "initial_duplicate_decision_sha256": initial_plan.decision_sha256,
            "replacement_queue_sha256": queue.queue_sha256,
        },
        expected_reviewed_max_queue_rank=max_queue_rank,
    )
    if (prior_supplemental_labels_csv is None) != (
        prior_supplemental_labels_manifest is None
    ):
        raise ReferenceDatasetError("prior_supplemental_artifact_pair_required")
    prior_labels: dict[SourceIdentity, str] = {}
    if (
        prior_supplemental_labels_csv is not None
        and prior_supplemental_labels_manifest is not None
    ):
        prior_labels, _decision_hash, prior_queue_hash, prior_max_rank = (
            _load_supplemental_labels(
                prior_supplemental_labels_csv,
                prior_supplemental_labels_manifest,
                queue_by_identity={item.post.identity: item for item in queue.items},
                expected_label_guide_id=context.label_guide_id,
            )
        )
        if prior_queue_hash != queue.queue_sha256 or prior_max_rank > max_queue_rank:
            raise ReferenceDatasetError("prior_supplemental_lineage_mismatch")
    conflicts = find_replacement_label_conflicts(
        initial_plan.representatives,
        queue,
        duplicate_decisions=replacement_decisions,
        max_queue_rank=max_queue_rank,
        prior_labels=prior_labels,
        lineage_posts=legacy.posts,
        initial_component_by_identity=initial_plan.component_by_identity,
    )
    return write_label_conflict_review_artifacts(
        conflicts,
        output_csv,
        output_manifest,
        duplicate_decision_sha256=file_sha256(
            replacement_duplicate_decisions_manifest
        ),
        component_member_sha256=replacement_computation.input_member_sha256,
        label_guide_id=context.label_guide_id,
    )


def build_supplemental_label_stage(
    context: ReferenceWorkflowContext,
    *,
    duplicate_decisions_csv: str | Path,
    duplicate_decisions_manifest: str | Path,
    label_resolutions_csv: str | Path,
    label_resolutions_manifest: str | Path,
    replacement_duplicate_decisions_csv: str | Path,
    replacement_duplicate_decisions_manifest: str | Path,
    replacement_label_resolutions_csv: str | Path,
    replacement_label_resolutions_manifest: str | Path,
    max_queue_rank: int,
    reserve_count: int,
    prior_supplemental_labels_csv: str | Path | None,
    prior_supplemental_labels_manifest: str | Path | None,
    output_csv: str | Path,
    output_manifest: str | Path,
) -> ArtifactPairResult:
    """完成候补重复检查后生成无标签补充旅游相关性任务。

    Args:
        context: 旧证据、只读数据库和冻结规则身份。
        duplicate_decisions_csv: 初始700条 finalized 重复决定。
        duplicate_decisions_manifest: 与初始决定配对的 manifest。
        label_resolutions_csv: 初始分量标签冲突完成 CSV。
        label_resolutions_manifest: 与冲突 CSV 配对的 finalized manifest。
        replacement_duplicate_decisions_csv: 候补前缀 finalized 重复决定。
        replacement_duplicate_decisions_manifest: 与候补决定配对的 manifest。
        replacement_label_resolutions_csv: 当前候补分量标签冲突完成 CSV。
        replacement_label_resolutions_manifest: 与候补冲突 CSV 配对的 finalized
            manifest。
        max_queue_rank: 已完成重复复核的候补最大冻结秩。
        reserve_count: 超出当前缺口的备用人工任务数。
        prior_supplemental_labels_csv: 可选的前轮累计完成 CSV。
        prior_supplemental_labels_manifest: 与前轮 CSV 配对的 finalized manifest。
        output_csv: 待处理补充标签 CSV。
        output_manifest: 配对 pending manifest。

    Returns:
        不含预读标签的补充任务 artifact 摘要。
    """

    legacy = load_legacy_reference_input(
        context.legacy_csv,
        context.legacy_manifest,
        context.derived_db,
        expected_label_guide_id=context.label_guide_id,
        normalization_rule_id=context.normalization_rule_id,
    )
    initial_candidates = compute_duplicate_candidates(legacy.posts)
    initial_decisions = validate_duplicate_decision_artifact(
        duplicate_decisions_csv,
        duplicate_decisions_manifest,
        expected_input_member_sha256=initial_candidates.input_member_sha256,
        expected_candidate_sha256=initial_candidates.candidate_sha256,
        expected_input_hashes={
            "legacy_csv_sha256": legacy.csv_sha256,
            "legacy_manifest_sha256": legacy.manifest_sha256,
        },
    )
    resolution_artifact = validate_label_resolution_artifact(
        label_resolutions_csv,
        label_resolutions_manifest,
        expected_duplicate_decision_sha256=file_sha256(
            duplicate_decisions_manifest
        ),
        expected_component_member_sha256=initial_candidates.input_member_sha256,
        expected_label_guide_id=context.label_guide_id,
    )
    initial_plan = build_duplicate_component_plan(
        legacy.posts,
        initial_candidates.candidates,
        initial_decisions,
        label_resolutions=resolution_artifact.resolutions,
    )
    population = load_candidate_population(
        context.derived_db, candidate_build_id=legacy.candidate_build_id
    )
    queue = build_replacement_queue(
        population,
        blocked_identities=[post.identity for post in legacy.posts],
        random_seed=context.random_seed,
    )
    replacement_computation = compute_replacement_duplicate_candidates(
        initial_plan.representatives,
        queue,
        max_queue_rank=max_queue_rank,
        lineage_posts=legacy.posts,
    )
    replacement_decisions = validate_duplicate_decision_artifact(
        replacement_duplicate_decisions_csv,
        replacement_duplicate_decisions_manifest,
        expected_input_member_sha256=replacement_computation.input_member_sha256,
        expected_candidate_sha256=replacement_computation.candidate_sha256,
        expected_input_hashes={
            "initial_duplicate_decision_sha256": initial_plan.decision_sha256,
            "replacement_queue_sha256": queue.queue_sha256,
        },
        expected_reviewed_max_queue_rank=max_queue_rank,
    )
    if (prior_supplemental_labels_csv is None) != (
        prior_supplemental_labels_manifest is None
    ):
        raise ReferenceDatasetError("prior_supplemental_artifact_pair_required")
    prior_labels: dict[SourceIdentity, str] = {}
    if (
        prior_supplemental_labels_csv is not None
        and prior_supplemental_labels_manifest is not None
    ):
        queue_by_identity = {item.post.identity: item for item in queue.items}
        (
            prior_labels,
            _prior_decision_hash,
            prior_queue_hash,
            prior_reviewed_max_rank,
        ) = _load_supplemental_labels(
            prior_supplemental_labels_csv,
            prior_supplemental_labels_manifest,
            queue_by_identity=queue_by_identity,
            expected_label_guide_id=context.label_guide_id,
        )
        if prior_queue_hash != queue.queue_sha256:
            raise ReferenceDatasetError("prior_supplemental_queue_hash_mismatch")
        if prior_reviewed_max_rank > max_queue_rank:
            raise ReferenceDatasetError("prior_supplemental_prefix_exceeds_current")
    replacement_resolution_artifact = validate_label_resolution_artifact(
        replacement_label_resolutions_csv,
        replacement_label_resolutions_manifest,
        expected_duplicate_decision_sha256=file_sha256(
            replacement_duplicate_decisions_manifest
        ),
        expected_component_member_sha256=replacement_computation.input_member_sha256,
        expected_label_guide_id=context.label_guide_id,
    )
    plan = prepare_replacement_annotation_plan(
        initial_plan.representatives,
        queue,
        duplicate_decisions=replacement_decisions,
        max_queue_rank=max_queue_rank,
        reserve_count=reserve_count,
        prior_labels=prior_labels,
        label_resolutions=replacement_resolution_artifact.resolutions,
        lineage_posts=legacy.posts,
        initial_component_by_identity=initial_plan.component_by_identity,
    )
    return write_supplemental_label_tasks(
        plan,
        output_csv,
        output_manifest,
        queue_sha256=queue.queue_sha256,
        label_guide_id=context.label_guide_id,
    )


def _load_supplemental_labels(
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    queue_by_identity: Mapping[SourceIdentity, ReplacementQueueItem],
    expected_label_guide_id: str,
) -> tuple[dict[SourceIdentity, str], str, str, int]:
    """读取并校验与冻结候补队列绑定的补充最终标签。

    Args:
        csv_path: 补充标注完成 CSV。
        manifest_path: 配对 supplemental manifest。
        queue_by_identity: 候补身份到队列项的映射，用于复核文本和任务身份。
        expected_label_guide_id: 当前冻结标签手册身份。

    Returns:
        候补身份到终止旅游标签的映射、重复决定摘要、队列摘要和已复核前缀末秩。

    Raises:
        ReferenceDatasetError: manifest 未 finalized、哈希/字段/身份/文本/标签不符。
    """

    csv_file = Path(csv_path)
    manifest_file = Path(manifest_path)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        with csv_file.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != SUPPLEMENTAL_LABEL_FIELDS:
                raise ReferenceDatasetError("supplemental_label_field_contract_mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, json.JSONDecodeError, csv.Error) as exc:
        raise ReferenceDatasetError("supplemental_label_artifact_unreadable") from exc
    expected_fields = set(SUPPLEMENTAL_LABEL_FIELDS)
    if any(
        set(row) != expected_fields or any(value is None for value in row.values())
        for row in rows
    ):
        raise ReferenceDatasetError("supplemental_label_row_structure_invalid")
    if (
        not isinstance(manifest, dict)
        or manifest.get("artifact_contract") != "final-reference-supplemental-labels"
        or manifest.get("status") != "finalized"
        or manifest.get("csv_sha256") != file_sha256(csv_file)
        or manifest.get("logical_name") != csv_file.name
        or manifest.get("row_count") != len(rows)
        or manifest.get("label_guide_id") != expected_label_guide_id
    ):
        raise ReferenceDatasetError("supplemental_label_manifest_invalid")
    task_payload: list[dict[str, object]] = []
    labels: dict[SourceIdentity, str] = {}
    for row in rows:
        try:
            identity = SourceIdentity(
                int(row["source_post_id"].strip()), int(row["source_version"].strip())
            )
        except (ValueError, ReferenceDatasetError) as exc:
            raise ReferenceDatasetError("supplemental_label_identity_invalid") from exc
        queue_item = queue_by_identity.get(identity)
        if queue_item is None:
            raise ReferenceDatasetError("supplemental_label_not_in_frozen_queue")
        post = queue_item.post
        if (
            row["review_status"].strip() != "finalized"
            or row["tourism_label"].strip()
            not in (set(ALLOWED_LABELS) | {"uncertain"})
        ):
            raise ReferenceDatasetError("supplemental_label_not_final")
        if (
            row["task_id"].strip() != post.task_id
            or row["normalized_model_text"] != post.normalized_model_text
            or row["selection_rank"].strip() != str(queue_item.queue_rank)
        ):
            raise ReferenceDatasetError("supplemental_label_queue_identity_mismatch")
        if identity in labels:
            raise ReferenceDatasetError("supplemental_label_identity_not_unique")
        labels[identity] = row["tourism_label"].strip()
        task_payload.append(
            {
                "identity": identity.as_list(),
                "task_id": row["task_id"],
                "queue_rank": queue_item.queue_rank,
                "normalized_sha256": post.normalized_sha256,
            }
        )
    if manifest.get("task_sha256") != canonical_sha256(task_payload):
        raise ReferenceDatasetError("supplemental_label_task_hash_mismatch")
    duplicate_hash = manifest.get("duplicate_decision_sha256")
    queue_hash = manifest.get("replacement_queue_sha256")
    reviewed_max_queue_rank = manifest.get("reviewed_max_queue_rank")
    if not all(
        isinstance(value, str) and len(value) == 64
        for value in (duplicate_hash, queue_hash)
    ):
        raise ReferenceDatasetError("supplemental_label_lineage_hash_missing")
    if (
        isinstance(reviewed_max_queue_rank, bool)
        or not isinstance(reviewed_max_queue_rank, int)
        or reviewed_max_queue_rank < 0
    ):
        raise ReferenceDatasetError("supplemental_label_reviewed_prefix_invalid")
    if any(item.queue_rank > reviewed_max_queue_rank for item in queue_by_identity.values() if item.post.identity in labels):
        raise ReferenceDatasetError("supplemental_label_outside_reviewed_prefix")
    return labels, duplicate_hash, queue_hash, reviewed_max_queue_rank


def finalize_reference_stage(
    context: ReferenceWorkflowContext,
    *,
    duplicate_decisions_csv: str | Path,
    duplicate_decisions_manifest: str | Path,
    label_resolutions_csv: str | Path,
    label_resolutions_manifest: str | Path,
    replacement_duplicate_decisions_csv: str | Path,
    replacement_duplicate_decisions_manifest: str | Path,
    replacement_label_resolutions_csv: str | Path,
    replacement_label_resolutions_manifest: str | Path,
    supplemental_labels_csv: str | Path,
    supplemental_labels_manifest: str | Path,
    output_csv: str | Path,
    output_manifest: str | Path,
) -> ArtifactPairResult:
    """组装并封存唯一最终700条不重复建模参考集。

    Args:
        context: 旧证据、只读数据库和冻结规则身份。
        duplicate_decisions_csv: 初始700条的人工最终重复决定。
        duplicate_decisions_manifest: 与初始决定唯一配对的 finalized manifest。
        label_resolutions_csv: 初始重复分量标签冲突完成 CSV。
        label_resolutions_manifest: 唯一配对 finalized manifest。
        replacement_duplicate_decisions_csv: 候补与初始全部谱系/同轮候补的人工决定；
            没有阈值候选时也须提供已封存的空完成文件。
        replacement_duplicate_decisions_manifest: 与候补决定配对的 finalized
            manifest；存在候补决定 CSV 时必填。
        replacement_label_resolutions_csv: 候补重复分量标签冲突完成 CSV。
        replacement_label_resolutions_manifest: 候补冲突配对 finalized manifest。
        supplemental_labels_csv: 已 finalized 的补充旅游相关性标签 CSV。
        supplemental_labels_manifest: 与补充标签 CSV 配对的 manifest。
        output_csv: 唯一最终权威 CSV。
        output_manifest: 唯一配对 finalized manifest。

    Returns:
        最终配对 artifact 的哈希和固定700条计数。

    Raises:
        ReferenceDatasetError: 任一人工步骤未关闭、候补耗尽或最终契约不满足。
    """

    legacy = load_legacy_reference_input(
        context.legacy_csv,
        context.legacy_manifest,
        context.derived_db,
        expected_label_guide_id=context.label_guide_id,
        normalization_rule_id=context.normalization_rule_id,
    )
    candidates = compute_duplicate_candidates(legacy.posts)
    decisions = validate_duplicate_decision_artifact(
        duplicate_decisions_csv,
        duplicate_decisions_manifest,
        expected_input_member_sha256=candidates.input_member_sha256,
        expected_candidate_sha256=candidates.candidate_sha256,
        expected_input_hashes={
            "legacy_csv_sha256": legacy.csv_sha256,
            "legacy_manifest_sha256": legacy.manifest_sha256,
        },
    )
    resolution_artifact = validate_label_resolution_artifact(
        label_resolutions_csv,
        label_resolutions_manifest,
        expected_duplicate_decision_sha256=file_sha256(
            duplicate_decisions_manifest
        ),
        expected_component_member_sha256=candidates.input_member_sha256,
        expected_label_guide_id=context.label_guide_id,
    )
    plan = build_duplicate_component_plan(
        legacy.posts,
        candidates.candidates,
        decisions,
        label_resolutions=resolution_artifact.resolutions,
        require_all_candidates_finalized=True,
    )
    population = load_candidate_population(
        context.derived_db, candidate_build_id=legacy.candidate_build_id
    )
    queue = build_replacement_queue(
        population,
        blocked_identities=[post.identity for post in legacy.posts],
        random_seed=context.random_seed,
    )
    queue_by_identity = {item.post.identity: item for item in queue.items}
    (
        all_labels,
        supplemental_decision_hash,
        supplemental_queue_hash,
        reviewed_max_rank,
    ) = _load_supplemental_labels(
        supplemental_labels_csv,
        supplemental_labels_manifest,
        queue_by_identity=queue_by_identity,
        expected_label_guide_id=context.label_guide_id,
    )
    labels = {
        identity: label
        for identity, label in all_labels.items()
        if label in ALLOWED_LABELS
    }
    replacement_computation = compute_replacement_duplicate_candidates(
        plan.representatives,
        queue,
        max_queue_rank=reviewed_max_rank,
        lineage_posts=legacy.posts,
    )
    replacement_decisions = validate_duplicate_decision_artifact(
        replacement_duplicate_decisions_csv,
        replacement_duplicate_decisions_manifest,
        expected_input_member_sha256=replacement_computation.input_member_sha256,
        expected_candidate_sha256=replacement_computation.candidate_sha256,
        expected_input_hashes={
            "initial_duplicate_decision_sha256": plan.decision_sha256,
            "replacement_queue_sha256": queue.queue_sha256,
        },
        expected_reviewed_max_queue_rank=reviewed_max_rank,
    )
    replacement_resolution_artifact = validate_label_resolution_artifact(
        replacement_label_resolutions_csv,
        replacement_label_resolutions_manifest,
        expected_duplicate_decision_sha256=file_sha256(
            replacement_duplicate_decisions_manifest
        ),
        expected_component_member_sha256=replacement_computation.input_member_sha256,
        expected_label_guide_id=context.label_guide_id,
    )
    selection = select_replacements(
        plan.representatives,
        queue,
        labels=labels,
        duplicate_decisions=replacement_decisions,
        max_queue_rank=reviewed_max_rank,
        label_resolutions=replacement_resolution_artifact.resolutions,
        lineage_posts=legacy.posts,
        initial_component_by_identity=plan.component_by_identity,
    )
    if supplemental_queue_hash != queue.queue_sha256:
        raise ReferenceDatasetError("supplemental_label_queue_hash_mismatch")
    if supplemental_decision_hash != selection.duplicate_decision_sha256:
        raise ReferenceDatasetError("supplemental_label_duplicate_hash_mismatch")
    return write_final_reference_artifacts(
        selection,
        plan,
        output_csv,
        output_manifest,
        legacy_input=legacy,
        initial_candidate_computation=candidates,
        initial_duplicate_decision_manifest_sha256=file_sha256(
            duplicate_decisions_manifest
        ),
        replacement_queue_sha256=queue.queue_sha256,
        label_guide_id=context.label_guide_id,
        candidate_build_id=legacy.candidate_build_id,
        normalization_rule_id=context.normalization_rule_id,
        supplemental_label_csv_sha256=file_sha256(supplemental_labels_csv),
        supplemental_label_manifest_sha256=file_sha256(
            supplemental_labels_manifest
        ),
        label_resolution_evidence_sha256=resolution_artifact.evidence_sha256,
        label_resolution_manifest_sha256=resolution_artifact.manifest_sha256,
        replacement_label_resolution_evidence_sha256=(
            replacement_resolution_artifact.evidence_sha256
        ),
        replacement_label_resolution_manifest_sha256=(
            replacement_resolution_artifact.manifest_sha256
        ),
        replacement_candidate_computation=replacement_computation,
        replacement_duplicate_decision_manifest_sha256=file_sha256(
            replacement_duplicate_decisions_manifest
        ),
    )
