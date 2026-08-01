"""清洗派生 SQLite 的连接约束与幂等迁移。

所有 DDL 只作用于独立派生库，不打开或回写正式采集库。版本迁移追加表、索引
和防绕过触发器，并在发现无法安全解释的历史行时整体回滚；应用层仓储与直接
SQL 因而共享相同的内容角色、人工证据、决定传播和审计状态机约束。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path


DERIVED_SCHEMA_VERSION = 24

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cleaning_runs (
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
);

CREATE TABLE IF NOT EXISTS source_snapshots (
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
    image_count INTEGER NOT NULL CHECK (image_count >= 0),
    table_counts_json TEXT NOT NULL,
    object_manifest_sha256 TEXT NOT NULL CHECK (length(object_manifest_sha256) = 64),
    input_contract_status TEXT NOT NULL CHECK (input_contract_status IN ('accepted', 'rejected')),
    input_contract_method TEXT NOT NULL,
    input_contract_reason_code TEXT,
    input_contract_details_json TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    created_at_asia_shanghai TEXT NOT NULL,
    code_version TEXT NOT NULL,
    environment_json TEXT NOT NULL
);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS source_post_inventory (
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
);

CREATE TABLE IF NOT EXISTS inventory_discoveries (
    snapshot_id TEXT PRIMARY KEY REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL UNIQUE REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    post_changes_json TEXT NOT NULL,
    image_changes_json TEXT NOT NULL,
    tasks_created INTEGER NOT NULL CHECK (tasks_created >= 0),
    completed_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_post_versions (
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    effective_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    author_sha256 TEXT NOT NULL CHECK (length(author_sha256) = 64),
    analysis_sha256 TEXT NOT NULL CHECK (length(analysis_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (source_post_id, source_version)
);

CREATE TABLE IF NOT EXISTS source_post_observations (
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

CREATE TABLE IF NOT EXISTS source_image_inventory (
    source_image_id INTEGER PRIMARY KEY,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    first_seen_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    last_seen_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    missing_since_snapshot_id TEXT REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    is_present INTEGER NOT NULL CHECK (is_present IN (0, 1)),
    current_source_version INTEGER NOT NULL CHECK (current_source_version > 0),
    current_relation_sha256 TEXT NOT NULL CHECK (length(current_relation_sha256) = 64),
    current_file_sha256 TEXT NOT NULL CHECK (length(current_file_sha256) = 64),
    image_index_sort INTEGER NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_image_versions (
    source_image_id INTEGER NOT NULL REFERENCES source_image_inventory(source_image_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    effective_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    relation_sha256 TEXT NOT NULL CHECK (length(relation_sha256) = 64),
    file_sha256 TEXT NOT NULL CHECK (length(file_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (source_image_id, source_version)
);

CREATE TABLE IF NOT EXISTS source_image_observations (
    snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    source_image_id INTEGER NOT NULL REFERENCES source_image_inventory(source_image_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL,
    change_kind TEXT NOT NULL CHECK (
        change_kind IN ('new', 'cleaning_changed', 'unchanged', 'missing')
    ),
    changed_axes_json TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, source_image_id),
    FOREIGN KEY (source_image_id, source_version)
        REFERENCES source_image_versions(source_image_id, source_version) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS cleaning_batches (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'completed_with_blocks', 'failed')
    ),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    post_count INTEGER NOT NULL CHECK (post_count >= 0),
    image_count INTEGER NOT NULL CHECK (image_count >= 0),
    task_count INTEGER NOT NULL CHECK (task_count > 0),
    frozen_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (run_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS stage_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    batch_id TEXT REFERENCES cleaning_batches(batch_id) ON DELETE RESTRICT,
    stage_name TEXT NOT NULL,
    object_type TEXT NOT NULL CHECK (object_type IN ('post', 'image')),
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

CREATE TABLE IF NOT EXISTS cleaning_batch_items (
    batch_id TEXT NOT NULL REFERENCES cleaning_batches(batch_id) ON DELETE RESTRICT,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    task_id TEXT NOT NULL UNIQUE REFERENCES stage_tasks(task_id) ON DELETE RESTRICT,
    object_type TEXT NOT NULL CHECK (object_type IN ('post', 'image')),
    source_object_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    stage_name TEXT NOT NULL,
    stage_version TEXT NOT NULL,
    PRIMARY KEY (batch_id, item_index)
);

CREATE TABLE IF NOT EXISTS stage_events (
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

CREATE INDEX IF NOT EXISTS idx_stage_tasks_unbatched
    ON stage_tasks(run_id, status, batch_id, source_post_id, source_object_id);
CREATE INDEX IF NOT EXISTS idx_stage_tasks_batch_status
    ON stage_tasks(batch_id, status, stage_name);
CREATE INDEX IF NOT EXISTS idx_stage_tasks_algorithm_history
    ON stage_tasks(stage_name, object_type, source_object_id, stage_version);
CREATE INDEX IF NOT EXISTS idx_post_observations_change
    ON source_post_observations(snapshot_id, change_kind);
CREATE INDEX IF NOT EXISTS idx_image_observations_change
    ON source_image_observations(snapshot_id, change_kind);

CREATE TRIGGER IF NOT EXISTS prevent_frozen_batch_item_insert
BEFORE INSERT ON cleaning_batch_items
WHEN EXISTS (
    SELECT 1 FROM cleaning_batches
    WHERE batch_id = NEW.batch_id AND frozen_at_utc != ''
)
BEGIN
    SELECT RAISE(ABORT, 'frozen batch items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_frozen_batch_item_update
BEFORE UPDATE ON cleaning_batch_items
BEGIN
    SELECT RAISE(ABORT, 'frozen batch items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_frozen_batch_item_delete
BEFORE DELETE ON cleaning_batch_items
BEGIN
    SELECT RAISE(ABORT, 'frozen batch items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_frozen_batch_identity_update
BEFORE UPDATE OF run_id, sequence_number, manifest_sha256, post_count, image_count,
                 task_count, frozen_at_utc
ON cleaning_batches
WHEN OLD.frozen_at_utc != ''
BEGIN
    SELECT RAISE(ABORT, 'frozen batch identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_task_batch_reassignment
BEFORE UPDATE OF batch_id ON stage_tasks
WHEN OLD.batch_id IS NOT NULL AND NEW.batch_id IS NOT OLD.batch_id
BEGIN
    SELECT RAISE(ABORT, 'task batch assignment is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_task_assignment_to_frozen_batch
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

CREATE TRIGGER IF NOT EXISTS prevent_task_insert_into_frozen_batch
BEFORE INSERT ON stage_tasks
WHEN NEW.batch_id IS NOT NULL
 AND EXISTS (
     SELECT 1 FROM cleaning_batches
     WHERE batch_id = NEW.batch_id AND frozen_at_utc != ''
 )
BEGIN
    SELECT RAISE(ABORT, 'cannot append task to frozen batch');
END;

CREATE TRIGGER IF NOT EXISTS prevent_batched_task_identity_update
BEFORE UPDATE OF run_id, stage_name, object_type, source_object_id,
                 source_post_id, source_version, stage_version, required,
                 max_attempts
ON stage_tasks
WHEN OLD.batch_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'batched task identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_stage_event_update
BEFORE UPDATE ON stage_events
BEGIN
    SELECT RAISE(ABORT, 'stage events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS prevent_stage_event_delete
BEFORE DELETE ON stage_events
BEGIN
    SELECT RAISE(ABORT, 'stage events are append-only');
END;

CREATE VIEW IF NOT EXISTS v_post_cleaning_progress AS
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

CREATE VIEW IF NOT EXISTS v_image_cleaning_progress AS
SELECT run_id,
       source_object_id AS source_image_id,
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
WHERE object_type = 'image'
GROUP BY run_id, source_object_id;

CREATE VIEW IF NOT EXISTS text_ready AS
SELECT run_id,
       source_post_id,
       MAX(source_version) AS source_version,
       1 AS is_text_ready
FROM stage_tasks
WHERE object_type = 'post'
  AND stage_name IN ('text_deterministic', 'text_relevance')
GROUP BY run_id, source_post_id
HAVING COUNT(*) > 0
   AND SUM(status NOT IN ('succeeded', 'skipped')) = 0;
"""

_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS text_deterministic_results (
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

CREATE TABLE IF NOT EXISTS text_candidate_builds (
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

CREATE TABLE IF NOT EXISTS text_exact_clusters (
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    cluster_id TEXT NOT NULL,
    exact_canonical_sha256 TEXT NOT NULL CHECK (length(exact_canonical_sha256) = 64),
    representative_source_post_id INTEGER NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    is_cross_platform INTEGER NOT NULL CHECK (is_cross_platform IN (0, 1)),
    PRIMARY KEY (build_id, cluster_id),
    UNIQUE (build_id, exact_canonical_sha256)
);

CREATE TABLE IF NOT EXISTS text_candidate_corpus_members (
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

CREATE TABLE IF NOT EXISTS text_exact_cluster_members (
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

CREATE TABLE IF NOT EXISTS text_near_candidate_pairs (
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

CREATE TABLE IF NOT EXISTS text_near_candidate_components (
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    component_id TEXT NOT NULL,
    representative_count INTEGER NOT NULL CHECK (representative_count > 0),
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    is_cross_platform INTEGER NOT NULL CHECK (is_cross_platform IN (0, 1)),
    PRIMARY KEY (build_id, component_id)
);

CREATE TABLE IF NOT EXISTS text_near_candidate_component_members (
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

CREATE INDEX IF NOT EXISTS idx_text_results_snapshot_status
    ON text_deterministic_results(source_snapshot_id, stage_version, structure_status);
CREATE INDEX IF NOT EXISTS idx_text_results_exact
    ON text_deterministic_results(run_id, exact_canonical_sha256);
CREATE INDEX IF NOT EXISTS idx_text_candidate_build_identity
    ON text_candidate_builds(run_id, source_snapshot_id, stage_version);

CREATE TRIGGER IF NOT EXISTS prevent_text_result_update
BEFORE UPDATE ON text_deterministic_results
BEGIN
    SELECT RAISE(ABORT, 'text deterministic results are append-only');
END;

CREATE TRIGGER IF NOT EXISTS prevent_text_result_delete
BEFORE DELETE ON text_deterministic_results
BEGIN
    SELECT RAISE(ABORT, 'text deterministic results are append-only');
END;

CREATE TRIGGER IF NOT EXISTS prevent_text_candidate_build_delete
BEFORE DELETE ON text_candidate_builds
BEGIN
    SELECT RAISE(ABORT, 'text candidate builds are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_text_candidate_corpus_member_update
BEFORE UPDATE ON text_candidate_corpus_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_candidate_corpus_member_delete
BEFORE DELETE ON text_candidate_corpus_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_exact_cluster_update
BEFORE UPDATE ON text_exact_clusters
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_exact_cluster_delete
BEFORE DELETE ON text_exact_clusters
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_exact_cluster_member_update
BEFORE UPDATE ON text_exact_cluster_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_exact_cluster_member_delete
BEFORE DELETE ON text_exact_cluster_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_near_pair_update
BEFORE UPDATE ON text_near_candidate_pairs
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_near_pair_delete
BEFORE DELETE ON text_near_candidate_pairs
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_near_component_update
BEFORE UPDATE ON text_near_candidate_components
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_near_component_delete
BEFORE DELETE ON text_near_candidate_components
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_near_component_member_update
BEFORE UPDATE ON text_near_candidate_component_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_near_component_member_delete
BEFORE DELETE ON text_near_candidate_component_members
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are immutable');
END;
"""

_SCHEMA_V4 = """
DROP TRIGGER IF EXISTS prevent_text_candidate_build_update;

CREATE TRIGGER IF NOT EXISTS require_text_candidate_building_insert
BEFORE INSERT ON text_candidate_builds
WHEN NEW.status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text candidate build must start in building state');
END;

CREATE TRIGGER IF NOT EXISTS prevent_text_candidate_build_identity_update
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

CREATE TRIGGER IF NOT EXISTS validate_text_candidate_build_seal
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

CREATE TRIGGER IF NOT EXISTS prevent_text_candidate_build_status_update
BEFORE UPDATE OF status ON text_candidate_builds
WHEN NOT (OLD.status = 'building' AND NEW.status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build status is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_finalized_corpus_member_insert
BEFORE INSERT ON text_candidate_corpus_members
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_exact_cluster_insert
BEFORE INSERT ON text_exact_clusters
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_exact_member_insert
BEFORE INSERT ON text_exact_cluster_members
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_near_pair_insert
BEFORE INSERT ON text_near_candidate_pairs
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_near_component_insert
BEFORE INSERT ON text_near_candidate_components
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_near_component_member_insert
BEFORE INSERT ON text_near_candidate_component_members
WHEN EXISTS (SELECT 1 FROM text_candidate_builds
             WHERE build_id = NEW.build_id AND status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text candidate build rows are sealed');
END;
"""

_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS text_sampling_runs (
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
    double_label_count INTEGER NOT NULL CHECK (double_label_count >= 0),
    periodic_round_number INTEGER NOT NULL DEFAULT 0 CHECK (periodic_round_number >= 0),
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (candidate_build_id, sample_kind, periodic_round_number, random_seed)
);

CREATE TABLE IF NOT EXISTS text_sample_members (
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
    requires_double_label INTEGER NOT NULL CHECK (requires_double_label IN (0, 1)),
    PRIMARY KEY (sample_run_id, source_post_id, sample_frame)
);

CREATE TABLE IF NOT EXISTS text_annotation_imports (
    import_id TEXT PRIMARY KEY,
    record_kind TEXT NOT NULL CHECK (
        record_kind IN ('post_annotation', 'post_adjudication',
                        'duplicate_annotation', 'duplicate_adjudication')
    ),
    guide_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    imported_by_hash TEXT NOT NULL CHECK (length(imported_by_hash) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (record_kind, source_sha256)
);

CREATE TABLE IF NOT EXISTS text_post_annotations (
    annotation_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    sample_run_id TEXT REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    annotator_hash TEXT NOT NULL CHECK (length(annotator_hash) = 64),
    assignment_slot INTEGER CHECK (assignment_slot IS NULL OR assignment_slot IN (1, 2)),
    structure_label TEXT NOT NULL CHECK (
        structure_label IN ('usable', 'invalid', 'uncertain')
    ),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain')
    ),
    commercial_label TEXT NOT NULL CHECK (
        commercial_label IN ('organic', 'promotion', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    guide_version TEXT NOT NULL,
    annotated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (sample_run_id, source_post_id, annotator_hash, assignment_slot, annotated_at_utc)
);

CREATE TABLE IF NOT EXISTS text_post_adjudications (
    adjudication_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    sample_run_id TEXT REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    adjudicator_hash TEXT NOT NULL CHECK (length(adjudicator_hash) = 64),
    structure_label TEXT NOT NULL CHECK (
        structure_label IN ('usable', 'invalid', 'uncertain')
    ),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain')
    ),
    commercial_label TEXT NOT NULL CHECK (
        commercial_label IN ('organic', 'promotion', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    evidence_annotation_ids_json TEXT NOT NULL,
    decision_context TEXT NOT NULL CHECK (
        decision_context IN ('gold', 'model_review', 'manual_review')
    ),
    guide_version TEXT NOT NULL,
    adjudicated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS text_near_duplicate_annotations (
    annotation_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    left_cluster_id TEXT NOT NULL,
    right_cluster_id TEXT NOT NULL,
    annotator_hash TEXT NOT NULL CHECK (length(annotator_hash) = 64),
    decision TEXT NOT NULL CHECK (decision IN ('duplicate', 'not_duplicate', 'uncertain')),
    reason_code TEXT NOT NULL,
    guide_version TEXT NOT NULL,
    annotated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    CHECK (left_cluster_id < right_cluster_id),
    FOREIGN KEY (build_id, left_cluster_id, right_cluster_id)
        REFERENCES text_near_candidate_pairs(build_id, left_cluster_id, right_cluster_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS text_near_duplicate_adjudications (
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

CREATE TABLE IF NOT EXISTS text_leakage_builds (
    leakage_build_id TEXT PRIMARY KEY,
    candidate_build_id TEXT NOT NULL REFERENCES text_candidate_builds(build_id) ON DELETE RESTRICT,
    adjudication_manifest_sha256 TEXT NOT NULL CHECK (length(adjudication_manifest_sha256) = 64),
    input_post_count INTEGER NOT NULL CHECK (input_post_count >= 0),
    component_count INTEGER NOT NULL CHECK (component_count >= 0),
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (candidate_build_id, adjudication_manifest_sha256)
);

CREATE TABLE IF NOT EXISTS text_leakage_members (
    leakage_build_id TEXT NOT NULL REFERENCES text_leakage_builds(leakage_build_id) ON DELETE RESTRICT,
    component_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    author_edge_used INTEGER NOT NULL CHECK (author_edge_used IN (0, 1)),
    exact_edge_used INTEGER NOT NULL CHECK (exact_edge_used IN (0, 1)),
    confirmed_near_edge_used INTEGER NOT NULL CHECK (confirmed_near_edge_used IN (0, 1)),
    PRIMARY KEY (leakage_build_id, source_post_id, source_version)
);

CREATE TABLE IF NOT EXISTS text_model_runs (
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
);

CREATE TABLE IF NOT EXISTS text_dataset_splits (
    model_run_id TEXT NOT NULL REFERENCES text_model_runs(model_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    component_id TEXT NOT NULL,
    split_name TEXT NOT NULL CHECK (split_name IN ('train', 'validation', 'test')),
    PRIMARY KEY (model_run_id, source_post_id, source_version)
);

CREATE TABLE IF NOT EXISTS text_model_predictions (
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

CREATE INDEX IF NOT EXISTS idx_text_annotations_post
    ON text_post_annotations(source_post_id, source_version, guide_version);
CREATE INDEX IF NOT EXISTS idx_text_adjudications_post
    ON text_post_adjudications(source_post_id, source_version, guide_version);
CREATE INDEX IF NOT EXISTS idx_text_duplicate_adjudications_pair
    ON text_near_duplicate_adjudications(build_id, left_cluster_id, right_cluster_id);

CREATE TRIGGER IF NOT EXISTS prevent_text_sampling_run_update
BEFORE UPDATE ON text_sampling_runs BEGIN
    SELECT RAISE(ABORT, 'text sampling runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_sampling_run_delete
BEFORE DELETE ON text_sampling_runs BEGIN
    SELECT RAISE(ABORT, 'text sampling runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_sample_member_update
BEFORE UPDATE ON text_sample_members BEGIN
    SELECT RAISE(ABORT, 'text sample members are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_sample_member_delete
BEFORE DELETE ON text_sample_members BEGIN
    SELECT RAISE(ABORT, 'text sample members are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_annotation_import_update
BEFORE UPDATE ON text_annotation_imports BEGIN
    SELECT RAISE(ABORT, 'text annotation imports are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_annotation_import_delete
BEFORE DELETE ON text_annotation_imports BEGIN
    SELECT RAISE(ABORT, 'text annotation imports are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_post_annotation_update
BEFORE UPDATE ON text_post_annotations BEGIN
    SELECT RAISE(ABORT, 'text post annotations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_post_annotation_delete
BEFORE DELETE ON text_post_annotations BEGIN
    SELECT RAISE(ABORT, 'text post annotations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_post_adjudication_update
BEFORE UPDATE ON text_post_adjudications BEGIN
    SELECT RAISE(ABORT, 'text post adjudications are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_post_adjudication_delete
BEFORE DELETE ON text_post_adjudications BEGIN
    SELECT RAISE(ABORT, 'text post adjudications are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_duplicate_annotation_update
BEFORE UPDATE ON text_near_duplicate_annotations BEGIN
    SELECT RAISE(ABORT, 'text duplicate annotations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_duplicate_annotation_delete
BEFORE DELETE ON text_near_duplicate_annotations BEGIN
    SELECT RAISE(ABORT, 'text duplicate annotations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_duplicate_adjudication_update
BEFORE UPDATE ON text_near_duplicate_adjudications BEGIN
    SELECT RAISE(ABORT, 'text duplicate adjudications are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_duplicate_adjudication_delete
BEFORE DELETE ON text_near_duplicate_adjudications BEGIN
    SELECT RAISE(ABORT, 'text duplicate adjudications are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_leakage_build_update
BEFORE UPDATE ON text_leakage_builds BEGIN
    SELECT RAISE(ABORT, 'text leakage builds are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_leakage_build_delete
BEFORE DELETE ON text_leakage_builds BEGIN
    SELECT RAISE(ABORT, 'text leakage builds are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_leakage_member_update
BEFORE UPDATE ON text_leakage_members BEGIN
    SELECT RAISE(ABORT, 'text leakage members are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_leakage_member_delete
BEFORE DELETE ON text_leakage_members BEGIN
    SELECT RAISE(ABORT, 'text leakage members are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_model_run_update
BEFORE UPDATE ON text_model_runs BEGIN
    SELECT RAISE(ABORT, 'text model runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_model_run_delete
BEFORE DELETE ON text_model_runs BEGIN
    SELECT RAISE(ABORT, 'text model runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_dataset_split_update
BEFORE UPDATE ON text_dataset_splits BEGIN
    SELECT RAISE(ABORT, 'text dataset splits are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_dataset_split_delete
BEFORE DELETE ON text_dataset_splits BEGIN
    SELECT RAISE(ABORT, 'text dataset splits are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_model_prediction_update
BEFORE UPDATE ON text_model_predictions BEGIN
    SELECT RAISE(ABORT, 'text model predictions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_model_prediction_delete
BEFORE DELETE ON text_model_predictions BEGIN
    SELECT RAISE(ABORT, 'text model predictions are immutable');
END;
"""

_SCHEMA_V6 = """
CREATE INDEX IF NOT EXISTS idx_text_adjudications_model_run
    ON text_post_adjudications(model_run_id, source_post_id, source_version);
"""

_SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS text_double_label_supplements (
    supplement_run_id TEXT PRIMARY KEY,
    sample_run_id TEXT NOT NULL REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    trigger_evaluation_sha256 TEXT NOT NULL CHECK (length(trigger_evaluation_sha256) = 64),
    requested_count INTEGER NOT NULL CHECK (requested_count > 0),
    selected_count INTEGER NOT NULL CHECK (selected_count >= 0),
    member_manifest_sha256 TEXT NOT NULL CHECK (length(member_manifest_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (sample_run_id, sequence_number),
    UNIQUE (sample_run_id, trigger_evaluation_sha256)
);

CREATE TABLE IF NOT EXISTS text_double_label_supplement_members (
    supplement_run_id TEXT NOT NULL REFERENCES text_double_label_supplements(supplement_run_id)
        ON DELETE RESTRICT,
    sample_run_id TEXT NOT NULL REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id)
        ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    selection_rank INTEGER NOT NULL CHECK (selection_rank > 0),
    PRIMARY KEY (supplement_run_id, source_post_id, source_version),
    UNIQUE (sample_run_id, source_post_id, source_version)
);

CREATE TABLE IF NOT EXISTS text_agreement_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    sample_run_id TEXT NOT NULL REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    input_manifest_sha256 TEXT NOT NULL CHECK (length(input_manifest_sha256) = 64),
    status TEXT NOT NULL CHECK (
        status IN ('incomplete', 'passed', 'supplement_created', 'supplement_exhausted')
    ),
    planned_pair_count INTEGER NOT NULL CHECK (planned_pair_count >= 0),
    complete_pair_count INTEGER NOT NULL CHECK (
        complete_pair_count >= 0 AND complete_pair_count <= planned_pair_count
    ),
    metrics_json TEXT,
    additional_double_label_required INTEGER NOT NULL CHECK (
        additional_double_label_required >= 0
    ),
    supplement_run_id TEXT REFERENCES text_double_label_supplements(supplement_run_id)
        ON DELETE RESTRICT,
    created_at_utc TEXT NOT NULL,
    UNIQUE (sample_run_id, input_manifest_sha256)
);

CREATE TRIGGER IF NOT EXISTS prevent_double_label_supplement_update
BEFORE UPDATE ON text_double_label_supplements BEGIN
    SELECT RAISE(ABORT, 'double-label supplements are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_double_label_supplement_delete
BEFORE DELETE ON text_double_label_supplements BEGIN
    SELECT RAISE(ABORT, 'double-label supplements are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_double_label_supplement_member_update
BEFORE UPDATE ON text_double_label_supplement_members BEGIN
    SELECT RAISE(ABORT, 'double-label supplement members are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_double_label_supplement_member_delete
BEFORE DELETE ON text_double_label_supplement_members BEGIN
    SELECT RAISE(ABORT, 'double-label supplement members are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_agreement_evaluation_update
BEFORE UPDATE ON text_agreement_evaluations BEGIN
    SELECT RAISE(ABORT, 'agreement evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_agreement_evaluation_delete
BEFORE DELETE ON text_agreement_evaluations BEGIN
    SELECT RAISE(ABORT, 'agreement evaluations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS reject_duplicate_annotation_slot
BEFORE INSERT ON text_post_annotations
WHEN NEW.sample_run_id IS NOT NULL
 AND NEW.assignment_slot IN (1, 2)
 AND EXISTS (
     SELECT 1 FROM text_post_annotations
     WHERE sample_run_id = NEW.sample_run_id
       AND source_post_id = NEW.source_post_id
       AND source_version = NEW.source_version
       AND assignment_slot = NEW.assignment_slot
 )
BEGIN
    SELECT RAISE(ABORT, 'annotation assignment slot already filled');
END;

CREATE TRIGGER IF NOT EXISTS reject_same_annotator_in_both_slots
BEFORE INSERT ON text_post_annotations
WHEN NEW.sample_run_id IS NOT NULL
 AND NEW.assignment_slot IN (1, 2)
 AND EXISTS (
     SELECT 1 FROM text_post_annotations
     WHERE sample_run_id = NEW.sample_run_id
       AND source_post_id = NEW.source_post_id
       AND source_version = NEW.source_version
       AND assignment_slot IN (1, 2)
       AND annotator_hash = NEW.annotator_hash
 )
BEGIN
    SELECT RAISE(ABORT, 'double-label annotators must differ');
END;

CREATE TRIGGER IF NOT EXISTS validate_double_label_adjudication_evidence
BEFORE INSERT ON text_post_adjudications
WHEN NEW.sample_run_id IS NOT NULL
 AND NEW.decision_context = 'gold'
 AND (
     EXISTS (
         SELECT 1 FROM text_sample_members
         WHERE sample_run_id = NEW.sample_run_id
           AND source_post_id = NEW.source_post_id
           AND source_version = NEW.source_version
           AND requires_double_label = 1
     )
     OR EXISTS (
         SELECT 1 FROM text_double_label_supplement_members
         WHERE sample_run_id = NEW.sample_run_id
           AND source_post_id = NEW.source_post_id
           AND source_version = NEW.source_version
     )
 )
 AND (
     json_valid(NEW.evidence_annotation_ids_json) = 0
     OR json_array_length(NEW.evidence_annotation_ids_json) != 2
     OR (
         SELECT COUNT(*)
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
         WHERE a.sample_run_id = NEW.sample_run_id
           AND a.source_post_id = NEW.source_post_id
           AND a.source_version = NEW.source_version
           AND a.guide_version = NEW.guide_version
           AND a.assignment_slot IN (1, 2)
     ) != 2
     OR (
         SELECT COUNT(DISTINCT a.assignment_slot)
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
     ) != 2
     OR (
         SELECT COUNT(DISTINCT a.annotator_hash)
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
     ) != 2
     OR EXISTS (
         SELECT 1
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
         WHERE a.annotator_hash = NEW.adjudicator_hash
     )
 )
BEGIN
    SELECT RAISE(ABORT, 'double-label adjudication evidence is invalid');
END;
"""

_SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS text_periodic_review_windows (
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
    created_at_utc TEXT NOT NULL,
    UNIQUE (baseline_sample_run_id, round_number)
);

CREATE TABLE IF NOT EXISTS text_periodic_review_window_members (
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

CREATE TRIGGER IF NOT EXISTS prevent_periodic_review_window_update
BEFORE UPDATE ON text_periodic_review_windows BEGIN
    SELECT RAISE(ABORT, 'periodic review windows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_periodic_review_window_delete
BEFORE DELETE ON text_periodic_review_windows BEGIN
    SELECT RAISE(ABORT, 'periodic review windows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_periodic_review_window_member_update
BEFORE UPDATE ON text_periodic_review_window_members BEGIN
    SELECT RAISE(ABORT, 'periodic review window members are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_periodic_review_window_member_delete
BEFORE DELETE ON text_periodic_review_window_members BEGIN
    SELECT RAISE(ABORT, 'periodic review window members are immutable');
END;
"""

_SCHEMA_V9 = """
DROP TRIGGER IF EXISTS prevent_text_sampling_run_update;
DROP TRIGGER IF EXISTS prevent_text_leakage_build_update;
DROP TRIGGER IF EXISTS prevent_text_model_run_update;

CREATE TRIGGER IF NOT EXISTS require_text_sampling_building_insert
BEFORE INSERT ON text_sampling_runs
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text sampling run must start in building state');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_sampling_identity_update
BEFORE UPDATE OF sample_run_id, run_id, source_snapshot_id, candidate_build_id,
                 baseline_sample_run_id, sample_kind, guide_version, random_seed,
                 population_manifest_sha256, population_count, probability_count,
                 targeted_count, double_label_count, periodic_round_number,
                 output_sha256, created_at_utc
ON text_sampling_runs
BEGIN
    SELECT RAISE(ABORT, 'text sampling runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_text_sampling_seal
BEFORE UPDATE OF seal_status ON text_sampling_runs
WHEN NEW.seal_status = 'finalized' AND (
    NEW.member_manifest_sha256 IS NULL
 OR NEW.member_manifest_sha256 != NEW.output_sha256
 OR (SELECT COUNT(*) FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND sample_frame = 'probability')
       != NEW.probability_count
 OR (SELECT COUNT(*) FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND sample_frame = 'targeted')
       != NEW.targeted_count
 OR (SELECT COUNT(*) FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND sample_frame = 'periodic_probability')
       != CASE WHEN NEW.sample_kind = 'periodic_review'
               THEN NEW.probability_count ELSE 0 END
 OR (SELECT COUNT(DISTINCT source_post_id || ':' || source_version)
     FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND requires_double_label = 1)
       != NEW.double_label_count
)
BEGIN
    SELECT RAISE(ABORT, 'text sampling run seal validation failed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_sampling_status_update
BEFORE UPDATE OF seal_status ON text_sampling_runs
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text sampling run status is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_sample_member_insert
BEFORE INSERT ON text_sample_members
WHEN EXISTS (SELECT 1 FROM text_sampling_runs
             WHERE sample_run_id = NEW.sample_run_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text sampling run rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS validate_sample_member_candidate_reference
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

CREATE TRIGGER IF NOT EXISTS require_text_leakage_building_insert
BEFORE INSERT ON text_leakage_builds
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text leakage build must start in building state');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_leakage_identity_update
BEFORE UPDATE OF leakage_build_id, candidate_build_id,
                 adjudication_manifest_sha256, input_post_count,
                 component_count, output_sha256, created_at_utc
ON text_leakage_builds
BEGIN
    SELECT RAISE(ABORT, 'text leakage builds are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_text_leakage_seal
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
CREATE TRIGGER IF NOT EXISTS prevent_text_leakage_status_update
BEFORE UPDATE OF seal_status ON text_leakage_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text leakage build status is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_leakage_member_insert
BEFORE INSERT ON text_leakage_members
WHEN EXISTS (SELECT 1 FROM text_leakage_builds
             WHERE leakage_build_id = NEW.leakage_build_id
               AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text leakage build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS validate_leakage_member_candidate_reference
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

CREATE TRIGGER IF NOT EXISTS validate_model_run_build_reference
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
CREATE TRIGGER IF NOT EXISTS require_text_model_building_insert
BEFORE INSERT ON text_model_runs
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'text model run must start in building state');
END;
CREATE TRIGGER IF NOT EXISTS prevent_text_model_identity_update
BEFORE UPDATE OF model_run_id, run_id, candidate_build_id, leakage_build_id,
                 guide_version, algorithm_version, config_sha256,
                 gold_manifest_sha256, split_manifest_sha256,
                 train_count, validation_count, test_count, chosen_c,
                 high_risk_threshold, low_risk_threshold, low_risk_enabled,
                 metrics_json, model_artifact_path, model_artifact_sha256,
                 status, expected_prediction_count, created_at_utc
ON text_model_runs
BEGIN
    SELECT RAISE(ABORT, 'text model runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_text_model_seal
BEFORE UPDATE OF seal_status ON text_model_runs
WHEN NEW.seal_status = 'finalized' AND (
    NEW.train_manifest_sha256 IS NULL
 OR NEW.validation_manifest_sha256 IS NULL
 OR NEW.test_manifest_sha256 IS NULL
 OR NEW.prediction_manifest_sha256 IS NULL
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
CREATE TRIGGER IF NOT EXISTS prevent_text_model_status_update
BEFORE UPDATE OF seal_status ON text_model_runs
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text model run status is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_dataset_split_insert
BEFORE INSERT ON text_dataset_splits
WHEN EXISTS (SELECT 1 FROM text_model_runs
             WHERE model_run_id = NEW.model_run_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text model run rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS validate_dataset_split_reference
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
CREATE TRIGGER IF NOT EXISTS prevent_finalized_model_prediction_insert
BEFORE INSERT ON text_model_predictions
WHEN EXISTS (SELECT 1 FROM text_model_runs
             WHERE model_run_id = NEW.model_run_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'text model run rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS validate_model_prediction_reference
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
CREATE TRIGGER IF NOT EXISTS validate_model_review_run_reference
BEFORE INSERT ON text_post_adjudications
WHEN NEW.model_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM text_model_runs
    WHERE model_run_id = NEW.model_run_id AND seal_status = 'finalized'
)
BEGIN
    SELECT RAISE(ABORT, 'model review references unknown or unsealed model run');
END;
"""

_SCHEMA_V10 = """
DROP TRIGGER IF EXISTS prevent_text_sampling_identity_update;
DROP TRIGGER IF EXISTS validate_text_sampling_seal;
DROP TRIGGER IF EXISTS prevent_text_leakage_identity_update;
DROP TRIGGER IF EXISTS prevent_text_model_identity_update;
DROP TRIGGER IF EXISTS validate_text_model_seal;
DROP TRIGGER IF EXISTS prevent_double_label_supplement_update;
DROP TRIGGER IF EXISTS prevent_periodic_review_window_update;

CREATE TRIGGER IF NOT EXISTS prevent_text_sampling_identity_update_v10
BEFORE UPDATE OF sample_run_id, run_id, source_snapshot_id, candidate_build_id,
                 baseline_sample_run_id, sample_kind, guide_version, random_seed,
                 population_manifest_sha256, population_count, probability_count,
                 targeted_count, double_label_count, periodic_round_number,
                 output_sha256, member_manifest_sha256, created_at_utc
ON text_sampling_runs
BEGIN
    SELECT RAISE(ABORT, 'text sampling runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_text_sampling_seal_v10
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
 OR (SELECT COUNT(DISTINCT source_post_id || ':' || source_version)
     FROM text_sample_members
     WHERE sample_run_id = NEW.sample_run_id AND requires_double_label = 1)
       != CASE WHEN NEW.sample_kind = 'initial' THEN NEW.double_label_count ELSE 0 END
)
BEGIN
    SELECT RAISE(ABORT, 'text sampling run seal validation failed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_sampling_parent_update
BEFORE UPDATE ON text_sampling_runs
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized text sampling runs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_text_leakage_identity_update_v10
BEFORE UPDATE OF leakage_build_id, candidate_build_id,
                 adjudication_manifest_sha256, input_post_count,
                 component_count, output_sha256, created_at_utc
ON text_leakage_builds
BEGIN
    SELECT RAISE(ABORT, 'text leakage builds are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_leakage_parent_update
BEFORE UPDATE ON text_leakage_builds
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized text leakage builds are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_text_model_identity_update_v10
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
CREATE TRIGGER IF NOT EXISTS validate_text_model_seal_v10
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
CREATE TRIGGER IF NOT EXISTS prevent_finalized_model_parent_update
BEFORE UPDATE ON text_model_runs
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized text model runs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS require_double_label_supplement_building_insert
BEFORE INSERT ON text_double_label_supplements
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'double-label supplement must start in building state');
END;
CREATE TRIGGER IF NOT EXISTS prevent_double_label_supplement_identity_update
BEFORE UPDATE OF supplement_run_id, sample_run_id, sequence_number,
                 trigger_evaluation_sha256, requested_count, selected_count,
                 member_manifest_sha256, created_at_utc
ON text_double_label_supplements
BEGIN
    SELECT RAISE(ABORT, 'double-label supplements are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_double_label_supplement_seal
BEFORE UPDATE OF seal_status ON text_double_label_supplements
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM text_double_label_supplement_members
     WHERE supplement_run_id = NEW.supplement_run_id) != NEW.selected_count
)
BEGIN
    SELECT RAISE(ABORT, 'double-label supplement seal validation failed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_double_label_supplement_status_update
BEFORE UPDATE OF seal_status ON text_double_label_supplements
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'double-label supplement status is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_double_label_supplement_update
BEFORE UPDATE ON text_double_label_supplements
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized double-label supplements are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_double_label_member_insert
BEFORE INSERT ON text_double_label_supplement_members
WHEN EXISTS (
    SELECT 1 FROM text_double_label_supplements
    WHERE supplement_run_id = NEW.supplement_run_id AND seal_status = 'finalized'
)
BEGIN
    SELECT RAISE(ABORT, 'double-label supplement rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS validate_double_label_member_parent
BEFORE INSERT ON text_double_label_supplement_members
WHEN NOT EXISTS (
    SELECT 1 FROM text_double_label_supplements
    WHERE supplement_run_id = NEW.supplement_run_id
      AND sample_run_id = NEW.sample_run_id
      AND seal_status = 'building'
)
BEGIN
    SELECT RAISE(ABORT, 'double-label supplement member parent mismatch');
END;

CREATE TRIGGER IF NOT EXISTS require_periodic_review_window_building_insert
BEFORE INSERT ON text_periodic_review_windows
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'periodic review window must start in building state');
END;
CREATE TRIGGER IF NOT EXISTS prevent_periodic_review_window_identity_update
BEFORE UPDATE OF sample_run_id, baseline_sample_run_id, candidate_build_id,
                 round_number, window_start_rank, window_end_rank,
                 new_post_count_at_freeze, window_member_count,
                 eligible_member_count, member_manifest_sha256, created_at_utc
ON text_periodic_review_windows
BEGIN
    SELECT RAISE(ABORT, 'periodic review windows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_periodic_review_window_seal
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
CREATE TRIGGER IF NOT EXISTS prevent_periodic_review_window_status_update
BEFORE UPDATE OF seal_status ON text_periodic_review_windows
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'periodic review window status is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_periodic_review_window_update
BEFORE UPDATE ON text_periodic_review_windows
WHEN OLD.seal_status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized periodic review windows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_periodic_review_member_insert
BEFORE INSERT ON text_periodic_review_window_members
WHEN EXISTS (
    SELECT 1 FROM text_periodic_review_windows
    WHERE sample_run_id = NEW.sample_run_id AND seal_status = 'finalized'
)
BEGIN
    SELECT RAISE(ABORT, 'periodic review window rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS validate_periodic_review_member_parent
BEFORE INSERT ON text_periodic_review_window_members
WHEN NOT EXISTS (
    SELECT 1 FROM text_periodic_review_windows
    WHERE sample_run_id = NEW.sample_run_id AND seal_status = 'building'
)
BEGIN
    SELECT RAISE(ABORT, 'periodic review window member parent mismatch');
END;
"""

_SCHEMA_V11 = """
CREATE TABLE IF NOT EXISTS image_manifest_imports (
    manifest_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    manifest_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    root_identity_sha256 TEXT NOT NULL CHECK (length(root_identity_sha256) = 64),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    accepted_row_count INTEGER NOT NULL CHECK (
        accepted_row_count >= 0 AND accepted_row_count <= row_count
    ),
    rejected_row_count INTEGER NOT NULL CHECK (
        rejected_row_count >= 0 AND rejected_row_count <= row_count
    ),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'accepted_with_rejections', 'rejected')),
    reason_code TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE (run_id, source_sha256)
);

CREATE TABLE IF NOT EXISTS image_manifest_rows (
    manifest_row_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES image_manifest_imports(manifest_id) ON DELETE RESTRICT,
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    source_image_id INTEGER NOT NULL REFERENCES source_image_inventory(source_image_id)
        ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id)
        ON DELETE RESTRICT,
    relation_role TEXT NOT NULL CHECK (relation_role IN ('author_avatar', 'page', 'content')),
    relative_path TEXT NOT NULL,
    expected_file_sha256 TEXT NOT NULL CHECK (length(expected_file_sha256) = 64),
    parent_file_sha256 TEXT CHECK (
        parent_file_sha256 IS NULL OR length(parent_file_sha256) = 64
    ),
    transform_json TEXT NOT NULL,
    row_identity_sha256 TEXT NOT NULL CHECK (length(row_identity_sha256) = 64),
    validation_status TEXT NOT NULL CHECK (
        validation_status IN ('accepted', 'rejected', 'source_conflict')
    ),
    reason_code TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE (manifest_id, row_number)
);

CREATE TABLE IF NOT EXISTS image_role_results (
    role_decision_id TEXT PRIMARY KEY,
    manifest_row_id TEXT NOT NULL REFERENCES image_manifest_rows(manifest_row_id)
        ON DELETE RESTRICT,
    role_version TEXT NOT NULL,
    relation_role TEXT NOT NULL CHECK (relation_role IN ('author_avatar', 'page', 'content')),
    handling_action TEXT NOT NULL CHECK (
        handling_action IN ('exclude_from_content', 'evidence_only', 'inspect_content')
    ),
    reason_code TEXT NOT NULL,
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (manifest_row_id, role_version)
);

CREATE TABLE IF NOT EXISTS image_processing_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    manifest_id TEXT REFERENCES image_manifest_imports(manifest_id) ON DELETE RESTRICT,
    manifest_row_id TEXT REFERENCES image_manifest_rows(manifest_row_id) ON DELETE RESTRICT,
    operation TEXT NOT NULL CHECK (
        operation IN ('roles', 'import_manifest', 'fingerprints', 'candidates')
    ),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'blocked', 'skipped', 'failed')),
    reason_code TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (run_id, operation, manifest_row_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS image_fingerprints (
    fingerprint_id TEXT PRIMARY KEY,
    manifest_row_id TEXT NOT NULL REFERENCES image_manifest_rows(manifest_row_id)
        ON DELETE RESTRICT,
    fingerprint_version TEXT NOT NULL,
    row_identity_sha256 TEXT NOT NULL CHECK (length(row_identity_sha256) = 64),
    file_sha256 TEXT NOT NULL CHECK (length(file_sha256) = 64),
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    width_px INTEGER NOT NULL CHECK (width_px > 0),
    height_px INTEGER NOT NULL CHECK (height_px > 0),
    has_alpha INTEGER NOT NULL CHECK (has_alpha IN (0, 1)),
    is_fully_transparent INTEGER NOT NULL CHECK (is_fully_transparent IN (0, 1)),
    sanitized_exif_json TEXT NOT NULL,
    phash_hex TEXT NOT NULL CHECK (length(phash_hex) = 16),
    phash_hash_size INTEGER NOT NULL CHECK (phash_hash_size > 0),
    phash_highfreq_factor INTEGER NOT NULL CHECK (phash_highfreq_factor > 0),
    library_versions_json TEXT NOT NULL,
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (manifest_row_id, fingerprint_version)
);

CREATE TABLE IF NOT EXISTS image_candidate_builds (
    build_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    manifest_id TEXT NOT NULL REFERENCES image_manifest_imports(manifest_id) ON DELETE RESTRICT,
    candidate_version TEXT NOT NULL,
    fingerprint_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    input_manifest_sha256 TEXT NOT NULL CHECK (length(input_manifest_sha256) = 64),
    expected_fingerprint_count INTEGER NOT NULL CHECK (expected_fingerprint_count >= 0),
    exact_cluster_count INTEGER NOT NULL CHECK (exact_cluster_count >= 0),
    exact_duplicate_cluster_count INTEGER NOT NULL CHECK (exact_duplicate_cluster_count >= 0),
    near_pair_count INTEGER NOT NULL CHECK (near_pair_count >= 0),
    signal_count INTEGER NOT NULL CHECK (signal_count >= 0),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (run_id, manifest_id, candidate_version, input_manifest_sha256)
);

CREATE TABLE IF NOT EXISTS image_candidate_build_members (
    build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    source_image_id INTEGER NOT NULL,
    source_post_id INTEGER NOT NULL,
    row_identity_sha256 TEXT NOT NULL CHECK (length(row_identity_sha256) = 64),
    PRIMARY KEY (build_id, fingerprint_id)
);

CREATE TABLE IF NOT EXISTS image_candidate_signals (
    build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    signal_code TEXT NOT NULL CHECK (
        signal_code IN ('url_role_hint', 'tiny_dimensions', 'tiny_file',
                        'extreme_aspect_ratio', 'fully_transparent', 'high_reuse')
    ),
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (build_id, fingerprint_id, signal_code)
);

CREATE TABLE IF NOT EXISTS image_exact_clusters (
    build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    cluster_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL CHECK (length(file_sha256) = 64),
    representative_fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id)
        ON DELETE RESTRICT,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    PRIMARY KEY (build_id, cluster_id),
    UNIQUE (build_id, file_sha256)
);

CREATE TABLE IF NOT EXISTS image_exact_cluster_members (
    build_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    is_representative INTEGER NOT NULL CHECK (is_representative IN (0, 1)),
    PRIMARY KEY (build_id, cluster_id, fingerprint_id),
    FOREIGN KEY (build_id, cluster_id)
        REFERENCES image_exact_clusters(build_id, cluster_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS image_near_candidate_pairs (
    build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    left_fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id)
        ON DELETE RESTRICT,
    right_fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id)
        ON DELETE RESTRICT,
    hamming_distance INTEGER NOT NULL CHECK (hamming_distance BETWEEN 0 AND 10),
    decision_status TEXT NOT NULL DEFAULT 'candidate' CHECK (decision_status = 'candidate'),
    PRIMARY KEY (build_id, left_fingerprint_id, right_fingerprint_id),
    CHECK (left_fingerprint_id < right_fingerprint_id)
);

CREATE INDEX IF NOT EXISTS idx_image_manifest_source
    ON image_manifest_rows(manifest_id, source_image_id, validation_status);
CREATE INDEX IF NOT EXISTS idx_image_manifest_path
    ON image_manifest_rows(manifest_id, relative_path);
CREATE INDEX IF NOT EXISTS idx_image_fingerprint_sha
    ON image_fingerprints(fingerprint_version, file_sha256);
CREATE INDEX IF NOT EXISTS idx_image_fingerprint_phash
    ON image_fingerprints(fingerprint_version, phash_hex);

CREATE TRIGGER IF NOT EXISTS require_image_candidate_building_insert
BEFORE INSERT ON image_candidate_builds
WHEN NEW.seal_status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'image candidate build must start in building state');
END;

CREATE TRIGGER IF NOT EXISTS validate_image_candidate_build_seal
BEFORE UPDATE OF seal_status ON image_candidate_builds
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM image_candidate_build_members WHERE build_id = NEW.build_id)
        != NEW.expected_fingerprint_count
 OR (SELECT COUNT(*) FROM image_exact_clusters WHERE build_id = NEW.build_id)
        != NEW.exact_cluster_count
 OR (SELECT COUNT(*) FROM image_exact_clusters
     WHERE build_id = NEW.build_id AND member_count > 1)
        != NEW.exact_duplicate_cluster_count
 OR (SELECT COUNT(*) FROM image_exact_cluster_members WHERE build_id = NEW.build_id)
        != NEW.expected_fingerprint_count
 OR EXISTS (
     SELECT 1 FROM image_exact_clusters AS c
     WHERE c.build_id = NEW.build_id
       AND c.member_count != (
           SELECT COUNT(*) FROM image_exact_cluster_members AS m
           WHERE m.build_id = c.build_id AND m.cluster_id = c.cluster_id
       )
 )
 OR (SELECT COUNT(*) FROM image_near_candidate_pairs WHERE build_id = NEW.build_id)
        != NEW.near_pair_count
 OR (SELECT COUNT(*) FROM image_candidate_signals WHERE build_id = NEW.build_id)
        != NEW.signal_count
)
BEGIN
    SELECT RAISE(ABORT, 'image candidate build counts do not match rows');
END;

CREATE TRIGGER IF NOT EXISTS prevent_image_candidate_build_identity_update
BEFORE UPDATE OF build_id, run_id, manifest_id, candidate_version, fingerprint_version,
                 config_sha256, input_manifest_sha256, expected_fingerprint_count,
                 exact_cluster_count, exact_duplicate_cluster_count, near_pair_count,
                 signal_count, output_sha256, created_at_utc
ON image_candidate_builds
BEGIN
    SELECT RAISE(ABORT, 'image candidate builds are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_image_candidate_build_status_update
BEFORE UPDATE OF seal_status ON image_candidate_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build status is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_image_candidate_build_delete
BEFORE DELETE ON image_candidate_builds
BEGIN
    SELECT RAISE(ABORT, 'image candidate builds are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_finalized_image_build_row_insert
BEFORE INSERT ON image_candidate_build_members
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;

CREATE TRIGGER IF NOT EXISTS prevent_image_manifest_import_update
BEFORE UPDATE ON image_manifest_imports BEGIN
    SELECT RAISE(ABORT, 'image manifest imports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_manifest_import_delete
BEFORE DELETE ON image_manifest_imports BEGIN
    SELECT RAISE(ABORT, 'image manifest imports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_manifest_row_update
BEFORE UPDATE ON image_manifest_rows BEGIN
    SELECT RAISE(ABORT, 'image manifest rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_manifest_row_delete
BEFORE DELETE ON image_manifest_rows BEGIN
    SELECT RAISE(ABORT, 'image manifest rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_role_update
BEFORE UPDATE ON image_role_results BEGIN
    SELECT RAISE(ABORT, 'image role decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_role_delete
BEFORE DELETE ON image_role_results BEGIN
    SELECT RAISE(ABORT, 'image role decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_attempt_update
BEFORE UPDATE ON image_processing_attempts BEGIN
    SELECT RAISE(ABORT, 'image processing attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_attempt_delete
BEFORE DELETE ON image_processing_attempts BEGIN
    SELECT RAISE(ABORT, 'image processing attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_fingerprint_update
BEFORE UPDATE ON image_fingerprints BEGIN
    SELECT RAISE(ABORT, 'image fingerprints are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_fingerprint_delete
BEFORE DELETE ON image_fingerprints BEGIN
    SELECT RAISE(ABORT, 'image fingerprints are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_build_member_update
BEFORE UPDATE ON image_candidate_build_members BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_build_member_delete
BEFORE DELETE ON image_candidate_build_members BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_signal_update
BEFORE UPDATE ON image_candidate_signals BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_signal_delete
BEFORE DELETE ON image_candidate_signals BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_exact_cluster_update
BEFORE UPDATE ON image_exact_clusters BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_exact_cluster_delete
BEFORE DELETE ON image_exact_clusters BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_exact_member_update
BEFORE UPDATE ON image_exact_cluster_members BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_exact_member_delete
BEFORE DELETE ON image_exact_cluster_members BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_near_pair_update
BEFORE UPDATE ON image_near_candidate_pairs BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_image_near_pair_delete
BEFORE DELETE ON image_near_candidate_pairs BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_finalized_image_signal_insert
BEFORE INSERT ON image_candidate_signals
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_image_exact_cluster_insert
BEFORE INSERT ON image_exact_clusters
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_image_exact_member_insert
BEFORE INSERT ON image_exact_cluster_members
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
CREATE TRIGGER IF NOT EXISTS prevent_finalized_image_near_pair_insert
BEFORE INSERT ON image_near_candidate_pairs
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
"""


_SCHEMA_V12 = """
-- v11 的子表只按全局 fingerprint 外键约束，无法阻止把另一个 build 的成员
-- 拼入当前构建。v12 重建四张子表，使所有指纹外键都同时绑定 build_id。
DROP TRIGGER IF EXISTS validate_image_candidate_build_seal;

CREATE TABLE image_candidate_signals_v12 (
    build_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    signal_code TEXT NOT NULL CHECK (
        signal_code IN ('url_role_hint', 'tiny_dimensions', 'tiny_file',
                        'extreme_aspect_ratio', 'fully_transparent', 'high_reuse')
    ),
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (build_id, fingerprint_id, signal_code),
    FOREIGN KEY (build_id, fingerprint_id)
        REFERENCES image_candidate_build_members(build_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TABLE image_exact_clusters_v12 (
    build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    cluster_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL CHECK (length(file_sha256) = 64),
    representative_fingerprint_id TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    PRIMARY KEY (build_id, cluster_id),
    UNIQUE (build_id, file_sha256),
    FOREIGN KEY (build_id, representative_fingerprint_id)
        REFERENCES image_candidate_build_members(build_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TABLE image_exact_cluster_members_v12 (
    build_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    is_representative INTEGER NOT NULL CHECK (is_representative IN (0, 1)),
    PRIMARY KEY (build_id, cluster_id, fingerprint_id),
    UNIQUE (build_id, fingerprint_id),
    FOREIGN KEY (build_id, cluster_id)
        REFERENCES image_exact_clusters_v12(build_id, cluster_id) ON DELETE RESTRICT,
    FOREIGN KEY (build_id, fingerprint_id)
        REFERENCES image_candidate_build_members(build_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TABLE image_near_candidate_pairs_v12 (
    build_id TEXT NOT NULL,
    left_fingerprint_id TEXT NOT NULL,
    right_fingerprint_id TEXT NOT NULL,
    hamming_distance INTEGER NOT NULL CHECK (hamming_distance BETWEEN 0 AND 10),
    decision_status TEXT NOT NULL DEFAULT 'candidate' CHECK (decision_status = 'candidate'),
    PRIMARY KEY (build_id, left_fingerprint_id, right_fingerprint_id),
    CHECK (left_fingerprint_id < right_fingerprint_id),
    FOREIGN KEY (build_id, left_fingerprint_id)
        REFERENCES image_candidate_build_members(build_id, fingerprint_id) ON DELETE RESTRICT,
    FOREIGN KEY (build_id, right_fingerprint_id)
        REFERENCES image_candidate_build_members(build_id, fingerprint_id) ON DELETE RESTRICT
);

INSERT INTO image_candidate_signals_v12
SELECT * FROM image_candidate_signals;
INSERT INTO image_exact_clusters_v12
SELECT * FROM image_exact_clusters;
INSERT INTO image_exact_cluster_members_v12
SELECT * FROM image_exact_cluster_members;
INSERT INTO image_near_candidate_pairs_v12
SELECT * FROM image_near_candidate_pairs;

DROP TABLE image_near_candidate_pairs;
DROP TABLE image_exact_cluster_members;
DROP TABLE image_exact_clusters;
DROP TABLE image_candidate_signals;

ALTER TABLE image_candidate_signals_v12 RENAME TO image_candidate_signals;
ALTER TABLE image_exact_clusters_v12 RENAME TO image_exact_clusters;
ALTER TABLE image_exact_cluster_members_v12 RENAME TO image_exact_cluster_members;
ALTER TABLE image_near_candidate_pairs_v12 RENAME TO image_near_candidate_pairs;

CREATE TRIGGER validate_image_candidate_build_seal
BEFORE UPDATE OF seal_status ON image_candidate_builds
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM image_candidate_build_members WHERE build_id = NEW.build_id)
        != NEW.expected_fingerprint_count
 OR (SELECT COUNT(*) FROM image_exact_clusters WHERE build_id = NEW.build_id)
        != NEW.exact_cluster_count
 OR (SELECT COUNT(*) FROM image_exact_clusters
     WHERE build_id = NEW.build_id AND member_count > 1)
        != NEW.exact_duplicate_cluster_count
 OR (SELECT COUNT(*) FROM image_exact_cluster_members WHERE build_id = NEW.build_id)
        != NEW.expected_fingerprint_count
 OR EXISTS (
     SELECT 1 FROM image_exact_clusters AS c
     WHERE c.build_id = NEW.build_id
       AND c.member_count != (
           SELECT COUNT(*) FROM image_exact_cluster_members AS m
           WHERE m.build_id = c.build_id AND m.cluster_id = c.cluster_id
       )
 )
 OR EXISTS (
     SELECT 1 FROM image_exact_clusters AS c
     WHERE c.build_id = NEW.build_id
       AND NOT EXISTS (
           SELECT 1 FROM image_exact_cluster_members AS m
           WHERE m.build_id = c.build_id AND m.cluster_id = c.cluster_id
             AND m.fingerprint_id = c.representative_fingerprint_id
       )
 )
 OR EXISTS (
     SELECT 1 FROM image_exact_clusters AS c
     WHERE c.build_id = NEW.build_id
       AND 1 != (
           SELECT COUNT(*) FROM image_exact_cluster_members AS m
           WHERE m.build_id = c.build_id AND m.cluster_id = c.cluster_id
             AND m.is_representative = 1
       )
 )
 OR EXISTS (
     SELECT 1 FROM image_exact_cluster_members AS m
     JOIN image_exact_clusters AS c
       ON c.build_id = m.build_id AND c.cluster_id = m.cluster_id
     WHERE m.build_id = NEW.build_id AND m.is_representative = 1
       AND m.fingerprint_id != c.representative_fingerprint_id
 )
 OR (SELECT COUNT(*) FROM image_near_candidate_pairs WHERE build_id = NEW.build_id)
        != NEW.near_pair_count
 OR (SELECT COUNT(*) FROM image_candidate_signals WHERE build_id = NEW.build_id)
        != NEW.signal_count
)
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are incomplete');
END;

CREATE TRIGGER prevent_image_signal_update
BEFORE UPDATE ON image_candidate_signals BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER prevent_image_signal_delete
BEFORE DELETE ON image_candidate_signals BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER prevent_image_exact_cluster_update
BEFORE UPDATE ON image_exact_clusters BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER prevent_image_exact_cluster_delete
BEFORE DELETE ON image_exact_clusters BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER prevent_image_exact_member_update
BEFORE UPDATE ON image_exact_cluster_members BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER prevent_image_exact_member_delete
BEFORE DELETE ON image_exact_cluster_members BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER prevent_image_near_pair_update
BEFORE UPDATE ON image_near_candidate_pairs BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;
CREATE TRIGGER prevent_image_near_pair_delete
BEFORE DELETE ON image_near_candidate_pairs BEGIN
    SELECT RAISE(ABORT, 'image candidate rows are immutable');
END;

CREATE TRIGGER prevent_finalized_image_signal_insert
BEFORE INSERT ON image_candidate_signals
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
CREATE TRIGGER prevent_finalized_image_exact_cluster_insert
BEFORE INSERT ON image_exact_clusters
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
CREATE TRIGGER prevent_finalized_image_exact_member_insert
BEFORE INSERT ON image_exact_cluster_members
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
CREATE TRIGGER prevent_finalized_image_near_pair_insert
BEFORE INSERT ON image_near_candidate_pairs
WHEN EXISTS (SELECT 1 FROM image_candidate_builds
             WHERE build_id = NEW.build_id AND seal_status = 'finalized')
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are sealed');
END;
"""


_SCHEMA_V13 = """
-- v12 已保证候选子表只引用同 build 成员；v13 进一步保证成员本身来自 build
-- 绑定的 manifest、指纹版本和 manifest 行上下文。
DROP TRIGGER IF EXISTS validate_image_candidate_build_seal;

CREATE TRIGGER validate_image_build_member_context
BEFORE INSERT ON image_candidate_build_members
WHEN NOT EXISTS (
    SELECT 1
    FROM image_candidate_builds AS b
    JOIN image_fingerprints AS f ON f.fingerprint_id = NEW.fingerprint_id
    JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
    WHERE b.build_id = NEW.build_id
      AND r.manifest_id = b.manifest_id
      AND f.fingerprint_version = b.fingerprint_version
      AND r.source_image_id = NEW.source_image_id
      AND r.source_post_id = NEW.source_post_id
      AND r.row_identity_sha256 = NEW.row_identity_sha256
      AND f.row_identity_sha256 = NEW.row_identity_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'image build member context mismatch');
END;

CREATE TRIGGER validate_image_candidate_build_seal
BEFORE UPDATE OF seal_status ON image_candidate_builds
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM image_candidate_build_members WHERE build_id = NEW.build_id)
        != NEW.expected_fingerprint_count
 OR EXISTS (
     SELECT 1
     FROM image_candidate_build_members AS m
     JOIN image_fingerprints AS f ON f.fingerprint_id = m.fingerprint_id
     JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
     WHERE m.build_id = NEW.build_id
       AND (r.manifest_id != NEW.manifest_id
            OR f.fingerprint_version != NEW.fingerprint_version
            OR r.source_image_id != m.source_image_id
            OR r.source_post_id != m.source_post_id
            OR r.row_identity_sha256 != m.row_identity_sha256
            OR f.row_identity_sha256 != m.row_identity_sha256)
 )
 OR (SELECT COUNT(*) FROM image_exact_clusters WHERE build_id = NEW.build_id)
        != NEW.exact_cluster_count
 OR (SELECT COUNT(*) FROM image_exact_clusters
     WHERE build_id = NEW.build_id AND member_count > 1)
        != NEW.exact_duplicate_cluster_count
 OR (SELECT COUNT(*) FROM image_exact_cluster_members WHERE build_id = NEW.build_id)
        != NEW.expected_fingerprint_count
 OR EXISTS (
     SELECT 1 FROM image_exact_clusters AS c
     WHERE c.build_id = NEW.build_id
       AND c.member_count != (
           SELECT COUNT(*) FROM image_exact_cluster_members AS m
           WHERE m.build_id = c.build_id AND m.cluster_id = c.cluster_id
       )
 )
 OR EXISTS (
     SELECT 1 FROM image_exact_clusters AS c
     WHERE c.build_id = NEW.build_id
       AND NOT EXISTS (
           SELECT 1 FROM image_exact_cluster_members AS m
           WHERE m.build_id = c.build_id AND m.cluster_id = c.cluster_id
             AND m.fingerprint_id = c.representative_fingerprint_id
       )
 )
 OR EXISTS (
     SELECT 1 FROM image_exact_clusters AS c
     WHERE c.build_id = NEW.build_id
       AND 1 != (
           SELECT COUNT(*) FROM image_exact_cluster_members AS m
           WHERE m.build_id = c.build_id AND m.cluster_id = c.cluster_id
             AND m.is_representative = 1
       )
 )
 OR EXISTS (
     SELECT 1 FROM image_exact_cluster_members AS m
     JOIN image_exact_clusters AS c
       ON c.build_id = m.build_id AND c.cluster_id = m.cluster_id
     WHERE m.build_id = NEW.build_id AND m.is_representative = 1
       AND m.fingerprint_id != c.representative_fingerprint_id
 )
 OR (SELECT COUNT(*) FROM image_near_candidate_pairs WHERE build_id = NEW.build_id)
        != NEW.near_pair_count
 OR (SELECT COUNT(*) FROM image_candidate_signals WHERE build_id = NEW.build_id)
        != NEW.signal_count
)
BEGIN
    SELECT RAISE(ABORT, 'image candidate build rows are incomplete');
END;
"""


_SCHEMA_V14 = """
-- 运行身份一经创建即冻结；状态机只允许更新下方未列出的状态、时间和错误字段。
CREATE TRIGGER prevent_cleaning_run_identity_update
BEFORE UPDATE OF run_id, protocol_version, config_sha256, random_seed,
                 code_version, environment_json, created_at_utc, run_type
ON cleaning_runs
BEGIN
    SELECT RAISE(ABORT, 'cleaning run identity is immutable');
END;

-- 快照绑定必须由创建流程在快照父行落库后执行一次，之后不能换绑或清空。
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

CREATE TRIGGER prevent_cleaning_run_delete
BEFORE DELETE ON cleaning_runs
BEGIN
    SELECT RAISE(ABORT, 'cleaning runs are immutable');
END;

-- source_snapshots 全行都是来源、文件、输入契约、计数、代码与创建时间谱系；
-- 没有状态机字段，因此整行只允许追加，不允许修改或删除。
CREATE TRIGGER prevent_source_snapshot_update
BEFORE UPDATE ON source_snapshots
BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;

CREATE TRIGGER prevent_source_snapshot_delete
BEFORE DELETE ON source_snapshots
BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;
"""


_SCHEMA_V15 = """
-- v2.4 的 64 位 DCT pHash 固定使用 8/4；近似对距离上限已由表 CHECK 固定为 10。
CREATE TRIGGER validate_image_fingerprint_algorithm_parameters
BEFORE INSERT ON image_fingerprints
WHEN NEW.phash_hash_size != 8 OR NEW.phash_highfreq_factor != 4
BEGIN
    SELECT RAISE(ABORT, 'image fingerprint algorithm parameters mismatch');
END;
"""


_SCHEMA_V16 = """
-- 图片人工复核始终绑定已封存的 Issue #9 候选构建；角色和文件阻塞不进入本层。
CREATE TABLE image_review_runs (
    review_run_id TEXT PRIMARY KEY,
    candidate_build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    review_kind TEXT NOT NULL CHECK (review_kind IN ('pilot', 'candidate_review', 'boundary')),
    guide_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    random_seed INTEGER NOT NULL,
    code_version TEXT NOT NULL,
    planned_count INTEGER NOT NULL CHECK (planned_count >= 0),
    member_count INTEGER NOT NULL CHECK (member_count >= 0),
    member_manifest_sha256 TEXT NOT NULL CHECK (length(member_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (candidate_build_id, review_kind, guide_version, config_sha256,
            random_seed, member_manifest_sha256)
);

CREATE TABLE image_review_members (
    review_run_id TEXT NOT NULL REFERENCES image_review_runs(review_run_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    exact_cluster_id TEXT NOT NULL,
    review_reason TEXT NOT NULL CHECK (
        review_reason IN ('pilot', 'technical_signal', 'phash_candidate',
                          'candidate_boundary', 'noncandidate_boundary')
    ),
    stable_rank INTEGER NOT NULL CHECK (stable_rank > 0),
    requires_double_label INTEGER NOT NULL CHECK (requires_double_label IN (0, 1)),
    PRIMARY KEY (review_run_id, fingerprint_id),
    UNIQUE (review_run_id, stable_rank)
);

CREATE TABLE image_phash_review_groups (
    review_run_id TEXT NOT NULL REFERENCES image_review_runs(review_run_id) ON DELETE RESTRICT,
    group_id TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    maximum_pair_distance INTEGER NOT NULL CHECK (maximum_pair_distance BETWEEN 0 AND 10),
    group_manifest_sha256 TEXT NOT NULL CHECK (length(group_manifest_sha256) = 64),
    PRIMARY KEY (review_run_id, group_id)
);

CREATE TABLE image_phash_review_group_members (
    review_run_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    stable_rank INTEGER NOT NULL CHECK (stable_rank > 0),
    PRIMARY KEY (review_run_id, group_id, fingerprint_id),
    UNIQUE (review_run_id, fingerprint_id),
    FOREIGN KEY (review_run_id, group_id)
        REFERENCES image_phash_review_groups(review_run_id, group_id) ON DELETE RESTRICT,
    FOREIGN KEY (review_run_id, fingerprint_id)
        REFERENCES image_review_members(review_run_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TABLE image_double_label_plans (
    plan_id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL REFERENCES image_review_runs(review_run_id) ON DELETE RESTRICT,
    plan_kind TEXT NOT NULL CHECK (
        plan_kind IN ('boundary', 'boundary_supplement', 'proposed_exclusion')
    ),
    guide_version TEXT NOT NULL,
    requested_count INTEGER NOT NULL CHECK (requested_count >= 0),
    member_count INTEGER NOT NULL CHECK (member_count >= 0),
    member_manifest_sha256 TEXT NOT NULL CHECK (length(member_manifest_sha256) = 64),
    source_evidence_sha256 TEXT NOT NULL CHECK (length(source_evidence_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (review_run_id, plan_kind, source_evidence_sha256)
);

CREATE TABLE image_double_label_plan_members (
    plan_id TEXT NOT NULL REFERENCES image_double_label_plans(plan_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    stable_rank INTEGER NOT NULL CHECK (stable_rank > 0),
    reason_code TEXT NOT NULL,
    PRIMARY KEY (plan_id, fingerprint_id),
    UNIQUE (plan_id, stable_rank)
);

CREATE TABLE image_annotation_imports (
    import_id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL REFERENCES image_review_runs(review_run_id) ON DELETE RESTRICT,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    imported_by_hash TEXT NOT NULL CHECK (length(imported_by_hash) = 64),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    accepted_count INTEGER NOT NULL CHECK (accepted_count >= 0),
    rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'accepted_with_rejections', 'rejected')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (review_run_id, source_sha256)
);

CREATE TABLE image_review_annotations (
    annotation_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES image_annotation_imports(import_id) ON DELETE RESTRICT,
    review_run_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    assignment_slot INTEGER NOT NULL CHECK (assignment_slot IN (1, 2)),
    annotator_hash TEXT NOT NULL CHECK (length(annotator_hash) = 64),
    guide_version TEXT NOT NULL,
    technical_noise_label TEXT NOT NULL CHECK (
        technical_noise_label IN ('valid_content', 'site_background', 'site_ui',
          'placeholder_or_error', 'tracking_or_qr_only', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    technical_flags_json TEXT NOT NULL,
    annotated_at_utc TEXT NOT NULL,
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64),
    UNIQUE (review_run_id, fingerprint_id, assignment_slot),
    FOREIGN KEY (review_run_id, fingerprint_id)
        REFERENCES image_review_members(review_run_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TABLE image_review_adjudications (
    adjudication_id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    left_annotation_id TEXT NOT NULL REFERENCES image_review_annotations(annotation_id) ON DELETE RESTRICT,
    right_annotation_id TEXT NOT NULL REFERENCES image_review_annotations(annotation_id) ON DELETE RESTRICT,
    adjudicator_hash TEXT NOT NULL CHECK (length(adjudicator_hash) = 64),
    guide_version TEXT NOT NULL,
    technical_noise_label TEXT NOT NULL CHECK (
        technical_noise_label IN ('valid_content', 'site_background', 'site_ui',
          'placeholder_or_error', 'tracking_or_qr_only', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    adjudicated_at_utc TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    UNIQUE (review_run_id, fingerprint_id, left_annotation_id, right_annotation_id),
    FOREIGN KEY (review_run_id, fingerprint_id)
        REFERENCES image_review_members(review_run_id, fingerprint_id) ON DELETE RESTRICT,
    CHECK (left_annotation_id < right_annotation_id)
);

CREATE TABLE image_agreement_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL REFERENCES image_review_runs(review_run_id) ON DELETE RESTRICT,
    plan_manifest_sha256 TEXT NOT NULL CHECK (length(plan_manifest_sha256) = 64),
    planned_pair_count INTEGER NOT NULL CHECK (planned_pair_count >= 0),
    complete_pair_count INTEGER NOT NULL CHECK (complete_pair_count >= 0),
    agreement_count INTEGER NOT NULL CHECK (agreement_count >= 0),
    raw_agreement REAL,
    cohen_kappa REAL,
    kappa_status TEXT NOT NULL CHECK (
        kappa_status IN ('estimated', 'undefined_single_category', 'incomplete')
    ),
    evaluation_status TEXT NOT NULL CHECK (
        evaluation_status IN ('incomplete', 'passed', 'supplement_required')
    ),
    label_disagreements_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (review_run_id, plan_manifest_sha256)
);

CREATE TABLE image_decision_builds (
    decision_build_id TEXT PRIMARY KEY,
    candidate_build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    guide_version TEXT NOT NULL,
    evidence_manifest_sha256 TEXT NOT NULL CHECK (length(evidence_manifest_sha256) = 64),
    expected_decision_count INTEGER NOT NULL CHECK (expected_decision_count >= 0),
    decision_manifest_sha256 TEXT NOT NULL CHECK (length(decision_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (candidate_build_id, guide_version, evidence_manifest_sha256)
);

CREATE TABLE image_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_build_id TEXT NOT NULL REFERENCES image_decision_builds(decision_build_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    technical_noise_label TEXT CHECK (
        technical_noise_label IS NULL OR technical_noise_label IN ('valid_content',
          'site_background', 'site_ui', 'placeholder_or_error',
          'tracking_or_qr_only', 'uncertain')
    ),
    decision_action TEXT NOT NULL CHECK (decision_action IN ('keep', 'review', 'exclude')),
    provenance TEXT NOT NULL CHECK (
        provenance IN ('default_keep_no_candidate', 'single_valid_content',
                       'double_agreement', 'adjudication')
    ),
    evidence_id TEXT,
    decision_sha256 TEXT NOT NULL CHECK (length(decision_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (decision_build_id, fingerprint_id)
);

CREATE TABLE image_sha_propagation_runs (
    propagation_run_id TEXT PRIMARY KEY,
    decision_build_id TEXT NOT NULL REFERENCES image_decision_builds(decision_build_id) ON DELETE RESTRICT,
    candidate_build_id TEXT NOT NULL REFERENCES image_candidate_builds(build_id) ON DELETE RESTRICT,
    exact_cluster_id TEXT NOT NULL,
    representative_decision_id TEXT NOT NULL REFERENCES image_decisions(decision_id) ON DELETE RESTRICT,
    technical_noise_label TEXT NOT NULL CHECK (
        technical_noise_label IN ('valid_content', 'site_background', 'site_ui',
          'placeholder_or_error', 'tracking_or_qr_only', 'uncertain')
    ),
    expected_member_count INTEGER NOT NULL CHECK (expected_member_count > 0),
    member_manifest_sha256 TEXT NOT NULL CHECK (length(member_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (decision_build_id, candidate_build_id, exact_cluster_id)
);

CREATE TABLE image_sha_propagation_members (
    propagation_run_id TEXT NOT NULL REFERENCES image_sha_propagation_runs(propagation_run_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    propagated_label TEXT NOT NULL CHECK (
        propagated_label IN ('valid_content', 'site_background', 'site_ui',
          'placeholder_or_error', 'tracking_or_qr_only', 'uncertain')
    ),
    source_decision_id TEXT NOT NULL REFERENCES image_decisions(decision_id) ON DELETE RESTRICT,
    PRIMARY KEY (propagation_run_id, fingerprint_id)
);

CREATE TABLE image_keep_audit_rounds (
    audit_round_id TEXT PRIMARY KEY,
    decision_build_id TEXT NOT NULL REFERENCES image_decision_builds(decision_build_id) ON DELETE RESTRICT,
    round_number INTEGER NOT NULL CHECK (round_number BETWEEN 1 AND 3),
    random_seed INTEGER NOT NULL,
    population_count INTEGER NOT NULL CHECK (population_count >= 0),
    population_manifest_sha256 TEXT NOT NULL CHECK (length(population_manifest_sha256) = 64),
    primary_count INTEGER NOT NULL CHECK (primary_count >= 0 AND primary_count <= 200),
    supplement_count INTEGER NOT NULL CHECK (supplement_count >= 0),
    primary_manifest_sha256 TEXT NOT NULL CHECK (length(primary_manifest_sha256) = 64),
    supplement_manifest_sha256 TEXT NOT NULL CHECK (length(supplement_manifest_sha256) = 64),
    interval_method TEXT NOT NULL CHECK (interval_method IN ('census', 'wilson_one_sided_95')),
    seal_status TEXT NOT NULL CHECK (seal_status IN ('building', 'finalized')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (decision_build_id, round_number)
);

CREATE TABLE image_keep_audit_members (
    audit_round_id TEXT NOT NULL REFERENCES image_keep_audit_rounds(audit_round_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    platform_key TEXT NOT NULL,
    sampling_layer TEXT NOT NULL CHECK (
        sampling_layer IN ('primary', 'platform_supplement')
    ),
    stable_rank INTEGER NOT NULL CHECK (stable_rank > 0),
    inclusion_probability REAL NOT NULL CHECK (inclusion_probability > 0 AND inclusion_probability <= 1),
    sampling_weight REAL NOT NULL CHECK (sampling_weight >= 1),
    PRIMARY KEY (audit_round_id, fingerprint_id),
    UNIQUE (audit_round_id, sampling_layer, stable_rank)
);

CREATE TABLE image_keep_audit_annotations (
    audit_annotation_id TEXT PRIMARY KEY,
    audit_round_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    annotator_hash TEXT NOT NULL CHECK (length(annotator_hash) = 64),
    guide_version TEXT NOT NULL,
    technical_noise_label TEXT NOT NULL CHECK (
        technical_noise_label IN ('valid_content', 'site_background', 'site_ui',
          'placeholder_or_error', 'tracking_or_qr_only', 'uncertain')
    ),
    reason_codes_json TEXT NOT NULL,
    annotated_at_utc TEXT NOT NULL,
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64),
    UNIQUE (audit_round_id, fingerprint_id),
    FOREIGN KEY (audit_round_id, fingerprint_id)
        REFERENCES image_keep_audit_members(audit_round_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TABLE image_keep_audit_evaluations (
    audit_evaluation_id TEXT PRIMARY KEY,
    audit_round_id TEXT NOT NULL UNIQUE REFERENCES image_keep_audit_rounds(audit_round_id) ON DELETE RESTRICT,
    completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
    primary_event_count INTEGER NOT NULL CHECK (primary_event_count >= 0),
    supplement_event_count INTEGER NOT NULL CHECK (supplement_event_count >= 0),
    primary_point_estimate REAL,
    one_sided_upper REAL,
    evaluation_status TEXT NOT NULL CHECK (
        evaluation_status IN ('incomplete', 'passed', 'failed')
    ),
    reason_code TEXT NOT NULL,
    evidence_manifest_sha256 TEXT NOT NULL CHECK (length(evidence_manifest_sha256) = 64),
    created_at_utc TEXT NOT NULL
);

-- 下面的 trigger 将父对象限定为 building→finalized，并在封存后冻结所有子行。
CREATE TRIGGER require_image_review_building_insert BEFORE INSERT ON image_review_runs
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
    SELECT 1 FROM image_candidate_builds b
    WHERE b.build_id = NEW.candidate_build_id AND b.seal_status = 'finalized'
) BEGIN SELECT RAISE(ABORT, 'image review run must bind a finalized candidate build'); END;
CREATE TRIGGER validate_image_review_seal BEFORE UPDATE OF seal_status ON image_review_runs
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM image_review_members m WHERE m.review_run_id = NEW.review_run_id) != NEW.member_count
 OR NEW.member_count > NEW.planned_count
 OR EXISTS (
    SELECT 1 FROM image_phash_review_groups g
    WHERE g.review_run_id = NEW.review_run_id
      AND g.member_count != (
        SELECT COUNT(*) FROM image_phash_review_group_members gm
        WHERE gm.review_run_id = g.review_run_id AND gm.group_id = g.group_id
      )
 )
) BEGIN SELECT RAISE(ABORT, 'image review run rows are incomplete'); END;
CREATE TRIGGER freeze_image_review_status BEFORE UPDATE OF seal_status ON image_review_runs
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image review status is immutable'); END;
CREATE TRIGGER freeze_image_review_parent AFTER UPDATE OF seal_status ON image_review_runs
WHEN NEW.seal_status = 'finalized' AND OLD.seal_status = 'building'
BEGIN SELECT 1; END;
CREATE TRIGGER validate_image_review_member BEFORE INSERT ON image_review_members
WHEN NOT EXISTS (
    SELECT 1 FROM image_review_runs r
    JOIN image_candidate_build_members m ON m.build_id = r.candidate_build_id
    JOIN image_exact_cluster_members e ON e.build_id = r.candidate_build_id
      AND e.fingerprint_id = NEW.fingerprint_id AND e.cluster_id = NEW.exact_cluster_id
    WHERE r.review_run_id = NEW.review_run_id AND r.seal_status = 'building'
) BEGIN SELECT RAISE(ABORT, 'image review member is outside candidate build'); END;

CREATE TRIGGER require_image_double_plan_building_insert BEFORE INSERT ON image_double_label_plans
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
    SELECT 1 FROM image_review_runs r WHERE r.review_run_id = NEW.review_run_id
      AND r.seal_status = 'finalized'
) BEGIN SELECT RAISE(ABORT, 'image double-label plan requires finalized review'); END;
CREATE TRIGGER validate_image_double_plan_seal BEFORE UPDATE OF seal_status ON image_double_label_plans
WHEN NEW.seal_status = 'finalized' AND (
    (SELECT COUNT(*) FROM image_double_label_plan_members m WHERE m.plan_id = NEW.plan_id) != NEW.member_count
 OR NEW.member_count > NEW.requested_count
) BEGIN SELECT RAISE(ABORT, 'image double-label plan rows are incomplete'); END;
CREATE TRIGGER freeze_image_double_plan_status BEFORE UPDATE OF seal_status ON image_double_label_plans
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image double-label plan status is immutable'); END;
CREATE TRIGGER validate_image_double_plan_member BEFORE INSERT ON image_double_label_plan_members
WHEN NOT EXISTS (
    SELECT 1 FROM image_double_label_plans p
    JOIN image_review_members m ON m.review_run_id = p.review_run_id
      AND m.fingerprint_id = NEW.fingerprint_id
    WHERE p.plan_id = NEW.plan_id AND p.seal_status = 'building'
) BEGIN SELECT RAISE(ABORT, 'double-label member is outside review run'); END;

CREATE TRIGGER validate_image_annotation_slot BEFORE INSERT ON image_review_annotations
WHEN NOT EXISTS (
    SELECT 1 FROM image_review_members m JOIN image_review_runs r
      ON r.review_run_id = m.review_run_id
    WHERE m.review_run_id = NEW.review_run_id AND m.fingerprint_id = NEW.fingerprint_id
      AND r.seal_status = 'finalized'
      AND (NEW.assignment_slot = 1 OR m.requires_double_label = 1
           OR EXISTS (SELECT 1 FROM image_double_label_plans p
             JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
             WHERE p.review_run_id = NEW.review_run_id
               AND pm.fingerprint_id = NEW.fingerprint_id AND p.seal_status = 'finalized'))
) BEGIN SELECT RAISE(ABORT, 'image annotation slot is not planned'); END;
CREATE TRIGGER reject_same_image_annotator_slots BEFORE INSERT ON image_review_annotations
WHEN EXISTS (SELECT 1 FROM image_review_annotations a
  WHERE a.review_run_id = NEW.review_run_id AND a.fingerprint_id = NEW.fingerprint_id
    AND a.annotator_hash = NEW.annotator_hash)
BEGIN SELECT RAISE(ABORT, 'image double-label annotators must differ'); END;
CREATE TRIGGER validate_image_adjudication BEFORE INSERT ON image_review_adjudications
WHEN (
    (SELECT COUNT(*) FROM image_review_annotations a
     WHERE a.annotation_id IN (NEW.left_annotation_id, NEW.right_annotation_id)
       AND a.review_run_id = NEW.review_run_id AND a.fingerprint_id = NEW.fingerprint_id
       AND a.guide_version = NEW.guide_version) != 2
 OR (SELECT COUNT(DISTINCT assignment_slot) FROM image_review_annotations a
     WHERE a.annotation_id IN (NEW.left_annotation_id, NEW.right_annotation_id)) != 2
 OR EXISTS (SELECT 1 FROM image_review_annotations a
     WHERE a.annotation_id IN (NEW.left_annotation_id, NEW.right_annotation_id)
       AND a.annotator_hash = NEW.adjudicator_hash)
) BEGIN SELECT RAISE(ABORT, 'image adjudication evidence is invalid'); END;

CREATE TRIGGER require_image_decision_building_insert BEFORE INSERT ON image_decision_builds
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
    SELECT 1 FROM image_candidate_builds b
    WHERE b.build_id = NEW.candidate_build_id AND b.seal_status = 'finalized'
) BEGIN SELECT RAISE(ABORT, 'image decision build requires finalized candidates'); END;
CREATE TRIGGER validate_image_decision_seal BEFORE UPDATE OF seal_status ON image_decision_builds
WHEN NEW.seal_status = 'finalized' AND
 (SELECT COUNT(*) FROM image_decisions d WHERE d.decision_build_id = NEW.decision_build_id)
 != NEW.expected_decision_count
BEGIN SELECT RAISE(ABORT, 'image decision rows are incomplete'); END;
CREATE TRIGGER freeze_image_decision_status BEFORE UPDATE OF seal_status ON image_decision_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image decision status is immutable'); END;
CREATE TRIGGER validate_image_decision_member BEFORE INSERT ON image_decisions
WHEN NOT EXISTS (SELECT 1 FROM image_decision_builds d
 JOIN image_candidate_build_members m ON m.build_id = d.candidate_build_id
 WHERE d.decision_build_id = NEW.decision_build_id AND d.seal_status = 'building'
   AND m.fingerprint_id = NEW.fingerprint_id)
BEGIN SELECT RAISE(ABORT, 'image decision is outside candidate build'); END;
CREATE TRIGGER validate_default_keep_semantics BEFORE INSERT ON image_decisions
WHEN NEW.provenance = 'default_keep_no_candidate'
 AND (NEW.technical_noise_label IS NOT NULL OR NEW.decision_action != 'keep' OR NEW.evidence_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'default keep cannot claim a human label'); END;
CREATE TRIGGER validate_image_exclusion_evidence BEFORE INSERT ON image_decisions
WHEN NEW.decision_action = 'exclude' AND (
 NEW.technical_noise_label NOT IN ('site_background', 'site_ui', 'placeholder_or_error', 'tracking_or_qr_only')
 OR NEW.provenance NOT IN ('double_agreement', 'adjudication') OR NEW.evidence_id IS NULL
)
BEGIN SELECT RAISE(ABORT, 'image exclusion requires confirmed technical noise'); END;

CREATE TRIGGER require_sha_propagation_building_insert BEFORE INSERT ON image_sha_propagation_runs
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
 SELECT 1 FROM image_decision_builds d
 JOIN image_decisions x ON x.decision_build_id = d.decision_build_id
 JOIN image_exact_clusters c ON c.build_id = NEW.candidate_build_id
   AND c.cluster_id = NEW.exact_cluster_id
 WHERE d.decision_build_id = NEW.decision_build_id AND d.seal_status = 'finalized'
   AND d.candidate_build_id = NEW.candidate_build_id
   AND x.decision_id = NEW.representative_decision_id
   AND x.fingerprint_id = c.representative_fingerprint_id
   AND x.technical_noise_label = NEW.technical_noise_label
) BEGIN SELECT RAISE(ABORT, 'SHA propagation source is invalid'); END;
CREATE TRIGGER validate_sha_propagation_member BEFORE INSERT ON image_sha_propagation_members
WHEN NOT EXISTS (
 SELECT 1 FROM image_sha_propagation_runs p
 JOIN image_exact_cluster_members m ON m.build_id = p.candidate_build_id
  AND m.cluster_id = p.exact_cluster_id AND m.fingerprint_id = NEW.fingerprint_id
 WHERE p.propagation_run_id = NEW.propagation_run_id AND p.seal_status = 'building'
  AND p.representative_decision_id = NEW.source_decision_id
  AND p.technical_noise_label = NEW.propagated_label
) BEGIN SELECT RAISE(ABORT, 'SHA propagation member is outside exact cluster'); END;
CREATE TRIGGER validate_sha_propagation_seal BEFORE UPDATE OF seal_status ON image_sha_propagation_runs
WHEN NEW.seal_status = 'finalized' AND
 (SELECT COUNT(*) FROM image_sha_propagation_members m
  WHERE m.propagation_run_id = NEW.propagation_run_id) != NEW.expected_member_count
BEGIN SELECT RAISE(ABORT, 'SHA propagation rows are incomplete'); END;
CREATE TRIGGER freeze_sha_propagation_status BEFORE UPDATE OF seal_status ON image_sha_propagation_runs
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'SHA propagation status is immutable'); END;

CREATE TRIGGER require_keep_audit_building_insert BEFORE INSERT ON image_keep_audit_rounds
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
 SELECT 1 FROM image_decision_builds d WHERE d.decision_build_id = NEW.decision_build_id
 AND d.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'keep audit requires finalized decisions'); END;
CREATE TRIGGER validate_keep_audit_member BEFORE INSERT ON image_keep_audit_members
WHEN NOT EXISTS (
 SELECT 1 FROM image_keep_audit_rounds r JOIN image_decisions d
  ON d.decision_build_id = r.decision_build_id AND d.fingerprint_id = NEW.fingerprint_id
 WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'building'
  AND d.decision_action IN ('keep', 'review'))
BEGIN SELECT RAISE(ABORT, 'keep audit member is outside keep population'); END;
CREATE TRIGGER validate_keep_audit_nonoverlap BEFORE INSERT ON image_keep_audit_members
WHEN EXISTS (
 SELECT 1 FROM image_keep_audit_rounds current
 JOIN image_keep_audit_rounds prior ON prior.decision_build_id = current.decision_build_id
 JOIN image_keep_audit_members m ON m.audit_round_id = prior.audit_round_id
 WHERE current.audit_round_id = NEW.audit_round_id AND prior.round_number < current.round_number
  AND m.fingerprint_id = NEW.fingerprint_id)
BEGIN SELECT RAISE(ABORT, 'keep audit rounds must not overlap'); END;
CREATE TRIGGER validate_keep_audit_seal BEFORE UPDATE OF seal_status ON image_keep_audit_rounds
WHEN NEW.seal_status = 'finalized' AND (
 (SELECT COUNT(*) FROM image_keep_audit_members m WHERE m.audit_round_id = NEW.audit_round_id
   AND m.sampling_layer = 'primary') != NEW.primary_count
 OR (SELECT COUNT(*) FROM image_keep_audit_members m WHERE m.audit_round_id = NEW.audit_round_id
   AND m.sampling_layer = 'platform_supplement') != NEW.supplement_count)
BEGIN SELECT RAISE(ABORT, 'keep audit rows are incomplete'); END;
CREATE TRIGGER freeze_keep_audit_status BEFORE UPDATE OF seal_status ON image_keep_audit_rounds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'keep audit status is immutable'); END;

-- 所有人工、仲裁、评估和封存结果只追加；子表在父对象 finalized 后禁止再加成员。
CREATE TRIGGER freeze_finalized_review_members BEFORE INSERT ON image_review_members
WHEN EXISTS (SELECT 1 FROM image_review_runs r WHERE r.review_run_id = NEW.review_run_id AND r.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image review rows are sealed'); END;
CREATE TRIGGER freeze_finalized_phash_groups BEFORE INSERT ON image_phash_review_groups
WHEN EXISTS (SELECT 1 FROM image_review_runs r WHERE r.review_run_id = NEW.review_run_id AND r.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image review rows are sealed'); END;
CREATE TRIGGER freeze_finalized_phash_members BEFORE INSERT ON image_phash_review_group_members
WHEN EXISTS (SELECT 1 FROM image_review_runs r WHERE r.review_run_id = NEW.review_run_id AND r.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image review rows are sealed'); END;
CREATE TRIGGER freeze_finalized_double_members BEFORE INSERT ON image_double_label_plan_members
WHEN EXISTS (SELECT 1 FROM image_double_label_plans p WHERE p.plan_id = NEW.plan_id AND p.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image double-label rows are sealed'); END;
CREATE TRIGGER freeze_finalized_decisions BEFORE INSERT ON image_decisions
WHEN EXISTS (SELECT 1 FROM image_decision_builds d WHERE d.decision_build_id = NEW.decision_build_id AND d.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image decision rows are sealed'); END;
CREATE TRIGGER freeze_finalized_sha_members BEFORE INSERT ON image_sha_propagation_members
WHEN EXISTS (SELECT 1 FROM image_sha_propagation_runs p WHERE p.propagation_run_id = NEW.propagation_run_id AND p.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'SHA propagation rows are sealed'); END;
CREATE TRIGGER freeze_finalized_audit_members BEFORE INSERT ON image_keep_audit_members
WHEN EXISTS (SELECT 1 FROM image_keep_audit_rounds r WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'keep audit rows are sealed'); END;

-- 业务表不可 UPDATE/DELETE；父表仅允许上方显式的封存状态更新。
CREATE TRIGGER freeze_image_review_parent_identity BEFORE UPDATE OF review_run_id,
 candidate_build_id, review_kind, guide_version, config_sha256, random_seed, code_version,
 planned_count, member_count, member_manifest_sha256, created_at_utc ON image_review_runs
BEGIN SELECT RAISE(ABORT, 'image review identity is immutable'); END;
CREATE TRIGGER freeze_double_plan_identity BEFORE UPDATE OF plan_id, review_run_id, plan_kind,
 guide_version, requested_count, member_count, member_manifest_sha256, source_evidence_sha256,
 created_at_utc ON image_double_label_plans
BEGIN SELECT RAISE(ABORT, 'image double-label identity is immutable'); END;
CREATE TRIGGER freeze_decision_identity BEFORE UPDATE OF decision_build_id, candidate_build_id,
 guide_version, evidence_manifest_sha256, expected_decision_count, decision_manifest_sha256,
 created_at_utc ON image_decision_builds
BEGIN SELECT RAISE(ABORT, 'image decision identity is immutable'); END;
CREATE TRIGGER freeze_sha_identity BEFORE UPDATE OF propagation_run_id, decision_build_id,
 candidate_build_id, exact_cluster_id, representative_decision_id, technical_noise_label,
 expected_member_count, member_manifest_sha256, created_at_utc ON image_sha_propagation_runs
BEGIN SELECT RAISE(ABORT, 'SHA propagation identity is immutable'); END;
CREATE TRIGGER freeze_audit_identity BEFORE UPDATE OF audit_round_id, decision_build_id,
 round_number, random_seed, population_count, population_manifest_sha256, primary_count,
 supplement_count, primary_manifest_sha256, supplement_manifest_sha256, interval_method,
 created_at_utc ON image_keep_audit_rounds
BEGIN SELECT RAISE(ABORT, 'keep audit identity is immutable'); END;

CREATE TRIGGER immutable_image_review_members_update BEFORE UPDATE ON image_review_members BEGIN SELECT RAISE(ABORT, 'image review rows are immutable'); END;
CREATE TRIGGER immutable_image_review_members_delete BEFORE DELETE ON image_review_members BEGIN SELECT RAISE(ABORT, 'image review rows are immutable'); END;
CREATE TRIGGER immutable_image_phash_groups_update BEFORE UPDATE ON image_phash_review_groups BEGIN SELECT RAISE(ABORT, 'pHash review rows are immutable'); END;
CREATE TRIGGER immutable_image_phash_groups_delete BEFORE DELETE ON image_phash_review_groups BEGIN SELECT RAISE(ABORT, 'pHash review rows are immutable'); END;
CREATE TRIGGER immutable_image_phash_members_update BEFORE UPDATE ON image_phash_review_group_members BEGIN SELECT RAISE(ABORT, 'pHash review rows are immutable'); END;
CREATE TRIGGER immutable_image_phash_members_delete BEFORE DELETE ON image_phash_review_group_members BEGIN SELECT RAISE(ABORT, 'pHash review rows are immutable'); END;
CREATE TRIGGER immutable_image_double_members_update BEFORE UPDATE ON image_double_label_plan_members BEGIN SELECT RAISE(ABORT, 'double-label rows are immutable'); END;
CREATE TRIGGER immutable_image_double_members_delete BEFORE DELETE ON image_double_label_plan_members BEGIN SELECT RAISE(ABORT, 'double-label rows are immutable'); END;
CREATE TRIGGER immutable_image_imports_update BEFORE UPDATE ON image_annotation_imports BEGIN SELECT RAISE(ABORT, 'annotation imports are immutable'); END;
CREATE TRIGGER immutable_image_imports_delete BEFORE DELETE ON image_annotation_imports BEGIN SELECT RAISE(ABORT, 'annotation imports are immutable'); END;
CREATE TRIGGER immutable_image_annotations_update BEFORE UPDATE ON image_review_annotations BEGIN SELECT RAISE(ABORT, 'image annotations are append-only'); END;
CREATE TRIGGER immutable_image_annotations_delete BEFORE DELETE ON image_review_annotations BEGIN SELECT RAISE(ABORT, 'image annotations are append-only'); END;
CREATE TRIGGER immutable_image_adjudications_update BEFORE UPDATE ON image_review_adjudications BEGIN SELECT RAISE(ABORT, 'image adjudications are append-only'); END;
CREATE TRIGGER immutable_image_adjudications_delete BEFORE DELETE ON image_review_adjudications BEGIN SELECT RAISE(ABORT, 'image adjudications are append-only'); END;
CREATE TRIGGER immutable_image_agreements_update BEFORE UPDATE ON image_agreement_evaluations BEGIN SELECT RAISE(ABORT, 'image agreement evaluations are immutable'); END;
CREATE TRIGGER immutable_image_agreements_delete BEFORE DELETE ON image_agreement_evaluations BEGIN SELECT RAISE(ABORT, 'image agreement evaluations are immutable'); END;
CREATE TRIGGER immutable_image_decisions_update BEFORE UPDATE ON image_decisions BEGIN SELECT RAISE(ABORT, 'image decisions are immutable'); END;
CREATE TRIGGER immutable_image_decisions_delete BEFORE DELETE ON image_decisions BEGIN SELECT RAISE(ABORT, 'image decisions are immutable'); END;
CREATE TRIGGER immutable_sha_members_update BEFORE UPDATE ON image_sha_propagation_members BEGIN SELECT RAISE(ABORT, 'SHA propagation rows are immutable'); END;
CREATE TRIGGER immutable_sha_members_delete BEFORE DELETE ON image_sha_propagation_members BEGIN SELECT RAISE(ABORT, 'SHA propagation rows are immutable'); END;
CREATE TRIGGER immutable_audit_members_update BEFORE UPDATE ON image_keep_audit_members BEGIN SELECT RAISE(ABORT, 'keep audit rows are immutable'); END;
CREATE TRIGGER immutable_audit_members_delete BEFORE DELETE ON image_keep_audit_members BEGIN SELECT RAISE(ABORT, 'keep audit rows are immutable'); END;
CREATE TRIGGER immutable_audit_annotations_update BEFORE UPDATE ON image_keep_audit_annotations BEGIN SELECT RAISE(ABORT, 'keep audit annotations are append-only'); END;
CREATE TRIGGER immutable_audit_annotations_delete BEFORE DELETE ON image_keep_audit_annotations BEGIN SELECT RAISE(ABORT, 'keep audit annotations are append-only'); END;
CREATE TRIGGER immutable_audit_evaluations_update BEFORE UPDATE ON image_keep_audit_evaluations BEGIN SELECT RAISE(ABORT, 'keep audit evaluations are immutable'); END;
CREATE TRIGGER immutable_audit_evaluations_delete BEFORE DELETE ON image_keep_audit_evaluations BEGIN SELECT RAISE(ABORT, 'keep audit evaluations are immutable'); END;
CREATE TRIGGER immutable_image_review_runs_delete BEFORE DELETE ON image_review_runs BEGIN SELECT RAISE(ABORT, 'image review runs are immutable'); END;
CREATE TRIGGER immutable_image_double_plans_delete BEFORE DELETE ON image_double_label_plans BEGIN SELECT RAISE(ABORT, 'image double-label plans are immutable'); END;
CREATE TRIGGER immutable_image_decision_builds_delete BEFORE DELETE ON image_decision_builds BEGIN SELECT RAISE(ABORT, 'image decision builds are immutable'); END;
CREATE TRIGGER immutable_sha_propagation_runs_delete BEFORE DELETE ON image_sha_propagation_runs BEGIN SELECT RAISE(ABORT, 'SHA propagation runs are immutable'); END;
CREATE TRIGGER immutable_keep_audit_rounds_delete BEFORE DELETE ON image_keep_audit_rounds BEGIN SELECT RAISE(ABORT, 'keep audit rounds are immutable'); END;
"""


_SCHEMA_V17 = """
-- v16 把双标计划 manifest 误当作评估唯一身份，导致 incomplete 后不能追加
-- complete 评估。v17 将当时已完成的原始标注 manifest 纳入身份并保留旧行。
DROP TRIGGER IF EXISTS immutable_image_agreements_update;
DROP TRIGGER IF EXISTS immutable_image_agreements_delete;

CREATE TABLE image_agreement_evaluations_v17 (
    evaluation_id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL REFERENCES image_review_runs(review_run_id) ON DELETE RESTRICT,
    plan_manifest_sha256 TEXT NOT NULL CHECK (length(plan_manifest_sha256) = 64),
    annotation_manifest_sha256 TEXT NOT NULL CHECK (length(annotation_manifest_sha256) = 64),
    planned_pair_count INTEGER NOT NULL CHECK (planned_pair_count >= 0),
    complete_pair_count INTEGER NOT NULL CHECK (complete_pair_count >= 0),
    agreement_count INTEGER NOT NULL CHECK (agreement_count >= 0),
    raw_agreement REAL,
    cohen_kappa REAL,
    kappa_status TEXT NOT NULL CHECK (
        kappa_status IN ('estimated', 'undefined_single_category', 'incomplete')
    ),
    evaluation_status TEXT NOT NULL CHECK (
        evaluation_status IN ('incomplete', 'passed', 'supplement_required')
    ),
    label_disagreements_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (review_run_id, plan_manifest_sha256, annotation_manifest_sha256)
);

INSERT INTO image_agreement_evaluations_v17(
    evaluation_id, review_run_id, plan_manifest_sha256, annotation_manifest_sha256,
    planned_pair_count, complete_pair_count, agreement_count, raw_agreement,
    cohen_kappa, kappa_status, evaluation_status, label_disagreements_json,
    created_at_utc
)
SELECT evaluation_id, review_run_id, plan_manifest_sha256,
       evaluation_id || evaluation_id,
       planned_pair_count, complete_pair_count, agreement_count, raw_agreement,
       cohen_kappa, kappa_status, evaluation_status, label_disagreements_json,
       created_at_utc
FROM image_agreement_evaluations;

DROP TABLE image_agreement_evaluations;
ALTER TABLE image_agreement_evaluations_v17 RENAME TO image_agreement_evaluations;

CREATE TRIGGER immutable_image_agreements_update BEFORE UPDATE ON image_agreement_evaluations
BEGIN SELECT RAISE(ABORT, 'image agreement evaluations are immutable'); END;
CREATE TRIGGER immutable_image_agreements_delete BEFORE DELETE ON image_agreement_evaluations
BEGIN SELECT RAISE(ABORT, 'image agreement evaluations are immutable'); END;
"""


_SCHEMA_V18 = """
-- v16 的审计成员 trigger 只接受代表 fingerprint；正式审计人口是全部保留关系，
-- 因而必须沿 SHA 精确簇回到代表决定，同时仍禁止跨 candidate build 混入成员。
DROP TRIGGER IF EXISTS validate_keep_audit_member;
CREATE TRIGGER validate_keep_audit_member BEFORE INSERT ON image_keep_audit_members
WHEN NOT EXISTS (
 SELECT 1 FROM image_keep_audit_rounds r
 JOIN image_decision_builds b ON b.decision_build_id = r.decision_build_id
 JOIN image_exact_cluster_members m ON m.build_id = b.candidate_build_id
   AND m.fingerprint_id = NEW.fingerprint_id
 JOIN image_exact_clusters c ON c.build_id = m.build_id AND c.cluster_id = m.cluster_id
 JOIN image_decisions d ON d.decision_build_id = b.decision_build_id
   AND d.fingerprint_id = c.representative_fingerprint_id
 WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'building'
   AND d.decision_action IN ('keep', 'review')
)
BEGIN SELECT RAISE(ABORT, 'keep audit member is outside keep population'); END;
"""


_SCHEMA_V19 = """
-- v19 补齐 Issue #10 直接 SQL 谱系约束：候选只来自 content；人工导入、手册、
-- 决定证据和来源运行必须同属；仲裁、SHA 传播和审计重试不能绕过应用层硬门。

DROP TRIGGER IF EXISTS validate_image_build_member_context;
CREATE TRIGGER validate_image_build_member_context
BEFORE INSERT ON image_candidate_build_members
WHEN NOT EXISTS (
    SELECT 1
    FROM image_candidate_builds AS b
    JOIN image_fingerprints AS f ON f.fingerprint_id = NEW.fingerprint_id
    JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
    WHERE b.build_id = NEW.build_id
      AND r.manifest_id = b.manifest_id
      AND r.relation_role = 'content'
      AND f.fingerprint_version = b.fingerprint_version
      AND r.source_image_id = NEW.source_image_id
      AND r.source_post_id = NEW.source_post_id
      AND r.row_identity_sha256 = NEW.row_identity_sha256
      AND f.row_identity_sha256 = NEW.row_identity_sha256
)
BEGIN SELECT RAISE(ABORT, 'image build member must be content in build context'); END;

CREATE TRIGGER validate_image_candidate_content_seal
BEFORE UPDATE OF seal_status ON image_candidate_builds
WHEN NEW.seal_status = 'finalized' AND EXISTS (
    SELECT 1 FROM image_candidate_build_members m
    JOIN image_fingerprints f ON f.fingerprint_id = m.fingerprint_id
    JOIN image_manifest_rows r ON r.manifest_row_id = f.manifest_row_id
    WHERE m.build_id = NEW.build_id AND r.relation_role != 'content'
)
BEGIN SELECT RAISE(ABORT, 'image candidate build contains non-content member'); END;

CREATE TRIGGER validate_image_annotation_lineage
BEFORE INSERT ON image_review_annotations
WHEN NOT EXISTS (
    SELECT 1 FROM image_annotation_imports i
    JOIN image_review_runs r ON r.review_run_id = NEW.review_run_id
    WHERE i.import_id = NEW.import_id
      AND i.review_run_id = NEW.review_run_id
      AND r.guide_version = NEW.guide_version
      AND r.seal_status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'image annotation import, run and guide mismatch'); END;

DROP TRIGGER IF EXISTS validate_image_adjudication;
CREATE TRIGGER validate_image_adjudication
BEFORE INSERT ON image_review_adjudications
WHEN (
    (SELECT COUNT(*) FROM image_review_annotations a
     JOIN image_review_runs r ON r.review_run_id = a.review_run_id
     WHERE a.annotation_id IN (NEW.left_annotation_id, NEW.right_annotation_id)
       AND a.review_run_id = NEW.review_run_id
       AND a.fingerprint_id = NEW.fingerprint_id
       AND a.guide_version = NEW.guide_version
       AND r.guide_version = NEW.guide_version) != 2
 OR (SELECT COUNT(DISTINCT assignment_slot) FROM image_review_annotations a
     WHERE a.annotation_id IN (NEW.left_annotation_id, NEW.right_annotation_id)) != 2
 OR EXISTS (SELECT 1 FROM image_review_annotations a
     WHERE a.annotation_id IN (NEW.left_annotation_id, NEW.right_annotation_id)
       AND a.annotator_hash = NEW.adjudicator_hash)
 OR NOT EXISTS (
     SELECT 1 FROM image_review_annotations l
     JOIN image_review_annotations r
       ON r.annotation_id = NEW.right_annotation_id
     WHERE l.annotation_id = NEW.left_annotation_id
       AND (l.technical_noise_label != r.technical_noise_label
            OR l.technical_noise_label = 'uncertain'
            OR r.technical_noise_label = 'uncertain')
 )
 OR NOT json_valid(NEW.reason_codes_json)
 OR json_type(NEW.reason_codes_json) != 'array'
 OR EXISTS (
     SELECT 1 FROM json_each(NEW.reason_codes_json)
     WHERE type != 'text' OR value = '' OR value GLOB '*[^a-z0-9_-]*'
 )
)
BEGIN SELECT RAISE(ABORT, 'image adjudication evidence or reason codes are invalid'); END;

-- 旧 v18 的 double_agreement 把两条 annotation ID 以加号拼进父行。先建立规范
-- 子表并迁移两条真实证据，再把父行指针收敛为字典序首条真实 ID。
DROP TRIGGER IF EXISTS immutable_image_decisions_update;
CREATE TABLE image_decision_evidence_links (
    decision_id TEXT NOT NULL REFERENCES image_decisions(decision_id) ON DELETE RESTRICT,
    evidence_id TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('annotation', 'adjudication')),
    review_run_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    PRIMARY KEY (decision_id, evidence_id),
    FOREIGN KEY (review_run_id, fingerprint_id)
      REFERENCES image_review_members(review_run_id, fingerprint_id) ON DELETE RESTRICT
);

INSERT INTO image_decision_evidence_links(
  decision_id, evidence_id, evidence_kind, review_run_id, fingerprint_id
)
SELECT d.decision_id, a.annotation_id, 'annotation', a.review_run_id, a.fingerprint_id
FROM image_decisions d JOIN image_review_annotations a ON a.annotation_id = d.evidence_id
WHERE d.provenance = 'single_valid_content';

INSERT INTO image_decision_evidence_links(
  decision_id, evidence_id, evidence_kind, review_run_id, fingerprint_id
)
SELECT d.decision_id, a.adjudication_id, 'adjudication', a.review_run_id, a.fingerprint_id
FROM image_decisions d JOIN image_review_adjudications a ON a.adjudication_id = d.evidence_id
WHERE d.provenance = 'adjudication';

INSERT INTO image_decision_evidence_links(
  decision_id, evidence_id, evidence_kind, review_run_id, fingerprint_id
)
SELECT d.decision_id, a.annotation_id, 'annotation', a.review_run_id, a.fingerprint_id
FROM image_decisions d JOIN image_review_annotations a
  ON a.annotation_id IN (
    substr(d.evidence_id, 1, instr(d.evidence_id, '+') - 1),
    substr(d.evidence_id, instr(d.evidence_id, '+') + 1)
  )
WHERE d.provenance = 'double_agreement';

UPDATE image_decisions
SET evidence_id = substr(evidence_id, 1, instr(evidence_id, '+') - 1)
WHERE provenance = 'double_agreement' AND instr(evidence_id, '+') > 0;

CREATE TRIGGER validate_image_decision_evidence_link
BEFORE INSERT ON image_decision_evidence_links
WHEN NOT EXISTS (
    SELECT 1 FROM image_decisions d
    JOIN image_decision_builds b ON b.decision_build_id = d.decision_build_id
    WHERE d.decision_id = NEW.decision_id
      AND d.fingerprint_id = NEW.fingerprint_id
      AND b.seal_status = 'building'
) OR NOT (
    (NEW.evidence_kind = 'annotation' AND EXISTS (
      SELECT 1 FROM image_review_annotations a
      JOIN image_review_runs r ON r.review_run_id = a.review_run_id
      JOIN image_decisions d ON d.decision_id = NEW.decision_id
      JOIN image_decision_builds b ON b.decision_build_id = d.decision_build_id
      WHERE a.annotation_id = NEW.evidence_id
        AND a.review_run_id = NEW.review_run_id
        AND a.fingerprint_id = NEW.fingerprint_id
        AND a.guide_version = b.guide_version
        AND r.candidate_build_id = b.candidate_build_id
        AND r.review_kind = 'candidate_review'
        AND r.seal_status = 'finalized'
    )) OR
    (NEW.evidence_kind = 'adjudication' AND EXISTS (
      SELECT 1 FROM image_review_adjudications a
      JOIN image_review_runs r ON r.review_run_id = a.review_run_id
      JOIN image_decisions d ON d.decision_id = NEW.decision_id
      JOIN image_decision_builds b ON b.decision_build_id = d.decision_build_id
      WHERE a.adjudication_id = NEW.evidence_id
        AND a.review_run_id = NEW.review_run_id
        AND a.fingerprint_id = NEW.fingerprint_id
        AND a.guide_version = b.guide_version
        AND r.candidate_build_id = b.candidate_build_id
        AND r.review_kind = 'candidate_review'
        AND r.seal_status = 'finalized'
    ))
)
BEGIN SELECT RAISE(ABORT, 'image decision evidence lineage mismatch'); END;

DROP TRIGGER IF EXISTS validate_image_decision_seal;
CREATE TRIGGER validate_image_decision_seal
BEFORE UPDATE OF seal_status ON image_decision_builds
WHEN NEW.seal_status = 'finalized' AND (
 (SELECT COUNT(*) FROM image_decisions d WHERE d.decision_build_id = NEW.decision_build_id)
    != NEW.expected_decision_count
 OR EXISTS (
   SELECT 1 FROM image_decisions d
   WHERE d.decision_build_id = NEW.decision_build_id AND (
     (d.provenance = 'default_keep_no_candidate' AND (
        d.evidence_id IS NOT NULL OR EXISTS (
          SELECT 1 FROM image_decision_evidence_links l WHERE l.decision_id = d.decision_id)
     ))
     OR (d.provenance = 'single_valid_content' AND (
        d.technical_noise_label != 'valid_content' OR d.decision_action != 'keep'
        OR d.evidence_id IS NULL
        OR (SELECT COUNT(*) FROM image_decision_evidence_links l
            WHERE l.decision_id = d.decision_id AND l.evidence_kind = 'annotation') != 1
        OR d.evidence_id != (SELECT MIN(l.evidence_id) FROM image_decision_evidence_links l
                             WHERE l.decision_id = d.decision_id)
     ))
     OR (d.provenance = 'double_agreement' AND (
        d.technical_noise_label IS NULL OR d.technical_noise_label = 'uncertain'
        OR d.evidence_id IS NULL
        OR (SELECT COUNT(*) FROM image_decision_evidence_links l
            WHERE l.decision_id = d.decision_id AND l.evidence_kind = 'annotation') != 2
        OR (SELECT COUNT(DISTINCT a.assignment_slot)
            FROM image_decision_evidence_links l
            JOIN image_review_annotations a ON a.annotation_id = l.evidence_id
            WHERE l.decision_id = d.decision_id) != 2
        OR EXISTS (SELECT 1 FROM image_decision_evidence_links l
            JOIN image_review_annotations a ON a.annotation_id = l.evidence_id
            WHERE l.decision_id = d.decision_id
              AND a.technical_noise_label != d.technical_noise_label)
        OR d.decision_action != CASE WHEN d.technical_noise_label = 'valid_content'
             THEN 'keep' ELSE 'exclude' END
        OR d.evidence_id != (SELECT MIN(l.evidence_id) FROM image_decision_evidence_links l
                             WHERE l.decision_id = d.decision_id)
     ))
     OR (d.provenance = 'adjudication' AND (
        d.evidence_id IS NULL
        OR (SELECT COUNT(*) FROM image_decision_evidence_links l
            WHERE l.decision_id = d.decision_id AND l.evidence_kind = 'adjudication') != 1
        OR NOT EXISTS (SELECT 1 FROM image_decision_evidence_links l
            JOIN image_review_adjudications a ON a.adjudication_id = l.evidence_id
            WHERE l.decision_id = d.decision_id
              AND a.technical_noise_label = d.technical_noise_label)
        OR d.decision_action != CASE
             WHEN d.technical_noise_label = 'valid_content' THEN 'keep'
             WHEN d.technical_noise_label = 'uncertain' THEN 'review'
             ELSE 'exclude' END
        OR d.evidence_id != (SELECT MIN(l.evidence_id) FROM image_decision_evidence_links l
                             WHERE l.decision_id = d.decision_id)
     ))
   )
 )
)
BEGIN SELECT RAISE(ABORT, 'image decision rows or evidence are incomplete'); END;

-- SQLite 层再次要求正式 pilot 与 boundary 完整通过；应用层还会计算并绑定门禁
-- manifest，因此这里的固定 30/50 和 0.80 是防直接写绕过，不替代科研身份。
CREATE TRIGGER validate_image_formal_review_gate
BEFORE UPDATE OF seal_status ON image_decision_builds
WHEN NEW.seal_status = 'finalized' AND (
 NOT EXISTS (
   SELECT 1 FROM image_review_runs r
   JOIN image_candidate_builds b ON b.build_id = r.candidate_build_id
   JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
     AND p.plan_kind = 'boundary' AND p.seal_status = 'finalized'
     AND p.member_count = r.member_count
   JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
     AND e.planned_pair_count = r.member_count AND e.complete_pair_count = r.member_count
   WHERE r.candidate_build_id = NEW.candidate_build_id
     AND r.review_kind = 'pilot' AND r.guide_version = NEW.guide_version
     AND r.config_sha256 = b.config_sha256 AND r.seal_status = 'finalized'
     AND r.member_count = min(30, (
       SELECT COUNT(*) FROM image_exact_clusters c
       WHERE c.build_id = NEW.candidate_build_id AND (
         c.member_count > 1
         OR EXISTS (SELECT 1 FROM image_candidate_signals s WHERE s.build_id = c.build_id
                      AND s.fingerprint_id = c.representative_fingerprint_id)
         OR EXISTS (SELECT 1 FROM image_near_candidate_pairs n WHERE n.build_id = c.build_id
                      AND (n.left_fingerprint_id = c.representative_fingerprint_id
                           OR n.right_fingerprint_id = c.representative_fingerprint_id))
       )))
     AND e.evaluation_status = 'passed' AND e.raw_agreement >= 0.80
 )
 OR NOT EXISTS (
   SELECT 1 FROM image_review_runs r
   JOIN image_candidate_builds b ON b.build_id = r.candidate_build_id
   JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
     AND p.plan_kind = 'boundary' AND p.seal_status = 'finalized'
     AND p.member_count = r.member_count
   JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
     AND e.planned_pair_count = r.member_count AND e.complete_pair_count = r.member_count
   WHERE r.candidate_build_id = NEW.candidate_build_id
     AND r.review_kind = 'boundary' AND r.guide_version = NEW.guide_version
     AND r.config_sha256 = b.config_sha256 AND r.seal_status = 'finalized'
     AND r.member_count = min(50, (SELECT COUNT(*) FROM image_exact_clusters c
                                  WHERE c.build_id = NEW.candidate_build_id))
     AND (
       (e.evaluation_status = 'passed' AND e.raw_agreement >= 0.80)
       OR (
         e.evaluation_status = 'supplement_required'
         AND EXISTS (
           SELECT 1 FROM image_review_runs sr
           JOIN image_double_label_plans sp ON sp.review_run_id = sr.review_run_id
             AND sp.plan_kind = 'boundary_supplement'
             AND sp.seal_status = 'finalized' AND sp.member_count = sr.member_count
           JOIN image_agreement_evaluations se ON se.review_run_id = sr.review_run_id
             AND se.planned_pair_count = sr.member_count
             AND se.complete_pair_count = sr.member_count
           WHERE sr.candidate_build_id = NEW.candidate_build_id
             AND sr.review_kind = 'boundary' AND sr.guide_version = NEW.guide_version
             AND sr.config_sha256 = b.config_sha256 AND sr.seal_status = 'finalized'
             AND se.evaluation_status = 'passed' AND se.raw_agreement >= 0.80
         )
       )
     )
 )
)
BEGIN SELECT RAISE(ABORT, 'formal image review gate is incomplete'); END;

DROP TRIGGER IF EXISTS require_sha_propagation_building_insert;
CREATE TRIGGER require_sha_propagation_building_insert
BEFORE INSERT ON image_sha_propagation_runs
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
 SELECT 1 FROM image_decision_builds d
 JOIN image_decisions x ON x.decision_build_id = d.decision_build_id
 JOIN image_exact_clusters c ON c.build_id = NEW.candidate_build_id
   AND c.cluster_id = NEW.exact_cluster_id
 WHERE d.decision_build_id = NEW.decision_build_id AND d.seal_status = 'finalized'
   AND d.candidate_build_id = NEW.candidate_build_id
   AND x.decision_id = NEW.representative_decision_id
   AND x.fingerprint_id = c.representative_fingerprint_id
   AND x.decision_action = 'exclude'
   AND x.provenance IN ('double_agreement', 'adjudication')
   AND x.technical_noise_label = NEW.technical_noise_label
   AND x.technical_noise_label IN (
     'site_background', 'site_ui', 'placeholder_or_error', 'tracking_or_qr_only')
) BEGIN SELECT RAISE(ABORT, 'SHA propagation source is not confirmed technical noise'); END;

DROP TRIGGER IF EXISTS validate_keep_audit_nonoverlap;
CREATE TRIGGER validate_keep_audit_nonoverlap
BEFORE INSERT ON image_keep_audit_members
WHEN EXISTS (
 SELECT 1 FROM image_keep_audit_rounds current
 JOIN image_decision_builds current_build
   ON current_build.decision_build_id = current.decision_build_id
 JOIN image_keep_audit_rounds prior
 JOIN image_decision_builds prior_build
   ON prior_build.decision_build_id = prior.decision_build_id
  AND prior_build.candidate_build_id = current_build.candidate_build_id
 JOIN image_keep_audit_members m ON m.audit_round_id = prior.audit_round_id
 WHERE current.audit_round_id = NEW.audit_round_id
   AND prior.round_number < current.round_number
   AND m.fingerprint_id = NEW.fingerprint_id
)
BEGIN SELECT RAISE(ABORT, 'keep audit rounds must not overlap across decisions'); END;

CREATE TRIGGER validate_keep_audit_round_sequence
BEFORE INSERT ON image_keep_audit_rounds
WHEN NEW.round_number > 3 OR EXISTS (
 SELECT 1 FROM image_keep_audit_rounds r
 JOIN image_decision_builds b ON b.decision_build_id = r.decision_build_id
 JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
 WHERE b.candidate_build_id = nb.candidate_build_id
   AND r.round_number = NEW.round_number
) OR (
 NEW.round_number = 1 AND EXISTS (
   SELECT 1 FROM image_keep_audit_rounds r
   JOIN image_decision_builds b ON b.decision_build_id = r.decision_build_id
   JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
   WHERE b.candidate_build_id = nb.candidate_build_id)
) OR (
 NEW.round_number > 1 AND NOT EXISTS (
   SELECT 1 FROM image_keep_audit_rounds prior
   JOIN image_decision_builds pb ON pb.decision_build_id = prior.decision_build_id
   JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
   JOIN image_keep_audit_evaluations e ON e.audit_round_id = prior.audit_round_id
   WHERE pb.candidate_build_id = nb.candidate_build_id
     AND prior.round_number = NEW.round_number - 1
     AND e.evaluation_status = 'failed'
     AND prior.decision_build_id != NEW.decision_build_id
     AND pb.decision_manifest_sha256 != nb.decision_manifest_sha256
 ))
BEGIN SELECT RAISE(ABORT, 'keep audit round sequence or revised decision is invalid'); END;

CREATE TRIGGER freeze_finalized_decision_evidence
BEFORE INSERT ON image_decision_evidence_links
WHEN EXISTS (
 SELECT 1 FROM image_decisions d
 JOIN image_decision_builds b ON b.decision_build_id = d.decision_build_id
 WHERE d.decision_id = NEW.decision_id AND b.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image decision evidence is sealed'); END;
CREATE TRIGGER immutable_image_decision_evidence_update
BEFORE UPDATE ON image_decision_evidence_links
BEGIN SELECT RAISE(ABORT, 'image decision evidence is immutable'); END;
CREATE TRIGGER immutable_image_decision_evidence_delete
BEFORE DELETE ON image_decision_evidence_links
BEGIN SELECT RAISE(ABORT, 'image decision evidence is immutable'); END;
CREATE TRIGGER immutable_image_decisions_update
BEFORE UPDATE ON image_decisions
BEGIN SELECT RAISE(ABORT, 'image decisions are immutable'); END;
"""


_SCHEMA_V20 = """
-- v20 不再信任可由单行自证的派生评估。旧 v19 评估原样保留但标为
-- untrusted_legacy；新评估必须先写 building 父行、链接真实原始标注，再由
-- 触发器重算计数、比例、κ/Wilson、状态和理由后单向封存。
DROP TRIGGER IF EXISTS immutable_image_agreements_update;
DROP TRIGGER IF EXISTS immutable_image_agreements_delete;
DROP TRIGGER IF EXISTS validate_image_formal_review_gate;
ALTER TABLE image_agreement_evaluations RENAME TO image_agreement_evaluations_v19;

CREATE TABLE image_agreement_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL REFERENCES image_review_runs(review_run_id) ON DELETE RESTRICT,
    plan_manifest_sha256 TEXT NOT NULL CHECK (length(plan_manifest_sha256) = 64),
    annotation_manifest_sha256 TEXT NOT NULL CHECK (length(annotation_manifest_sha256) = 64),
    planned_pair_count INTEGER NOT NULL CHECK (planned_pair_count >= 0),
    complete_pair_count INTEGER NOT NULL CHECK (complete_pair_count >= 0),
    agreement_count INTEGER NOT NULL CHECK (agreement_count >= 0),
    raw_agreement REAL,
    cohen_kappa REAL,
    kappa_status TEXT NOT NULL CHECK (
        kappa_status IN ('estimated', 'undefined_single_category', 'incomplete')
    ),
    evaluation_status TEXT NOT NULL CHECK (
        evaluation_status IN ('incomplete', 'passed', 'supplement_required')
    ),
    label_disagreements_json TEXT NOT NULL,
    seal_status TEXT NOT NULL CHECK (
        seal_status IN ('building', 'finalized', 'untrusted_legacy')
    ),
    created_at_utc TEXT NOT NULL
);

INSERT INTO image_agreement_evaluations(
    evaluation_id, review_run_id, plan_manifest_sha256, annotation_manifest_sha256,
    planned_pair_count, complete_pair_count, agreement_count, raw_agreement,
    cohen_kappa, kappa_status, evaluation_status, label_disagreements_json,
    seal_status, created_at_utc
)
SELECT evaluation_id, review_run_id, plan_manifest_sha256, annotation_manifest_sha256,
       planned_pair_count, complete_pair_count, agreement_count, raw_agreement,
       cohen_kappa, kappa_status, evaluation_status, label_disagreements_json,
       'untrusted_legacy', created_at_utc
FROM image_agreement_evaluations_v19;
DROP TABLE image_agreement_evaluations_v19;

CREATE UNIQUE INDEX idx_verified_image_agreement_identity
ON image_agreement_evaluations(
  review_run_id, plan_manifest_sha256, annotation_manifest_sha256
) WHERE seal_status IN ('building', 'finalized');

CREATE TABLE image_agreement_evaluation_annotations (
    evaluation_id TEXT NOT NULL
      REFERENCES image_agreement_evaluations(evaluation_id) ON DELETE RESTRICT,
    annotation_id TEXT NOT NULL
      REFERENCES image_review_annotations(annotation_id) ON DELETE RESTRICT,
    review_run_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    assignment_slot INTEGER NOT NULL CHECK (assignment_slot IN (1, 2)),
    PRIMARY KEY (evaluation_id, annotation_id),
    UNIQUE (evaluation_id, fingerprint_id, assignment_slot),
    FOREIGN KEY (review_run_id, fingerprint_id)
      REFERENCES image_review_members(review_run_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TRIGGER require_image_agreement_building_insert
BEFORE INSERT ON image_agreement_evaluations
WHEN NEW.seal_status != 'building'
BEGIN SELECT RAISE(ABORT, 'image agreement evaluation must start building'); END;

CREATE TRIGGER validate_image_agreement_annotation_link
BEFORE INSERT ON image_agreement_evaluation_annotations
WHEN NOT EXISTS (
  SELECT 1 FROM image_agreement_evaluations e
  JOIN image_review_annotations a ON a.annotation_id = NEW.annotation_id
  JOIN image_review_runs r ON r.review_run_id = e.review_run_id
  JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
    AND p.seal_status = 'finalized'
    AND p.plan_kind IN ('boundary', 'boundary_supplement')
  JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
    AND pm.fingerprint_id = a.fingerprint_id
  WHERE e.evaluation_id = NEW.evaluation_id AND e.seal_status = 'building'
    AND a.review_run_id = e.review_run_id
    AND a.review_run_id = NEW.review_run_id
    AND a.fingerprint_id = NEW.fingerprint_id
    AND a.assignment_slot = NEW.assignment_slot
    AND a.guide_version = r.guide_version
)
BEGIN SELECT RAISE(ABORT, 'image agreement annotation lineage mismatch'); END;

CREATE TRIGGER validate_image_agreement_parent_and_links
BEFORE UPDATE OF seal_status ON image_agreement_evaluations
WHEN NEW.seal_status = 'finalized' AND (
  NOT EXISTS (
    SELECT 1 FROM image_review_runs r
    JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
      AND p.seal_status = 'finalized'
      AND p.plan_kind IN ('boundary', 'boundary_supplement')
    WHERE r.review_run_id = NEW.review_run_id AND r.seal_status = 'finalized'
      AND p.guide_version = r.guide_version
      AND p.member_count = NEW.planned_pair_count
      AND p.member_manifest_sha256 = NEW.plan_manifest_sha256
      AND (SELECT COUNT(*) FROM image_double_label_plans p2
           WHERE p2.review_run_id = r.review_run_id
             AND p2.seal_status = 'finalized'
             AND p2.plan_kind IN ('boundary', 'boundary_supplement')) = 1
  )
  OR (SELECT COUNT(*) FROM image_agreement_evaluation_annotations l
      WHERE l.evaluation_id = NEW.evaluation_id) != (
    SELECT COUNT(*) FROM image_review_annotations a
    JOIN image_double_label_plans p ON p.review_run_id = a.review_run_id
      AND p.seal_status = 'finalized'
      AND p.plan_kind IN ('boundary', 'boundary_supplement')
    JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
      AND pm.fingerprint_id = a.fingerprint_id
    WHERE a.review_run_id = NEW.review_run_id
  )
  OR EXISTS (
    SELECT 1 FROM image_review_annotations a
    JOIN image_double_label_plans p ON p.review_run_id = a.review_run_id
      AND p.seal_status = 'finalized'
      AND p.plan_kind IN ('boundary', 'boundary_supplement')
    JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
      AND pm.fingerprint_id = a.fingerprint_id
    WHERE a.review_run_id = NEW.review_run_id AND NOT EXISTS (
      SELECT 1 FROM image_agreement_evaluation_annotations l
      WHERE l.evaluation_id = NEW.evaluation_id
        AND l.annotation_id = a.annotation_id
    )
  )
  OR EXISTS (
    SELECT 1 FROM image_double_label_plans p
    JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
    JOIN image_review_annotations a1 ON a1.review_run_id = p.review_run_id
      AND a1.fingerprint_id = pm.fingerprint_id AND a1.assignment_slot = 1
    JOIN image_review_annotations a2 ON a2.review_run_id = p.review_run_id
      AND a2.fingerprint_id = pm.fingerprint_id AND a2.assignment_slot = 2
    WHERE p.review_run_id = NEW.review_run_id AND p.seal_status = 'finalized'
      AND p.plan_kind IN ('boundary', 'boundary_supplement')
      AND a1.annotator_hash = a2.annotator_hash
  )
)
BEGIN SELECT RAISE(ABORT, 'image agreement evidence is incomplete'); END;

CREATE TRIGGER validate_image_agreement_counts_and_status
BEFORE UPDATE OF seal_status ON image_agreement_evaluations
WHEN NEW.seal_status = 'finalized' AND (
  NEW.planned_pair_count <= 0
  OR NEW.complete_pair_count != (
    SELECT COUNT(*) FROM image_double_label_plans p
    JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
    WHERE p.review_run_id = NEW.review_run_id AND p.seal_status = 'finalized'
      AND p.plan_kind IN ('boundary', 'boundary_supplement')
      AND EXISTS (SELECT 1 FROM image_review_annotations a1
                  WHERE a1.review_run_id = p.review_run_id
                    AND a1.fingerprint_id = pm.fingerprint_id
                    AND a1.assignment_slot = 1)
      AND EXISTS (SELECT 1 FROM image_review_annotations a2
                  WHERE a2.review_run_id = p.review_run_id
                    AND a2.fingerprint_id = pm.fingerprint_id
                    AND a2.assignment_slot = 2)
  )
  OR NEW.agreement_count != (
    SELECT COUNT(*) FROM image_double_label_plans p
    JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
    JOIN image_review_annotations a1 ON a1.review_run_id = p.review_run_id
      AND a1.fingerprint_id = pm.fingerprint_id AND a1.assignment_slot = 1
    JOIN image_review_annotations a2 ON a2.review_run_id = p.review_run_id
      AND a2.fingerprint_id = pm.fingerprint_id AND a2.assignment_slot = 2
    WHERE p.review_run_id = NEW.review_run_id AND p.seal_status = 'finalized'
      AND p.plan_kind IN ('boundary', 'boundary_supplement')
      AND a1.technical_noise_label = a2.technical_noise_label
  )
  OR (NEW.complete_pair_count < NEW.planned_pair_count AND (
       NEW.raw_agreement IS NOT NULL OR NEW.cohen_kappa IS NOT NULL
       OR NEW.kappa_status != 'incomplete' OR NEW.evaluation_status != 'incomplete'))
  OR (NEW.complete_pair_count = NEW.planned_pair_count AND (
       NEW.raw_agreement IS NULL
       OR abs(NEW.raw_agreement -
              (NEW.agreement_count * 1.0 / NEW.planned_pair_count)) > 1.0e-9
       OR NEW.evaluation_status != CASE
            WHEN (NEW.agreement_count * 1.0 / NEW.planned_pair_count) >= 0.80
            THEN 'passed' ELSE 'supplement_required' END))
)
BEGIN SELECT RAISE(ABORT, 'image agreement counts or status mismatch'); END;

CREATE TRIGGER validate_image_agreement_kappa_and_disagreements
BEFORE UPDATE OF seal_status ON image_agreement_evaluations
WHEN NEW.seal_status = 'finalized' AND (
  (NEW.complete_pair_count = NEW.planned_pair_count AND (
    (
      (SELECT COUNT(DISTINCT a.technical_noise_label)
       FROM image_review_annotations a
       JOIN image_double_label_plans p ON p.review_run_id = a.review_run_id
         AND p.seal_status = 'finalized'
         AND p.plan_kind IN ('boundary', 'boundary_supplement')
       JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
         AND pm.fingerprint_id = a.fingerprint_id
       WHERE a.review_run_id = NEW.review_run_id) < 2
      AND (NEW.kappa_status != 'undefined_single_category'
           OR NEW.cohen_kappa IS NOT NULL)
    ) OR (
      (SELECT COUNT(DISTINCT a.technical_noise_label)
       FROM image_review_annotations a
       JOIN image_double_label_plans p ON p.review_run_id = a.review_run_id
         AND p.seal_status = 'finalized'
         AND p.plan_kind IN ('boundary', 'boundary_supplement')
       JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
         AND pm.fingerprint_id = a.fingerprint_id
       WHERE a.review_run_id = NEW.review_run_id) >= 2
      AND (
        NEW.kappa_status != 'estimated' OR NEW.cohen_kappa IS NULL
        OR abs(NEW.cohen_kappa - (
          ((NEW.agreement_count * 1.0 / NEW.planned_pair_count) -
           (SELECT SUM(x.left_count * x.right_count) * 1.0 /
                        (NEW.planned_pair_count * NEW.planned_pair_count)
            FROM (
              SELECT a.technical_noise_label,
                     SUM(CASE WHEN a.assignment_slot = 1 THEN 1 ELSE 0 END) AS left_count,
                     SUM(CASE WHEN a.assignment_slot = 2 THEN 1 ELSE 0 END) AS right_count
              FROM image_review_annotations a
              JOIN image_double_label_plans p ON p.review_run_id = a.review_run_id
                AND p.seal_status = 'finalized'
                AND p.plan_kind IN ('boundary', 'boundary_supplement')
              JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
                AND pm.fingerprint_id = a.fingerprint_id
              WHERE a.review_run_id = NEW.review_run_id
              GROUP BY a.technical_noise_label
            ) x)) /
          (1.0 -
           (SELECT SUM(x.left_count * x.right_count) * 1.0 /
                        (NEW.planned_pair_count * NEW.planned_pair_count)
            FROM (
              SELECT a.technical_noise_label,
                     SUM(CASE WHEN a.assignment_slot = 1 THEN 1 ELSE 0 END) AS left_count,
                     SUM(CASE WHEN a.assignment_slot = 2 THEN 1 ELSE 0 END) AS right_count
              FROM image_review_annotations a
              JOIN image_double_label_plans p ON p.review_run_id = a.review_run_id
                AND p.seal_status = 'finalized'
                AND p.plan_kind IN ('boundary', 'boundary_supplement')
              JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
                AND pm.fingerprint_id = a.fingerprint_id
              WHERE a.review_run_id = NEW.review_run_id
              GROUP BY a.technical_noise_label
            ) x)))
        )) > 1.0e-9
      )
    )
  )
  OR NOT json_valid(NEW.label_disagreements_json)
  OR json_type(NEW.label_disagreements_json) != 'array'
  OR json_array_length(NEW.label_disagreements_json) != (
    SELECT COUNT(*) FROM (
      SELECT a1.technical_noise_label, a2.technical_noise_label
      FROM image_double_label_plans p
      JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
      JOIN image_review_annotations a1 ON a1.review_run_id = p.review_run_id
        AND a1.fingerprint_id = pm.fingerprint_id AND a1.assignment_slot = 1
      JOIN image_review_annotations a2 ON a2.review_run_id = p.review_run_id
        AND a2.fingerprint_id = pm.fingerprint_id AND a2.assignment_slot = 2
      WHERE p.review_run_id = NEW.review_run_id AND p.seal_status = 'finalized'
        AND p.plan_kind IN ('boundary', 'boundary_supplement')
        AND a1.technical_noise_label != a2.technical_noise_label
      GROUP BY a1.technical_noise_label, a2.technical_noise_label
    )
  )
  OR EXISTS (
    SELECT 1 FROM json_each(NEW.label_disagreements_json) j
    WHERE json_type(j.value) != 'array' OR json_array_length(j.value) != 3
      OR NOT EXISTS (
        SELECT 1 FROM image_double_label_plans p
        JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
        JOIN image_review_annotations a1 ON a1.review_run_id = p.review_run_id
          AND a1.fingerprint_id = pm.fingerprint_id AND a1.assignment_slot = 1
        JOIN image_review_annotations a2 ON a2.review_run_id = p.review_run_id
          AND a2.fingerprint_id = pm.fingerprint_id AND a2.assignment_slot = 2
        WHERE p.review_run_id = NEW.review_run_id AND p.seal_status = 'finalized'
          AND p.plan_kind IN ('boundary', 'boundary_supplement')
          AND a1.technical_noise_label = json_extract(j.value, '$[0]')
          AND a2.technical_noise_label = json_extract(j.value, '$[1]')
          AND a1.technical_noise_label != a2.technical_noise_label
        GROUP BY a1.technical_noise_label, a2.technical_noise_label
        HAVING COUNT(*) = json_extract(j.value, '$[2]')
      )
  )
  OR EXISTS (
    SELECT 1 FROM (
      SELECT a1.technical_noise_label AS left_label,
             a2.technical_noise_label AS right_label,
             COUNT(*) AS pair_count
      FROM image_double_label_plans p
      JOIN image_double_label_plan_members pm ON pm.plan_id = p.plan_id
      JOIN image_review_annotations a1 ON a1.review_run_id = p.review_run_id
        AND a1.fingerprint_id = pm.fingerprint_id AND a1.assignment_slot = 1
      JOIN image_review_annotations a2 ON a2.review_run_id = p.review_run_id
        AND a2.fingerprint_id = pm.fingerprint_id AND a2.assignment_slot = 2
      WHERE p.review_run_id = NEW.review_run_id AND p.seal_status = 'finalized'
        AND p.plan_kind IN ('boundary', 'boundary_supplement')
        AND a1.technical_noise_label != a2.technical_noise_label
      GROUP BY a1.technical_noise_label, a2.technical_noise_label
    ) actual
    WHERE NOT EXISTS (
      SELECT 1 FROM json_each(NEW.label_disagreements_json) j
      WHERE json_extract(j.value, '$[0]') = actual.left_label
        AND json_extract(j.value, '$[1]') = actual.right_label
        AND json_extract(j.value, '$[2]') = actual.pair_count
    )
  )
)
BEGIN SELECT RAISE(ABORT, 'image agreement kappa or disagreements mismatch'); END;

CREATE TRIGGER freeze_image_agreement_status
BEFORE UPDATE OF seal_status ON image_agreement_evaluations
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'image agreement status is immutable'); END;
CREATE TRIGGER immutable_image_agreements_update
BEFORE UPDATE OF evaluation_id, review_run_id, plan_manifest_sha256,
  annotation_manifest_sha256, planned_pair_count, complete_pair_count,
  agreement_count, raw_agreement, cohen_kappa, kappa_status,
  evaluation_status, label_disagreements_json, created_at_utc
ON image_agreement_evaluations
BEGIN SELECT RAISE(ABORT, 'image agreement evaluations are immutable'); END;
CREATE TRIGGER immutable_image_agreements_delete
BEFORE DELETE ON image_agreement_evaluations
BEGIN SELECT RAISE(ABORT, 'image agreement evaluations are immutable'); END;
CREATE TRIGGER freeze_finalized_image_agreement_links
BEFORE INSERT ON image_agreement_evaluation_annotations
WHEN EXISTS (SELECT 1 FROM image_agreement_evaluations e
             WHERE e.evaluation_id = NEW.evaluation_id
               AND e.seal_status != 'building')
BEGIN SELECT RAISE(ABORT, 'image agreement evidence is sealed'); END;
CREATE TRIGGER immutable_image_agreement_links_update
BEFORE UPDATE ON image_agreement_evaluation_annotations
BEGIN SELECT RAISE(ABORT, 'image agreement evidence is immutable'); END;
CREATE TRIGGER immutable_image_agreement_links_delete
BEFORE DELETE ON image_agreement_evaluation_annotations
BEGIN SELECT RAISE(ABORT, 'image agreement evidence is immutable'); END;

DROP TRIGGER IF EXISTS immutable_audit_evaluations_update;
DROP TRIGGER IF EXISTS immutable_audit_evaluations_delete;
DROP TRIGGER IF EXISTS validate_keep_audit_round_sequence;
ALTER TABLE image_keep_audit_evaluations RENAME TO image_keep_audit_evaluations_v19;

CREATE TABLE image_keep_audit_evaluations (
    audit_evaluation_id TEXT PRIMARY KEY,
    audit_round_id TEXT NOT NULL REFERENCES image_keep_audit_rounds(audit_round_id) ON DELETE RESTRICT,
    completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
    primary_event_count INTEGER NOT NULL CHECK (primary_event_count >= 0),
    supplement_event_count INTEGER NOT NULL CHECK (supplement_event_count >= 0),
    primary_point_estimate REAL,
    one_sided_upper REAL,
    evaluation_status TEXT NOT NULL CHECK (
        evaluation_status IN ('incomplete', 'passed', 'failed')
    ),
    reason_code TEXT NOT NULL,
    evidence_manifest_sha256 TEXT NOT NULL CHECK (length(evidence_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (
        seal_status IN ('building', 'finalized', 'untrusted_legacy')
    ),
    created_at_utc TEXT NOT NULL
);

INSERT INTO image_keep_audit_evaluations(
  audit_evaluation_id, audit_round_id, completed_count, primary_event_count,
  supplement_event_count, primary_point_estimate, one_sided_upper,
  evaluation_status, reason_code, evidence_manifest_sha256, seal_status,
  created_at_utc
)
SELECT audit_evaluation_id, audit_round_id, completed_count, primary_event_count,
       supplement_event_count, primary_point_estimate, one_sided_upper,
       evaluation_status, reason_code, evidence_manifest_sha256,
       'untrusted_legacy', created_at_utc
FROM image_keep_audit_evaluations_v19;
DROP TABLE image_keep_audit_evaluations_v19;

CREATE UNIQUE INDEX idx_verified_keep_audit_round_evaluation
ON image_keep_audit_evaluations(audit_round_id)
WHERE seal_status IN ('building', 'finalized');

CREATE TABLE image_keep_audit_evaluation_annotations (
    audit_evaluation_id TEXT NOT NULL
      REFERENCES image_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    audit_annotation_id TEXT NOT NULL
      REFERENCES image_keep_audit_annotations(audit_annotation_id) ON DELETE RESTRICT,
    audit_round_id TEXT NOT NULL,
    fingerprint_id TEXT NOT NULL,
    sampling_layer TEXT NOT NULL CHECK (
      sampling_layer IN ('primary', 'platform_supplement')
    ),
    PRIMARY KEY (audit_evaluation_id, audit_annotation_id),
    UNIQUE (audit_evaluation_id, fingerprint_id),
    FOREIGN KEY (audit_round_id, fingerprint_id)
      REFERENCES image_keep_audit_members(audit_round_id, fingerprint_id) ON DELETE RESTRICT
);

CREATE TRIGGER require_keep_audit_evaluation_building_insert
BEFORE INSERT ON image_keep_audit_evaluations
WHEN NEW.seal_status != 'building'
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation must start building'); END;

CREATE TRIGGER validate_keep_audit_evaluation_annotation_link
BEFORE INSERT ON image_keep_audit_evaluation_annotations
WHEN NOT EXISTS (
  SELECT 1 FROM image_keep_audit_evaluations e
  JOIN image_keep_audit_annotations a
    ON a.audit_annotation_id = NEW.audit_annotation_id
  JOIN image_keep_audit_rounds r ON r.audit_round_id = e.audit_round_id
  JOIN image_decision_builds d ON d.decision_build_id = r.decision_build_id
  JOIN image_keep_audit_members m ON m.audit_round_id = r.audit_round_id
    AND m.fingerprint_id = a.fingerprint_id
  WHERE e.audit_evaluation_id = NEW.audit_evaluation_id
    AND e.seal_status = 'building' AND r.seal_status = 'finalized'
    AND a.audit_round_id = e.audit_round_id
    AND a.audit_round_id = NEW.audit_round_id
    AND a.fingerprint_id = NEW.fingerprint_id
    AND a.guide_version = d.guide_version
    AND m.sampling_layer = NEW.sampling_layer
)
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation annotation lineage mismatch'); END;

CREATE TRIGGER validate_keep_audit_evaluation_seal
BEFORE UPDATE OF seal_status ON image_keep_audit_evaluations
WHEN NEW.seal_status = 'finalized' AND (
  NOT EXISTS (SELECT 1 FROM image_keep_audit_rounds r
              WHERE r.audit_round_id = NEW.audit_round_id
                AND r.seal_status = 'finalized')
  OR (SELECT COUNT(*) FROM image_keep_audit_members m
      WHERE m.audit_round_id = NEW.audit_round_id) <= 0
  OR (SELECT COUNT(*) FROM image_keep_audit_annotations a
      WHERE a.audit_round_id = NEW.audit_round_id) !=
     (SELECT COUNT(*) FROM image_keep_audit_members m
      WHERE m.audit_round_id = NEW.audit_round_id)
  OR (SELECT COUNT(*) FROM image_keep_audit_evaluation_annotations l
      WHERE l.audit_evaluation_id = NEW.audit_evaluation_id) !=
     (SELECT COUNT(*) FROM image_keep_audit_members m
      WHERE m.audit_round_id = NEW.audit_round_id)
  OR EXISTS (
    SELECT 1 FROM image_keep_audit_members m
    WHERE m.audit_round_id = NEW.audit_round_id AND NOT EXISTS (
      SELECT 1 FROM image_keep_audit_evaluation_annotations l
      WHERE l.audit_evaluation_id = NEW.audit_evaluation_id
        AND l.fingerprint_id = m.fingerprint_id
        AND l.sampling_layer = m.sampling_layer))
  OR NEW.completed_count != (
     SELECT COUNT(*) FROM image_keep_audit_members m
     WHERE m.audit_round_id = NEW.audit_round_id)
  OR NEW.primary_event_count != (
     SELECT COUNT(*) FROM image_keep_audit_annotations a
     JOIN image_keep_audit_members m ON m.audit_round_id = a.audit_round_id
       AND m.fingerprint_id = a.fingerprint_id
     WHERE a.audit_round_id = NEW.audit_round_id AND m.sampling_layer = 'primary'
       AND a.technical_noise_label IN (
         'site_background', 'site_ui', 'placeholder_or_error',
         'tracking_or_qr_only', 'uncertain'))
  OR NEW.supplement_event_count != (
     SELECT COUNT(*) FROM image_keep_audit_annotations a
     JOIN image_keep_audit_members m ON m.audit_round_id = a.audit_round_id
       AND m.fingerprint_id = a.fingerprint_id
     WHERE a.audit_round_id = NEW.audit_round_id
       AND m.sampling_layer = 'platform_supplement'
       AND a.technical_noise_label IN (
         'site_background', 'site_ui', 'placeholder_or_error',
         'tracking_or_qr_only', 'uncertain'))
  OR NEW.primary_point_estimate IS NULL OR NEW.one_sided_upper IS NULL
  OR abs(NEW.primary_point_estimate -
         (NEW.primary_event_count * 1.0 /
          (SELECT COUNT(*) FROM image_keep_audit_members m
           WHERE m.audit_round_id = NEW.audit_round_id
             AND m.sampling_layer = 'primary'))) > 1.0e-9
  OR abs(NEW.one_sided_upper - CASE
       WHEN (SELECT r.interval_method FROM image_keep_audit_rounds r
             WHERE r.audit_round_id = NEW.audit_round_id) = 'census'
       THEN NEW.primary_point_estimate
       ELSE (
         NEW.primary_point_estimate + 2.705543454095404 /
           (2.0 * (SELECT COUNT(*) FROM image_keep_audit_members m
                   WHERE m.audit_round_id = NEW.audit_round_id
                     AND m.sampling_layer = 'primary'))
         + 1.6448536269514722 * sqrt(
             NEW.primary_point_estimate * (1.0 - NEW.primary_point_estimate) /
               (SELECT COUNT(*) FROM image_keep_audit_members m
                WHERE m.audit_round_id = NEW.audit_round_id
                  AND m.sampling_layer = 'primary')
             + 2.705543454095404 /
               (4.0 * (SELECT COUNT(*) FROM image_keep_audit_members m
                       WHERE m.audit_round_id = NEW.audit_round_id
                         AND m.sampling_layer = 'primary') *
                      (SELECT COUNT(*) FROM image_keep_audit_members m
                       WHERE m.audit_round_id = NEW.audit_round_id
                         AND m.sampling_layer = 'primary'))
           )
       ) / (1.0 + 2.705543454095404 /
           (SELECT COUNT(*) FROM image_keep_audit_members m
            WHERE m.audit_round_id = NEW.audit_round_id
              AND m.sampling_layer = 'primary'))
     END) > 1.0e-9
  OR NEW.evaluation_status != CASE
       WHEN NEW.supplement_event_count = 0
        AND NEW.primary_point_estimate <= 0.02
        AND NEW.one_sided_upper <= 0.02
       THEN 'passed' ELSE 'failed' END
  OR NEW.reason_code != CASE
       WHEN NEW.supplement_event_count = 0
        AND NEW.primary_point_estimate <= 0.02
        AND NEW.one_sided_upper <= 0.02
       THEN 'audit_quality_gate_passed'
       ELSE 'residual_technical_noise_detected' END
)
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation facts mismatch'); END;

CREATE TRIGGER freeze_keep_audit_evaluation_status
BEFORE UPDATE OF seal_status ON image_keep_audit_evaluations
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation status is immutable'); END;
CREATE TRIGGER immutable_audit_evaluations_update
BEFORE UPDATE OF audit_evaluation_id, audit_round_id, completed_count,
  primary_event_count, supplement_event_count, primary_point_estimate,
  one_sided_upper, evaluation_status, reason_code, evidence_manifest_sha256,
  created_at_utc
ON image_keep_audit_evaluations
BEGIN SELECT RAISE(ABORT, 'keep audit evaluations are immutable'); END;
CREATE TRIGGER immutable_audit_evaluations_delete
BEFORE DELETE ON image_keep_audit_evaluations
BEGIN SELECT RAISE(ABORT, 'keep audit evaluations are immutable'); END;
CREATE TRIGGER freeze_finalized_keep_audit_evaluation_links
BEFORE INSERT ON image_keep_audit_evaluation_annotations
WHEN EXISTS (SELECT 1 FROM image_keep_audit_evaluations e
             WHERE e.audit_evaluation_id = NEW.audit_evaluation_id
               AND e.seal_status != 'building')
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation evidence is sealed'); END;
CREATE TRIGGER immutable_keep_audit_evaluation_links_update
BEFORE UPDATE ON image_keep_audit_evaluation_annotations
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation evidence is immutable'); END;
CREATE TRIGGER immutable_keep_audit_evaluation_links_delete
BEFORE DELETE ON image_keep_audit_evaluation_annotations
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation evidence is immutable'); END;

-- v19 的后续轮门只检查 evaluation_status；v20 还要求上一轮是经原始证据
-- 重算封存的可信 failed，旧 untrusted_legacy 行永远不能打开第二轮。
DROP TRIGGER IF EXISTS validate_keep_audit_round_sequence;
CREATE TRIGGER validate_keep_audit_round_sequence
BEFORE INSERT ON image_keep_audit_rounds
WHEN NEW.round_number > 3 OR EXISTS (
 SELECT 1 FROM image_keep_audit_rounds r
 JOIN image_decision_builds b ON b.decision_build_id = r.decision_build_id
 JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
 WHERE b.candidate_build_id = nb.candidate_build_id
   AND r.round_number = NEW.round_number
) OR (
 NEW.round_number = 1 AND EXISTS (
   SELECT 1 FROM image_keep_audit_rounds r
   JOIN image_decision_builds b ON b.decision_build_id = r.decision_build_id
   JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
   WHERE b.candidate_build_id = nb.candidate_build_id)
) OR (
 NEW.round_number > 1 AND NOT EXISTS (
   SELECT 1 FROM image_keep_audit_rounds prior
   JOIN image_decision_builds pb ON pb.decision_build_id = prior.decision_build_id
   JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
   JOIN image_keep_audit_evaluations e ON e.audit_round_id = prior.audit_round_id
   WHERE pb.candidate_build_id = nb.candidate_build_id
     AND prior.round_number = NEW.round_number - 1
     AND e.seal_status = 'finalized' AND e.evaluation_status = 'failed'
     AND prior.decision_build_id != NEW.decision_build_id
     AND pb.decision_manifest_sha256 != nb.decision_manifest_sha256
 ))
BEGIN SELECT RAISE(ABORT, 'keep audit round sequence or revised decision is invalid'); END;

-- v19 的正式门可能读取迁移后保留的旧 passed；增加可信评估硬门，要求 pilot、
-- 首轮 boundary（或失败首轮+通过补充）均为 v20 finalized。
CREATE TRIGGER validate_image_formal_review_evaluations_trusted
BEFORE UPDATE OF seal_status ON image_decision_builds
WHEN NEW.seal_status = 'finalized' AND (
  NOT EXISTS (
    SELECT 1 FROM image_review_runs r
    JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
    WHERE r.candidate_build_id = NEW.candidate_build_id
      AND r.review_kind = 'pilot' AND r.guide_version = NEW.guide_version
      AND e.seal_status = 'finalized' AND e.evaluation_status = 'passed'
      AND e.complete_pair_count = r.member_count AND e.raw_agreement >= 0.80
  )
  OR NOT EXISTS (
    SELECT 1 FROM image_review_runs r
    JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
      AND p.plan_kind = 'boundary' AND p.seal_status = 'finalized'
    JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
      AND e.seal_status = 'finalized'
    WHERE r.candidate_build_id = NEW.candidate_build_id
      AND r.review_kind = 'boundary' AND r.guide_version = NEW.guide_version
      AND (
        (e.evaluation_status = 'passed' AND e.raw_agreement >= 0.80)
        OR (e.evaluation_status = 'supplement_required' AND EXISTS (
          SELECT 1 FROM image_review_runs sr
          JOIN image_double_label_plans sp ON sp.review_run_id = sr.review_run_id
            AND sp.plan_kind = 'boundary_supplement' AND sp.seal_status = 'finalized'
          JOIN image_agreement_evaluations se ON se.review_run_id = sr.review_run_id
            AND se.seal_status = 'finalized' AND se.evaluation_status = 'passed'
            AND se.raw_agreement >= 0.80
          WHERE sr.candidate_build_id = NEW.candidate_build_id
            AND sr.review_kind = 'boundary' AND sr.guide_version = NEW.guide_version
        ))
      )
  )
)
BEGIN SELECT RAISE(ABORT, 'trusted formal image review evaluations are incomplete'); END;

-- 保留 v19 正式门的样本规模、配置谱系与补充轮约束；区别仅在于评估必须是
-- v20 由原始标注证据封存的 finalized 行，不能再读取迁移保留的旧派生结果。
CREATE TRIGGER validate_image_formal_review_gate
BEFORE UPDATE OF seal_status ON image_decision_builds
WHEN NEW.seal_status = 'finalized' AND (
 NOT EXISTS (
   SELECT 1 FROM image_review_runs r
   JOIN image_candidate_builds b ON b.build_id = r.candidate_build_id
   JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
     AND p.plan_kind = 'boundary' AND p.seal_status = 'finalized'
     AND p.member_count = r.member_count
   JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
     AND e.seal_status = 'finalized'
     AND e.planned_pair_count = r.member_count AND e.complete_pair_count = r.member_count
   WHERE r.candidate_build_id = NEW.candidate_build_id
     AND r.review_kind = 'pilot' AND r.guide_version = NEW.guide_version
     AND r.config_sha256 = b.config_sha256 AND r.seal_status = 'finalized'
     AND r.member_count = min(30, (
       SELECT COUNT(*) FROM image_exact_clusters c
       WHERE c.build_id = NEW.candidate_build_id AND (
         c.member_count > 1
         OR EXISTS (SELECT 1 FROM image_candidate_signals s WHERE s.build_id = c.build_id
                      AND s.fingerprint_id = c.representative_fingerprint_id)
         OR EXISTS (SELECT 1 FROM image_near_candidate_pairs n WHERE n.build_id = c.build_id
                      AND (n.left_fingerprint_id = c.representative_fingerprint_id
                           OR n.right_fingerprint_id = c.representative_fingerprint_id))
       )))
     AND e.evaluation_status = 'passed' AND e.raw_agreement >= 0.80
 )
 OR NOT EXISTS (
   SELECT 1 FROM image_review_runs r
   JOIN image_candidate_builds b ON b.build_id = r.candidate_build_id
   JOIN image_double_label_plans p ON p.review_run_id = r.review_run_id
     AND p.plan_kind = 'boundary' AND p.seal_status = 'finalized'
     AND p.member_count = r.member_count
   JOIN image_agreement_evaluations e ON e.review_run_id = r.review_run_id
     AND e.seal_status = 'finalized'
     AND e.planned_pair_count = r.member_count AND e.complete_pair_count = r.member_count
   WHERE r.candidate_build_id = NEW.candidate_build_id
     AND r.review_kind = 'boundary' AND r.guide_version = NEW.guide_version
     AND r.config_sha256 = b.config_sha256 AND r.seal_status = 'finalized'
     AND r.member_count = min(50, (SELECT COUNT(*) FROM image_exact_clusters c
                                  WHERE c.build_id = NEW.candidate_build_id))
     AND (
       (e.evaluation_status = 'passed' AND e.raw_agreement >= 0.80)
       OR (
         e.evaluation_status = 'supplement_required'
         AND EXISTS (
           SELECT 1 FROM image_review_runs sr
           JOIN image_double_label_plans sp ON sp.review_run_id = sr.review_run_id
             AND sp.plan_kind = 'boundary_supplement'
             AND sp.seal_status = 'finalized' AND sp.member_count = sr.member_count
           JOIN image_agreement_evaluations se ON se.review_run_id = sr.review_run_id
             AND se.seal_status = 'finalized'
             AND se.planned_pair_count = sr.member_count
             AND se.complete_pair_count = sr.member_count
           WHERE sr.candidate_build_id = NEW.candidate_build_id
             AND sr.review_kind = 'boundary' AND sr.guide_version = NEW.guide_version
             AND sr.config_sha256 = b.config_sha256 AND sr.seal_status = 'finalized'
             AND se.evaluation_status = 'passed' AND se.raw_agreement >= 0.80
         )
       )
     )
 )
)
BEGIN SELECT RAISE(ABORT, 'formal image review gate is incomplete'); END;
"""


_SCHEMA_V21 = """
-- v21 将审计轮本身从自报父行升级为可验证证据。v20 及更早轮次保留为
-- untrusted_legacy；新轮次必须冻结完整决定后人口快照，并由相同 SHA-256
-- 抽样算法在 SQLite 中重建 primary、平台补充、概率、权重和 manifest。
ALTER TABLE image_keep_audit_rounds ADD COLUMN integrity_status TEXT NOT NULL
  DEFAULT 'untrusted_legacy'
  CHECK (integrity_status IN ('building', 'finalized', 'untrusted_legacy'));
ALTER TABLE image_keep_audit_rounds ADD COLUMN sampling_algorithm_version TEXT NOT NULL
  DEFAULT 'untrusted_legacy';

CREATE TABLE image_keep_audit_population_members (
    audit_round_id TEXT NOT NULL
      REFERENCES image_keep_audit_rounds(audit_round_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL
      REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    platform_key TEXT NOT NULL,
    population_rank INTEGER NOT NULL CHECK (population_rank > 0),
    PRIMARY KEY (audit_round_id, fingerprint_id),
    UNIQUE (audit_round_id, population_rank)
);

-- 决定动作作用于 SHA 代表，审计单位则是该代表精确簇内的每条 content 图片
-- 关系；平台来自同一冻结 build member 对应的帖子库存。
CREATE VIEW image_keep_audit_actual_population AS
SELECT DISTINCT b.decision_build_id, b.candidate_build_id,
       m.fingerprint_id, p.platform_key
FROM image_decision_builds b
JOIN image_decisions d ON d.decision_build_id = b.decision_build_id
  AND d.decision_action IN ('keep', 'review')
JOIN image_exact_clusters c ON c.build_id = b.candidate_build_id
  AND c.representative_fingerprint_id = d.fingerprint_id
JOIN image_exact_cluster_members m ON m.build_id = c.build_id
  AND m.cluster_id = c.cluster_id
JOIN image_candidate_build_members bm ON bm.build_id = m.build_id
  AND bm.fingerprint_id = m.fingerprint_id
JOIN source_post_inventory p ON p.source_post_id = bm.source_post_id
WHERE b.seal_status = 'finalized';

-- 该视图为每个 building/finalized 可信轮重建唯一应出现的两层成员。历史轮
-- 成员只在自身也经 v21 封存时参与跨轮排除，不能由 untrusted 行改变样本框。
CREATE VIEW image_keep_audit_expected_members AS
WITH eligible_base AS (
  SELECT r.audit_round_id, r.random_seed, p.fingerprint_id, p.platform_key,
         audit_sample_rank(r.random_seed, 'image-audit-primary',
                           p.fingerprint_id) AS sample_key
  FROM image_keep_audit_rounds r
  JOIN image_keep_audit_population_members p
    ON p.audit_round_id = r.audit_round_id
  JOIN image_decision_builds current_build
    ON current_build.decision_build_id = r.decision_build_id
  WHERE r.integrity_status IN ('building', 'finalized')
    AND r.sampling_algorithm_version = 'image-keep-audit-sampling-v1'
    AND NOT EXISTS (
      SELECT 1 FROM image_keep_audit_rounds prior
      JOIN image_decision_builds prior_build
        ON prior_build.decision_build_id = prior.decision_build_id
      JOIN image_keep_audit_members used
        ON used.audit_round_id = prior.audit_round_id
      WHERE prior_build.candidate_build_id = current_build.candidate_build_id
        AND prior.integrity_status = 'finalized'
        AND prior.seal_status = 'finalized'
        AND prior.round_number < r.round_number
        AND used.fingerprint_id = p.fingerprint_id
    )
), eligible AS (
  SELECT e.*,
         COUNT(*) OVER (PARTITION BY e.audit_round_id) AS eligible_count,
         ROW_NUMBER() OVER (
           PARTITION BY e.audit_round_id ORDER BY e.sample_key, e.fingerprint_id
         ) AS primary_rank
  FROM eligible_base e
), primary_expected AS (
  SELECT audit_round_id, fingerprint_id, platform_key, 'primary' AS sampling_layer,
         primary_rank AS stable_rank,
         MIN(200, eligible_count) * 1.0 / eligible_count AS inclusion_probability,
         eligible_count * 1.0 / MIN(200, eligible_count) AS sampling_weight
  FROM eligible
  WHERE primary_rank <= MIN(200, eligible_count)
), platform_population AS (
  SELECT audit_round_id, platform_key, COUNT(*) AS platform_population_count
  FROM image_keep_audit_population_members
  GROUP BY audit_round_id, platform_key
), platform_primary AS (
  SELECT audit_round_id, platform_key, COUNT(*) AS platform_primary_count
  FROM primary_expected GROUP BY audit_round_id, platform_key
), supplement_pool_base AS (
  SELECT e.audit_round_id, e.random_seed, e.fingerprint_id, e.platform_key,
         pp.platform_population_count,
         COALESCE(pc.platform_primary_count, 0) AS platform_primary_count,
         audit_sample_rank(
           e.random_seed, 'image-audit-supplement:' || e.platform_key,
           e.fingerprint_id
         ) AS sample_key
  FROM eligible e
  JOIN platform_population pp ON pp.audit_round_id = e.audit_round_id
    AND pp.platform_key = e.platform_key
  LEFT JOIN platform_primary pc ON pc.audit_round_id = e.audit_round_id
    AND pc.platform_key = e.platform_key
  LEFT JOIN primary_expected pe ON pe.audit_round_id = e.audit_round_id
    AND pe.fingerprint_id = e.fingerprint_id
  WHERE pe.fingerprint_id IS NULL
), supplement_ranked AS (
  SELECT s.*,
         COUNT(*) OVER (
           PARTITION BY s.audit_round_id, s.platform_key
         ) AS pool_count,
         ROW_NUMBER() OVER (
           PARTITION BY s.audit_round_id, s.platform_key
           ORDER BY s.sample_key, s.fingerprint_id
         ) AS platform_rank
  FROM supplement_pool_base s
), supplement_candidates AS (
  SELECT s.*,
         MAX(0, MIN(30, s.platform_population_count) -
                s.platform_primary_count) AS needed_count
  FROM supplement_ranked s
), supplement_local AS (
  SELECT audit_round_id, fingerprint_id, platform_key,
         platform_rank, pool_count, MIN(needed_count, pool_count) AS chosen_count
  FROM supplement_candidates
  WHERE platform_rank <= MIN(needed_count, pool_count)
), supplement_expected AS (
  SELECT audit_round_id, fingerprint_id, platform_key,
         'platform_supplement' AS sampling_layer,
         ROW_NUMBER() OVER (
           PARTITION BY audit_round_id ORDER BY platform_key, platform_rank
         ) AS stable_rank,
         chosen_count * 1.0 / pool_count AS inclusion_probability,
         pool_count * 1.0 / chosen_count AS sampling_weight
  FROM supplement_local
)
SELECT * FROM primary_expected
UNION ALL
SELECT * FROM supplement_expected;

DROP TRIGGER IF EXISTS require_keep_audit_building_insert;
CREATE TRIGGER require_keep_audit_building_insert
BEFORE INSERT ON image_keep_audit_rounds
WHEN NEW.seal_status != 'building' OR NEW.integrity_status != 'building'
 OR NEW.sampling_algorithm_version != 'image-keep-audit-sampling-v1'
 OR NEW.random_seed <= 0 OR NOT EXISTS (
   SELECT 1 FROM image_decision_builds d
   WHERE d.decision_build_id = NEW.decision_build_id
     AND d.seal_status = 'finalized'
 )
BEGIN SELECT RAISE(ABORT, 'keep audit requires trusted building identity'); END;

CREATE TRIGGER validate_keep_audit_population_member
BEFORE INSERT ON image_keep_audit_population_members
WHEN NOT EXISTS (
  SELECT 1 FROM image_keep_audit_rounds r
  JOIN image_keep_audit_actual_population p
    ON p.decision_build_id = r.decision_build_id
   AND p.fingerprint_id = NEW.fingerprint_id
   AND p.platform_key = NEW.platform_key
  WHERE r.audit_round_id = NEW.audit_round_id
    AND r.seal_status = 'building' AND r.integrity_status = 'building'
    AND NEW.population_rank = 1 + (
      SELECT COUNT(*) FROM image_keep_audit_actual_population p2
      WHERE p2.decision_build_id = r.decision_build_id
        AND p2.fingerprint_id < NEW.fingerprint_id
    )
)
BEGIN SELECT RAISE(ABORT, 'keep audit population evidence mismatch'); END;

DROP TRIGGER IF EXISTS validate_keep_audit_member;
CREATE TRIGGER validate_keep_audit_member
BEFORE INSERT ON image_keep_audit_members
WHEN NOT EXISTS (
  SELECT 1 FROM image_keep_audit_rounds r
  JOIN image_keep_audit_expected_members e
    ON e.audit_round_id = r.audit_round_id
   AND e.fingerprint_id = NEW.fingerprint_id
   AND e.platform_key = NEW.platform_key
   AND e.sampling_layer = NEW.sampling_layer
   AND e.stable_rank = NEW.stable_rank
   AND abs(e.inclusion_probability - NEW.inclusion_probability) <= 1.0e-12
   AND abs(e.sampling_weight - NEW.sampling_weight) <= 1.0e-9
  WHERE r.audit_round_id = NEW.audit_round_id
    AND r.seal_status = 'building' AND r.integrity_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'keep audit member differs from deterministic sample'); END;

DROP TRIGGER IF EXISTS validate_keep_audit_nonoverlap;
CREATE TRIGGER validate_keep_audit_nonoverlap
BEFORE INSERT ON image_keep_audit_members
WHEN EXISTS (
 SELECT 1 FROM image_keep_audit_rounds current
 JOIN image_decision_builds current_build
   ON current_build.decision_build_id = current.decision_build_id
 JOIN image_keep_audit_rounds prior
 JOIN image_decision_builds prior_build
   ON prior_build.decision_build_id = prior.decision_build_id
  AND prior_build.candidate_build_id = current_build.candidate_build_id
 JOIN image_keep_audit_members m ON m.audit_round_id = prior.audit_round_id
 WHERE current.audit_round_id = NEW.audit_round_id
   AND prior.integrity_status = 'finalized' AND prior.seal_status = 'finalized'
   AND prior.round_number < current.round_number
   AND m.fingerprint_id = NEW.fingerprint_id
)
BEGIN SELECT RAISE(ABORT, 'keep audit rounds must not overlap'); END;

DROP TRIGGER IF EXISTS validate_keep_audit_seal;
CREATE TRIGGER validate_keep_audit_seal
BEFORE UPDATE OF integrity_status ON image_keep_audit_rounds
WHEN NEW.integrity_status = 'finalized' AND (
  NEW.seal_status != 'finalized'
  OR (SELECT COUNT(*) FROM image_keep_audit_actual_population p
      WHERE p.decision_build_id = NEW.decision_build_id) <= 0
  OR NEW.population_count != (
      SELECT COUNT(*) FROM image_keep_audit_actual_population p
      WHERE p.decision_build_id = NEW.decision_build_id)
  OR NEW.population_count != (
      SELECT COUNT(*) FROM image_keep_audit_population_members p
      WHERE p.audit_round_id = NEW.audit_round_id)
  OR EXISTS (
      SELECT 1 FROM image_keep_audit_actual_population p
      WHERE p.decision_build_id = NEW.decision_build_id AND NOT EXISTS (
        SELECT 1 FROM image_keep_audit_population_members s
        WHERE s.audit_round_id = NEW.audit_round_id
          AND s.fingerprint_id = p.fingerprint_id
          AND s.platform_key = p.platform_key))
  OR NEW.population_manifest_sha256 != canonical_json_sha256(
      COALESCE((SELECT json_group_array(json(payload)) FROM (
        SELECT json_array(p.fingerprint_id, p.platform_key) AS payload
        FROM image_keep_audit_population_members p
        WHERE p.audit_round_id = NEW.audit_round_id
        ORDER BY p.fingerprint_id
      )), '[]'))
  OR NEW.primary_count <= 0
  OR NEW.primary_count != (
      SELECT COUNT(*) FROM image_keep_audit_expected_members e
      WHERE e.audit_round_id = NEW.audit_round_id AND e.sampling_layer = 'primary')
  OR NEW.supplement_count != (
      SELECT COUNT(*) FROM image_keep_audit_expected_members e
      WHERE e.audit_round_id = NEW.audit_round_id
        AND e.sampling_layer = 'platform_supplement')
  OR NEW.primary_count != (
      SELECT COUNT(*) FROM image_keep_audit_members m
      WHERE m.audit_round_id = NEW.audit_round_id AND m.sampling_layer = 'primary')
  OR NEW.supplement_count != (
      SELECT COUNT(*) FROM image_keep_audit_members m
      WHERE m.audit_round_id = NEW.audit_round_id
        AND m.sampling_layer = 'platform_supplement')
  OR EXISTS (
      SELECT 1 FROM image_keep_audit_expected_members e
      WHERE e.audit_round_id = NEW.audit_round_id AND NOT EXISTS (
        SELECT 1 FROM image_keep_audit_members m
        WHERE m.audit_round_id = e.audit_round_id
          AND m.fingerprint_id = e.fingerprint_id
          AND m.platform_key = e.platform_key
          AND m.sampling_layer = e.sampling_layer
          AND m.stable_rank = e.stable_rank
          AND abs(m.inclusion_probability - e.inclusion_probability) <= 1.0e-12
          AND abs(m.sampling_weight - e.sampling_weight) <= 1.0e-9))
  OR EXISTS (
      SELECT 1 FROM image_keep_audit_members m
      WHERE m.audit_round_id = NEW.audit_round_id AND NOT EXISTS (
        SELECT 1 FROM image_keep_audit_expected_members e
        WHERE e.audit_round_id = m.audit_round_id
          AND e.fingerprint_id = m.fingerprint_id
          AND e.platform_key = m.platform_key
          AND e.sampling_layer = m.sampling_layer
          AND e.stable_rank = m.stable_rank
          AND abs(e.inclusion_probability - m.inclusion_probability) <= 1.0e-12
          AND abs(e.sampling_weight - m.sampling_weight) <= 1.0e-9))
  OR NEW.primary_manifest_sha256 != audit_sample_manifest_sha256(
      COALESCE((SELECT json_group_array(json(payload)) FROM (
        SELECT json_array(m.fingerprint_id, m.platform_key, m.sampling_layer,
                          m.stable_rank, m.inclusion_probability,
                          m.sampling_weight) AS payload
        FROM image_keep_audit_members m
        WHERE m.audit_round_id = NEW.audit_round_id
          AND m.sampling_layer = 'primary'
        ORDER BY m.stable_rank
      )), '[]'))
  OR NEW.supplement_manifest_sha256 != audit_sample_manifest_sha256(
      COALESCE((SELECT json_group_array(json(payload)) FROM (
        SELECT json_array(m.fingerprint_id, m.platform_key, m.sampling_layer,
                          m.stable_rank, m.inclusion_probability,
                          m.sampling_weight) AS payload
        FROM image_keep_audit_members m
        WHERE m.audit_round_id = NEW.audit_round_id
          AND m.sampling_layer = 'platform_supplement'
        ORDER BY m.stable_rank
      )), '[]'))
  OR NEW.interval_method != CASE
      WHEN NEW.primary_count = NEW.population_count
       AND NEW.supplement_count = 0
       AND NOT EXISTS (
         SELECT 1 FROM image_keep_audit_members m
         WHERE m.audit_round_id = NEW.audit_round_id
           AND m.sampling_layer = 'primary'
           AND (abs(m.inclusion_probability - 1.0) > 1.0e-12
                OR abs(m.sampling_weight - 1.0) > 1.0e-9))
      THEN 'census' ELSE 'wilson_one_sided_95' END
  OR NEW.audit_round_id != substr(canonical_json_sha256(json_array(
      'image-keep-audit-v2', NEW.decision_build_id, NEW.round_number,
      NEW.population_manifest_sha256, NEW.primary_manifest_sha256,
      NEW.supplement_manifest_sha256)), 1, 32)
)
BEGIN SELECT RAISE(ABORT, 'keep audit population or selection integrity mismatch'); END;

DROP TRIGGER IF EXISTS freeze_keep_audit_status;
CREATE TRIGGER freeze_keep_audit_status
BEFORE UPDATE OF seal_status ON image_keep_audit_rounds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'keep audit status is immutable'); END;
CREATE TRIGGER freeze_keep_audit_integrity_status
BEFORE UPDATE OF integrity_status ON image_keep_audit_rounds
WHEN NOT (OLD.integrity_status = 'building' AND NEW.integrity_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'keep audit integrity status is immutable'); END;

DROP TRIGGER IF EXISTS freeze_audit_identity;
CREATE TRIGGER freeze_audit_identity BEFORE UPDATE OF audit_round_id, decision_build_id,
 round_number, random_seed, population_count, population_manifest_sha256, primary_count,
 supplement_count, primary_manifest_sha256, supplement_manifest_sha256, interval_method,
 sampling_algorithm_version, created_at_utc ON image_keep_audit_rounds
BEGIN SELECT RAISE(ABORT, 'keep audit identity is immutable'); END;

DROP TRIGGER IF EXISTS freeze_finalized_audit_members;
CREATE TRIGGER freeze_finalized_audit_members
BEFORE INSERT ON image_keep_audit_members
WHEN EXISTS (SELECT 1 FROM image_keep_audit_rounds r
             WHERE r.audit_round_id = NEW.audit_round_id
               AND (r.seal_status != 'building' OR r.integrity_status != 'building'))
BEGIN SELECT RAISE(ABORT, 'keep audit rows are sealed'); END;
CREATE TRIGGER freeze_finalized_audit_population
BEFORE INSERT ON image_keep_audit_population_members
WHEN EXISTS (SELECT 1 FROM image_keep_audit_rounds r
             WHERE r.audit_round_id = NEW.audit_round_id
               AND (r.seal_status != 'building' OR r.integrity_status != 'building'))
BEGIN SELECT RAISE(ABORT, 'keep audit population is sealed'); END;
CREATE TRIGGER immutable_audit_population_update
BEFORE UPDATE ON image_keep_audit_population_members
BEGIN SELECT RAISE(ABORT, 'keep audit population is immutable'); END;
CREATE TRIGGER immutable_audit_population_delete
BEFORE DELETE ON image_keep_audit_population_members
BEGIN SELECT RAISE(ABORT, 'keep audit population is immutable'); END;

-- 只有可信轮才允许封存评估；补充成员不能在 primary 为空时制造 failed。
CREATE TRIGGER validate_keep_audit_evaluation_round_trusted
BEFORE UPDATE OF seal_status ON image_keep_audit_evaluations
WHEN NEW.seal_status = 'finalized' AND NOT EXISTS (
  SELECT 1 FROM image_keep_audit_rounds r
  WHERE r.audit_round_id = NEW.audit_round_id
    AND r.seal_status = 'finalized' AND r.integrity_status = 'finalized'
    AND r.primary_count > 0
)
BEGIN SELECT RAISE(ABORT, 'keep audit evaluation requires trusted round'); END;

DROP TRIGGER IF EXISTS validate_keep_audit_round_sequence;
CREATE TRIGGER validate_keep_audit_round_sequence
BEFORE INSERT ON image_keep_audit_rounds
WHEN NEW.round_number > 3 OR EXISTS (
 SELECT 1 FROM image_keep_audit_rounds r
 JOIN image_decision_builds b ON b.decision_build_id = r.decision_build_id
 JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
 WHERE b.candidate_build_id = nb.candidate_build_id
   AND r.round_number = NEW.round_number
) OR (
 NEW.round_number = 1 AND EXISTS (
   SELECT 1 FROM image_keep_audit_rounds r
   JOIN image_decision_builds b ON b.decision_build_id = r.decision_build_id
   JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
   WHERE b.candidate_build_id = nb.candidate_build_id)
) OR (
 NEW.round_number > 1 AND NOT EXISTS (
   SELECT 1 FROM image_keep_audit_rounds prior
   JOIN image_decision_builds pb ON pb.decision_build_id = prior.decision_build_id
   JOIN image_decision_builds nb ON nb.decision_build_id = NEW.decision_build_id
   JOIN image_keep_audit_evaluations e ON e.audit_round_id = prior.audit_round_id
   WHERE pb.candidate_build_id = nb.candidate_build_id
     AND prior.round_number = NEW.round_number - 1
     AND prior.seal_status = 'finalized' AND prior.integrity_status = 'finalized'
     AND e.seal_status = 'finalized' AND e.evaluation_status = 'failed'
     AND prior.decision_build_id != NEW.decision_build_id
     AND pb.decision_manifest_sha256 != nb.decision_manifest_sha256
 ))
BEGIN SELECT RAISE(ABORT, 'keep audit round sequence or revised decision is invalid'); END;
"""


_SCHEMA_V22 = """
-- v22 将轮次 seed 绑定到候选构建所属 cleaning run 的协议基种子。v21 只验证
-- seed 为正，攻击者可用任意 seed 自洽地重算成员和 manifest；新约束在父行
-- 写入与最终封存两处独立验证，并在升级时降级不符合真实运行 seed 的旧轮。
DROP TRIGGER IF EXISTS require_keep_audit_building_insert;
CREATE TRIGGER require_keep_audit_building_insert
BEFORE INSERT ON image_keep_audit_rounds
WHEN NEW.seal_status != 'building' OR NEW.integrity_status != 'building'
 OR NEW.sampling_algorithm_version != 'image-keep-audit-sampling-v1'
 OR NOT EXISTS (
   SELECT 1 FROM image_decision_builds d
   JOIN image_candidate_builds b ON b.build_id = d.candidate_build_id
   JOIN cleaning_runs run ON run.run_id = b.run_id
   WHERE d.decision_build_id = NEW.decision_build_id
     AND d.seal_status = 'finalized' AND b.seal_status = 'finalized'
     AND run.random_seed > 0
     AND NEW.random_seed = run.random_seed + NEW.round_number - 1
 )
BEGIN SELECT RAISE(ABORT, 'keep audit requires protocol-derived seed'); END;

-- 先暂时移除状态冻结，逐行对照不可变 decision→candidate→run 谱系；只降级
-- seed 不匹配的历史 finalized 轮，不改写其 seed、成员或 manifest。
DROP TRIGGER IF EXISTS freeze_keep_audit_integrity_status;
UPDATE image_keep_audit_rounds
SET integrity_status = 'untrusted_legacy'
WHERE integrity_status = 'finalized' AND NOT EXISTS (
  SELECT 1 FROM image_decision_builds d
  JOIN image_candidate_builds b ON b.build_id = d.candidate_build_id
  JOIN cleaning_runs run ON run.run_id = b.run_id
  WHERE d.decision_build_id = image_keep_audit_rounds.decision_build_id
    AND d.seal_status = 'finalized' AND b.seal_status = 'finalized'
    AND run.random_seed > 0
    AND image_keep_audit_rounds.random_seed =
        run.random_seed + image_keep_audit_rounds.round_number - 1
);
CREATE TRIGGER freeze_keep_audit_integrity_status
BEFORE UPDATE OF integrity_status ON image_keep_audit_rounds
WHEN NOT (OLD.integrity_status = 'building' AND NEW.integrity_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'keep audit integrity status is immutable'); END;

CREATE TRIGGER validate_keep_audit_seed_on_seal
BEFORE UPDATE OF integrity_status ON image_keep_audit_rounds
WHEN NEW.integrity_status = 'finalized' AND NOT EXISTS (
  SELECT 1 FROM image_decision_builds d
  JOIN image_candidate_builds b ON b.build_id = d.candidate_build_id
  JOIN cleaning_runs run ON run.run_id = b.run_id
  WHERE d.decision_build_id = NEW.decision_build_id
    AND d.seal_status = 'finalized' AND b.seal_status = 'finalized'
    AND run.random_seed > 0
    AND NEW.random_seed = run.random_seed + NEW.round_number - 1
)
BEGIN SELECT RAISE(ABORT, 'keep audit seed differs from protocol run'); END;
"""

_SCHEMA_V23 = """
-- v23 将文本清洗正式契约收紧为“结构可用性 + 旅游相关性”双轴。商业
-- 属性不属于清洗金标，因此从原始标注和仲裁表中移除；结构无效时旅游轴
-- 统一迁移为 not_applicable，避免旧接口的 uncertain 被误计入一致性或模型。
DROP TRIGGER IF EXISTS prevent_text_post_annotation_update;
DROP TRIGGER IF EXISTS prevent_text_post_annotation_delete;
DROP TRIGGER IF EXISTS prevent_text_post_adjudication_update;
DROP TRIGGER IF EXISTS prevent_text_post_adjudication_delete;
DROP TRIGGER IF EXISTS reject_duplicate_annotation_slot;
DROP TRIGGER IF EXISTS reject_same_annotator_in_both_slots;
DROP TRIGGER IF EXISTS validate_double_label_adjudication_evidence;
DROP TRIGGER IF EXISTS validate_model_review_run_reference;
DROP INDEX IF EXISTS idx_text_annotations_post;
DROP INDEX IF EXISTS idx_text_adjudications_post;
DROP INDEX IF EXISTS idx_text_adjudications_model_run;

ALTER TABLE text_post_annotations RENAME TO text_post_annotations_v22;
CREATE TABLE text_post_annotations (
    annotation_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    sample_run_id TEXT REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    annotator_hash TEXT NOT NULL CHECK (length(annotator_hash) = 64),
    assignment_slot INTEGER CHECK (assignment_slot IS NULL OR assignment_slot IN (1, 2)),
    structure_label TEXT NOT NULL CHECK (
        structure_label IN ('usable', 'invalid', 'uncertain')
    ),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain', 'not_applicable')
    ),
    reason_codes_json TEXT NOT NULL,
    guide_version TEXT NOT NULL,
    annotated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (sample_run_id, source_post_id, annotator_hash, assignment_slot, annotated_at_utc),
    CHECK (
        (structure_label = 'invalid' AND tourism_label = 'not_applicable')
        OR
        (structure_label IN ('usable', 'uncertain')
         AND tourism_label IN ('related', 'unrelated', 'uncertain'))
    )
);
INSERT INTO text_post_annotations(
    annotation_id, import_id, sample_run_id, source_post_id, source_version,
    annotator_hash, assignment_slot, structure_label, tourism_label,
    reason_codes_json, guide_version, annotated_at_utc, created_at_utc
)
SELECT annotation_id, import_id, sample_run_id, source_post_id, source_version,
       annotator_hash, assignment_slot, structure_label,
       CASE WHEN structure_label = 'invalid' THEN 'not_applicable'
            ELSE tourism_label END,
       reason_codes_json, guide_version, annotated_at_utc, created_at_utc
FROM text_post_annotations_v22;
DROP TABLE text_post_annotations_v22;

ALTER TABLE text_post_adjudications RENAME TO text_post_adjudications_v22;
CREATE TABLE text_post_adjudications (
    adjudication_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES text_annotation_imports(import_id) ON DELETE RESTRICT,
    sample_run_id TEXT REFERENCES text_sampling_runs(sample_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    adjudicator_hash TEXT NOT NULL CHECK (length(adjudicator_hash) = 64),
    structure_label TEXT NOT NULL CHECK (
        structure_label IN ('usable', 'invalid', 'uncertain')
    ),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain', 'not_applicable')
    ),
    reason_codes_json TEXT NOT NULL,
    evidence_annotation_ids_json TEXT NOT NULL,
    decision_context TEXT NOT NULL CHECK (
        decision_context IN ('gold', 'model_review', 'manual_review')
    ),
    guide_version TEXT NOT NULL,
    adjudicated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    model_run_id TEXT,
    CHECK (
        (structure_label = 'invalid' AND tourism_label = 'not_applicable')
        OR
        (structure_label IN ('usable', 'uncertain')
         AND tourism_label IN ('related', 'unrelated', 'uncertain'))
    )
);
INSERT INTO text_post_adjudications(
    adjudication_id, import_id, sample_run_id, source_post_id, source_version,
    adjudicator_hash, structure_label, tourism_label, reason_codes_json,
    evidence_annotation_ids_json, decision_context, guide_version,
    adjudicated_at_utc, created_at_utc, model_run_id
)
SELECT adjudication_id, import_id, sample_run_id, source_post_id, source_version,
       adjudicator_hash, structure_label,
       CASE WHEN structure_label = 'invalid' THEN 'not_applicable'
            ELSE tourism_label END,
       reason_codes_json, evidence_annotation_ids_json, decision_context,
       guide_version, adjudicated_at_utc, created_at_utc, model_run_id
FROM text_post_adjudications_v22;
DROP TABLE text_post_adjudications_v22;

CREATE INDEX idx_text_annotations_post
    ON text_post_annotations(source_post_id, source_version, guide_version);
CREATE INDEX idx_text_adjudications_post
    ON text_post_adjudications(source_post_id, source_version, guide_version);
CREATE INDEX idx_text_adjudications_model_run
    ON text_post_adjudications(model_run_id, source_post_id, source_version);

CREATE TRIGGER prevent_text_post_annotation_update
BEFORE UPDATE ON text_post_annotations BEGIN
    SELECT RAISE(ABORT, 'text post annotations are append-only');
END;
CREATE TRIGGER prevent_text_post_annotation_delete
BEFORE DELETE ON text_post_annotations BEGIN
    SELECT RAISE(ABORT, 'text post annotations are append-only');
END;
CREATE TRIGGER prevent_text_post_adjudication_update
BEFORE UPDATE ON text_post_adjudications BEGIN
    SELECT RAISE(ABORT, 'text post adjudications are append-only');
END;
CREATE TRIGGER prevent_text_post_adjudication_delete
BEFORE DELETE ON text_post_adjudications BEGIN
    SELECT RAISE(ABORT, 'text post adjudications are append-only');
END;

CREATE TRIGGER reject_duplicate_annotation_slot
BEFORE INSERT ON text_post_annotations
WHEN NEW.sample_run_id IS NOT NULL
 AND NEW.assignment_slot IN (1, 2)
 AND EXISTS (
     SELECT 1 FROM text_post_annotations
     WHERE sample_run_id = NEW.sample_run_id
       AND source_post_id = NEW.source_post_id
       AND source_version = NEW.source_version
       AND assignment_slot = NEW.assignment_slot
 )
BEGIN
    SELECT RAISE(ABORT, 'annotation assignment slot already filled');
END;

CREATE TRIGGER reject_same_annotator_in_both_slots
BEFORE INSERT ON text_post_annotations
WHEN NEW.sample_run_id IS NOT NULL
 AND NEW.assignment_slot IN (1, 2)
 AND EXISTS (
     SELECT 1 FROM text_post_annotations
     WHERE sample_run_id = NEW.sample_run_id
       AND source_post_id = NEW.source_post_id
       AND source_version = NEW.source_version
       AND assignment_slot IN (1, 2)
       AND annotator_hash = NEW.annotator_hash
 )
BEGIN
    SELECT RAISE(ABORT, 'double-label annotators must differ');
END;

CREATE TRIGGER validate_double_label_adjudication_evidence
BEFORE INSERT ON text_post_adjudications
WHEN NEW.sample_run_id IS NOT NULL
 AND NEW.decision_context = 'gold'
 AND (
     EXISTS (
         SELECT 1 FROM text_sample_members
         WHERE sample_run_id = NEW.sample_run_id
           AND source_post_id = NEW.source_post_id
           AND source_version = NEW.source_version
           AND requires_double_label = 1
     )
     OR EXISTS (
         SELECT 1 FROM text_double_label_supplement_members
         WHERE sample_run_id = NEW.sample_run_id
           AND source_post_id = NEW.source_post_id
           AND source_version = NEW.source_version
     )
 )
 AND (
     json_valid(NEW.evidence_annotation_ids_json) = 0
     OR json_array_length(NEW.evidence_annotation_ids_json) != 2
     OR (
         SELECT COUNT(*)
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
         WHERE a.sample_run_id = NEW.sample_run_id
           AND a.source_post_id = NEW.source_post_id
           AND a.source_version = NEW.source_version
           AND a.guide_version = NEW.guide_version
           AND a.assignment_slot IN (1, 2)
     ) != 2
     OR (
         SELECT COUNT(DISTINCT a.assignment_slot)
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
     ) != 2
     OR (
         SELECT COUNT(DISTINCT a.annotator_hash)
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
     ) != 2
     OR EXISTS (
         SELECT 1
         FROM text_post_annotations AS a
         JOIN json_each(NEW.evidence_annotation_ids_json) AS evidence
           ON evidence.value = a.annotation_id
         WHERE a.annotator_hash = NEW.adjudicator_hash
     )
 )
BEGIN
    SELECT RAISE(ABORT, 'double-label adjudication evidence is invalid');
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
"""


_SCHEMA_V24 = """
-- v24 将帖子决定、分析去重、文本保留集审计和分析发布拆成独立的
-- 版本化对象。候选决定先供审计冻结人口；最终决定必须引用通过的审计，
-- 从结构上消除“先发布、再用发布结果证明审计”的循环依赖。
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

CREATE TABLE post_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_build_id TEXT NOT NULL
        REFERENCES post_decision_builds(decision_build_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL
        REFERENCES source_post_inventory(source_post_id) ON DELETE RESTRICT,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    structure_label TEXT NOT NULL CHECK (
        structure_label IN ('usable', 'invalid', 'uncertain')
    ),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain', 'not_applicable')
    ),
    decision_action TEXT NOT NULL CHECK (decision_action IN ('keep', 'review', 'exclude')),
    reason_code TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (
        provenance IN ('deterministic_invalid', 'human_adjudication',
                       'model_low_risk', 'model_review_candidate',
                       'insufficient_evidence', 'evidence_conflict')
    ),
    model_run_id TEXT REFERENCES text_model_runs(model_run_id) ON DELETE RESTRICT,
    evidence_manifest_sha256 TEXT NOT NULL CHECK (length(evidence_manifest_sha256) = 64),
    decision_sha256 TEXT NOT NULL CHECK (length(decision_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE (decision_build_id, source_post_id, source_version),
    CHECK (
        (structure_label = 'invalid' AND tourism_label = 'not_applicable')
        OR
        (structure_label IN ('usable', 'uncertain')
         AND tourism_label IN ('related', 'unrelated', 'uncertain'))
    ),
    CHECK (
        (provenance = 'deterministic_invalid'
         AND structure_label = 'invalid' AND tourism_label = 'not_applicable'
         AND decision_action = 'exclude' AND model_run_id IS NULL)
        OR
        (provenance = 'human_adjudication'
         AND structure_label = 'usable'
         AND tourism_label IN ('related', 'unrelated')
         AND decision_action = CASE tourism_label WHEN 'related' THEN 'keep' ELSE 'exclude' END
         AND model_run_id IS NULL)
        OR
        (provenance = 'model_low_risk'
         AND structure_label = 'usable' AND tourism_label = 'related'
         AND decision_action IN ('keep', 'review') AND model_run_id IS NOT NULL)
        OR
        (provenance = 'model_review_candidate'
         AND structure_label = 'usable' AND tourism_label = 'uncertain'
         AND decision_action = 'review' AND model_run_id IS NOT NULL)
        OR
        (provenance IN ('insufficient_evidence', 'evidence_conflict')
         AND decision_action = 'review' AND model_run_id IS NULL)
    )
);

-- 多态证据链接由下方 trigger 按 evidence_kind 回查真实父表；不能只放一个
-- 无法验证的 JSON 数组，也不能由“最新一条”记录静默替换人工证据。
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

CREATE TABLE text_keep_audit_population_members (
    audit_round_id TEXT NOT NULL
        REFERENCES text_keep_audit_rounds(audit_round_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    platform_key TEXT NOT NULL,
    PRIMARY KEY (audit_round_id, source_post_id, source_version)
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

CREATE TABLE text_keep_audit_annotations (
    audit_annotation_id TEXT PRIMARY KEY,
    audit_round_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    annotator_hash TEXT NOT NULL CHECK (length(annotator_hash) = 64),
    guide_version TEXT NOT NULL,
    structure_label TEXT NOT NULL CHECK (
        structure_label IN ('usable', 'invalid', 'uncertain')
    ),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain', 'not_applicable')
    ),
    reason_codes_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64),
    annotated_at_utc TEXT NOT NULL,
    UNIQUE (audit_round_id, source_post_id, source_version),
    FOREIGN KEY (audit_round_id, source_post_id, source_version)
        REFERENCES text_keep_audit_members(
            audit_round_id, source_post_id, source_version
        ) ON DELETE RESTRICT,
    CHECK (
        (structure_label = 'invalid' AND tourism_label = 'not_applicable')
        OR
        (structure_label IN ('usable', 'uncertain')
         AND tourism_label IN ('related', 'unrelated', 'uncertain'))
    )
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

CREATE TABLE text_keep_audit_evaluation_evidence_links (
    audit_evaluation_id TEXT NOT NULL
        REFERENCES text_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    audit_annotation_id TEXT NOT NULL
        REFERENCES text_keep_audit_annotations(audit_annotation_id) ON DELETE RESTRICT,
    PRIMARY KEY (audit_evaluation_id, audit_annotation_id)
);

-- 平台切片仅是总体审计的分层描述，不单独决定发布是否通过。切片随评估一起
-- 封存，SQLite 在封存时从冻结人口、样本和人工证据重算全部计数与比例。
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

-- 发布父表只保存显式版本引用和可重算计数；formal/smoke 是不可变身份，
-- smoke 即使所有合成夹具通过，也不能推动 cleaning_runs 进入 accepted。
CREATE TABLE analysis_release_builds (
    release_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    release_mode TEXT NOT NULL CHECK (release_mode IN ('formal', 'smoke')),
    post_decision_build_id TEXT NOT NULL
        REFERENCES post_decision_builds(decision_build_id) ON DELETE RESTRICT,
    text_dedup_build_id TEXT NOT NULL
        REFERENCES text_dedup_builds(dedup_build_id) ON DELETE RESTRICT,
    text_keep_audit_evaluation_id TEXT NOT NULL
        REFERENCES text_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    image_decision_build_id TEXT NOT NULL
        REFERENCES image_decision_builds(decision_build_id) ON DELETE RESTRICT,
    image_keep_audit_evaluation_id TEXT NOT NULL
        REFERENCES image_keep_audit_evaluations(audit_evaluation_id) ON DELETE RESTRICT,
    protocol_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 24),
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    code_version TEXT NOT NULL,
    request_manifest_sha256 TEXT NOT NULL CHECK (length(request_manifest_sha256) = 64),
    posts_eligible_count INTEGER NOT NULL CHECK (posts_eligible_count >= 0),
    posts_deduplicated_count INTEGER NOT NULL CHECK (posts_deduplicated_count >= 0),
    images_eligible_count INTEGER NOT NULL CHECK (images_eligible_count >= 0),
    images_evidence_only_count INTEGER NOT NULL CHECK (images_evidence_only_count >= 0),
    release_manifest_sha256 TEXT NOT NULL CHECK (length(release_manifest_sha256) = 64),
    seal_status TEXT NOT NULL CHECK (
        seal_status IN ('building', 'finalized', 'accepted')
    ),
    created_at_utc TEXT NOT NULL,
    finalized_at_utc TEXT,
    accepted_at_utc TEXT,
    UNIQUE (run_id, request_manifest_sha256),
    CHECK (
        (seal_status = 'building' AND finalized_at_utc IS NULL AND accepted_at_utc IS NULL)
        OR
        (seal_status = 'finalized' AND finalized_at_utc IS NOT NULL AND accepted_at_utc IS NULL)
        OR
        (seal_status = 'accepted' AND finalized_at_utc IS NOT NULL AND accepted_at_utc IS NOT NULL)
    )
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

CREATE TABLE analysis_images_eligible (
    release_id TEXT NOT NULL REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    decision_id TEXT NOT NULL REFERENCES image_decisions(decision_id) ON DELETE RESTRICT,
    fingerprint_id TEXT NOT NULL REFERENCES image_fingerprints(fingerprint_id) ON DELETE RESTRICT,
    manifest_row_id TEXT NOT NULL REFERENCES image_manifest_rows(manifest_row_id) ON DELETE RESTRICT,
    source_image_id INTEGER NOT NULL,
    source_image_version INTEGER NOT NULL CHECK (source_image_version > 0),
    source_post_id INTEGER NOT NULL,
    source_post_version INTEGER NOT NULL CHECK (source_post_version > 0),
    PRIMARY KEY (release_id, manifest_row_id),
    UNIQUE (release_id, fingerprint_id)
);

CREATE TABLE analysis_images_evidence_only (
    release_id TEXT NOT NULL REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    manifest_row_id TEXT NOT NULL REFERENCES image_manifest_rows(manifest_row_id) ON DELETE RESTRICT,
    role_decision_id TEXT NOT NULL REFERENCES image_role_results(role_decision_id) ON DELETE RESTRICT,
    source_image_id INTEGER NOT NULL,
    source_image_version INTEGER NOT NULL CHECK (source_image_version > 0),
    source_post_id INTEGER NOT NULL,
    source_post_version INTEGER NOT NULL CHECK (source_post_version > 0),
    PRIMARY KEY (release_id, manifest_row_id),
    UNIQUE (release_id, role_decision_id)
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

CREATE TABLE analysis_release_manifests (
    release_id TEXT NOT NULL REFERENCES analysis_release_builds(release_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES cleaning_runs(run_id) ON DELETE RESTRICT,
    manifest_kind TEXT NOT NULL CHECK (
        manifest_kind IN ('release', 'posts_eligible', 'posts_deduplicated',
                          'images_eligible', 'images_evidence_only', 'artifact')
    ),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    artifact_relative_path TEXT,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (release_id, manifest_kind)
);

DROP VIEW IF EXISTS text_ready;
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

CREATE INDEX idx_post_decisions_build_action
    ON post_decisions(decision_build_id, decision_action, source_post_id);
CREATE INDEX idx_text_dedup_members_cluster
    ON text_dedup_members(dedup_build_id, cluster_id, is_representative);
CREATE INDEX idx_text_keep_audit_population_platform
    ON text_keep_audit_population_members(audit_round_id, platform_key);
CREATE INDEX idx_analysis_release_run_status
    ON analysis_release_builds(run_id, release_mode, seal_status);

-- 帖子决定父对象只能从 building 封存一次；final 构建必须绑定同一运行中
-- 已通过的 candidate 保留集审计，发布因此不能直接消费候选决定。
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

CREATE TRIGGER prevent_post_decision_build_identity_update
BEFORE UPDATE OF decision_build_id, run_id, source_snapshot_id, build_kind,
                 source_candidate_decision_build_id, text_keep_audit_evaluation_id,
                 decision_version, guide_version, rules_sha256,
                 input_manifest_sha256, expected_post_count, keep_count,
                 review_count, exclude_count, decision_manifest_sha256,
                 created_at_utc
ON post_decision_builds
BEGIN SELECT RAISE(ABORT, 'post decision build identity is immutable'); END;
CREATE TRIGGER prevent_post_decision_build_delete
BEFORE DELETE ON post_decision_builds
BEGIN SELECT RAISE(ABORT, 'post decision builds are immutable'); END;
CREATE TRIGGER prevent_post_decision_status_transition
BEFORE UPDATE OF seal_status ON post_decision_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'post decision build status is immutable'); END;

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
            AND a.structure_label = d.structure_label
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
            AND source.structure_label = d.structure_label
            AND source.tourism_label = d.tourism_label
            AND source.provenance = d.provenance
            AND source.model_run_id IS d.model_run_id
        )
    )
 ))
)
BEGIN SELECT RAISE(ABORT, 'post decision rows or evidence are incomplete'); END;

CREATE TRIGGER prevent_post_decision_insert_after_seal
BEFORE INSERT ON post_decisions
WHEN EXISTS (SELECT 1 FROM post_decision_builds b
             WHERE b.decision_build_id = NEW.decision_build_id
               AND b.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'post decision build is sealed'); END;
CREATE TRIGGER prevent_post_decision_update
BEFORE UPDATE ON post_decisions
BEGIN SELECT RAISE(ABORT, 'post decisions are immutable'); END;
CREATE TRIGGER prevent_post_decision_delete
BEFORE DELETE ON post_decisions
BEGIN SELECT RAISE(ABORT, 'post decisions are immutable'); END;
CREATE TRIGGER prevent_post_decision_evidence_update
BEFORE UPDATE ON post_decision_evidence_links
BEGIN SELECT RAISE(ABORT, 'post decision evidence is immutable'); END;
CREATE TRIGGER prevent_post_decision_evidence_delete
BEFORE DELETE ON post_decision_evidence_links
BEGIN SELECT RAISE(ABORT, 'post decision evidence is immutable'); END;

-- 分析去重只接受精确同一边或人工 duplicate 仲裁边；簇和成员必须覆盖
-- final 决定中的全部 keep 帖子，且每簇恰有一个冻结代表项。
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

CREATE TRIGGER validate_text_dedup_cluster_insert
BEFORE INSERT ON text_dedup_clusters
WHEN NOT EXISTS (SELECT 1 FROM text_dedup_builds b
                 WHERE b.dedup_build_id = NEW.dedup_build_id
                   AND b.seal_status = 'building')
BEGIN SELECT RAISE(ABORT, 'text dedup build is sealed'); END;
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

CREATE TRIGGER prevent_text_dedup_build_identity_update
BEFORE UPDATE OF dedup_build_id, run_id, post_decision_build_id,
                 candidate_build_id, dedup_version, selection_strategy,
                 expected_eligible_count, edge_count, cluster_count,
                 member_count, representative_count, input_manifest_sha256,
                 member_manifest_sha256, created_at_utc
ON text_dedup_builds
BEGIN SELECT RAISE(ABORT, 'text dedup build identity is immutable'); END;
CREATE TRIGGER prevent_text_dedup_status_transition
BEFORE UPDATE OF seal_status ON text_dedup_builds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'text dedup build status is immutable'); END;
CREATE TRIGGER prevent_text_dedup_build_delete
BEFORE DELETE ON text_dedup_builds
BEGIN SELECT RAISE(ABORT, 'text dedup builds are immutable'); END;
CREATE TRIGGER prevent_text_dedup_edge_update BEFORE UPDATE ON text_dedup_edges
BEGIN SELECT RAISE(ABORT, 'text dedup edges are immutable'); END;
CREATE TRIGGER prevent_text_dedup_edge_delete BEFORE DELETE ON text_dedup_edges
BEGIN SELECT RAISE(ABORT, 'text dedup edges are immutable'); END;
CREATE TRIGGER prevent_text_dedup_cluster_update BEFORE UPDATE ON text_dedup_clusters
BEGIN SELECT RAISE(ABORT, 'text dedup clusters are immutable'); END;
CREATE TRIGGER prevent_text_dedup_cluster_delete BEFORE DELETE ON text_dedup_clusters
BEGIN SELECT RAISE(ABORT, 'text dedup clusters are immutable'); END;
CREATE TRIGGER prevent_text_dedup_member_update BEFORE UPDATE ON text_dedup_members
BEGIN SELECT RAISE(ABORT, 'text dedup members are immutable'); END;
CREATE TRIGGER prevent_text_dedup_member_delete BEFORE DELETE ON text_dedup_members
BEGIN SELECT RAISE(ABORT, 'text dedup members are immutable'); END;

-- 文本保留集人口只能来自 candidate 构建的 keep 候选；封存时由 SQLite
-- 对照决定表重算整个人口和抽样规模，避免调用者只上报一个“已完成”数字。
CREATE TRIGGER require_text_keep_audit_building_insert
BEFORE INSERT ON text_keep_audit_rounds
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
  SELECT 1 FROM post_decision_builds b
  WHERE b.decision_build_id = NEW.candidate_decision_build_id
    AND b.run_id = NEW.run_id AND b.build_kind = 'candidate'
    AND b.seal_status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'text keep audit requires finalized candidate decisions'); END;

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

CREATE TRIGGER prevent_text_keep_audit_identity_update
BEFORE UPDATE OF audit_round_id, run_id, candidate_decision_build_id,
                 audit_mode, round_number, random_seed, sampling_method,
                 estimator, population_count, population_manifest_sha256,
                 sample_count, sample_manifest_sha256, created_at_utc
ON text_keep_audit_rounds
BEGIN SELECT RAISE(ABORT, 'text keep audit identity is immutable'); END;
CREATE TRIGGER prevent_text_keep_audit_status_transition
BEFORE UPDATE OF seal_status ON text_keep_audit_rounds
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'text keep audit status is immutable'); END;
CREATE TRIGGER prevent_text_keep_audit_round_delete
BEFORE DELETE ON text_keep_audit_rounds
BEGIN SELECT RAISE(ABORT, 'text keep audit rounds are immutable'); END;
CREATE TRIGGER prevent_text_keep_population_update
BEFORE UPDATE ON text_keep_audit_population_members
BEGIN SELECT RAISE(ABORT, 'text keep audit population is immutable'); END;
CREATE TRIGGER prevent_text_keep_population_delete
BEFORE DELETE ON text_keep_audit_population_members
BEGIN SELECT RAISE(ABORT, 'text keep audit population is immutable'); END;
CREATE TRIGGER prevent_text_keep_member_update BEFORE UPDATE ON text_keep_audit_members
BEGIN SELECT RAISE(ABORT, 'text keep audit members are immutable'); END;
CREATE TRIGGER prevent_text_keep_member_delete BEFORE DELETE ON text_keep_audit_members
BEGIN SELECT RAISE(ABORT, 'text keep audit members are immutable'); END;

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
CREATE TRIGGER prevent_text_keep_annotation_update
BEFORE UPDATE ON text_keep_audit_annotations
BEGIN SELECT RAISE(ABORT, 'text keep audit annotations are append-only'); END;
CREATE TRIGGER prevent_text_keep_annotation_delete
BEFORE DELETE ON text_keep_audit_annotations
BEGIN SELECT RAISE(ABORT, 'text keep audit annotations are append-only'); END;

CREATE TRIGGER require_text_keep_evaluation_building_insert
BEFORE INSERT ON text_keep_audit_evaluations
WHEN NEW.seal_status != 'building' OR NOT EXISTS (
  SELECT 1 FROM text_keep_audit_rounds r
  WHERE r.audit_round_id = NEW.audit_round_id AND r.seal_status = 'finalized'
)
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation requires a sealed sample'); END;

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

-- event 由结构无效、旅游不相关或任一 uncertain 直接重算；point estimate 和
-- Wilson 上限也由已注册的确定性函数重算，调用方无法自报 passed。
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
      AND (a.structure_label != 'usable' OR a.tourism_label != 'related')
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
            AND (a.structure_label != 'usable' OR a.tourism_label != 'related')
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

CREATE TRIGGER prevent_text_keep_evaluation_identity_update
BEFORE UPDATE OF audit_evaluation_id, audit_round_id, completed_count,
                 event_count, event_point_estimate, one_sided_upper,
                 evaluation_status, reason_code, evidence_manifest_sha256,
                 created_at_utc
ON text_keep_audit_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation identity is immutable'); END;
CREATE TRIGGER prevent_text_keep_evaluation_status_transition
BEFORE UPDATE OF seal_status ON text_keep_audit_evaluations
WHEN NOT (OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation status is immutable'); END;
CREATE TRIGGER prevent_text_keep_evaluation_delete
BEFORE DELETE ON text_keep_audit_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluations are immutable'); END;
CREATE TRIGGER prevent_text_keep_evaluation_link_update
BEFORE UPDATE ON text_keep_audit_evaluation_evidence_links
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation evidence is immutable'); END;
CREATE TRIGGER prevent_text_keep_evaluation_link_delete
BEFORE DELETE ON text_keep_audit_evaluation_evidence_links
BEGIN SELECT RAISE(ABORT, 'text keep audit evaluation evidence is immutable'); END;
CREATE TRIGGER prevent_text_keep_platform_evaluation_update
BEFORE UPDATE ON text_keep_audit_platform_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit platform slices are immutable'); END;
CREATE TRIGGER prevent_text_keep_platform_evaluation_delete
BEFORE DELETE ON text_keep_audit_platform_evaluations
BEGIN SELECT RAISE(ABORT, 'text keep audit platform slices are immutable'); END;

-- 四类发布成员的 INSERT 均要求父构建仍为 building，并逐行回查选定证据。
-- 这既阻止跨 run 拼接，也保证每张 content 图片仍保留自己的技术决定。
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
)
 OR NOT EXISTS (
    SELECT 1 FROM post_decision_builds p
    WHERE p.decision_build_id = NEW.post_decision_build_id
      AND p.run_id = NEW.run_id AND p.source_snapshot_id = NEW.source_snapshot_id
      AND p.build_kind = 'final' AND p.seal_status = 'finalized'
 )
BEGIN SELECT RAISE(ABORT, 'analysis release run identity is invalid'); END;

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

CREATE TRIGGER validate_analysis_image_eligible_insert
BEFORE INSERT ON analysis_images_eligible
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_release_builds r
  JOIN image_decision_builds decision_build
    ON decision_build.decision_build_id = r.image_decision_build_id
  JOIN image_decisions d
    ON d.decision_build_id = r.image_decision_build_id
   AND d.decision_id = NEW.decision_id AND d.decision_action = 'keep'
  JOIN image_exact_clusters cluster
    ON cluster.build_id = decision_build.candidate_build_id
   AND cluster.representative_fingerprint_id = d.fingerprint_id
  JOIN image_exact_cluster_members member
    ON member.build_id = cluster.build_id AND member.cluster_id = cluster.cluster_id
   AND member.fingerprint_id = NEW.fingerprint_id
  JOIN image_fingerprints f
    ON f.fingerprint_id = member.fingerprint_id
  JOIN image_manifest_rows row
    ON row.manifest_row_id = f.manifest_row_id
   AND row.manifest_row_id = NEW.manifest_row_id
   AND row.relation_role = 'content' AND row.validation_status = 'accepted'
   AND row.source_image_id = NEW.source_image_id
   AND row.source_post_id = NEW.source_post_id
  JOIN source_image_observations image_observation
    ON image_observation.snapshot_id = r.source_snapshot_id
   AND image_observation.source_image_id = row.source_image_id
   AND image_observation.source_version = NEW.source_image_version
   AND image_observation.change_kind != 'missing'
  JOIN source_post_observations post_observation
    ON post_observation.snapshot_id = r.source_snapshot_id
   AND post_observation.source_post_id = row.source_post_id
   AND post_observation.source_version = NEW.source_post_version
   AND post_observation.change_kind != 'missing'
  JOIN analysis_posts_eligible p
    ON p.release_id = r.release_id AND p.source_post_id = row.source_post_id
   AND p.source_version = NEW.source_post_version
  WHERE r.release_id = NEW.release_id AND r.run_id = NEW.run_id
    AND r.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'analysis eligible image lacks eligible parent or keep decision'); END;

CREATE TRIGGER validate_analysis_image_evidence_insert
BEFORE INSERT ON analysis_images_evidence_only
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_release_builds r
  JOIN image_decision_builds decisions
    ON decisions.decision_build_id = r.image_decision_build_id
  JOIN image_candidate_builds candidates
    ON candidates.build_id = decisions.candidate_build_id
  JOIN image_manifest_rows row
    ON row.manifest_id = candidates.manifest_id
   AND row.manifest_row_id = NEW.manifest_row_id
   AND row.relation_role = 'page' AND row.validation_status = 'accepted'
   AND row.source_image_id = NEW.source_image_id
   AND row.source_post_id = NEW.source_post_id
  JOIN image_role_results role
    ON role.role_decision_id = NEW.role_decision_id
   AND role.manifest_row_id = row.manifest_row_id
   AND role.relation_role = 'page' AND role.handling_action = 'evidence_only'
  JOIN source_image_observations image_observation
    ON image_observation.snapshot_id = r.source_snapshot_id
   AND image_observation.source_image_id = row.source_image_id
   AND image_observation.source_version = NEW.source_image_version
   AND image_observation.change_kind != 'missing'
  JOIN source_post_observations post_observation
    ON post_observation.snapshot_id = r.source_snapshot_id
   AND post_observation.source_post_id = row.source_post_id
   AND post_observation.source_version = NEW.source_post_version
   AND post_observation.change_kind != 'missing'
  WHERE r.release_id = NEW.release_id AND r.run_id = NEW.run_id
    AND r.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'analysis evidence-only image is not a page relation'); END;

CREATE TRIGGER validate_analysis_release_report_insert
BEFORE INSERT ON analysis_release_reports
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_release_builds r
  WHERE r.release_id = NEW.release_id AND r.run_id = NEW.run_id
    AND r.seal_status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'analysis release report parent is sealed'); END;
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

-- finalized 只说明包内证据、集合与计数可重建；accepted 还要求显式先将
-- cleaning_runs 置为 accepted。两端 trigger 都重复关键门禁，不能靠单边写入绕过。
CREATE TRIGGER validate_analysis_release_finalize
BEFORE UPDATE OF seal_status ON analysis_release_builds
WHEN NEW.seal_status = 'finalized' AND (
    NOT EXISTS (
      SELECT 1
      FROM post_decision_builds p
      JOIN text_dedup_builds d ON d.dedup_build_id = NEW.text_dedup_build_id
      JOIN text_keep_audit_evaluations te
        ON te.audit_evaluation_id = NEW.text_keep_audit_evaluation_id
      JOIN text_keep_audit_rounds tr ON tr.audit_round_id = te.audit_round_id
      JOIN image_decision_builds i ON i.decision_build_id = NEW.image_decision_build_id
      JOIN image_candidate_builds ic ON ic.build_id = i.candidate_build_id
      JOIN image_manifest_imports im ON im.manifest_id = ic.manifest_id
      JOIN image_keep_audit_evaluations ie
        ON ie.audit_evaluation_id = NEW.image_keep_audit_evaluation_id
      JOIN image_keep_audit_rounds ir ON ir.audit_round_id = ie.audit_round_id
      WHERE p.decision_build_id = NEW.post_decision_build_id
        AND p.run_id = NEW.run_id AND p.source_snapshot_id = NEW.source_snapshot_id
        AND p.build_kind = 'final' AND p.seal_status = 'finalized'
        AND p.text_keep_audit_evaluation_id = te.audit_evaluation_id
        AND d.run_id = NEW.run_id AND d.post_decision_build_id = p.decision_build_id
        AND d.seal_status = 'finalized'
        AND tr.run_id = NEW.run_id AND tr.seal_status = 'finalized'
        AND te.seal_status = 'finalized' AND te.evaluation_status = 'passed'
        AND tr.audit_mode = NEW.release_mode
        AND ic.run_id = NEW.run_id AND im.source_snapshot_id = NEW.source_snapshot_id
        AND i.seal_status = 'finalized'
        AND ir.decision_build_id = i.decision_build_id
        AND ir.seal_status = 'finalized' AND ir.integrity_status = 'finalized'
        AND ie.audit_round_id = ir.audit_round_id
        AND ie.seal_status = 'finalized' AND ie.evaluation_status = 'passed'
    )
 OR (SELECT COUNT(*) FROM analysis_posts_eligible p
     WHERE p.release_id = NEW.release_id) != NEW.posts_eligible_count
 OR (SELECT COUNT(*) FROM post_decisions d
     WHERE d.decision_build_id = NEW.post_decision_build_id
       AND d.decision_action = 'keep') != NEW.posts_eligible_count
 OR EXISTS (
    SELECT 1 FROM post_decisions d
    WHERE d.decision_build_id = NEW.post_decision_build_id
      AND d.decision_action = 'keep'
      AND NOT EXISTS (
        SELECT 1 FROM analysis_posts_eligible p
        WHERE p.release_id = NEW.release_id AND p.decision_id = d.decision_id
      )
 )
 OR (SELECT COUNT(*) FROM analysis_posts_deduplicated p
     WHERE p.release_id = NEW.release_id) != NEW.posts_deduplicated_count
 OR (SELECT COUNT(*) FROM text_dedup_clusters c
     WHERE c.dedup_build_id = NEW.text_dedup_build_id)
       != NEW.posts_deduplicated_count
 OR EXISTS (
    SELECT 1 FROM text_dedup_clusters c
    WHERE c.dedup_build_id = NEW.text_dedup_build_id
      AND NOT EXISTS (
        SELECT 1 FROM analysis_posts_deduplicated p
        WHERE p.release_id = NEW.release_id
          AND p.dedup_build_id = c.dedup_build_id AND p.cluster_id = c.cluster_id
      )
 )
 OR (SELECT COUNT(*) FROM analysis_images_eligible i
     WHERE i.release_id = NEW.release_id) != NEW.images_eligible_count
 OR (SELECT COUNT(*)
     FROM image_decision_builds decision_build
     JOIN image_decisions d
       ON d.decision_build_id = decision_build.decision_build_id
     JOIN image_exact_clusters cluster
       ON cluster.build_id = decision_build.candidate_build_id
      AND cluster.representative_fingerprint_id = d.fingerprint_id
     JOIN image_exact_cluster_members member
       ON member.build_id = cluster.build_id AND member.cluster_id = cluster.cluster_id
     JOIN image_fingerprints f ON f.fingerprint_id = member.fingerprint_id
     JOIN image_manifest_rows row ON row.manifest_row_id = f.manifest_row_id
     JOIN analysis_posts_eligible p
       ON p.release_id = NEW.release_id AND p.source_post_id = row.source_post_id
     WHERE decision_build.decision_build_id = NEW.image_decision_build_id
       AND d.decision_action = 'keep' AND row.relation_role = 'content'
       AND row.validation_status = 'accepted') != NEW.images_eligible_count
 OR EXISTS (
    SELECT 1
    FROM image_decision_builds decision_build
    JOIN image_decisions d
      ON d.decision_build_id = decision_build.decision_build_id
    JOIN image_exact_clusters cluster
      ON cluster.build_id = decision_build.candidate_build_id
     AND cluster.representative_fingerprint_id = d.fingerprint_id
    JOIN image_exact_cluster_members member
      ON member.build_id = cluster.build_id AND member.cluster_id = cluster.cluster_id
    JOIN image_fingerprints f ON f.fingerprint_id = member.fingerprint_id
    JOIN image_manifest_rows row ON row.manifest_row_id = f.manifest_row_id
    JOIN analysis_posts_eligible p
      ON p.release_id = NEW.release_id AND p.source_post_id = row.source_post_id
    WHERE decision_build.decision_build_id = NEW.image_decision_build_id
      AND d.decision_action = 'keep' AND row.relation_role = 'content'
      AND row.validation_status = 'accepted'
      AND NOT EXISTS (
        SELECT 1 FROM analysis_images_eligible out
        WHERE out.release_id = NEW.release_id
          AND out.manifest_row_id = row.manifest_row_id
          AND out.decision_id = d.decision_id
      )
 )
 OR (SELECT COUNT(*) FROM analysis_images_evidence_only i
     WHERE i.release_id = NEW.release_id) != NEW.images_evidence_only_count
 OR (SELECT COUNT(*)
     FROM image_decision_builds decisions
     JOIN image_candidate_builds candidates
       ON candidates.build_id = decisions.candidate_build_id
     JOIN image_manifest_rows row ON row.manifest_id = candidates.manifest_id
     WHERE decisions.decision_build_id = NEW.image_decision_build_id
       AND row.relation_role = 'page' AND row.validation_status = 'accepted')
       != NEW.images_evidence_only_count
 OR EXISTS (
    SELECT 1
    FROM image_decision_builds decisions
    JOIN image_candidate_builds candidates
      ON candidates.build_id = decisions.candidate_build_id
    JOIN image_manifest_rows row ON row.manifest_id = candidates.manifest_id
    WHERE decisions.decision_build_id = NEW.image_decision_build_id
      AND row.relation_role = 'page' AND row.validation_status = 'accepted'
      AND NOT EXISTS (
        SELECT 1 FROM analysis_images_evidence_only out
        WHERE out.release_id = NEW.release_id AND out.manifest_row_id = row.manifest_row_id
      )
 )
 OR NOT EXISTS (SELECT 1 FROM analysis_release_reports x
                WHERE x.release_id = NEW.release_id AND x.report_kind = 'quality_summary')
 OR NOT EXISTS (SELECT 1 FROM analysis_release_reports x
                WHERE x.release_id = NEW.release_id AND x.report_kind = 'lineage')
 OR (SELECT COUNT(*) FROM analysis_release_manifests x
     WHERE x.release_id = NEW.release_id
       AND x.manifest_kind IN ('release', 'posts_eligible', 'posts_deduplicated',
                               'images_eligible', 'images_evidence_only')) != 5
)
BEGIN SELECT RAISE(ABORT, 'analysis release evidence or member counts are invalid'); END;

CREATE TRIGGER prevent_analysis_release_identity_update
BEFORE UPDATE OF release_id, run_id, source_snapshot_id, release_mode,
                 post_decision_build_id, text_dedup_build_id,
                 text_keep_audit_evaluation_id, image_decision_build_id,
                 image_keep_audit_evaluation_id, protocol_version,
                 schema_version, config_sha256, code_version,
                 request_manifest_sha256, posts_eligible_count,
                 posts_deduplicated_count, images_eligible_count,
                 images_evidence_only_count, release_manifest_sha256,
                 created_at_utc
ON analysis_release_builds
BEGIN SELECT RAISE(ABORT, 'analysis release identity is immutable'); END;
CREATE TRIGGER prevent_analysis_release_status_transition
BEFORE UPDATE OF seal_status ON analysis_release_builds
WHEN NOT ((OLD.seal_status = 'building' AND NEW.seal_status = 'finalized')
       OR (OLD.seal_status = 'finalized' AND NEW.seal_status = 'accepted'))
BEGIN SELECT RAISE(ABORT, 'analysis release status transition is invalid'); END;
CREATE TRIGGER prevent_analysis_release_delete
BEFORE DELETE ON analysis_release_builds
BEGIN SELECT RAISE(ABORT, 'analysis releases are immutable'); END;

CREATE TRIGGER validate_analysis_release_accept
BEFORE UPDATE OF seal_status ON analysis_release_builds
WHEN NEW.seal_status = 'accepted' AND (
    NEW.release_mode != 'formal'
 OR NOT EXISTS (SELECT 1 FROM cleaning_runs r
                WHERE r.run_id = NEW.run_id AND r.status = 'accepted')
 OR EXISTS (SELECT 1 FROM post_decisions d
            WHERE d.decision_build_id = NEW.post_decision_build_id
              AND d.decision_action = 'review')
 OR EXISTS (SELECT 1 FROM image_decisions d
            WHERE d.decision_build_id = NEW.image_decision_build_id
              AND d.decision_action = 'review')
 OR EXISTS (SELECT 1 FROM stage_tasks t
            WHERE t.run_id = NEW.run_id AND t.required = 1
              AND t.status IN ('pending', 'running', 'failed', 'blocked'))
 OR NOT EXISTS (
    SELECT 1 FROM text_keep_audit_evaluations te
    JOIN text_keep_audit_rounds tr ON tr.audit_round_id = te.audit_round_id
    JOIN image_keep_audit_evaluations ie
      ON ie.audit_evaluation_id = NEW.image_keep_audit_evaluation_id
    JOIN image_keep_audit_rounds ir ON ir.audit_round_id = ie.audit_round_id
    WHERE te.audit_evaluation_id = NEW.text_keep_audit_evaluation_id
      AND te.seal_status = 'finalized' AND te.evaluation_status = 'passed'
      AND tr.audit_mode = 'formal' AND tr.seal_status = 'finalized'
      AND ie.seal_status = 'finalized' AND ie.evaluation_status = 'passed'
      AND ir.integrity_status = 'finalized' AND ir.seal_status = 'finalized'
 )
)
BEGIN SELECT RAISE(ABORT, 'analysis release quality gates are not satisfied'); END;

-- cleaning_runs 的 accepted 必须先找到同 run 的合格 formal finalized 发布；
-- 随后 release 的 accepted trigger 再要求 run 已 accepted，形成双向实质校验。
CREATE TRIGGER reject_accepted_cleaning_run_insert
BEFORE INSERT ON cleaning_runs
WHEN NEW.status = 'accepted'
BEGIN SELECT RAISE(ABORT, 'cleaning run cannot start accepted'); END;
CREATE TRIGGER validate_cleaning_run_acceptance
BEFORE UPDATE OF status ON cleaning_runs
WHEN NEW.status = 'accepted' AND OLD.status != 'accepted' AND NOT EXISTS (
  SELECT 1 FROM analysis_release_builds release
  JOIN post_decision_builds p
    ON p.decision_build_id = release.post_decision_build_id
   AND p.build_kind = 'final' AND p.seal_status = 'finalized'
  JOIN text_dedup_builds d
    ON d.dedup_build_id = release.text_dedup_build_id
   AND d.seal_status = 'finalized'
  JOIN text_keep_audit_evaluations te
    ON te.audit_evaluation_id = release.text_keep_audit_evaluation_id
   AND te.seal_status = 'finalized' AND te.evaluation_status = 'passed'
  JOIN text_keep_audit_rounds tr ON tr.audit_round_id = te.audit_round_id
  JOIN image_decision_builds i
    ON i.decision_build_id = release.image_decision_build_id
   AND i.seal_status = 'finalized'
  JOIN image_keep_audit_evaluations ie
    ON ie.audit_evaluation_id = release.image_keep_audit_evaluation_id
   AND ie.seal_status = 'finalized' AND ie.evaluation_status = 'passed'
  JOIN image_keep_audit_rounds ir ON ir.audit_round_id = ie.audit_round_id
  WHERE release.run_id = NEW.run_id AND release.release_mode = 'formal'
    AND release.seal_status = 'finalized'
    AND tr.audit_mode = 'formal' AND tr.seal_status = 'finalized'
    AND ir.integrity_status = 'finalized' AND ir.seal_status = 'finalized'
    AND NOT EXISTS (SELECT 1 FROM post_decisions x
                    WHERE x.decision_build_id = p.decision_build_id
                      AND x.decision_action = 'review')
    AND NOT EXISTS (SELECT 1 FROM image_decisions x
                    WHERE x.decision_build_id = i.decision_build_id
                      AND x.decision_action = 'review')
    AND NOT EXISTS (SELECT 1 FROM stage_tasks t
                    WHERE t.run_id = NEW.run_id AND t.required = 1
                      AND t.status IN ('pending', 'running', 'failed', 'blocked'))
)
BEGIN SELECT RAISE(ABORT, 'cleaning run requires a qualified finalized formal release'); END;

-- 所有发布子对象自创建即只允许追加；父表一旦 finalized，INSERT trigger
-- 也会拒绝新行，从而冻结成员、报告和 manifest 的完整集合。
CREATE TRIGGER prevent_analysis_post_eligible_update BEFORE UPDATE ON analysis_posts_eligible
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_post_eligible_delete BEFORE DELETE ON analysis_posts_eligible
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_post_dedup_update BEFORE UPDATE ON analysis_posts_deduplicated
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_post_dedup_delete BEFORE DELETE ON analysis_posts_deduplicated
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_image_eligible_update BEFORE UPDATE ON analysis_images_eligible
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_image_eligible_delete BEFORE DELETE ON analysis_images_eligible
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_image_evidence_update BEFORE UPDATE ON analysis_images_evidence_only
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_image_evidence_delete BEFORE DELETE ON analysis_images_evidence_only
BEGIN SELECT RAISE(ABORT, 'analysis release members are immutable'); END;
CREATE TRIGGER prevent_analysis_release_report_update BEFORE UPDATE ON analysis_release_reports
BEGIN SELECT RAISE(ABORT, 'analysis release reports are immutable'); END;
CREATE TRIGGER prevent_analysis_release_report_delete BEFORE DELETE ON analysis_release_reports
BEGIN SELECT RAISE(ABORT, 'analysis release reports are immutable'); END;
CREATE TRIGGER prevent_analysis_release_manifest_update BEFORE UPDATE ON analysis_release_manifests
BEGIN SELECT RAISE(ABORT, 'analysis release manifests are immutable'); END;
CREATE TRIGGER prevent_analysis_release_manifest_delete BEFORE DELETE ON analysis_release_manifests
BEGIN SELECT RAISE(ABORT, 'analysis release manifests are immutable'); END;
"""


def _assert_image_fingerprint_parameters(connection: sqlite3.Connection) -> None:
    """迁移前拒绝不符合 v2.4 固定 8/4 pHash 契约的历史指纹。"""

    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'image_fingerprints'
        """
    ).fetchone()
    if table_exists is None:
        return
    mismatch = connection.execute(
        """
        SELECT 1 FROM image_fingerprints
        WHERE phash_hash_size != 8 OR phash_highfreq_factor != 4
        LIMIT 1
        """
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.IntegrityError("image fingerprint algorithm parameters mismatch")


def _assert_image_build_member_context(connection: sqlite3.Connection) -> None:
    """迁移前拒绝无法归属于原 build 冻结上下文的历史候选成员。"""

    # 单元测试会暂时跳过整个 v11 DDL 来构造 v10 文件；真实迁移会先创建该表，
    # 而 v13 DDL 本身也要求表存在，因此这里只为 v10 夹具跳过数据检查。
    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'image_candidate_build_members'
        """
    ).fetchone()
    if table_exists is None:
        return
    mismatch = connection.execute(
        """
        SELECT 1
        FROM image_candidate_build_members AS m
        LEFT JOIN image_candidate_builds AS b ON b.build_id = m.build_id
        LEFT JOIN image_fingerprints AS f ON f.fingerprint_id = m.fingerprint_id
        LEFT JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
        WHERE b.build_id IS NULL OR f.fingerprint_id IS NULL OR r.manifest_row_id IS NULL
           OR r.manifest_id != b.manifest_id
           OR f.fingerprint_version != b.fingerprint_version
           OR r.source_image_id != m.source_image_id
           OR r.source_post_id != m.source_post_id
           OR r.row_identity_sha256 != m.row_identity_sha256
           OR f.row_identity_sha256 != m.row_identity_sha256
        LIMIT 1
        """
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.IntegrityError("image build member context mismatch")


def _assert_image_candidate_members_are_content(
    connection: sqlite3.Connection,
) -> None:
    """升级 v19 前拒绝历史候选构建中的非 content 成员。

    角色是上游冻结结构事实；迁移不能把头像或页面图静默改写为内容图。合法
    v15–v18 数据原样保留，发现越界成员则整次迁移回滚并要求修复来源构建。
    """

    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'image_candidate_build_members'
        """
    ).fetchone()
    if table_exists is None:
        return
    mismatch = connection.execute(
        """
        SELECT 1 FROM image_candidate_build_members m
        JOIN image_fingerprints f ON f.fingerprint_id = m.fingerprint_id
        JOIN image_manifest_rows r ON r.manifest_row_id = f.manifest_row_id
        WHERE r.relation_role != 'content' LIMIT 1
        """
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.IntegrityError("image candidate member is not content")


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    """仅在旧数据库缺少字段时追加字段，保证迁移可重复执行。"""

    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if column not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}')


def _v24_prerequisites_present(connection: sqlite3.Connection) -> bool:
    """判断 v24 依赖的真实 v23 结构是否已经完整存在。

    部分迁移测试会暂时把旧版 DDL 替换为空字符串，以构造历史数据库；这些
    夹具仍会留下迁移编号，但不能据此断言相应表和列真实存在。v24 必须等到
    文本双轴表及图片可信审计列都就绪后再执行，避免提前创建的 trigger 阻碍
    随后的旧表重建。正式 v23 数据库一次即满足这些结构条件。
    """

    required_columns = {
        "text_post_adjudications": {"tourism_label", "model_run_id"},
        "image_decision_builds": {"decision_build_id", "seal_status"},
        "image_keep_audit_rounds": {"audit_round_id", "integrity_status", "seal_status"},
        "image_keep_audit_evaluations": {
            "audit_evaluation_id",
            "evaluation_status",
            "seal_status",
        },
    }
    for table, columns in required_columns.items():
        actual = {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if not columns <= actual:
            return False
    adjudication_columns = {
        str(row[1])
        for row in connection.execute('PRAGMA table_info("text_post_adjudications")')
    }
    return "commercial_label" not in adjudication_columns


def _canonical_json_sha256(value: str) -> str:
    """为 SQLite trigger 提供与 Python manifest 相同的规范 JSON 摘要。

    输入必须是合法 JSON；对象键排序、中文不转义且无多余空白。解析或编码失败
    会使触发该函数的 SQL 语句失败，不能退化为接受父表自报的 manifest。
    """

    parsed = json.loads(value)
    payload = json.dumps(
        parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_sample_manifest_sha256(value: str) -> str:
    """规范化审计成员概率/权重到 15 位后计算成员 manifest。

    JSON 必须是六字段成员数组；浮点舍入与
    :func:`image_keep_audit_repository._sample_manifest` 保持一致。结构错误让 SQL
    失败，以免 schema 在无法解释抽样证据时继续封存。
    """

    rows = json.loads(value)
    normalized: list[list[object]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("audit member manifest row is invalid")
        normalized.append([*row[:4], round(float(row[4]), 15), round(float(row[5]), 15)])
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_sample_rank(seed: int, namespace: str, identity: str) -> str:
    """暴露冻结 SHA-256 抽样次序给 SQLite，末尾身份用于极小碰撞时定序。"""

    digest = hashlib.sha256(
        f"{int(seed)}:{namespace}:{identity}".encode("utf-8")
    ).hexdigest()
    return f"{digest}:{identity}"


def _wilson_upper_95(event_count: int, sample_count: int) -> float:
    """重算二项事件率的单侧 95% Wilson 上限。

    计数来自已经通过外键锁定的人工审计证据；函数只承担确定性数值计算，
    不接受调用者传入的点估计或通过状态。空样本返回 1.0，使质量门安全失败。
    """

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


def connect_derived(path: str | Path) -> sqlite3.Connection:
    """打开可写派生库，并统一启用外键、超时、行映射和 WAL。

    ``path`` 只可指向清洗派生 SQLite；函数会创建缺失父目录但不执行 schema
    迁移。成功返回由调用方负责关闭的连接，文件系统或 SQLite 打开失败时原样
    抛出异常；不得把正式采集库路径传给本入口。
    """

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    # v21+ 的审计轮封存 trigger 必须能够独立重算 manifest 与确定性抽样次序。
    # 外部 SQLite 客户端未注册函数时封存语句会安全失败，而不会跳过校验。
    connection.create_function(
        "canonical_json_sha256", 1, _canonical_json_sha256, deterministic=True
    )
    connection.create_function(
        "audit_sample_manifest_sha256",
        1,
        _audit_sample_manifest_sha256,
        deterministic=True,
    )
    connection.create_function(
        "audit_sample_rank", 3, _audit_sample_rank, deterministic=True
    )
    # v24 的文本保留集评估由 trigger 通过该函数独立复核。第三方 SQLite
    # 客户端若未注册函数，封存会失败而不是绕过 Wilson 停止线。
    connection.create_function(
        "wilson_upper_95", 2, _wilson_upper_95, deterministic=True
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def migrate_derived(connection: sqlite3.Connection) -> None:
    """按版本幂等迁移派生库，不覆盖任何既有运行或结果。

    输入必须是由 :func:`connect_derived` 打开的可写连接。函数从 v1 顺序补齐至
    :data:`DERIVED_SCHEMA_VERSION`，每版只在迁移记录缺失时执行；重复调用无
    变化。历史候选角色、指纹算法或证据谱系违反新不变量时抛出
    ``sqlite3.IntegrityError`` 并回滚当前迁移，调用方须修复派生数据而非跳过。
    """

    with connection:
        connection.executescript(_SCHEMA_V1)
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name, applied_at_utc)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (1, "input_contract_schema"),
        )
        _ensure_column(
            connection,
            "cleaning_runs",
            "run_type",
            "TEXT NOT NULL DEFAULT 'incremental' CHECK (run_type IN ('full', 'incremental', 'reprocess'))",
        )
        _ensure_column(
            connection,
            "cleaning_runs",
            "source_snapshot_id",
            "TEXT REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT",
        )
        _ensure_column(connection, "cleaning_runs", "started_at_utc", "TEXT")
        _ensure_column(connection, "cleaning_runs", "finished_at_utc", "TEXT")
        connection.executescript(_SCHEMA_V2)
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name, applied_at_utc)
            VALUES (2, 'incremental_inventory_and_scheduler',
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """
        )
        connection.executescript(_SCHEMA_V3)
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name, applied_at_utc)
            VALUES (3, 'text_deterministic_results_and_candidates',
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """
        )
        version_four_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 4"
        ).fetchone()
        if version_four_exists is None:
            _ensure_column(
                connection,
                "text_deterministic_results",
                "runtime_versions_json",
                "TEXT",
            )
            _ensure_column(connection, "text_deterministic_results", "runtime_sha256", "TEXT")
            _ensure_column(connection, "text_candidate_builds", "runtime_sha256", "TEXT")
            _ensure_column(
                connection,
                "text_candidate_builds",
                "status",
                "TEXT NOT NULL DEFAULT 'finalized' CHECK (status IN ('building', 'finalized'))",
            )
            connection.executescript(_SCHEMA_V4)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (4, 'seal_text_candidates_and_lock_runtime',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_five_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 5"
        ).fetchone()
        if version_five_exists is None:
            _ensure_column(
                connection,
                "source_post_inventory",
                "current_author_identity_present",
                "INTEGER NOT NULL DEFAULT 0 CHECK (current_author_identity_present IN (0, 1))",
            )
            _ensure_column(
                connection,
                "source_post_versions",
                "author_identity_present",
                "INTEGER NOT NULL DEFAULT 0 CHECK (author_identity_present IN (0, 1))",
            )
            connection.executescript(_SCHEMA_V5)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (5, 'text_annotations_leakage_and_relevance_model',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_six_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 6"
        ).fetchone()
        if version_six_exists is None:
            _ensure_column(
                connection,
                "text_post_adjudications",
                "model_run_id",
                "TEXT",
            )
            connection.executescript(_SCHEMA_V6)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (6, 'link_model_review_adjudications',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_seven_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 7"
        ).fetchone()
        if version_seven_exists is None:
            connection.executescript(_SCHEMA_V7)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (7, 'double_label_agreement_workflow',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_eight_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 8"
        ).fetchone()
        if version_eight_exists is None:
            connection.executescript(_SCHEMA_V8)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (8, 'freeze_periodic_review_windows',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_nine_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 9"
        ).fetchone()
        if version_nine_exists is None:
            _ensure_column(
                connection,
                "text_sampling_runs",
                "seal_status",
                "TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized'))",
            )
            _ensure_column(
                connection,
                "text_sampling_runs",
                "member_manifest_sha256",
                "TEXT CHECK (member_manifest_sha256 IS NULL OR length(member_manifest_sha256) = 64)",
            )
            _ensure_column(
                connection,
                "text_leakage_builds",
                "seal_status",
                "TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized'))",
            )
            _ensure_column(
                connection,
                "text_model_runs",
                "seal_status",
                "TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized'))",
            )
            _ensure_column(
                connection,
                "text_model_runs",
                "expected_prediction_count",
                "INTEGER NOT NULL DEFAULT 0 CHECK (expected_prediction_count >= 0)",
            )
            for column in (
                "train_manifest_sha256",
                "validation_manifest_sha256",
                "test_manifest_sha256",
                "prediction_manifest_sha256",
            ):
                _ensure_column(
                    connection,
                    "text_model_runs",
                    column,
                    f"TEXT CHECK ({column} IS NULL OR length({column}) = 64)",
                )
            # v5 的整行 update trigger 会阻止迁移补齐旧运行的可验证计数；先移除，
            # 随后由 v9 更细粒度的 identity/status trigger 接管。
            connection.executescript(
                """
                DROP TRIGGER IF EXISTS prevent_text_sampling_run_update;
                DROP TRIGGER IF EXISTS prevent_text_leakage_build_update;
                DROP TRIGGER IF EXISTS prevent_text_model_run_update;
                """
            )
            connection.execute(
                """
                UPDATE text_sampling_runs
                SET member_manifest_sha256 = output_sha256
                WHERE member_manifest_sha256 IS NULL
                  AND (SELECT COUNT(*) FROM text_sample_members AS m
                       WHERE m.sample_run_id = text_sampling_runs.sample_run_id
                         AND m.sample_frame = 'probability') = probability_count
                  AND (SELECT COUNT(*) FROM text_sample_members AS m
                       WHERE m.sample_run_id = text_sampling_runs.sample_run_id
                         AND m.sample_frame = 'targeted') = targeted_count
                  AND (SELECT COUNT(DISTINCT m.source_post_id || ':' || m.source_version)
                       FROM text_sample_members AS m
                       WHERE m.sample_run_id = text_sampling_runs.sample_run_id
                         AND m.requires_double_label = 1) = double_label_count
                """
            )
            connection.execute(
                """
                UPDATE text_model_runs
                SET expected_prediction_count = (
                    SELECT COUNT(*) FROM text_model_predictions AS p
                    WHERE p.model_run_id = text_model_runs.model_run_id
                )
                """
            )
            connection.executescript(_SCHEMA_V9)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (9, 'seal_annotation_and_model_outputs',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_ten_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 10"
        ).fetchone()
        if version_ten_exists is None:
            _ensure_column(
                connection,
                "text_double_label_supplements",
                "seal_status",
                "TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized'))",
            )
            _ensure_column(
                connection,
                "text_periodic_review_windows",
                "seal_status",
                "TEXT NOT NULL DEFAULT 'finalized' CHECK (seal_status IN ('building', 'finalized'))",
            )
            _ensure_column(
                connection,
                "text_model_runs",
                "request_manifest_sha256",
                "TEXT CHECK (request_manifest_sha256 IS NULL OR length(request_manifest_sha256) = 64)",
            )
            _ensure_column(
                connection,
                "text_model_runs",
                "candidate_prediction_manifest_sha256",
                "TEXT CHECK (candidate_prediction_manifest_sha256 IS NULL OR length(candidate_prediction_manifest_sha256) = 64)",
            )
            connection.executescript(_SCHEMA_V10)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_text_model_request_manifest
                ON text_model_runs(request_manifest_sha256)
                WHERE request_manifest_sha256 IS NOT NULL
                """
            )
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (10, 'seal_review_windows_and_model_requests',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_eleven_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 11"
        ).fetchone()
        if version_eleven_exists is None:
            connection.executescript(_SCHEMA_V11)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (11, 'image_manifest_fingerprints_and_candidates',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_twelve_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 12"
        ).fetchone()
        if version_twelve_exists is None:
            connection.executescript(_SCHEMA_V12)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (12, 'bind_image_candidates_to_build_members',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_thirteen_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 13"
        ).fetchone()
        if version_thirteen_exists is None:
            _assert_image_build_member_context(connection)
            connection.executescript(_SCHEMA_V13)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (13, 'bind_image_build_members_to_manifest_context',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_fourteen_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 14"
        ).fetchone()
        if version_fourteen_exists is None:
            connection.executescript(_SCHEMA_V14)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (14, 'freeze_run_and_snapshot_identity',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_fifteen_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 15"
        ).fetchone()
        if version_fifteen_exists is None:
            _assert_image_fingerprint_parameters(connection)
            connection.executescript(_SCHEMA_V15)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (15, 'fix_image_fingerprint_algorithm_parameters',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_sixteen_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 16"
        ).fetchone()
        if version_sixteen_exists is None:
            connection.executescript(_SCHEMA_V16)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (16, 'image_human_review_decisions_and_keep_audit',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_seventeen_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 17"
        ).fetchone()
        if version_seventeen_exists is None:
            connection.executescript(_SCHEMA_V17)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (17, 'version_image_agreement_evidence_manifest',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_eighteen_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 18"
        ).fetchone()
        if version_eighteen_exists is None:
            connection.executescript(_SCHEMA_V18)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (18, 'audit_all_exact_cluster_relations',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_nineteen_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 19"
        ).fetchone()
        if version_nineteen_exists is None:
            _assert_image_candidate_members_are_content(connection)
            connection.executescript(_SCHEMA_V19)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (19, 'enforce_image_review_and_decision_lineage',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_twenty_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 20"
        ).fetchone()
        if version_twenty_exists is None:
            connection.executescript(_SCHEMA_V20)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (20, 'verify_image_derived_evaluations_from_annotations',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_twenty_one_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 21"
        ).fetchone()
        if version_twenty_one_exists is None:
            connection.executescript(_SCHEMA_V21)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (21, 'verify_image_keep_audit_population_and_sampling',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_twenty_two_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 22"
        ).fetchone()
        if version_twenty_two_exists is None:
            connection.executescript(_SCHEMA_V22)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (22, 'bind_image_keep_audit_seed_to_protocol_run',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_twenty_three_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 23"
        ).fetchone()
        if version_twenty_three_exists is None:
            connection.executescript(_SCHEMA_V23)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (23, 'text_cleaning_dual_axis_contract',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        version_twenty_four_exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 24"
        ).fetchone()
        if version_twenty_four_exists is None and _v24_prerequisites_present(connection):
            connection.executescript(_SCHEMA_V24)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (24, 'final_decisions_dedup_audits_and_analysis_release',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
