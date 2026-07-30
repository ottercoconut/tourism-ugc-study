"""清洗派生 SQLite 的连接约束与幂等迁移。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


DERIVED_SCHEMA_VERSION = 4

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


def connect_derived(path: str | Path) -> sqlite3.Connection:
    """打开可写派生库，并统一启用外键、超时和 WAL。"""

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def migrate_derived(connection: sqlite3.Connection) -> None:
    """按版本幂等迁移派生库，不覆盖任何既有运行或结果。"""

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
