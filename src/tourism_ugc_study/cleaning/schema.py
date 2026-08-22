"""文本清洗派生 SQLite 的连接约束与 3.2 建库契约。

所有 DDL 只作用于独立派生库；正式采集库始终只读。3.2 不兼容旧版派生
schema，旧派生库必须归档后从源快照重建。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path

DERIVED_SCHEMA_VERSION = 33
ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION = 33

_TEXT_ONLY_SCHEMA = r"""
CREATE TABLE source_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_path TEXT NOT NULL,
    source_identity_sha256 TEXT NOT NULL CHECK (length(source_identity_sha256) = 64),
    source_sha256_before TEXT NOT NULL CHECK (length(source_sha256_before) = 64),
    source_sha256_after TEXT NOT NULL CHECK (length(source_sha256_after) = 64),
    source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes >= 0),
    snapshot_path TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    snapshot_size_bytes INTEGER NOT NULL CHECK (snapshot_size_bytes >= 0),
    post_count INTEGER NOT NULL CHECK (post_count >= 0),
    table_counts_json TEXT NOT NULL CHECK (json_valid(table_counts_json)),
    object_manifest_sha256 TEXT NOT NULL CHECK (length(object_manifest_sha256) = 64),
    input_contract_status TEXT NOT NULL CHECK (input_contract_status IN ('accepted', 'rejected')),
    input_contract_method TEXT NOT NULL,
    input_contract_reason_code TEXT,
    input_contract_details_json TEXT NOT NULL CHECK (json_valid(input_contract_details_json)),
    manifest_path TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    created_at_asia_shanghai TEXT NOT NULL,
    code_version TEXT NOT NULL,
    environment_json TEXT NOT NULL CHECK (json_valid(environment_json))
);

CREATE TABLE inventory_discoveries (
    snapshot_id TEXT PRIMARY KEY REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL UNIQUE REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    post_changes_json TEXT NOT NULL CHECK (json_valid(post_changes_json)),
    tasks_created INTEGER NOT NULL CHECK (tasks_created >= 0),
    completed_at_utc TEXT NOT NULL
);

CREATE TABLE cleaning_batches (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'completed_with_blocks', 'failed')
    ),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    post_count INTEGER NOT NULL CHECK (post_count >= 0),
    task_count INTEGER NOT NULL CHECK (task_count > 0),
    frozen_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (run_id, sequence_number)
);

CREATE TABLE analysis_release_builds (
    release_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    release_mode TEXT NOT NULL CHECK (release_mode IN ('formal', 'smoke')),
    post_decision_build_id TEXT NOT NULL REFERENCES post_decision_builds(decision_build_id) ON DELETE RESTRICT,
    text_dedup_build_id TEXT NOT NULL REFERENCES text_dedup_builds(dedup_build_id) ON DELETE RESTRICT,
    text_keep_audit_evaluation_id TEXT NOT NULL REFERENCES text_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    protocol_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 33),
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    code_version TEXT NOT NULL,
    request_manifest_sha256 TEXT NOT NULL CHECK (length(request_manifest_sha256) = 64),
    posts_eligible_count INTEGER NOT NULL CHECK (posts_eligible_count >= 0),
    posts_deduplicated_count INTEGER NOT NULL CHECK (posts_deduplicated_count >= 0),
    release_manifest_sha256 TEXT NOT NULL CHECK (length(release_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized', 'accepted')),
    created_at_utc TEXT NOT NULL,
    finalized_at_utc TEXT,
    accepted_at_utc TEXT,
    UNIQUE (run_id, request_manifest_sha256),
    CHECK (
        (seal_status = 'building' AND finalized_at_utc IS NULL AND accepted_at_utc IS NULL)
        OR (seal_status = 'finalized' AND finalized_at_utc IS NOT NULL AND accepted_at_utc IS NULL)
        OR (seal_status = 'accepted' AND finalized_at_utc IS NOT NULL AND accepted_at_utc IS NOT NULL)
    )
);

CREATE TABLE analysis_release_manifests (
    release_id TEXT NOT NULL REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    manifest_kind TEXT NOT NULL CHECK (
        manifest_kind IN ('release', 'posts_eligible', 'posts_deduplicated', 'artifact')
    ),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    artifact_relative_path TEXT,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (release_id, manifest_kind)
);

CREATE TABLE analysis_posts_deduplicated (
    release_id TEXT NOT NULL REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    dedup_build_id TEXT NOT NULL REFERENCES text_dedup_builds(dedup_build_id) ON DELETE RESTRICT,
    cluster_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    PRIMARY KEY (release_id, source_post_id, source_version),
    UNIQUE (release_id, dedup_build_id, cluster_id),
    FOREIGN KEY (dedup_build_id, cluster_id)
        REFERENCES text_dedup_clusters(dedup_build_id, cluster_id) ON DELETE RESTRICT
);

CREATE TABLE analysis_posts_eligible (
    release_id TEXT NOT NULL REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    decision_id TEXT NOT NULL REFERENCES post_decisions(decision_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    PRIMARY KEY (release_id, source_post_id, source_version),
    UNIQUE (release_id, decision_id)
);

CREATE TABLE analysis_release_acceptance_attestations (
    release_id TEXT PRIMARY KEY
        REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    snapshot_size_bytes INTEGER NOT NULL CHECK (snapshot_size_bytes >= 0),
    snapshot_access_mode TEXT NOT NULL CHECK (snapshot_access_mode = 'mode=ro'),
    snapshot_query_only INTEGER NOT NULL CHECK (snapshot_query_only = 1),
    snapshot_integrity_check TEXT NOT NULL CHECK (snapshot_integrity_check = 'ok'),
    artifact_manifest_sha256 TEXT NOT NULL
        CHECK (length(artifact_manifest_sha256) = 64),
    attestation_sha256 TEXT NOT NULL UNIQUE CHECK (length(attestation_sha256) = 64),
    verified_at_utc TEXT NOT NULL,
    UNIQUE (release_id, run_id)
);

CREATE TABLE analysis_release_reports (
    release_id TEXT NOT NULL REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    report_kind TEXT NOT NULL CHECK (report_kind IN ('quality_summary', 'lineage')),
    report_json TEXT NOT NULL CHECK (json_valid(report_json)),
    report_sha256 TEXT NOT NULL CHECK (length(report_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (release_id, report_kind)
);

CREATE TABLE cleaning_batch_items (
    batch_id TEXT NOT NULL REFERENCES cleaning_batches(batch_id) ON DELETE RESTRICT,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    task_id TEXT NOT NULL UNIQUE REFERENCES stage_tasks(task_id) ON DELETE RESTRICT,
    object_type TEXT NOT NULL CHECK (object_type = 'post'),
    source_object_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    stage_name TEXT NOT NULL,
    stage_version TEXT NOT NULL,
    PRIMARY KEY (batch_id, item_index)
);

CREATE TABLE cleaning_runs (
    run_id TEXT PRIMARY KEY,
    protocol_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    random_seed INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('planned', 'running', 'paused', 'accepted', 'failed',
                   'aborted', 'input_rejected')
    ),
    reason_code TEXT,
    code_version TEXT NOT NULL,
    environment_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
, "run_type" TEXT NOT NULL DEFAULT 'incremental' CHECK (run_type IN ('full', 'incremental', 'reprocess')), "source_snapshot_id" TEXT REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT, "started_at_utc" TEXT, "finished_at_utc" TEXT);

CREATE TABLE post_decision_builds (
    decision_build_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    build_kind TEXT NOT NULL CHECK (build_kind IN ('candidate', 'final')),
    source_candidate_decision_build_id TEXT
        REFERENCES post_decision_builds(decision_build_id) ON DELETE RESTRICT,
    text_keep_audit_evaluation_id TEXT
        REFERENCES text_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    decision_version TEXT NOT NULL,
    guide_version TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL CHECK (length(rules_sha256) = 64),
    input_manifest_sha256 TEXT NOT NULL CHECK (length(input_manifest_sha256) = 64),
    expected_post_count INTEGER NOT NULL CHECK (expected_post_count >= 0),
    keep_count INTEGER NOT NULL CHECK (keep_count >= 0),
    review_count INTEGER NOT NULL CHECK (review_count >= 0),
    exclude_count INTEGER NOT NULL CHECK (exclude_count >= 0),
    decision_manifest_sha256 TEXT NOT NULL CHECK (length(decision_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (run_id, source_snapshot_id, build_kind, decision_version, input_manifest_sha256),
    CHECK (keep_count + review_count + exclude_count = expected_post_count),
    CHECK (
        (build_kind = 'candidate'
         AND source_candidate_decision_build_id IS NULL
         AND text_keep_audit_evaluation_id IS NULL)
        OR
        (build_kind = 'final'
         AND source_candidate_decision_build_id IS NOT NULL
         AND source_candidate_decision_build_id != decision_build_id
         AND text_keep_audit_evaluation_id IS NOT NULL)
    )
);

CREATE TABLE post_decision_evidence_links (
    decision_id TEXT NOT NULL REFERENCES post_decisions(decision_id) ON DELETE RESTRICT,
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN ('deterministic_result', 'human_annotation',
                          'human_adjudication', 'model_prediction')
    ),
    evidence_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    PRIMARY KEY (decision_id, evidence_kind, evidence_id)
);

CREATE TABLE post_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_build_id TEXT NOT NULL
        REFERENCES post_decision_builds(decision_build_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL
        REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain')
    ),
    decision_action TEXT NOT NULL CHECK (decision_action IN ('keep', 'review', 'exclude')),
    reason_code TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (
        provenance IN ('human_adjudication', 'model_low_risk', 'model_review_candidate',
                       'insufficient_evidence', 'evidence_conflict')
    ),
    model_run_id TEXT REFERENCES text_model_runs(model_run_id) ON DELETE RESTRICT,
    evidence_manifest_sha256 TEXT NOT NULL CHECK (length(evidence_manifest_sha256) = 64),
    decision_sha256 TEXT NOT NULL CHECK (length(decision_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (decision_build_id, source_post_id, source_version),
    CHECK (
        (provenance = 'human_adjudication'
         AND tourism_label IN ('related', 'unrelated', 'uncertain')
         AND decision_action = CASE tourism_label
             WHEN 'related' THEN 'keep'
             WHEN 'unrelated' THEN 'exclude'
             ELSE 'review' END
         AND model_run_id IS NULL)
        OR
        (provenance = 'model_low_risk'
         AND tourism_label = 'related'
         AND decision_action IN ('keep', 'review') AND model_run_id IS NOT NULL)
        OR
        (provenance = 'model_review_candidate'
         AND tourism_label = 'uncertain'
         AND decision_action = 'review' AND model_run_id IS NOT NULL)
        OR
        (provenance IN ('insufficient_evidence', 'evidence_conflict')
         AND decision_action = 'review' AND model_run_id IS NULL)
    )
);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at_utc TEXT NOT NULL
);

CREATE TABLE source_post_inventory (
    source_post_id INTEGER PRIMARY KEY,
    platform_key TEXT NOT NULL,
    first_seen_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    last_seen_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    missing_since_snapshot_id TEXT REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    is_present INTEGER NOT NULL CHECK (is_present IN (0, 1)),
    current_source_version INTEGER NOT NULL CHECK (current_source_version > 0),
    current_text_sha256 TEXT NOT NULL CHECK (length(current_text_sha256) = 64),
    current_author_sha256 TEXT NOT NULL CHECK (length(current_author_sha256) = 64),
    current_analysis_sha256 TEXT NOT NULL CHECK (length(current_analysis_sha256) = 64),
    captured_at_sort TEXT,
    updated_at_utc TEXT NOT NULL
, "current_author_identity_present" INTEGER NOT NULL DEFAULT 0 CHECK (current_author_identity_present IN (0, 1)));

CREATE TABLE source_post_observations (
    snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL,
    change_kind TEXT NOT NULL CHECK (
        change_kind IN ('new', 'cleaning_changed', 'analysis_only', 'unchanged', 'missing')
    ),
    changed_axes_json TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, source_post_id),
    FOREIGN KEY (source_post_id, source_version)
        REFERENCES source_post_versions(source_post_id, source_version) ON DELETE RESTRICT
);

CREATE TABLE source_post_versions (
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    effective_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    author_sha256 TEXT NOT NULL CHECK (length(author_sha256) = 64),
    analysis_sha256 TEXT NOT NULL CHECK (length(analysis_sha256) = 64),
    created_at_utc TEXT NOT NULL, "author_identity_present" INTEGER NOT NULL DEFAULT 0 CHECK (author_identity_present IN (0, 1)),
    PRIMARY KEY (source_post_id, source_version)
);

CREATE TABLE stage_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    batch_id TEXT REFERENCES cleaning_batches(batch_id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES stage_tasks(task_id) ON DELETE RESTRICT,
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason_code TEXT,
    actor_sha256 TEXT CHECK (actor_sha256 IS NULL OR length(actor_sha256) = 64),
    error_summary TEXT,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE stage_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    batch_id TEXT REFERENCES cleaning_batches(batch_id) ON DELETE RESTRICT,
    stage_name TEXT NOT NULL,
    object_type TEXT NOT NULL CHECK (object_type = 'post'),
    source_object_id INTEGER NOT NULL,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    stage_version TEXT NOT NULL,
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'blocked', 'skipped')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    claimed_at_utc TEXT,
    heartbeat_at_utc TEXT,
    completed_at_utc TEXT,
    error_code TEXT,
    error_summary TEXT,
    output_sha256 TEXT CHECK (output_sha256 IS NULL OR length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (run_id, stage_name, object_type, source_object_id, source_version, stage_version)
);

CREATE TABLE text_annotation_imports (
    import_id TEXT PRIMARY KEY,
    record_kind TEXT NOT NULL CHECK (
        record_kind IN ('post_annotation', 'post_final_review',
                        'duplicate_annotation', 'duplicate_final_review')
    ),
    guide_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    imported_by_hash TEXT NOT NULL CHECK (length(imported_by_hash) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (record_kind, source_sha256)
);

CREATE TABLE text_candidate_builds (
    build_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    stage_version TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL CHECK (length(rules_sha256) = 64),
    runtime_sha256 TEXT NOT NULL CHECK (length(runtime_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('building', 'finalized')),
    corpus_manifest_sha256 TEXT NOT NULL CHECK (length(corpus_manifest_sha256) = 64),
    expected_post_count INTEGER NOT NULL CHECK (expected_post_count >= 0),
    processed_post_count INTEGER NOT NULL CHECK (processed_post_count >= 0),
    usable_post_count INTEGER NOT NULL CHECK (usable_post_count >= 0),
    is_complete_corpus INTEGER NOT NULL CHECK (is_complete_corpus IN (0, 1)),
    exact_cluster_count INTEGER NOT NULL CHECK (exact_cluster_count >= 0),
    exact_duplicate_cluster_count INTEGER NOT NULL CHECK (exact_duplicate_cluster_count >= 0),
    exact_cross_platform_cluster_count INTEGER NOT NULL CHECK (
        exact_cross_platform_cluster_count >= 0
    ),
    exact_cross_platform_member_count INTEGER NOT NULL CHECK (
        exact_cross_platform_member_count >= 0
    ),
    near_candidate_pair_count INTEGER NOT NULL CHECK (near_candidate_pair_count >= 0),
    near_cross_platform_candidate_pair_count INTEGER NOT NULL CHECK (
        near_cross_platform_candidate_pair_count >= 0
    ),
    near_candidate_component_count INTEGER NOT NULL CHECK (
        near_candidate_component_count >= 0
    ),
    library_versions_json TEXT NOT NULL,
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (run_id, source_snapshot_id, stage_version, corpus_manifest_sha256)
);

CREATE TABLE text_candidate_corpus_members (
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES text_deterministic_results(task_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    structure_status TEXT NOT NULL CHECK (structure_status IN ('usable', 'invalid', 'uncertain')),
    exact_cluster_id TEXT,
    is_near_representative INTEGER NOT NULL CHECK (is_near_representative IN (0, 1)),
    PRIMARY KEY (build_id, source_post_id, source_version),
    UNIQUE (build_id, task_id),
    FOREIGN KEY (build_id, exact_cluster_id)
        REFERENCES text_exact_clusters(build_id, cluster_id) ON DELETE RESTRICT
);

CREATE TABLE text_dataset_splits (
    model_run_id TEXT NOT NULL REFERENCES text_model_runs(model_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    component_id TEXT NOT NULL,
    split_name TEXT NOT NULL CHECK (split_name IN ('train', 'validation', 'test')),
    PRIMARY KEY (model_run_id, source_post_id, source_version)
);

CREATE TABLE text_dedup_builds (
    dedup_build_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    post_decision_build_id TEXT NOT NULL
        REFERENCES post_decision_builds(decision_build_id) ON DELETE RESTRICT,
    candidate_build_id TEXT NOT NULL
        REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    dedup_version TEXT NOT NULL,
    selection_strategy TEXT NOT NULL,
    expected_eligible_count INTEGER NOT NULL CHECK (expected_eligible_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    cluster_count INTEGER NOT NULL CHECK (cluster_count >= 0),
    member_count INTEGER NOT NULL CHECK (member_count >= 0),
    representative_count INTEGER NOT NULL CHECK (representative_count >= 0),
    input_manifest_sha256 TEXT NOT NULL CHECK (length(input_manifest_sha256) = 64),
    member_manifest_sha256 TEXT NOT NULL CHECK (length(member_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (post_decision_build_id, candidate_build_id, dedup_version, input_manifest_sha256),
    CHECK (member_count = expected_eligible_count),
    CHECK (representative_count = cluster_count)
);

CREATE TABLE text_dedup_clusters (
    dedup_build_id TEXT NOT NULL
        REFERENCES text_dedup_builds(dedup_build_id) ON DELETE RESTRICT,
    cluster_id TEXT NOT NULL,
    representative_source_post_id INTEGER NOT NULL,
    representative_source_version INTEGER NOT NULL CHECK (representative_source_version > 0),
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    representative_reason_code TEXT NOT NULL,
    member_manifest_sha256 TEXT NOT NULL CHECK (length(member_manifest_sha256) = 64),
    PRIMARY KEY (dedup_build_id, cluster_id)
);

CREATE TABLE text_dedup_edges (
    edge_id TEXT PRIMARY KEY,
    dedup_build_id TEXT NOT NULL
        REFERENCES text_dedup_builds(dedup_build_id) ON DELETE RESTRICT,
    left_source_post_id INTEGER NOT NULL,
    left_source_version INTEGER NOT NULL CHECK (left_source_version > 0),
    right_source_post_id INTEGER NOT NULL,
    right_source_version INTEGER NOT NULL CHECK (right_source_version > 0),
    relation_kind TEXT NOT NULL CHECK (relation_kind IN ('exact', 'human_duplicate')),
    exact_cluster_id TEXT,
    adjudication_id TEXT
        REFERENCES text_near_duplicate_adjudications(adjudication_id) ON DELETE RESTRICT,
    reason_code TEXT NOT NULL,
    edge_sha256 TEXT NOT NULL CHECK (length(edge_sha256) = 64),
    UNIQUE (dedup_build_id, left_source_post_id, left_source_version,
            right_source_post_id, right_source_version),
    CHECK (left_source_post_id < right_source_post_id),
    CHECK (
        (relation_kind = 'exact' AND exact_cluster_id IS NOT NULL AND adjudication_id IS NULL)
        OR
        (relation_kind = 'human_duplicate' AND exact_cluster_id IS NULL
         AND adjudication_id IS NOT NULL)
    )
);

CREATE TABLE text_dedup_members (
    dedup_build_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    is_representative INTEGER NOT NULL CHECK (is_representative IN (0, 1)),
    selection_reason_code TEXT NOT NULL,
    PRIMARY KEY (dedup_build_id, source_post_id, source_version),
    UNIQUE (dedup_build_id, cluster_id, source_post_id, source_version),
    FOREIGN KEY (dedup_build_id, cluster_id)
        REFERENCES text_dedup_clusters(dedup_build_id, cluster_id) ON DELETE RESTRICT
);

CREATE TABLE text_deterministic_results (
    task_id TEXT PRIMARY KEY REFERENCES stage_tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    stage_version TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL CHECK (length(rules_sha256) = 64),
    runtime_versions_json TEXT NOT NULL,
    runtime_sha256 TEXT NOT NULL CHECK (length(runtime_sha256) = 64),
    structure_status TEXT NOT NULL CHECK (structure_status IN ('usable', 'invalid', 'uncertain')),
    structure_reason_code TEXT NOT NULL,
    structure_evidence_json TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    normalized_body TEXT NOT NULL,
    normalized_model_text TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL CHECK (length(normalized_sha256) = 64),
    exact_canonical_sha256 TEXT CHECK (
        exact_canonical_sha256 IS NULL OR length(exact_canonical_sha256) = 64
    ),
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (run_id, source_post_id, source_version, stage_version)
);

CREATE TABLE text_exact_cluster_members (
    build_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    is_representative INTEGER NOT NULL CHECK (is_representative IN (0, 1)),
    PRIMARY KEY (build_id, cluster_id, source_post_id, source_version),
    FOREIGN KEY (build_id, cluster_id)
        REFERENCES text_exact_clusters(build_id, cluster_id) ON DELETE RESTRICT
);

CREATE TABLE text_exact_clusters (
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    cluster_id TEXT NOT NULL,
    exact_canonical_sha256 TEXT NOT NULL CHECK (length(exact_canonical_sha256) = 64),
    representative_source_post_id INTEGER NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    is_cross_platform INTEGER NOT NULL CHECK (is_cross_platform IN (0, 1)),
    PRIMARY KEY (build_id, cluster_id),
    UNIQUE (build_id, exact_canonical_sha256)
);

CREATE TABLE text_keep_audit_annotations (
    audit_annotation_id TEXT PRIMARY KEY,
    audit_round_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    guide_version TEXT NOT NULL,
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (audit_round_id, source_post_id, source_version),
    FOREIGN KEY (audit_round_id, source_post_id, source_version)
        REFERENCES text_keep_audit_members(
            audit_round_id, source_post_id, source_version
        ) ON DELETE RESTRICT
);

CREATE TABLE text_keep_audit_evaluation_evidence_links (
    audit_evaluation_id TEXT NOT NULL
        REFERENCES text_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    audit_annotation_id TEXT NOT NULL
        REFERENCES text_keep_audit_annotations(audit_annotation_id) ON DELETE RESTRICT,
    PRIMARY KEY (audit_evaluation_id, audit_annotation_id)
);

CREATE TABLE text_keep_audit_evaluations (
    audit_evaluation_id TEXT PRIMARY KEY,
    audit_round_id TEXT NOT NULL
        REFERENCES text_keep_audit_rounds(audit_round_id) ON DELETE RESTRICT,
    completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
    event_count INTEGER NOT NULL CHECK (event_count >= 0),
    event_point_estimate REAL NOT NULL CHECK (
        event_point_estimate >= 0 AND event_point_estimate <= 1
    ),
    one_sided_upper REAL NOT NULL CHECK (one_sided_upper >= 0 AND one_sided_upper <= 1),
    evaluation_status TEXT NOT NULL CHECK (evaluation_status IN ('passed', 'failed')),
    reason_code TEXT NOT NULL,
    evidence_manifest_sha256 TEXT NOT NULL CHECK (length(evidence_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (audit_round_id)
);

CREATE TABLE text_keep_audit_members (
    audit_round_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    stratum_rank INTEGER NOT NULL CHECK (stratum_rank > 0),
    inclusion_probability REAL NOT NULL CHECK (
        inclusion_probability > 0 AND inclusion_probability <= 1
    ),
    sampling_weight REAL NOT NULL CHECK (sampling_weight >= 1),
    PRIMARY KEY (audit_round_id, source_post_id, source_version),
    UNIQUE (audit_round_id, platform_key, stratum_rank),
    FOREIGN KEY (audit_round_id, source_post_id, source_version)
        REFERENCES text_keep_audit_population_members(
            audit_round_id, source_post_id, source_version
        ) ON DELETE RESTRICT
);

CREATE TABLE text_keep_audit_platform_evaluations (
    audit_evaluation_id TEXT NOT NULL
        REFERENCES text_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    platform_key TEXT NOT NULL,
    population_count INTEGER NOT NULL CHECK (population_count > 0),
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
    event_count INTEGER NOT NULL CHECK (event_count >= 0),
    event_point_estimate REAL,
    PRIMARY KEY (audit_evaluation_id, platform_key),
    CHECK (
        (sample_count = 0 AND completed_count = 0 AND event_count = 0
         AND event_point_estimate IS NULL)
        OR
        (sample_count > 0 AND completed_count = sample_count
         AND event_count <= completed_count
         AND event_point_estimate >= 0 AND event_point_estimate <= 1)
    )
);

CREATE TABLE text_keep_audit_population_members (
    audit_round_id TEXT NOT NULL
        REFERENCES text_keep_audit_rounds(audit_round_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    PRIMARY KEY (audit_round_id, source_post_id, source_version)
);

CREATE TABLE text_keep_audit_rounds (
    audit_round_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    candidate_decision_build_id TEXT NOT NULL
        REFERENCES post_decision_builds(decision_build_id) ON DELETE RESTRICT,
    audit_mode TEXT NOT NULL CHECK (audit_mode IN ('formal', 'smoke')),
    round_number INTEGER NOT NULL CHECK (round_number > 0),
    random_seed INTEGER NOT NULL,
    sampling_method TEXT NOT NULL CHECK (
        sampling_method IN ('census', 'platform_stratified_equal_probability')
    ),
    estimator TEXT NOT NULL CHECK (estimator IN ('census', 'wilson_one_sided_95')),
    population_count INTEGER NOT NULL CHECK (population_count >= 0),
    population_manifest_sha256 TEXT NOT NULL CHECK (length(population_manifest_sha256) = 64),
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    sample_manifest_sha256 TEXT NOT NULL CHECK (length(sample_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (candidate_decision_build_id, round_number),
    UNIQUE (population_manifest_sha256),
    CHECK (
        (population_count < 300 AND sampling_method = 'census'
         AND estimator = 'census' AND sample_count = population_count)
        OR
        (population_count >= 300 AND sample_count >= 300 AND sample_count <= population_count
         AND ((sampling_method = 'census' AND estimator = 'census'
               AND sample_count = population_count)
              OR
              (sampling_method = 'platform_stratified_equal_probability'
               AND estimator = 'wilson_one_sided_95')))
    )
);

CREATE TABLE text_leakage_builds (
    leakage_build_id TEXT PRIMARY KEY,
    candidate_build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    adjudication_manifest_sha256 TEXT NOT NULL CHECK (length(adjudication_manifest_sha256) = 64),
    input_post_count INTEGER NOT NULL CHECK (input_post_count >= 0),
    component_count INTEGER NOT NULL CHECK (component_count >= 0),
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL, "seal_status" TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized')),
    UNIQUE (candidate_build_id, adjudication_manifest_sha256)
);

CREATE TABLE text_leakage_members (
    leakage_build_id TEXT NOT NULL REFERENCES text_leakage_builds(leakage_build_id) ON DELETE RESTRICT,
    component_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    author_edge_used INTEGER NOT NULL CHECK (author_edge_used IN (0, 1)),
    exact_edge_used INTEGER NOT NULL CHECK (exact_edge_used IN (0, 1)),
    confirmed_near_edge_used INTEGER NOT NULL CHECK (confirmed_near_edge_used IN (0, 1)),
    PRIMARY KEY (leakage_build_id, source_post_id, source_version)
);

CREATE TABLE text_model_predictions (
    model_run_id TEXT NOT NULL REFERENCES text_model_runs(model_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    margin REAL NOT NULL,
    suggested_action TEXT NOT NULL CHECK (
        suggested_action IN ('high_risk_review', 'manual_review',
                             'low_risk_keep_candidate')
    ),
    requires_human_review INTEGER NOT NULL CHECK (requires_human_review IN (0, 1)),
    low_risk_audit_selected INTEGER NOT NULL CHECK (low_risk_audit_selected IN (0, 1)),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (model_run_id, source_post_id, source_version),
    CHECK (low_risk_audit_selected = 0 OR requires_human_review = 1)
);

CREATE TABLE text_model_runs (
    model_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    candidate_build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    leakage_build_id TEXT NOT NULL REFERENCES text_leakage_builds(leakage_build_id) ON DELETE RESTRICT,
    guide_version TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    gold_manifest_sha256 TEXT NOT NULL CHECK (length(gold_manifest_sha256) = 64),
    split_manifest_sha256 TEXT NOT NULL CHECK (length(split_manifest_sha256) = 64),
    train_count INTEGER NOT NULL CHECK (train_count > 0),
    validation_count INTEGER NOT NULL CHECK (validation_count > 0),
    test_count INTEGER NOT NULL CHECK (test_count > 0),
    chosen_c REAL NOT NULL CHECK (chosen_c > 0),
    high_risk_threshold REAL,
    low_risk_threshold REAL,
    low_risk_enabled INTEGER NOT NULL CHECK (low_risk_enabled IN (0, 1)),
    metrics_json TEXT NOT NULL,
    model_artifact_path TEXT NOT NULL,
    model_artifact_sha256 TEXT NOT NULL CHECK (length(model_artifact_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('completed', 'smoke')),
    created_at_utc TEXT NOT NULL
, "seal_status" TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized')), "expected_prediction_count" INTEGER NOT NULL DEFAULT 0 CHECK (expected_prediction_count >= 0), "train_manifest_sha256" TEXT CHECK (train_manifest_sha256 IS NULL OR length(train_manifest_sha256) = 64), "validation_manifest_sha256" TEXT CHECK (validation_manifest_sha256 IS NULL OR length(validation_manifest_sha256) = 64), "test_manifest_sha256" TEXT CHECK (test_manifest_sha256 IS NULL OR length(test_manifest_sha256) = 64), "prediction_manifest_sha256" TEXT CHECK (prediction_manifest_sha256 IS NULL OR length(prediction_manifest_sha256) = 64), "request_manifest_sha256" TEXT CHECK (request_manifest_sha256 IS NULL OR length(request_manifest_sha256) = 64), "candidate_prediction_manifest_sha256" TEXT CHECK (candidate_prediction_manifest_sha256 IS NULL OR length(candidate_prediction_manifest_sha256) = 64));

CREATE TABLE text_near_candidate_component_members (
    build_id TEXT NOT NULL,
    component_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    is_cluster_representative INTEGER NOT NULL CHECK (is_cluster_representative IN (0, 1)),
    PRIMARY KEY (build_id, component_id, cluster_id, source_post_id, source_version),
    FOREIGN KEY (build_id, component_id)
        REFERENCES text_near_candidate_components(build_id, component_id) ON DELETE RESTRICT,
    FOREIGN KEY (build_id, cluster_id)
        REFERENCES text_exact_clusters(build_id, cluster_id) ON DELETE RESTRICT
);

CREATE TABLE text_near_candidate_components (
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    component_id TEXT NOT NULL,
    representative_count INTEGER NOT NULL CHECK (representative_count > 0),
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    is_cross_platform INTEGER NOT NULL CHECK (is_cross_platform IN (0, 1)),
    PRIMARY KEY (build_id, component_id)
);

CREATE TABLE text_near_candidate_pairs (
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    left_cluster_id TEXT NOT NULL,
    right_cluster_id TEXT NOT NULL,
    left_source_post_id INTEGER NOT NULL,
    right_source_post_id INTEGER NOT NULL,
    similarity_ppm INTEGER NOT NULL CHECK (similarity_ppm BETWEEN 0 AND 1000000),
    length_ratio_ppm INTEGER NOT NULL CHECK (length_ratio_ppm BETWEEN 0 AND 1000000),
    shared_block_key_count INTEGER NOT NULL CHECK (shared_block_key_count > 0),
    is_cross_platform INTEGER NOT NULL CHECK (is_cross_platform IN (0, 1)),
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (build_id, left_cluster_id, right_cluster_id),
    CHECK (left_cluster_id < right_cluster_id),
    FOREIGN KEY (build_id, left_cluster_id)
        REFERENCES text_exact_clusters(build_id, cluster_id) ON DELETE RESTRICT,
    FOREIGN KEY (build_id, right_cluster_id)
        REFERENCES text_exact_clusters(build_id, cluster_id) ON DELETE RESTRICT
);

CREATE TABLE text_near_duplicate_adjudications (
    adjudication_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    left_cluster_id TEXT NOT NULL,
    right_cluster_id TEXT NOT NULL,
    adjudicator_hash TEXT NOT NULL CHECK (length(adjudicator_hash) = 64),
    decision TEXT NOT NULL CHECK (decision IN ('duplicate', 'not_duplicate', 'uncertain')),
    reason_code TEXT NOT NULL,
    evidence_annotation_ids_json TEXT NOT NULL,
    guide_version TEXT NOT NULL,
    adjudicated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    CHECK (left_cluster_id < right_cluster_id),
    FOREIGN KEY (build_id, left_cluster_id, right_cluster_id)
        REFERENCES text_near_candidate_pairs(build_id, left_cluster_id, right_cluster_id)
        ON DELETE RESTRICT
);

CREATE TABLE text_near_duplicate_annotations (
    annotation_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    left_cluster_id TEXT NOT NULL,
    right_cluster_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('duplicate', 'not_duplicate', 'uncertain')),
    reason_code TEXT NOT NULL,
    guide_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    CHECK (left_cluster_id < right_cluster_id),
    FOREIGN KEY (build_id, left_cluster_id, right_cluster_id)
        REFERENCES text_near_candidate_pairs(build_id, left_cluster_id, right_cluster_id)
        ON DELETE RESTRICT
);

CREATE TABLE text_periodic_review_window_members (
    sample_run_id TEXT NOT NULL REFERENCES text_periodic_review_windows(sample_run_id)
        ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id)
        ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    window_rank INTEGER NOT NULL CHECK (window_rank > 0),
    eligible_in_candidate_build INTEGER NOT NULL CHECK (
        eligible_in_candidate_build IN (0, 1)
    ),
    PRIMARY KEY (sample_run_id, source_post_id),
    UNIQUE (sample_run_id, window_rank)
);

CREATE TABLE text_periodic_review_windows (
    sample_run_id TEXT PRIMARY KEY REFERENCES text_sampling_runs(sample_run_id)
        ON DELETE RESTRICT,
    baseline_sample_run_id TEXT NOT NULL REFERENCES text_sampling_runs(sample_run_id)
        ON DELETE RESTRICT,
    candidate_build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id)
        ON DELETE RESTRICT,
    round_number INTEGER NOT NULL CHECK (round_number > 0),
    window_start_rank INTEGER NOT NULL CHECK (window_start_rank > 0),
    window_end_rank INTEGER NOT NULL CHECK (window_end_rank >= window_start_rank),
    new_post_count_at_freeze INTEGER NOT NULL CHECK (new_post_count_at_freeze >= 0),
    window_member_count INTEGER NOT NULL CHECK (window_member_count > 0),
    eligible_member_count INTEGER NOT NULL CHECK (eligible_member_count >= 0),
    member_manifest_sha256 TEXT NOT NULL CHECK (length(member_manifest_sha256) = 64),
    created_at_utc TEXT NOT NULL, "seal_status" TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized')),
    UNIQUE (baseline_sample_run_id, round_number)
);

CREATE TABLE text_post_adjudications (
    adjudication_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    sample_run_id TEXT REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    adjudicator_hash TEXT NOT NULL CHECK (length(adjudicator_hash) = 64),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    evidence_annotation_ids_json TEXT NOT NULL,
    decision_context TEXT NOT NULL CHECK (
        decision_context IN ('reference', 'model_review', 'manual_review')
    ),
    guide_version TEXT NOT NULL,
    adjudicated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    model_run_id TEXT
);

CREATE TABLE text_post_annotations (
    annotation_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    sample_run_id TEXT REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    guide_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (sample_run_id, source_post_id, source_version)
);

CREATE TABLE text_sample_members (
    sample_run_id TEXT NOT NULL REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    sample_frame TEXT NOT NULL CHECK (
        sample_frame IN ('probability', 'targeted', 'periodic_probability')
    ),
    selection_reason_code TEXT NOT NULL,
    selection_rank INTEGER NOT NULL CHECK (selection_rank > 0),
    inclusion_probability_ppm INTEGER CHECK (
        inclusion_probability_ppm IS NULL
        OR inclusion_probability_ppm BETWEEN 1 AND 1000000
    ),
    analysis_weight REAL CHECK (analysis_weight IS NULL OR analysis_weight >= 1.0),
    PRIMARY KEY (sample_run_id, source_post_id, sample_frame)
);

CREATE TABLE text_sampling_runs (
    sample_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    candidate_build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    baseline_sample_run_id TEXT REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    sample_kind TEXT NOT NULL CHECK (sample_kind IN ('initial', 'periodic_review')),
    guide_version TEXT NOT NULL,
    random_seed INTEGER NOT NULL,
    population_manifest_sha256 TEXT NOT NULL CHECK (length(population_manifest_sha256) = 64),
    population_count INTEGER NOT NULL CHECK (population_count >= 0),
    probability_count INTEGER NOT NULL CHECK (probability_count >= 0),
    targeted_count INTEGER NOT NULL CHECK (targeted_count >= 0),
    periodic_round_number INTEGER NOT NULL DEFAULT 0 CHECK (periodic_round_number >= 0),
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL, "seal_status" TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized')), "member_manifest_sha256" TEXT CHECK (member_manifest_sha256 IS NULL OR length(member_manifest_sha256) = 64),
    UNIQUE (candidate_build_id, sample_kind, periodic_round_number, random_seed)
);

CREATE INDEX idx_post_decisions_build_action
    ON post_decisions(decision_build_id, decision_action, source_post_id);

CREATE INDEX idx_post_observations_change
    ON source_post_observations(snapshot_id, change_kind);

CREATE INDEX idx_stage_tasks_algorithm_history
    ON stage_tasks(stage_name, object_type, source_object_id, stage_version);

CREATE INDEX idx_stage_tasks_batch_status
    ON stage_tasks(batch_id, status, stage_name);

CREATE INDEX idx_stage_tasks_unbatched
    ON stage_tasks(run_id, status, batch_id, source_post_id, source_object_id);

CREATE INDEX idx_text_adjudications_model_run
    ON text_post_adjudications(model_run_id, source_post_id, source_version);

CREATE INDEX idx_text_adjudications_post
    ON text_post_adjudications(source_post_id, source_version, guide_version);

CREATE INDEX idx_text_annotations_post
    ON text_post_annotations(source_post_id, source_version, guide_version);

CREATE INDEX idx_text_candidate_build_identity
    ON text_candidate_builds(run_id, source_snapshot_id, stage_version);

CREATE INDEX idx_text_dedup_members_cluster
    ON text_dedup_members(dedup_build_id, cluster_id, is_representative);

CREATE INDEX idx_text_duplicate_adjudications_pair
    ON text_near_duplicate_adjudications(build_id, left_cluster_id, right_cluster_id);

CREATE INDEX idx_text_keep_audit_population_platform
    ON text_keep_audit_population_members(audit_round_id, platform_key);

CREATE UNIQUE INDEX idx_text_model_request_manifest
                ON text_model_runs(request_manifest_sha256)
                WHERE request_manifest_sha256 IS NOT NULL
                ;

CREATE INDEX idx_text_results_exact
    ON text_deterministic_results(run_id, exact_canonical_sha256);

CREATE INDEX idx_text_results_snapshot_status
    ON text_deterministic_results(source_snapshot_id, stage_version, structure_status);

CREATE VIEW text_ready AS
SELECT b.run_id,
       b.decision_build_id,
       d.source_post_id,
       d.source_version,
       1 AS is_text_ready,
       1 AS is_provisional
FROM post_decision_builds AS b
JOIN post_decisions AS d ON d.decision_build_id = b.decision_build_id
WHERE b.seal_status = 'finalized'
  AND d.decision_action = 'keep';

CREATE VIEW v_post_cleaning_progress AS
SELECT run_id,
       source_post_id,
       CASE
           WHEN SUM(status = 'failed') > 0 THEN 'failed'
           WHEN SUM(status = 'blocked') > 0 THEN 'blocked'
           WHEN SUM(status = 'running') > 0 THEN 'processing'
           WHEN SUM(status = 'pending') > 0 THEN 'pending'
           WHEN SUM(required = 1 AND status NOT IN ('succeeded', 'skipped')) = 0
                THEN 'completed'
           ELSE 'needs_review'
       END AS progress_status
FROM stage_tasks
WHERE object_type = 'post'
GROUP BY run_id, source_post_id;

CREATE TRIGGER prevent_analysis_post_dedup_delete BEFORE DELETE ON analysis_posts_deduplicated
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;

CREATE TRIGGER prevent_analysis_post_dedup_update BEFORE UPDATE ON analysis_posts_deduplicated
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;

CREATE TRIGGER prevent_analysis_post_eligible_delete
BEFORE DELETE ON analysis_posts_eligible
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;

CREATE TRIGGER prevent_analysis_post_eligible_update
BEFORE UPDATE ON analysis_posts_eligible
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;

CREATE TRIGGER prevent_analysis_release_attestation_delete
BEFORE DELETE ON analysis_release_acceptance_attestations
BEGIN SELECT RAISE(ABORT, 'analysis release acceptance attestations are immutable'); END;

CREATE TRIGGER prevent_analysis_release_attestation_update
BEFORE UPDATE ON analysis_release_acceptance_attestations
BEGIN SELECT RAISE(ABORT, 'analysis release acceptance attestations are immutable'); END;

CREATE TRIGGER prevent_analysis_release_manifest_delete BEFORE DELETE ON analysis_release_manifests
BEGIN SELECT RAISE(ABORT, 'analysis release manifests are immutable'); END;

CREATE TRIGGER prevent_analysis_release_manifest_update BEFORE UPDATE ON analysis_release_manifests
BEGIN SELECT RAISE(ABORT, 'analysis release manifests are immutable'); END;

CREATE TRIGGER prevent_analysis_release_report_delete BEFORE DELETE ON analysis_release_reports
BEGIN SELECT RAISE(ABORT, 'analysis release reports are immutable'); END;

CREATE TRIGGER prevent_analysis_release_report_update BEFORE UPDATE ON analysis_release_reports
BEGIN SELECT RAISE(ABORT, 'analysis release reports are immutable'); END;

CREATE TRIGGER prevent_batched_task_identity_update
BEFORE UPDATE OF run_id, stage_name, object_type, source_object_id,
                 source_post_id, source_version, stage_version, required,
                 max_attempts
ON stage_tasks
WHEN OLD.batch_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'batched task identity is immutable');
END;

CREATE TRIGGER prevent_cleaning_run_delete
BEFORE DELETE ON cleaning_runs
BEGIN
    SELECT RAISE(ABORT, 'cleaning runs are immutable');
END;

CREATE TRIGGER prevent_cleaning_run_identity_update
BEFORE UPDATE OF run_id, protocol_version, config_sha256, random_seed,
                 code_version, environment_json, created_at_utc, run_type
ON cleaning_runs
BEGIN
    SELECT RAISE(ABORT, 'cleaning run identity is immutable');
END;

CREATE TRIGGER prevent_finalized_corpus_member_insert
BEFORE INSERT ON text_candidate_corpus_members
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;

CREATE TRIGGER prevent_finalized_dataset_split_insert
BEFORE INSERT ON text_dataset_splits
WHEN EXISTS (SELECT 1 FROM text_model_runs
             WHERE model_run_id = NEW.model_run_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text model run rows are sealed');
END;

CREATE TRIGGER prevent_finalized_exact_cluster_insert
BEFORE INSERT ON text_exact_clusters
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;

CREATE TRIGGER prevent_finalized_exact_member_insert
BEFORE INSERT ON text_exact_cluster_members
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;

CREATE TRIGGER prevent_finalized_leakage_member_insert
BEFORE INSERT ON text_leakage_members
WHEN EXISTS (SELECT 1 FROM text_leakage_builds
             WHERE leakage_build_id = NEW.leakage_build_id
               AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text leakage build rows are sealed');
END;

CREATE TRIGGER prevent_finalized_leakage_parent_update
BEFORE UPDATE ON text_leakage_builds
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized text leakage builds are immutable');
END;

CREATE TRIGGER prevent_finalized_model_parent_update
BEFORE UPDATE ON text_model_runs
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized text model runs are immutable');
END;

CREATE TRIGGER prevent_finalized_model_prediction_insert
BEFORE INSERT ON text_model_predictions
WHEN EXISTS (SELECT 1 FROM text_model_runs
             WHERE model_run_id = NEW.model_run_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text model run rows are sealed');
END;

CREATE TRIGGER prevent_finalized_near_component_insert
BEFORE INSERT ON text_near_candidate_components
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;

CREATE TRIGGER prevent_finalized_near_component_member_insert
BEFORE INSERT ON text_near_candidate_component_members
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;

CREATE TRIGGER prevent_finalized_near_pair_insert
BEFORE INSERT ON text_near_candidate_pairs
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;

CREATE TRIGGER prevent_finalized_periodic_review_member_insert
BEFORE INSERT ON text_periodic_review_window_members
WHEN EXISTS (
    SELECT 1 FROM text_periodic_review_windows
    WHERE sample_run_id = NEW.sample_run_id AND seal_status = 'finalized'
)
BEGIN
    SELECT RAISE(ABORT, 'periodic review window rows are sealed');
END;

CREATE TRIGGER prevent_finalized_periodic_review_window_update
BEFORE UPDATE ON text_periodic_review_windows
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized periodic review windows are immutable');
END;

CREATE TRIGGER prevent_finalized_sample_member_insert
BEFORE INSERT ON text_sample_members
WHEN EXISTS (SELECT 1 FROM text_sampling_runs
             WHERE sample_run_id = NEW.sample_run_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text sampling run rows are sealed');
END;

CREATE TRIGGER prevent_finalized_sampling_parent_update
BEFORE UPDATE ON text_sampling_runs
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized text sampling runs are immutable');
END;

CREATE TRIGGER prevent_frozen_batch_item_delete
BEFORE DELETE ON cleaning_batch_items
BEGIN
    SELECT RAISE(ABORT, 'frozen batch items are immutable');
END;

CREATE TRIGGER prevent_frozen_batch_item_insert
BEFORE INSERT ON cleaning_batch_items
WHEN EXISTS (
    SELECT 1 FROM cleaning_batches
    WHERE batch_id = NEW.batch_id AND frozen_at_utc != ''
)
BEGIN
    SELECT RAISE(ABORT, 'frozen batch items are immutable');
END;

CREATE TRIGGER prevent_frozen_batch_item_update
BEFORE UPDATE ON cleaning_batch_items
BEGIN
    SELECT RAISE(ABORT, 'frozen batch items are immutable');
END;

CREATE TRIGGER prevent_periodic_review_window_delete
BEFORE DELETE ON text_periodic_review_windows BEGIN
    SELECT RAISE(ABORT, 'periodic review windows are immutable');
END;

CREATE TRIGGER prevent_periodic_review_window_identity_update
BEFORE UPDATE OF sample_run_id, baseline_sample_run_id, candidate_build_id,
                 round_number, window_start_rank, window_end_rank,
                 new_post_count_at_freeze, window_member_count,
                 eligible_member_count, member_manifest_sha256, created_at_utc
ON text_periodic_review_windows
BEGIN
    SELECT RAISE(ABORT, 'periodic review windows are immutable');
END;

CREATE TRIGGER prevent_periodic_review_window_member_delete
BEFORE DELETE ON text_periodic_review_window_members BEGIN
    SELECT RAISE(ABORT, 'periodic review window members are immutable');
END;

CREATE TRIGGER prevent_periodic_review_window_member_update
BEFORE UPDATE ON text_periodic_review_window_members BEGIN
    SELECT RAISE(ABORT, 'periodic review window members are immutable');
END;

CREATE TRIGGER prevent_periodic_review_window_status_update
BEFORE UPDATE OF seal_status ON text_periodic_review_windows
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'periodic review window status is immutable');
END;

CREATE TRIGGER prevent_post_decision_build_delete
BEFORE DELETE ON post_decision_builds
BEGIN SELECT RAISE(ABORT, 'post decision builds are immutable'); END;

CREATE TRIGGER prevent_post_decision_build_identity_update
BEFORE UPDATE OF decision_build_id, run_id, source_snapshot_id, build_kind,
                 source_candidate_decision_build_id, text_keep_audit_evaluation_id,
                 decision_version, guide_version, rules_sha256,
                 input_manifest_sha256, expected_post_count, keep_count,
                 review_count, exclude_count, decision_manifest_sha256,
                 created_at_utc
ON post_decision_builds
BEGIN SELECT RAISE(ABORT, 'post decision build identity is immutable'); END;

CREATE TRIGGER prevent_post_decision_delete
BEFORE DELETE ON post_decisions
BEGIN SELECT RAISE(ABORT, 'post decisions are immutable'); END;

CREATE TRIGGER prevent_post_decision_evidence_delete
BEFORE DELETE ON post_decision_evidence_links
BEGIN SELECT RAISE(ABORT, 'post decision evidence is immutable'); END;

CREATE TRIGGER prevent_post_decision_evidence_update
BEFORE UPDATE ON post_decision_evidence_links
BEGIN SELECT RAISE(ABORT, 'post decision evidence is immutable'); END;

CREATE TRIGGER prevent_post_decision_insert_after_seal
BEFORE INSERT ON post_decisions
WHEN EXISTS (SELECT 1 FROM post_decision_builds b
             WHERE b.decision_build_id = NEW.decision_build_id
               AND b.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'post decision build is sealed'); END;

CREATE TRIGGER prevent_post_decision_status_transition
BEFORE UPDATE OF seal_status ON post_decision_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'post decision build status is immutable'); END;

CREATE TRIGGER prevent_post_decision_update
BEFORE UPDATE ON post_decisions
BEGIN SELECT RAISE(ABORT, 'post decisions are immutable'); END;

CREATE TRIGGER prevent_source_snapshot_delete
BEFORE DELETE ON source_snapshots
BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;

CREATE TRIGGER prevent_source_snapshot_update
BEFORE UPDATE ON source_snapshots
BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;

CREATE TRIGGER prevent_stage_event_delete
BEFORE DELETE ON stage_events
BEGIN
    SELECT RAISE(ABORT, 'stage events are append-only');
END;

CREATE TRIGGER prevent_stage_event_update
BEFORE UPDATE ON stage_events
BEGIN
    SELECT RAISE(ABORT, 'stage events are append-only');
END;

CREATE TRIGGER prevent_task_assignment_to_frozen_batch
BEFORE UPDATE OF batch_id ON stage_tasks
WHEN OLD.batch_id IS NULL
 AND NEW.batch_id IS NOT NULL
 AND EXISTS (
     SELECT 1 FROM cleaning_batches
     WHERE batch_id = NEW.batch_id AND frozen_at_utc != ''
 )
BEGIN
    SELECT RAISE(ABORT, 'cannot append task to frozen batch');
END;

CREATE TRIGGER prevent_task_batch_reassignment
BEFORE UPDATE OF batch_id ON stage_tasks
WHEN OLD.batch_id IS NOT NULL AND NEW.batch_id IS NOT OLD.batch_id
BEGIN
    SELECT RAISE(ABORT, 'task batch assignment is immutable');
END;

CREATE TRIGGER prevent_task_insert_into_frozen_batch
BEFORE INSERT ON stage_tasks
WHEN NEW.batch_id IS NOT NULL
 AND EXISTS (
     SELECT 1 FROM cleaning_batches
     WHERE batch_id = NEW.batch_id AND frozen_at_utc != ''
 )
BEGIN
    SELECT RAISE(ABORT, 'cannot append task to frozen batch');
END;

CREATE TRIGGER prevent_text_annotation_import_delete
BEFORE DELETE ON text_annotation_imports BEGIN
    SELECT RAISE(ABORT, 'text annotation imports are append-only');
END;

CREATE TRIGGER prevent_text_annotation_import_update
BEFORE UPDATE ON text_annotation_imports BEGIN
    SELECT RAISE(ABORT, 'text annotation imports are append-only');
END;

CREATE TRIGGER prevent_text_candidate_build_delete
BEFORE DELETE ON text_candidate_builds
BEGIN
    SELECT RAISE(ABORT, 'text candidate builds are immutable');
END;

CREATE TRIGGER prevent_text_candidate_build_identity_update
BEFORE UPDATE OF build_id, run_id, source_snapshot_id, stage_version,
                 rules_version, rules_sha256, runtime_sha256,
                 corpus_manifest_sha256, expected_post_count,
                 processed_post_count, usable_post_count, is_complete_corpus,
                 exact_cluster_count, exact_duplicate_cluster_count,
                 exact_cross_platform_cluster_count,
                 exact_cross_platform_member_count, near_candidate_pair_count,
                 near_cross_platform_candidate_pair_count,
                 near_candidate_component_count, library_versions_json,
                 output_sha256, created_at_utc
ON text_candidate_builds
BEGIN
    SELECT RAISE(ABORT, 'text candidate builds are immutable');
END;

CREATE TRIGGER prevent_text_candidate_build_status_update
BEFORE UPDATE OF status ON text_candidate_builds
WHEN NOT (OLD.status = 'building' AND NEW.status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build status is immutable');
END;

CREATE TRIGGER prevent_text_candidate_corpus_member_delete
BEFORE DELETE ON text_candidate_corpus_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_candidate_corpus_member_update
BEFORE UPDATE ON text_candidate_corpus_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_dataset_split_delete
BEFORE DELETE ON text_dataset_splits BEGIN
    SELECT RAISE(ABORT, 'text dataset splits are immutable');
END;

CREATE TRIGGER prevent_text_dataset_split_update
BEFORE UPDATE ON text_dataset_splits BEGIN
    SELECT RAISE(ABORT, 'text dataset splits are immutable');
END;

CREATE TRIGGER prevent_text_dedup_build_delete
BEFORE DELETE ON text_dedup_builds
BEGIN SELECT RAISE(ABORT, 'text dedup builds are immutable'); END;

CREATE TRIGGER prevent_text_dedup_build_identity_update
BEFORE UPDATE OF dedup_build_id, run_id, post_decision_build_id,
                 candidate_build_id, dedup_version, selection_strategy,
                 expected_eligible_count, edge_count, cluster_count,
                 member_count, representative_count, input_manifest_sha256,
                 member_manifest_sha256, created_at_utc
ON text_dedup_builds
BEGIN SELECT RAISE(ABORT, 'text dedup build identity is immutable'); END;

CREATE TRIGGER prevent_text_dedup_cluster_delete BEFORE DELETE ON text_dedup_clusters
BEGIN SELECT RAISE(ABORT, 'text dedup clusters are immutable'); END;

CREATE TRIGGER prevent_text_dedup_cluster_update BEFORE UPDATE ON text_dedup_clusters
BEGIN SELECT RAISE(ABORT, 'text dedup clusters are immutable'); END;

CREATE TRIGGER prevent_text_dedup_edge_delete BEFORE DELETE ON text_dedup_edges
BEGIN SELECT RAISE(ABORT, 'text dedup edges are immutable'); END;

CREATE TRIGGER prevent_text_dedup_edge_update BEFORE UPDATE ON text_dedup_edges
BEGIN SELECT RAISE(ABORT, 'text dedup edges are immutable'); END;

CREATE TRIGGER prevent_text_dedup_member_delete BEFORE DELETE ON text_dedup_members
BEGIN SELECT RAISE(ABORT, 'text dedup members are immutable'); END;

CREATE TRIGGER prevent_text_dedup_member_update BEFORE UPDATE ON text_dedup_members
BEGIN SELECT RAISE(ABORT, 'text dedup members are immutable'); END;

CREATE TRIGGER prevent_text_dedup_status_transition
BEFORE UPDATE OF seal_status ON text_dedup_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'text dedup build status is immutable'); END;

CREATE TRIGGER prevent_text_duplicate_adjudication_delete
BEFORE DELETE ON text_near_duplicate_adjudications BEGIN
    SELECT RAISE(ABORT, 'text duplicate adjudications are append-only');
END;

CREATE TRIGGER prevent_text_duplicate_adjudication_update
BEFORE UPDATE ON text_near_duplicate_adjudications BEGIN
    SELECT RAISE(ABORT, 'text duplicate adjudications are append-only');
END;

CREATE TRIGGER prevent_text_duplicate_annotation_delete
BEFORE DELETE ON text_near_duplicate_annotations BEGIN
    SELECT RAISE(ABORT, 'text duplicate annotations are append-only');
END;

CREATE TRIGGER prevent_text_duplicate_annotation_update
BEFORE UPDATE ON text_near_duplicate_annotations BEGIN
    SELECT RAISE(ABORT, 'text duplicate annotations are append-only');
END;

CREATE TRIGGER prevent_text_exact_cluster_delete
BEFORE DELETE ON text_exact_clusters
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_exact_cluster_member_delete
BEFORE DELETE ON text_exact_cluster_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_exact_cluster_member_update
BEFORE UPDATE ON text_exact_cluster_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_exact_cluster_update
BEFORE UPDATE ON text_exact_clusters
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_keep_annotation_delete
BEFORE DELETE ON text_keep_audit_annotations
BEGIN SELECT RAISE(ABORT, 'text keep audit annotations are append-only'); END;

CREATE TRIGGER prevent_text_keep_annotation_update
BEFORE UPDATE ON text_keep_audit_annotations
BEGIN SELECT RAISE(ABORT, 'text keep audit annotations are append-only'); END;

CREATE TRIGGER prevent_text_keep_audit_identity_update
BEFORE UPDATE OF audit_round_id, run_id, candidate_decision_build_id,
                 audit_mode, round_number, random_seed, sampling_method,
                 estimator, population_count, population_manifest_sha256,
                 sample_count, sample_manifest_sha256, created_at_utc
ON text_keep_audit_rounds
BEGIN SELECT RAISE(ABORT, 'text keep audit identity is immutable'); END;

CREATE TRIGGER prevent_text_keep_audit_round_delete
BEFORE DELETE ON text_keep_audit_rounds
BEGIN SELECT RAISE(ABORT, 'text keep audit rounds are immutable'); END;

CREATE TRIGGER prevent_text_keep_audit_status_transition
BEFORE UPDATE OF seal_status ON text_keep_audit_rounds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'text keep audit status is immutable'); END;

CREATE TRIGGER prevent_text_keep_evaluation_delete
BEFORE DELETE ON text_keep_audit_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluations are immutable'); END;

CREATE TRIGGER prevent_text_keep_evaluation_identity_update
BEFORE UPDATE OF audit_evaluation_id, audit_round_id, completed_count,
                 event_count, event_point_estimate, one_sided_upper,
                 evaluation_status, reason_code, evidence_manifest_sha256,
                 created_at_utc
ON text_keep_audit_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation identity is immutable'); END;

CREATE TRIGGER prevent_text_keep_evaluation_link_delete
BEFORE DELETE ON text_keep_audit_evaluation_evidence_links
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation evidence is immutable'); END;

CREATE TRIGGER prevent_text_keep_evaluation_link_update
BEFORE UPDATE ON text_keep_audit_evaluation_evidence_links
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation evidence is immutable'); END;

CREATE TRIGGER prevent_text_keep_evaluation_status_transition
BEFORE UPDATE OF seal_status ON text_keep_audit_evaluations
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation status is immutable'); END;

CREATE TRIGGER prevent_text_keep_member_delete BEFORE DELETE ON text_keep_audit_members
BEGIN SELECT RAISE(ABORT, 'text keep audit members are immutable'); END;

CREATE TRIGGER prevent_text_keep_member_update BEFORE UPDATE ON text_keep_audit_members
BEGIN SELECT RAISE(ABORT, 'text keep audit members are immutable'); END;

CREATE TRIGGER prevent_text_keep_platform_evaluation_delete
BEFORE DELETE ON text_keep_audit_platform_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit platform slices are immutable'); END;

CREATE TRIGGER prevent_text_keep_platform_evaluation_update
BEFORE UPDATE ON text_keep_audit_platform_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit platform slices are immutable'); END;

CREATE TRIGGER prevent_text_keep_population_delete
BEFORE DELETE ON text_keep_audit_population_members
BEGIN SELECT RAISE(ABORT, 'text keep audit population is immutable'); END;

CREATE TRIGGER prevent_text_keep_population_update
BEFORE UPDATE ON text_keep_audit_population_members
BEGIN SELECT RAISE(ABORT, 'text keep audit population is immutable'); END;

CREATE TRIGGER prevent_text_leakage_build_delete
BEFORE DELETE ON text_leakage_builds BEGIN
    SELECT RAISE(ABORT, 'text leakage builds are immutable');
END;

CREATE TRIGGER prevent_text_leakage_identity_update_v10
BEFORE UPDATE OF leakage_build_id, candidate_build_id,
                 adjudication_manifest_sha256, input_post_count,
                 component_count, output_sha256, created_at_utc
ON text_leakage_builds
BEGIN
    SELECT RAISE(ABORT, 'text leakage builds are immutable');
END;

CREATE TRIGGER prevent_text_leakage_member_delete
BEFORE DELETE ON text_leakage_members BEGIN
    SELECT RAISE(ABORT, 'text leakage members are immutable');
END;

CREATE TRIGGER prevent_text_leakage_member_update
BEFORE UPDATE ON text_leakage_members BEGIN
    SELECT RAISE(ABORT, 'text leakage members are immutable');
END;

CREATE TRIGGER prevent_text_leakage_status_update
BEFORE UPDATE OF seal_status ON text_leakage_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text leakage build status is immutable');
END;

CREATE TRIGGER prevent_text_model_identity_update_v10
BEFORE UPDATE OF model_run_id, run_id, candidate_build_id, leakage_build_id,
                 guide_version, algorithm_version, config_sha256,
                 gold_manifest_sha256, split_manifest_sha256,
                 train_manifest_sha256, validation_manifest_sha256,
                 test_manifest_sha256, prediction_manifest_sha256,
                 request_manifest_sha256, candidate_prediction_manifest_sha256,
                 train_count, validation_count, test_count, chosen_c,
                 high_risk_threshold, low_risk_threshold, low_risk_enabled,
                 metrics_json, model_artifact_path, model_artifact_sha256,
                 status, expected_prediction_count, created_at_utc
ON text_model_runs
BEGIN
    SELECT RAISE(ABORT, 'text model runs are immutable');
END;

CREATE TRIGGER prevent_text_model_prediction_delete
BEFORE DELETE ON text_model_predictions BEGIN
    SELECT RAISE(ABORT, 'text model predictions are immutable');
END;

CREATE TRIGGER prevent_text_model_prediction_update
BEFORE UPDATE ON text_model_predictions BEGIN
    SELECT RAISE(ABORT, 'text model predictions are immutable');
END;

CREATE TRIGGER prevent_text_model_run_delete
BEFORE DELETE ON text_model_runs BEGIN
    SELECT RAISE(ABORT, 'text model runs are immutable');
END;

CREATE TRIGGER prevent_text_model_status_update
BEFORE UPDATE OF seal_status ON text_model_runs
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text model run status is immutable');
END;

CREATE TRIGGER prevent_text_near_component_delete
BEFORE DELETE ON text_near_candidate_components
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_near_component_member_delete
BEFORE DELETE ON text_near_candidate_component_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_near_component_member_update
BEFORE UPDATE ON text_near_candidate_component_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_near_component_update
BEFORE UPDATE ON text_near_candidate_components
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_near_pair_delete
BEFORE DELETE ON text_near_candidate_pairs
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_near_pair_update
BEFORE UPDATE ON text_near_candidate_pairs
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;

CREATE TRIGGER prevent_text_post_adjudication_delete
BEFORE DELETE ON text_post_adjudications BEGIN
    SELECT RAISE(ABORT, 'text post adjudications are append-only');
END;

CREATE TRIGGER prevent_text_post_adjudication_update
BEFORE UPDATE ON text_post_adjudications BEGIN
    SELECT RAISE(ABORT, 'text post adjudications are append-only');
END;

CREATE TRIGGER prevent_text_post_annotation_delete
BEFORE DELETE ON text_post_annotations BEGIN
    SELECT RAISE(ABORT, 'text post annotations are append-only');
END;

CREATE TRIGGER prevent_text_post_annotation_update
BEFORE UPDATE ON text_post_annotations BEGIN
    SELECT RAISE(ABORT, 'text post annotations are append-only');
END;

CREATE TRIGGER prevent_text_result_delete
BEFORE DELETE ON text_deterministic_results
BEGIN
    SELECT RAISE(ABORT, 'text deterministic results are append-only');
END;

CREATE TRIGGER prevent_text_result_update
BEFORE UPDATE ON text_deterministic_results
BEGIN
    SELECT RAISE(ABORT, 'text deterministic results are append-only');
END;

CREATE TRIGGER prevent_text_sample_member_delete
BEFORE DELETE ON text_sample_members BEGIN
    SELECT RAISE(ABORT, 'text sample members are append-only');
END;

CREATE TRIGGER prevent_text_sample_member_update
BEFORE UPDATE ON text_sample_members BEGIN
    SELECT RAISE(ABORT, 'text sample members are append-only');
END;

CREATE TRIGGER prevent_text_sampling_identity_update_v10
BEFORE UPDATE OF sample_run_id, run_id, source_snapshot_id, candidate_build_id,
                 baseline_sample_run_id, sample_kind, guide_version, random_seed,
                 population_manifest_sha256, population_count, probability_count,
                 targeted_count, periodic_round_number,
                 output_sha256, member_manifest_sha256, created_at_utc
ON text_sampling_runs
BEGIN
    SELECT RAISE(ABORT, 'text sampling runs are immutable');
END;

CREATE TRIGGER prevent_text_sampling_run_delete
BEFORE DELETE ON text_sampling_runs BEGIN
    SELECT RAISE(ABORT, 'text sampling runs are immutable');
END;

CREATE TRIGGER prevent_text_sampling_status_update
BEFORE UPDATE OF seal_status ON text_sampling_runs
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text sampling run status is immutable');
END;

CREATE TRIGGER reject_accepted_cleaning_run_insert
BEFORE INSERT ON cleaning_runs
WHEN NEW.status = 'accepted'
BEGIN SELECT RAISE(ABORT, 'cleaning run cannot start accepted'); END;

CREATE TRIGGER require_periodic_review_window_building_insert
BEFORE INSERT ON text_periodic_review_windows
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'periodic review window must start in building state');
END;

CREATE TRIGGER require_post_decision_building_insert
BEFORE INSERT ON post_decision_builds
WHEN NEW.seal_status != 'building'
 OR NOT EXISTS (
    SELECT 1 FROM cleaning_runs r
    JOIN source_snapshots s ON s.snapshot_id = NEW.source_snapshot_id
    WHERE r.run_id = NEW.run_id AND s.run_id = r.run_id
      AND r.source_snapshot_id = s.snapshot_id AND r.status != 'accepted'
 )
 OR (
    NEW.build_kind = 'final' AND NOT EXISTS (
      SELECT 1
      FROM post_decision_builds candidate
      JOIN text_keep_audit_rounds audit
        ON audit.candidate_decision_build_id = candidate.decision_build_id
      JOIN text_keep_audit_evaluations evaluation
        ON evaluation.audit_round_id = audit.audit_round_id
      WHERE candidate.decision_build_id = NEW.source_candidate_decision_build_id
        AND candidate.run_id = NEW.run_id
        AND candidate.source_snapshot_id = NEW.source_snapshot_id
        AND candidate.build_kind = 'candidate'
        AND candidate.seal_status = 'finalized'
        AND evaluation.audit_evaluation_id = NEW.text_keep_audit_evaluation_id
        AND evaluation.seal_status = 'finalized'
        AND evaluation.evaluation_status = 'passed'
    )
 )
BEGIN SELECT RAISE(ABORT, 'post decision build lineage is invalid'); END;

CREATE TRIGGER require_text_candidate_building_insert
BEFORE INSERT ON text_candidate_builds
WHEN NEW.status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text candidate build must start in building state');
END;

CREATE TRIGGER require_text_dedup_building_insert
BEFORE INSERT ON text_dedup_builds
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
  SELECT 1 FROM post_decision_builds p
  JOIN text_candidate_builds c ON c.build_id = NEW.candidate_build_id
  WHERE p.decision_build_id = NEW.post_decision_build_id
    AND p.run_id = NEW.run_id AND p.build_kind = 'final'
    AND p.seal_status = 'finalized'
    AND c.run_id = p.run_id AND c.source_snapshot_id = p.source_snapshot_id
    AND c.status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'text dedup build lineage is invalid'); END;

CREATE TRIGGER require_text_keep_audit_building_insert
BEFORE INSERT ON text_keep_audit_rounds
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
  SELECT 1 FROM post_decision_builds b
  WHERE b.decision_build_id = NEW.candidate_decision_build_id
    AND b.run_id = NEW.run_id AND b.build_kind = 'candidate'
    AND b.seal_status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'text keep audit requires finalized candidate decisions'); END;

CREATE TRIGGER require_text_keep_evaluation_building_insert
BEFORE INSERT ON text_keep_audit_evaluations
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
  SELECT 1 FROM text_keep_audit_rounds r
  WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation requires a sealed sample'); END;

CREATE TRIGGER require_text_leakage_building_insert
BEFORE INSERT ON text_leakage_builds
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text leakage build must start in building state');
END;

CREATE TRIGGER require_text_model_building_insert
BEFORE INSERT ON text_model_runs
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text model run must start in building state');
END;

CREATE TRIGGER require_text_sampling_building_insert
BEFORE INSERT ON text_sampling_runs
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text sampling run must start in building state');
END;

CREATE TRIGGER validate_analysis_post_deduplicated_insert
BEFORE INSERT ON analysis_posts_deduplicated
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_release_builds r
  JOIN text_dedup_members m
    ON m.dedup_build_id = r.text_dedup_build_id
   AND m.dedup_build_id = NEW.dedup_build_id
   AND m.cluster_id = NEW.cluster_id
   AND m.source_post_id = NEW.source_post_id
   AND m.source_version = NEW.source_version
   AND m.is_representative = 1
  JOIN analysis_posts_eligible p
    ON p.release_id = r.release_id
   AND p.source_post_id = m.source_post_id
   AND p.source_version = m.source_version
  WHERE r.release_id = NEW.release_id AND r.run_id = NEW.run_id
    AND r.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'analysis deduplicated post is not a frozen representative'); END;

CREATE TRIGGER validate_analysis_post_eligible_insert
BEFORE INSERT ON analysis_posts_eligible
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_release_builds r
  JOIN post_decisions d
    ON d.decision_build_id = r.post_decision_build_id
   AND d.decision_id = NEW.decision_id
  WHERE r.release_id = NEW.release_id AND r.run_id = NEW.run_id
    AND r.seal_status = 'building' AND d.decision_action = 'keep'
    AND d.source_post_id = NEW.source_post_id
    AND d.source_version = NEW.source_version
)
BEGIN SELECT RAISE(ABORT, 'analysis eligible post lacks keep decision'); END;

CREATE TRIGGER validate_analysis_release_attestation_insert
BEFORE INSERT ON analysis_release_acceptance_attestations
WHEN release_acceptance_guard(
       NEW.run_id, NEW.release_id, NEW.snapshot_sha256, NEW.snapshot_size_bytes,
       NEW.snapshot_access_mode, NEW.snapshot_query_only,
       NEW.snapshot_integrity_check, NEW.artifact_manifest_sha256,
       NEW.attestation_sha256, NEW.verified_at_utc, 'attestation'
     ) != 1
 OR NOT EXISTS (
    SELECT 1
    FROM analysis_release_builds release
    JOIN source_snapshots snapshot
      ON snapshot.snapshot_id = release.source_snapshot_id
     AND snapshot.run_id = release.run_id
    JOIN analysis_release_manifests artifact
      ON artifact.release_id = release.release_id
     AND artifact.run_id = release.run_id
     AND artifact.manifest_kind = 'artifact'
    JOIN cleaning_runs run ON run.run_id = release.run_id
    WHERE release.release_id = NEW.release_id
      AND release.run_id = NEW.run_id
      AND release.release_mode = 'formal'
      AND release.seal_status = 'finalized'
      AND run.status != 'accepted'
      AND NEW.snapshot_sha256 = snapshot.snapshot_sha256
      AND NEW.snapshot_size_bytes = snapshot.snapshot_size_bytes
      AND NEW.artifact_manifest_sha256 = artifact.manifest_sha256
 )
BEGIN SELECT RAISE(ABORT, 'analysis release acceptance attestation is invalid'); END;

CREATE TRIGGER validate_analysis_release_manifest_insert
BEFORE INSERT ON analysis_release_manifests
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_release_builds r
  WHERE r.release_id = NEW.release_id AND r.run_id = NEW.run_id
    AND r.seal_status = 'building'
    AND (NEW.manifest_kind != 'release'
         OR NEW.manifest_sha256 = r.release_manifest_sha256)
)
BEGIN SELECT RAISE(ABORT, 'analysis release manifest parent or hash is invalid'); END;

CREATE TRIGGER validate_analysis_release_report_insert
BEFORE INSERT ON analysis_release_reports
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_release_builds r
  WHERE r.release_id = NEW.release_id AND r.run_id = NEW.run_id
    AND r.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'analysis release report parent is sealed'); END;

CREATE TRIGGER validate_cleaning_run_snapshot_binding
BEFORE UPDATE OF source_snapshot_id ON cleaning_runs
WHEN NOT (
    OLD.source_snapshot_id IS NULL
    AND NEW.source_snapshot_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM source_snapshots AS s
        WHERE s.snapshot_id = NEW.source_snapshot_id AND s.run_id = OLD.run_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'cleaning run snapshot binding is immutable');
END;

CREATE TRIGGER validate_dataset_split_reference
BEFORE INSERT ON text_dataset_splits
WHEN NOT EXISTS (
    SELECT 1 FROM text_model_runs AS m
    JOIN text_leakage_members AS l
      ON l.leakage_build_id = m.leakage_build_id
     AND l.source_post_id = NEW.source_post_id
     AND l.source_version = NEW.source_version
     AND l.component_id = NEW.component_id
    WHERE m.model_run_id = NEW.model_run_id
)
BEGIN
    SELECT RAISE(ABORT, 'dataset split is outside leakage build');
END;

CREATE TRIGGER validate_leakage_member_candidate_reference
BEFORE INSERT ON text_leakage_members
WHEN NOT EXISTS (
    SELECT 1 FROM text_leakage_builds AS l
    JOIN text_candidate_corpus_members AS c
      ON c.build_id = l.candidate_build_id
     AND c.source_post_id = NEW.source_post_id
     AND c.source_version = NEW.source_version
    WHERE l.leakage_build_id = NEW.leakage_build_id
      AND c.structure_status = 'usable'
)
BEGIN
    SELECT RAISE(ABORT, 'leakage member is outside usable candidate build');
END;

CREATE TRIGGER validate_model_prediction_reference
BEFORE INSERT ON text_model_predictions
WHEN NOT EXISTS (
    SELECT 1 FROM text_model_runs AS m
    JOIN text_candidate_corpus_members AS c
      ON c.build_id = m.candidate_build_id
     AND c.source_post_id = NEW.source_post_id
     AND c.source_version = NEW.source_version
    WHERE m.model_run_id = NEW.model_run_id AND c.structure_status = 'usable'
)
BEGIN
    SELECT RAISE(ABORT, 'model prediction is outside usable candidate build');
END;

CREATE TRIGGER validate_model_review_run_reference
BEFORE INSERT ON text_post_adjudications
WHEN NEW.model_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM text_model_runs
    WHERE model_run_id = NEW.model_run_id AND seal_status = 'finalized'
)
BEGIN
    SELECT RAISE(ABORT, 'model review references unknown or unsealed model run');
END;

CREATE TRIGGER validate_model_run_build_reference
BEFORE INSERT ON text_model_runs
WHEN NOT EXISTS (
    SELECT 1 FROM text_leakage_builds AS l
    WHERE l.leakage_build_id = NEW.leakage_build_id
      AND l.candidate_build_id = NEW.candidate_build_id
      AND l.seal_status = 'finalized'
)
BEGIN
    SELECT RAISE(ABORT, 'model run build reference is invalid');
END;

CREATE TRIGGER validate_periodic_review_member_parent
BEFORE INSERT ON text_periodic_review_window_members
WHEN NOT EXISTS (
    SELECT 1 FROM text_periodic_review_windows
    WHERE sample_run_id = NEW.sample_run_id AND seal_status = 'building'
)
BEGIN
    SELECT RAISE(ABORT, 'periodic review window member parent mismatch');
END;

CREATE TRIGGER validate_periodic_review_window_seal
BEFORE UPDATE OF seal_status ON text_periodic_review_windows
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM text_periodic_review_window_members
     WHERE sample_run_id = NEW.sample_run_id) != NEW.window_member_count
 OR (SELECT COUNT(*) FROM text_periodic_review_window_members
     WHERE sample_run_id = NEW.sample_run_id AND eligible_in_candidate_build = 1)
       != NEW.eligible_member_count
)
BEGIN
    SELECT RAISE(ABORT, 'periodic review window seal validation failed');
END;

CREATE TRIGGER validate_post_decision_evidence_link
BEFORE INSERT ON post_decision_evidence_links
WHEN NOT EXISTS (
    SELECT 1 FROM post_decisions d JOIN post_decision_builds b
      ON b.decision_build_id = d.decision_build_id
    WHERE d.decision_id = NEW.decision_id AND b.seal_status = 'building'
      AND d.source_post_id = NEW.source_post_id
      AND d.source_version = NEW.source_version
 )
 OR (
    NEW.evidence_kind = 'deterministic_result' AND NOT EXISTS (
      SELECT 1 FROM text_deterministic_results x
      WHERE x.task_id = NEW.evidence_id
        AND x.source_post_id = NEW.source_post_id
        AND x.source_version = NEW.source_version
    )
 )
 OR (
    NEW.evidence_kind = 'human_annotation' AND NOT EXISTS (
      SELECT 1 FROM text_post_annotations x
      WHERE x.annotation_id = NEW.evidence_id
        AND x.source_post_id = NEW.source_post_id
        AND x.source_version = NEW.source_version
    )
 )
 OR (
    NEW.evidence_kind = 'human_adjudication' AND NOT EXISTS (
      SELECT 1 FROM text_post_adjudications x
      WHERE x.adjudication_id = NEW.evidence_id
        AND x.source_post_id = NEW.source_post_id
        AND x.source_version = NEW.source_version
    )
 )
 OR (
    NEW.evidence_kind = 'model_prediction' AND NOT EXISTS (
      SELECT 1 FROM post_decisions d
      JOIN text_model_predictions x
        ON x.model_run_id = d.model_run_id
       AND x.source_post_id = NEW.source_post_id
       AND x.source_version = NEW.source_version
      WHERE d.decision_id = NEW.decision_id
        AND d.model_run_id = NEW.evidence_id
    )
 )
BEGIN SELECT RAISE(ABORT, 'post decision evidence link is invalid'); END;

CREATE TRIGGER validate_post_decision_insert
BEFORE INSERT ON post_decisions
WHEN NOT EXISTS (
  SELECT 1
  FROM post_decision_builds b
  JOIN source_post_observations o
    ON o.snapshot_id = b.source_snapshot_id
   AND o.source_post_id = NEW.source_post_id
   AND o.source_version = NEW.source_version
   AND o.change_kind != 'missing'
  WHERE b.decision_build_id = NEW.decision_build_id
    AND b.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'post decision is outside frozen snapshot'); END;

CREATE TRIGGER validate_post_decision_seal
BEFORE UPDATE OF seal_status ON post_decision_builds
WHEN NEW.seal_status = 'finalized' AND (
    NEW.expected_post_count != (
      SELECT post_count FROM source_snapshots WHERE snapshot_id = NEW.source_snapshot_id
    )
 OR (SELECT COUNT(*) FROM post_decisions d
     WHERE d.decision_build_id = NEW.decision_build_id) != NEW.expected_post_count
 OR (SELECT COUNT(*) FROM post_decisions d
     WHERE d.decision_build_id = NEW.decision_build_id
       AND d.decision_action = 'keep') != NEW.keep_count
 OR (SELECT COUNT(*) FROM post_decisions d
     WHERE d.decision_build_id = NEW.decision_build_id
       AND d.decision_action = 'review') != NEW.review_count
 OR (SELECT COUNT(*) FROM post_decisions d
     WHERE d.decision_build_id = NEW.decision_build_id
       AND d.decision_action = 'exclude') != NEW.exclude_count
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.decision_build_id
      AND NOT EXISTS (
        SELECT 1 FROM post_decision_evidence_links l WHERE l.decision_id = d.decision_id
      )
 )
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.decision_build_id
      AND d.provenance = 'model_review_candidate'
      AND (
        (SELECT COUNT(*) FROM post_decision_evidence_links l
         WHERE l.decision_id = d.decision_id
           AND l.evidence_kind = 'model_prediction') != 1
        OR NOT EXISTS (
          SELECT 1 FROM text_model_runs model
          JOIN text_model_predictions prediction
            ON prediction.model_run_id = model.model_run_id
           AND prediction.source_post_id = d.source_post_id
           AND prediction.source_version = d.source_version
          WHERE model.model_run_id = d.model_run_id
            AND model.seal_status = 'finalized'
            AND prediction.suggested_action IN ('high_risk_review', 'manual_review')
        )
      )
 )
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.decision_build_id
      AND d.provenance = 'deterministic_invalid'
      AND (SELECT COUNT(*) FROM post_decision_evidence_links l
           WHERE l.decision_id = d.decision_id
             AND l.evidence_kind = 'deterministic_result') != 1
 )
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.decision_build_id
      AND d.provenance = 'human_adjudication'
      AND (
        (SELECT COUNT(*) FROM post_decision_evidence_links l
         WHERE l.decision_id = d.decision_id
           AND l.evidence_kind = 'human_adjudication') != 1
        OR NOT EXISTS (
          SELECT 1 FROM post_decision_evidence_links l
          JOIN text_post_adjudications a ON a.adjudication_id = l.evidence_id
          WHERE l.decision_id = d.decision_id
            AND a.tourism_label = d.tourism_label
        )
      )
 )
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.decision_build_id
      AND d.provenance = 'model_low_risk'
      AND (
        (SELECT COUNT(*) FROM post_decision_evidence_links l
         WHERE l.decision_id = d.decision_id
           AND l.evidence_kind = 'model_prediction') != 1
        OR (NEW.build_kind = 'final' AND NOT EXISTS (
          SELECT 1 FROM text_model_runs m
          WHERE m.model_run_id = d.model_run_id
            AND m.status = 'completed' AND m.seal_status = 'finalized'
            AND m.low_risk_enabled = 1
        ))
      )
 )
 OR (NEW.build_kind = 'final' AND (
    (SELECT COUNT(*) FROM post_decisions source
     WHERE source.decision_build_id = NEW.source_candidate_decision_build_id)
       != NEW.expected_post_count
    OR EXISTS (
      SELECT 1 FROM post_decisions d
      WHERE d.decision_build_id = NEW.decision_build_id
        AND NOT EXISTS (
          SELECT 1 FROM post_decisions source
          WHERE source.decision_build_id = NEW.source_candidate_decision_build_id
            AND source.source_post_id = d.source_post_id
            AND source.source_version = d.source_version
            AND source.tourism_label = d.tourism_label
            AND source.provenance = d.provenance
            AND source.model_run_id IS d.model_run_id
        )
    )
 ))
)
BEGIN SELECT RAISE(ABORT, 'post decision rows or evidence are incomplete'); END;

CREATE TRIGGER validate_sample_member_candidate_reference
BEFORE INSERT ON text_sample_members
WHEN NOT EXISTS (
    SELECT 1 FROM text_sampling_runs AS s
    JOIN text_candidate_corpus_members AS c
      ON c.build_id = s.candidate_build_id
     AND c.source_post_id = NEW.source_post_id
     AND c.source_version = NEW.source_version
    WHERE s.sample_run_id = NEW.sample_run_id AND c.structure_status = 'usable'
)
BEGIN
    SELECT RAISE(ABORT, 'sample member is outside usable candidate build');
END;

CREATE TRIGGER validate_text_candidate_build_seal
BEFORE UPDATE OF status ON text_candidate_builds
WHEN NEW.status = 'finalized' AND (
    (SELECT COUNT(*) FROM text_candidate_corpus_members WHERE build_id = NEW.build_id)
        != NEW.processed_post_count
 OR (SELECT COUNT(*) FROM text_candidate_corpus_members
     WHERE build_id = NEW.build_id AND structure_status = 'usable')
        != NEW.usable_post_count
 OR (SELECT COUNT(*) FROM text_exact_clusters WHERE build_id = NEW.build_id)
        != NEW.exact_cluster_count
 OR (SELECT COUNT(*) FROM text_exact_clusters
     WHERE build_id = NEW.build_id AND member_count > 1)
        != NEW.exact_duplicate_cluster_count
 OR (SELECT COUNT(*) FROM text_exact_clusters
     WHERE build_id = NEW.build_id AND member_count > 1 AND is_cross_platform = 1)
        != NEW.exact_cross_platform_cluster_count
 OR (SELECT COALESCE(SUM(member_count), 0) FROM text_exact_clusters
     WHERE build_id = NEW.build_id AND member_count > 1 AND is_cross_platform = 1)
        != NEW.exact_cross_platform_member_count
 OR (SELECT COUNT(*) FROM text_exact_cluster_members WHERE build_id = NEW.build_id)
        != NEW.usable_post_count
 OR (SELECT COUNT(*) FROM text_near_candidate_pairs WHERE build_id = NEW.build_id)
        != NEW.near_candidate_pair_count
 OR (SELECT COUNT(*) FROM text_near_candidate_pairs
     WHERE build_id = NEW.build_id AND is_cross_platform = 1)
        != NEW.near_cross_platform_candidate_pair_count
 OR (SELECT COUNT(*) FROM text_near_candidate_components WHERE build_id = NEW.build_id)
        != NEW.near_candidate_component_count
 OR (SELECT COUNT(*) FROM text_near_candidate_component_members WHERE build_id = NEW.build_id)
        != NEW.usable_post_count
)
BEGIN
    SELECT RAISE(ABORT, 'text candidate build counts do not match rows');
END;

CREATE TRIGGER validate_text_dedup_cluster_insert
BEFORE INSERT ON text_dedup_clusters
WHEN NOT EXISTS (SELECT 1 FROM text_dedup_builds b
                 WHERE b.dedup_build_id = NEW.dedup_build_id
                   AND b.seal_status = 'building')
BEGIN SELECT RAISE(ABORT, 'text dedup build is sealed'); END;

CREATE TRIGGER validate_text_dedup_edge_insert
BEFORE INSERT ON text_dedup_edges
WHEN NOT EXISTS (
  SELECT 1 FROM text_dedup_builds b
  WHERE b.dedup_build_id = NEW.dedup_build_id AND b.seal_status = 'building'
)
 OR (NEW.relation_kind = 'exact' AND NOT EXISTS (
   SELECT 1 FROM text_dedup_builds b
   JOIN text_exact_cluster_members left_member
     ON left_member.build_id = b.candidate_build_id
    AND left_member.cluster_id = NEW.exact_cluster_id
    AND left_member.source_post_id = NEW.left_source_post_id
    AND left_member.source_version = NEW.left_source_version
   JOIN text_exact_cluster_members right_member
     ON right_member.build_id = b.candidate_build_id
    AND right_member.cluster_id = NEW.exact_cluster_id
    AND right_member.source_post_id = NEW.right_source_post_id
    AND right_member.source_version = NEW.right_source_version
   WHERE b.dedup_build_id = NEW.dedup_build_id
 ))
 OR (NEW.relation_kind = 'human_duplicate' AND NOT EXISTS (
   SELECT 1 FROM text_dedup_builds b
   JOIN text_near_duplicate_adjudications a
     ON a.adjudication_id = NEW.adjudication_id
    AND a.build_id = b.candidate_build_id AND a.decision = 'duplicate'
   JOIN text_exact_cluster_members left_member
     ON left_member.build_id = b.candidate_build_id
    AND left_member.cluster_id IN (a.left_cluster_id, a.right_cluster_id)
    AND left_member.source_post_id = NEW.left_source_post_id
    AND left_member.source_version = NEW.left_source_version
   JOIN text_exact_cluster_members right_member
     ON right_member.build_id = b.candidate_build_id
    AND right_member.cluster_id IN (a.left_cluster_id, a.right_cluster_id)
    AND right_member.source_post_id = NEW.right_source_post_id
    AND right_member.source_version = NEW.right_source_version
   WHERE b.dedup_build_id = NEW.dedup_build_id
     AND left_member.cluster_id != right_member.cluster_id
 ))
BEGIN SELECT RAISE(ABORT, 'text dedup edge lacks allowed evidence'); END;

CREATE TRIGGER validate_text_dedup_member_insert
BEFORE INSERT ON text_dedup_members
WHEN NOT EXISTS (
  SELECT 1 FROM text_dedup_builds b
  JOIN post_decisions d ON d.decision_build_id = b.post_decision_build_id
  WHERE b.dedup_build_id = NEW.dedup_build_id AND b.seal_status = 'building'
    AND d.source_post_id = NEW.source_post_id
    AND d.source_version = NEW.source_version AND d.decision_action = 'keep'
)
BEGIN SELECT RAISE(ABORT, 'text dedup member is not an eligible post'); END;

CREATE TRIGGER validate_text_dedup_seal
BEFORE UPDATE OF seal_status ON text_dedup_builds
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM post_decisions d
     WHERE d.decision_build_id = NEW.post_decision_build_id
       AND d.decision_action = 'keep') != NEW.expected_eligible_count
 OR (SELECT COUNT(*) FROM text_dedup_edges e
     WHERE e.dedup_build_id = NEW.dedup_build_id) != NEW.edge_count
 OR (SELECT COUNT(*) FROM text_dedup_clusters c
     WHERE c.dedup_build_id = NEW.dedup_build_id) != NEW.cluster_count
 OR (SELECT COUNT(*) FROM text_dedup_members m
     WHERE m.dedup_build_id = NEW.dedup_build_id) != NEW.member_count
 OR (SELECT COUNT(*) FROM text_dedup_members m
     WHERE m.dedup_build_id = NEW.dedup_build_id
       AND m.is_representative = 1) != NEW.representative_count
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.post_decision_build_id
      AND d.decision_action = 'keep'
      AND NOT EXISTS (
        SELECT 1 FROM text_dedup_members m
        WHERE m.dedup_build_id = NEW.dedup_build_id
          AND m.source_post_id = d.source_post_id
          AND m.source_version = d.source_version
      )
 )
 OR EXISTS (
    SELECT 1 FROM text_dedup_clusters c
    WHERE c.dedup_build_id = NEW.dedup_build_id
      AND (
        c.member_count != (SELECT COUNT(*) FROM text_dedup_members m
                           WHERE m.dedup_build_id = c.dedup_build_id
                             AND m.cluster_id = c.cluster_id)
        OR (SELECT COUNT(*) FROM text_dedup_members m
            WHERE m.dedup_build_id = c.dedup_build_id
              AND m.cluster_id = c.cluster_id
              AND m.is_representative = 1) != 1
        OR NOT EXISTS (
          SELECT 1 FROM text_dedup_members m
          WHERE m.dedup_build_id = c.dedup_build_id
            AND m.cluster_id = c.cluster_id
            AND m.source_post_id = c.representative_source_post_id
            AND m.source_version = c.representative_source_version
            AND m.is_representative = 1
        )
      )
 )
 OR EXISTS (
    SELECT 1 FROM text_dedup_edges e
    WHERE e.dedup_build_id = NEW.dedup_build_id
      AND NOT EXISTS (
        SELECT 1 FROM text_dedup_members l
        JOIN text_dedup_members r
          ON r.dedup_build_id = l.dedup_build_id AND r.cluster_id = l.cluster_id
        WHERE l.dedup_build_id = e.dedup_build_id
          AND l.source_post_id = e.left_source_post_id
          AND l.source_version = e.left_source_version
          AND r.source_post_id = e.right_source_post_id
          AND r.source_version = e.right_source_version
      )
 )
)
BEGIN SELECT RAISE(ABORT, 'text dedup rows do not conserve eligible members'); END;

CREATE TRIGGER validate_text_keep_annotation_insert
BEFORE INSERT ON text_keep_audit_annotations
WHEN NOT EXISTS (
  SELECT 1 FROM text_keep_audit_rounds r
  JOIN text_keep_audit_members m
    ON m.audit_round_id = r.audit_round_id
   AND m.source_post_id = NEW.source_post_id
   AND m.source_version = NEW.source_version
  WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'text keep audit annotation is outside sealed sample'); END;

CREATE TRIGGER validate_text_keep_audit_seal
BEFORE UPDATE OF seal_status ON text_keep_audit_rounds
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM post_decisions d
     WHERE d.decision_build_id = NEW.candidate_decision_build_id
       AND d.decision_action = 'keep') != NEW.population_count
 OR (SELECT COUNT(*) FROM text_keep_audit_population_members p
     WHERE p.audit_round_id = NEW.audit_round_id) != NEW.population_count
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.candidate_decision_build_id
      AND d.decision_action = 'keep'
      AND NOT EXISTS (
        SELECT 1 FROM text_keep_audit_population_members p
        WHERE p.audit_round_id = NEW.audit_round_id
          AND p.source_post_id = d.source_post_id
          AND p.source_version = d.source_version
      )
 )
 OR (SELECT COUNT(*) FROM text_keep_audit_members m
     WHERE m.audit_round_id = NEW.audit_round_id) != NEW.sample_count
 OR (NEW.sampling_method = 'census' AND EXISTS (
    SELECT 1 FROM text_keep_audit_members m
    WHERE m.audit_round_id = NEW.audit_round_id
      AND (abs(m.inclusion_probability - 1.0) > 0.000000001
           OR abs(m.sampling_weight - 1.0) > 0.000000001)
 ))
 OR (NEW.sampling_method = 'platform_stratified_equal_probability' AND EXISTS (
    SELECT 1 FROM text_keep_audit_members m
    WHERE m.audit_round_id = NEW.audit_round_id
      AND (
        abs(m.inclusion_probability -
            (1.0 * NEW.sample_count / NEW.population_count)) > 0.000000001
        OR abs(m.sampling_weight -
               (1.0 * NEW.population_count / NEW.sample_count)) > 0.000000001
      )
 ))
)
BEGIN SELECT RAISE(ABORT, 'text keep audit population or sample is invalid'); END;

CREATE TRIGGER validate_text_keep_evaluation_link_insert
BEFORE INSERT ON text_keep_audit_evaluation_evidence_links
WHEN NOT EXISTS (
  SELECT 1 FROM text_keep_audit_evaluations e
  JOIN text_keep_audit_annotations a
    ON a.audit_annotation_id = NEW.audit_annotation_id
   AND a.audit_round_id = e.audit_round_id
  WHERE e.audit_evaluation_id = NEW.audit_evaluation_id
    AND e.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation evidence is invalid'); END;

CREATE TRIGGER validate_text_keep_evaluation_seal
BEFORE UPDATE OF seal_status ON text_keep_audit_evaluations
WHEN NEW.seal_status = 'finalized' AND (
    NEW.completed_count != (
      SELECT r.sample_count FROM text_keep_audit_rounds r
      WHERE r.audit_round_id = NEW.audit_round_id
    )
 OR (SELECT COUNT(*) FROM text_keep_audit_evaluation_evidence_links l
     WHERE l.audit_evaluation_id = NEW.audit_evaluation_id) != NEW.completed_count
 OR EXISTS (
    SELECT 1 FROM text_keep_audit_members m
    WHERE m.audit_round_id = NEW.audit_round_id
      AND NOT EXISTS (
        SELECT 1 FROM text_keep_audit_evaluation_evidence_links l
        JOIN text_keep_audit_annotations a
          ON a.audit_annotation_id = l.audit_annotation_id
        WHERE l.audit_evaluation_id = NEW.audit_evaluation_id
          AND a.audit_round_id = m.audit_round_id
          AND a.source_post_id = m.source_post_id
          AND a.source_version = m.source_version
      )
 )
 OR NEW.event_count != (
    SELECT COUNT(*)
    FROM text_keep_audit_evaluation_evidence_links l
    JOIN text_keep_audit_annotations a
      ON a.audit_annotation_id = l.audit_annotation_id
    WHERE l.audit_evaluation_id = NEW.audit_evaluation_id
      AND a.tourism_label != 'related'
 )
 OR (SELECT COUNT(*) FROM text_keep_audit_platform_evaluations s
     WHERE s.audit_evaluation_id = NEW.audit_evaluation_id) != (
    SELECT COUNT(DISTINCT p.platform_key)
    FROM text_keep_audit_rounds r
    JOIN text_keep_audit_population_members p ON p.audit_round_id = r.audit_round_id
    WHERE r.audit_round_id = NEW.audit_round_id
 )
 OR EXISTS (
    SELECT 1
    FROM (
      SELECT p.platform_key, COUNT(*) AS population_count
      FROM text_keep_audit_population_members p
      WHERE p.audit_round_id = NEW.audit_round_id
      GROUP BY p.platform_key
    ) AS population
    LEFT JOIN text_keep_audit_platform_evaluations s
      ON s.audit_evaluation_id = NEW.audit_evaluation_id
     AND s.platform_key = population.platform_key
    WHERE s.platform_key IS NULL
       OR s.population_count != population.population_count
       OR s.sample_count != (
          SELECT COUNT(*) FROM text_keep_audit_members m
          WHERE m.audit_round_id = NEW.audit_round_id
            AND m.platform_key = population.platform_key
       )
       OR s.completed_count != (
          SELECT COUNT(*)
          FROM text_keep_audit_evaluation_evidence_links l
          JOIN text_keep_audit_annotations a
            ON a.audit_annotation_id = l.audit_annotation_id
          JOIN text_keep_audit_members m
            ON m.audit_round_id = a.audit_round_id
           AND m.source_post_id = a.source_post_id
           AND m.source_version = a.source_version
          WHERE l.audit_evaluation_id = NEW.audit_evaluation_id
            AND m.platform_key = population.platform_key
       )
       OR s.event_count != (
          SELECT COUNT(*)
          FROM text_keep_audit_evaluation_evidence_links l
          JOIN text_keep_audit_annotations a
            ON a.audit_annotation_id = l.audit_annotation_id
          JOIN text_keep_audit_members m
            ON m.audit_round_id = a.audit_round_id
           AND m.source_post_id = a.source_post_id
           AND m.source_version = a.source_version
          WHERE l.audit_evaluation_id = NEW.audit_evaluation_id
            AND m.platform_key = population.platform_key
            AND a.tourism_label != 'related'
       )
       OR (s.sample_count = 0 AND s.event_point_estimate IS NOT NULL)
       OR (s.sample_count > 0 AND abs(
            s.event_point_estimate - (1.0 * s.event_count / s.sample_count)
          ) > 0.000000001)
 )
 OR abs(NEW.event_point_estimate -
        (1.0 * NEW.event_count / max(NEW.completed_count, 1))) > 0.000000001
 OR abs(NEW.one_sided_upper - CASE
      WHEN (SELECT r.estimator FROM text_keep_audit_rounds r
            WHERE r.audit_round_id = NEW.audit_round_id) = 'census'
        THEN 1.0 * NEW.event_count / max(NEW.completed_count, 1)
      ELSE wilson_upper_95(NEW.event_count, NEW.completed_count)
    END) > 0.000000001
 OR NEW.evaluation_status != CASE
      WHEN NEW.completed_count > 0
       AND (1.0 * NEW.event_count / NEW.completed_count) <= 0.03
       AND CASE
          WHEN (SELECT r.estimator FROM text_keep_audit_rounds r
                WHERE r.audit_round_id = NEW.audit_round_id) = 'census'
            THEN 1.0 * NEW.event_count / NEW.completed_count
          ELSE wilson_upper_95(NEW.event_count, NEW.completed_count)
       END <= 0.05
      THEN 'passed' ELSE 'failed' END
)
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation does not match evidence'); END;

CREATE TRIGGER validate_text_keep_member_insert
BEFORE INSERT ON text_keep_audit_members
WHEN NOT EXISTS (
  SELECT 1 FROM text_keep_audit_rounds r
  JOIN text_keep_audit_population_members p
    ON p.audit_round_id = r.audit_round_id
   AND p.source_post_id = NEW.source_post_id
   AND p.source_version = NEW.source_version
   AND p.platform_key = NEW.platform_key
  WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'text keep audit member is outside frozen population'); END;

CREATE TRIGGER validate_text_keep_platform_evaluation_insert
BEFORE INSERT ON text_keep_audit_platform_evaluations
WHEN NOT EXISTS (
  SELECT 1 FROM text_keep_audit_evaluations e
  JOIN text_keep_audit_rounds r ON r.audit_round_id = e.audit_round_id
  JOIN text_keep_audit_population_members p ON p.audit_round_id = r.audit_round_id
  WHERE e.audit_evaluation_id = NEW.audit_evaluation_id
    AND e.seal_status = 'building' AND p.platform_key = NEW.platform_key
)
BEGIN SELECT RAISE(ABORT, 'text keep audit platform slice is invalid'); END;

CREATE TRIGGER validate_text_keep_population_insert
BEFORE INSERT ON text_keep_audit_population_members
WHEN NOT EXISTS (
  SELECT 1 FROM text_keep_audit_rounds r
  JOIN post_decisions d ON d.decision_build_id = r.candidate_decision_build_id
  JOIN source_post_inventory p ON p.source_post_id = d.source_post_id
  WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'building'
    AND d.source_post_id = NEW.source_post_id
    AND d.source_version = NEW.source_version
    AND d.decision_action = 'keep' AND p.platform_key = NEW.platform_key
)
BEGIN SELECT RAISE(ABORT, 'text keep audit population is not a keep candidate'); END;

CREATE TRIGGER validate_text_leakage_seal
BEFORE UPDATE OF seal_status ON text_leakage_builds
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM text_leakage_members
     WHERE leakage_build_id = NEW.leakage_build_id) != NEW.input_post_count
 OR (SELECT COUNT(DISTINCT component_id) FROM text_leakage_members
     WHERE leakage_build_id = NEW.leakage_build_id) != NEW.component_count
)
BEGIN
    SELECT RAISE(ABORT, 'text leakage build seal validation failed');
END;

CREATE TRIGGER validate_text_model_seal_v10
BEFORE UPDATE OF seal_status ON text_model_runs
WHEN NEW.seal_status = 'finalized' AND (
    NEW.train_manifest_sha256 IS NULL
 OR NEW.validation_manifest_sha256 IS NULL
 OR NEW.test_manifest_sha256 IS NULL
 OR NEW.prediction_manifest_sha256 IS NULL
 OR NEW.request_manifest_sha256 IS NULL
 OR NEW.candidate_prediction_manifest_sha256 IS NULL
 OR (SELECT COUNT(*) FROM text_dataset_splits
     WHERE model_run_id = NEW.model_run_id AND split_name = 'train') != NEW.train_count
 OR (SELECT COUNT(*) FROM text_dataset_splits
     WHERE model_run_id = NEW.model_run_id AND split_name = 'validation')
       != NEW.validation_count
 OR (SELECT COUNT(*) FROM text_dataset_splits
     WHERE model_run_id = NEW.model_run_id AND split_name = 'test') != NEW.test_count
 OR (SELECT COUNT(*) FROM text_model_predictions
     WHERE model_run_id = NEW.model_run_id) != NEW.expected_prediction_count
)
BEGIN
    SELECT RAISE(ABORT, 'text model run seal validation failed');
END;

CREATE TRIGGER validate_text_sampling_seal_v10
BEFORE UPDATE OF seal_status ON text_sampling_runs
WHEN NEW.seal_status = 'finalized' AND (
    NEW.member_manifest_sha256 IS NULL
 OR NEW.member_manifest_sha256 != NEW.output_sha256
 OR (SELECT COUNT(*) FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND sample_frame = 'probability')
       != CASE WHEN NEW.sample_kind = 'initial' THEN NEW.probability_count ELSE 0 END
 OR (SELECT COUNT(*) FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND sample_frame = 'targeted')
       != CASE WHEN NEW.sample_kind = 'initial' THEN NEW.targeted_count ELSE 0 END
 OR (SELECT COUNT(*) FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND sample_frame = 'periodic_probability')
       != CASE WHEN NEW.sample_kind = 'periodic_review'
               THEN NEW.probability_count ELSE 0 END
)
BEGIN
    SELECT RAISE(ABORT, 'text sampling run seal validation failed');
END;

CREATE TRIGGER prevent_frozen_batch_identity_update
BEFORE UPDATE OF run_id, sequence_number, manifest_sha256, post_count,
                 task_count, frozen_at_utc
ON cleaning_batches
WHEN OLD.frozen_at_utc != ''
BEGIN SELECT RAISE(ABORT, 'frozen batch identity is immutable'); END;

CREATE TRIGGER require_analysis_release_building_insert
BEFORE INSERT ON analysis_release_builds
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
  SELECT 1 FROM cleaning_runs r
  JOIN source_snapshots s ON s.snapshot_id = NEW.source_snapshot_id
  WHERE r.run_id = NEW.run_id AND s.run_id = r.run_id
    AND r.source_snapshot_id = s.snapshot_id AND r.status != 'accepted'
    AND r.protocol_version = NEW.protocol_version
    AND r.config_sha256 = NEW.config_sha256
    AND r.code_version = NEW.code_version
) OR NOT EXISTS (
  SELECT 1 FROM post_decision_builds p
  WHERE p.decision_build_id = NEW.post_decision_build_id
    AND p.run_id = NEW.run_id AND p.source_snapshot_id = NEW.source_snapshot_id
    AND p.build_kind = 'final' AND p.seal_status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'analysis release run identity is invalid'); END;

CREATE TRIGGER prevent_analysis_release_identity_update
BEFORE UPDATE OF release_id, run_id, source_snapshot_id, release_mode,
                 post_decision_build_id, text_dedup_build_id,
                 text_keep_audit_evaluation_id, protocol_version,
                 schema_version, config_sha256, code_version,
                 request_manifest_sha256, posts_eligible_count,
                 posts_deduplicated_count, release_manifest_sha256,
                 created_at_utc
ON analysis_release_builds
BEGIN SELECT RAISE(ABORT, 'analysis release identity is immutable'); END;

CREATE TRIGGER validate_analysis_release_finalize
BEFORE UPDATE OF seal_status ON analysis_release_builds
WHEN NEW.seal_status = 'finalized' AND (
  NOT EXISTS (
    SELECT 1 FROM post_decision_builds p
    JOIN text_dedup_builds d ON d.dedup_build_id = NEW.text_dedup_build_id
    JOIN text_keep_audit_evaluations te ON te.audit_evaluation_id = NEW.text_keep_audit_evaluation_id
    JOIN text_keep_audit_rounds tr ON tr.audit_round_id = te.audit_round_id
    WHERE p.decision_build_id = NEW.post_decision_build_id
      AND p.run_id = NEW.run_id AND p.source_snapshot_id = NEW.source_snapshot_id
      AND p.build_kind = 'final' AND p.seal_status = 'finalized'
      AND p.text_keep_audit_evaluation_id = te.audit_evaluation_id
      AND d.run_id = NEW.run_id AND d.post_decision_build_id = p.decision_build_id
      AND d.seal_status = 'finalized'
      AND tr.run_id = NEW.run_id AND tr.seal_status = 'finalized'
      AND te.seal_status = 'finalized' AND te.evaluation_status = 'passed'
      AND tr.audit_mode = NEW.release_mode
  )
  OR (SELECT COUNT(*) FROM analysis_posts_eligible p WHERE p.release_id = NEW.release_id)
       != NEW.posts_eligible_count
  OR (SELECT COUNT(*) FROM post_decisions d WHERE d.decision_build_id = NEW.post_decision_build_id
      AND d.decision_action = 'keep') != NEW.posts_eligible_count
  OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.post_decision_build_id AND d.decision_action = 'keep'
      AND NOT EXISTS (SELECT 1 FROM analysis_posts_eligible p
                      WHERE p.release_id = NEW.release_id AND p.decision_id = d.decision_id)
  )
  OR (SELECT COUNT(*) FROM analysis_posts_deduplicated p WHERE p.release_id = NEW.release_id)
       != NEW.posts_deduplicated_count
  OR (SELECT COUNT(*) FROM text_dedup_clusters c WHERE c.dedup_build_id = NEW.text_dedup_build_id)
       != NEW.posts_deduplicated_count
  OR EXISTS (
    SELECT 1 FROM text_dedup_clusters c
    WHERE c.dedup_build_id = NEW.text_dedup_build_id
      AND NOT EXISTS (SELECT 1 FROM analysis_posts_deduplicated p
                      WHERE p.release_id = NEW.release_id
                        AND p.dedup_build_id = c.dedup_build_id AND p.cluster_id = c.cluster_id)
  )
  OR NOT EXISTS (SELECT 1 FROM analysis_release_reports x
                 WHERE x.release_id = NEW.release_id AND x.report_kind = 'quality_summary')
  OR NOT EXISTS (SELECT 1 FROM analysis_release_reports x
                 WHERE x.release_id = NEW.release_id AND x.report_kind = 'lineage')
  OR (SELECT COUNT(*) FROM analysis_release_manifests x
      WHERE x.release_id = NEW.release_id
        AND x.manifest_kind IN ('release', 'posts_eligible', 'posts_deduplicated')) != 3
)
BEGIN SELECT RAISE(ABORT, 'analysis release evidence or member counts are invalid'); END;

CREATE TRIGGER validate_analysis_release_accept
BEFORE UPDATE OF seal_status ON analysis_release_builds
WHEN NEW.seal_status = 'accepted' AND (
  NEW.release_mode != 'formal'
  OR NOT EXISTS (SELECT 1 FROM cleaning_runs r WHERE r.run_id = NEW.run_id AND r.status = 'accepted')
  OR EXISTS (SELECT 1 FROM post_decisions d WHERE d.decision_build_id = NEW.post_decision_build_id
             AND d.decision_action = 'review')
  OR EXISTS (SELECT 1 FROM stage_tasks t WHERE t.run_id = NEW.run_id AND t.required = 1
             AND t.status IN ('pending', 'running', 'failed', 'blocked'))
  OR NOT EXISTS (
    SELECT 1 FROM text_keep_audit_evaluations te
    JOIN text_keep_audit_rounds tr ON tr.audit_round_id = te.audit_round_id
    WHERE te.audit_evaluation_id = NEW.text_keep_audit_evaluation_id
      AND te.seal_status = 'finalized' AND te.evaluation_status = 'passed'
      AND tr.audit_mode = 'formal' AND tr.seal_status = 'finalized'
  )
  OR NOT EXISTS (
    SELECT 1 FROM analysis_release_acceptance_attestations a
    WHERE a.release_id = NEW.release_id AND a.run_id = NEW.run_id
      AND release_acceptance_guard(
        a.run_id, a.release_id, a.snapshot_sha256, a.snapshot_size_bytes,
        a.snapshot_access_mode, a.snapshot_query_only, a.snapshot_integrity_check,
        a.artifact_manifest_sha256, a.attestation_sha256, a.verified_at_utc, 'release'
      ) = 1
  )
)
BEGIN SELECT RAISE(ABORT, 'analysis release quality gates are not satisfied'); END;
"""


def _canonical_json_sha256(value: str) -> str:
    """按规范 JSON 编码计算 SQLite 触发器所需摘要。"""
    parsed = json.loads(value)
    payload = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_sample_manifest_sha256(value: str) -> str:
    """规范化文本审计成员概率和权重后计算摘要。"""
    rows = json.loads(value)
    normalized: list[list[object]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("audit member manifest row is invalid")
        normalized.append([*row[:4], round(float(row[4]), 15), round(float(row[5]), 15)])
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_sample_rank(seed: int, namespace: str, identity: str) -> str:
    """返回冻结 SHA-256 抽样次序。"""
    digest = hashlib.sha256(f"{int(seed)}:{namespace}:{identity}".encode("utf-8")).hexdigest()
    return f"{digest}:{identity}"


def _wilson_upper_95(event_count: int, sample_count: int) -> float:
    """重算二项事件率的单侧 95% Wilson 上限。"""
    if sample_count <= 0 or event_count < 0 or event_count > sample_count:
        return 1.0
    z_value = 1.6448536269514722
    proportion = event_count / sample_count
    z_squared = z_value * z_value
    denominator = 1.0 + z_squared / sample_count
    centre = proportion + z_squared / (2.0 * sample_count)
    spread = z_value * math.sqrt(
        proportion * (1.0 - proportion) / sample_count
        + z_squared / (4.0 * sample_count * sample_count)
    )
    return min(1.0, (centre + spread) / denominator)


def _deny_release_acceptance_guard(*evidence_fields: object) -> int:
    """普通连接恒拒绝 accepted 迁移。"""
    del evidence_fields
    return 0


def connect_derived(path: str | Path) -> sqlite3.Connection:
    """打开可写派生库并注册文本清洗完整性函数。"""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.create_function("canonical_json_sha256", 1, _canonical_json_sha256, deterministic=True)
    connection.create_function("audit_sample_manifest_sha256", 1, _audit_sample_manifest_sha256, deterministic=True)
    connection.create_function("audit_sample_rank", 3, _audit_sample_rank, deterministic=True)
    connection.create_function("wilson_upper_95", 2, _wilson_upper_95, deterministic=True)
    connection.create_function("release_acceptance_guard", -1, _deny_release_acceptance_guard, deterministic=True)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def migrate_derived(connection: sqlite3.Connection) -> None:
    """幂等建立 3.2 文本清洗 schema 33；拒绝就地升级旧版派生库。"""
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if existing is not None:
        versions = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
        if versions == {DERIVED_SCHEMA_VERSION}:
            return
        if versions:
            raise sqlite3.IntegrityError("legacy_derived_schema_requires_rebuild")
    with connection:
        connection.executescript(_TEXT_ONLY_SCHEMA)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at_utc) "
            "VALUES (?, 'text_only_cleaning_v3_2_schema_33', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (DERIVED_SCHEMA_VERSION,),
        )
