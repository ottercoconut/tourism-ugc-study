"""正式文本清洗框架的独立内部 schema 契约。

该 schema 只描述模型、策略、推理、预测和人工证据之间的引用关系，不替换
当前 schema 34；重建派生库前先用迁移 manifest 固化样本框。所有封存表均通过
SQLite 触发器禁止修改，源库仍由调用方以只读方式打开。
"""

from __future__ import annotations

import sqlite3


FORMAL_SCHEMA_NAME = "cleaning_formal"
FORMAL_SCHEMA_REVISION = 1

FORMAL_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS cleaning_formal_schema (
    schema_name TEXT PRIMARY KEY CHECK (schema_name = 'cleaning_formal'),
    schema_revision INTEGER NOT NULL CHECK (schema_revision = 1),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cleaning_model_artifacts (
    model_id TEXT PRIMARY KEY,
    algorithm_id TEXT NOT NULL,
    feature_contract TEXT NOT NULL CHECK (feature_contract = 'normalized_model_text'),
    calibration_method TEXT NOT NULL CHECK (calibration_method = 'sigmoid'),
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
    calibration_artifact_path TEXT NOT NULL,
    calibration_artifact_sha256 TEXT NOT NULL CHECK (length(calibration_artifact_sha256) = 64),
    reference_csv_sha256 TEXT NOT NULL CHECK (length(reference_csv_sha256) = 64),
    reference_manifest_sha256 TEXT NOT NULL CHECK (length(reference_manifest_sha256) = 64),
    sample_migration_manifest_sha256 TEXT NOT NULL CHECK (length(sample_migration_manifest_sha256) = 64),
    leakage_manifest_sha256 TEXT NOT NULL CHECK (length(leakage_manifest_sha256) = 64),
    test_manifest_sha256 TEXT NOT NULL CHECK (length(test_manifest_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('draft', 'frozen')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (artifact_sha256, calibration_artifact_sha256)
);

CREATE TABLE IF NOT EXISTS cleaning_threshold_policies (
    policy_id TEXT PRIMARY KEY,
    policy_status TEXT NOT NULL CHECK (policy_status IN ('UNSET', 'frozen')),
    model_id TEXT NOT NULL REFERENCES cleaning_model_artifacts(model_id) ON DELETE RESTRICT,
    t_keep REAL CHECK (t_keep IS NULL OR (t_keep >= 0.0 AND t_keep <= 1.0)),
    t_exclude REAL CHECK (t_exclude IS NULL OR (t_exclude >= 0.0 AND t_exclude <= 1.0)),
    audit_sample_size INTEGER CHECK (audit_sample_size IS NULL OR audit_sample_size > 0),
    audit_max_event_rate REAL CHECK (audit_max_event_rate IS NULL OR (audit_max_event_rate >= 0.0 AND audit_max_event_rate <= 1.0)),
    audit_upper_confidence_limit REAL CHECK (audit_upper_confidence_limit IS NULL OR (audit_upper_confidence_limit >= 0.0 AND audit_upper_confidence_limit <= 1.0)),
    recovery_gate TEXT,
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    CHECK (
        (policy_status = 'UNSET'
         AND t_keep IS NULL AND t_exclude IS NULL
         AND audit_sample_size IS NULL
         AND audit_max_event_rate IS NULL
         AND audit_upper_confidence_limit IS NULL)
        OR
        (policy_status = 'frozen'
         AND t_keep IS NOT NULL AND t_exclude IS NOT NULL
         AND t_keep < t_exclude)
    )
);

CREATE TABLE IF NOT EXISTS cleaning_inference_runs (
    inference_run_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES cleaning_model_artifacts(model_id) ON DELETE RESTRICT,
    policy_id TEXT NOT NULL REFERENCES cleaning_threshold_policies(policy_id) ON DELETE RESTRICT,
    target_batch_id TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL CHECK (length(input_manifest_sha256) = 64),
    output_manifest_sha256 TEXT NOT NULL CHECK (length(output_manifest_sha256) = 64),
    fit_called INTEGER NOT NULL CHECK (fit_called = 0),
    status TEXT NOT NULL CHECK (status IN ('planned', 'scored', 'paused', 'failed')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (model_id, policy_id, target_batch_id, input_manifest_sha256)
);

CREATE TABLE IF NOT EXISTS cleaning_predictions (
    inference_run_id TEXT NOT NULL REFERENCES cleaning_inference_runs(inference_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    p_unrelated REAL NOT NULL CHECK (p_unrelated >= 0.0 AND p_unrelated <= 1.0),
    route_action TEXT NOT NULL CHECK (route_action IN ('auto_keep', 'manual_review', 'auto_exclude', 'unset')),
    model_id TEXT NOT NULL REFERENCES cleaning_model_artifacts(model_id) ON DELETE RESTRICT,
    policy_id TEXT NOT NULL REFERENCES cleaning_threshold_policies(policy_id) ON DELETE RESTRICT,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (inference_run_id, source_post_id, source_version),
    FOREIGN KEY (inference_run_id) REFERENCES cleaning_inference_runs(inference_run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS cleaning_manual_evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    csv_sha256 TEXT NOT NULL CHECK (length(csv_sha256) = 64),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    tourism_label TEXT NOT NULL CHECK (tourism_label IN ('related', 'unrelated', 'uncertain')),
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('reference', 'manual_review', 'retained_audit')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (csv_sha256, manifest_sha256, source_post_id, source_version)
);

CREATE TABLE IF NOT EXISTS cleaning_final_decisions (
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    decision_action TEXT NOT NULL CHECK (decision_action IN ('keep', 'exclude', 'review')),
    decision_source TEXT NOT NULL CHECK (decision_source IN ('manual_evidence', 'model_route')),
    inference_run_id TEXT REFERENCES cleaning_inference_runs(inference_run_id) ON DELETE RESTRICT,
    evidence_id TEXT REFERENCES cleaning_manual_evidence(evidence_id) ON DELETE RESTRICT,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (source_post_id, source_version),
    CHECK (
        (decision_source = 'manual_evidence' AND evidence_id IS NOT NULL)
        OR (decision_source = 'model_route' AND inference_run_id IS NOT NULL)
    )
);

CREATE TRIGGER IF NOT EXISTS cleaning_prediction_lineage_check
BEFORE INSERT ON cleaning_predictions
WHEN NOT EXISTS (
    SELECT 1 FROM cleaning_inference_runs AS run
    JOIN cleaning_threshold_policies AS policy
      ON policy.policy_id = run.policy_id
    WHERE run.inference_run_id = NEW.inference_run_id
      AND run.model_id = NEW.model_id
      AND run.policy_id = NEW.policy_id
      AND (
          (policy.policy_status = 'UNSET' AND NEW.route_action = 'manual_review')
          OR
          (policy.policy_status = 'frozen' AND NEW.route_action IN ('auto_keep', 'manual_review', 'auto_exclude'))
      )
)
BEGIN SELECT RAISE(ABORT, 'prediction lineage or unset policy route is invalid'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_formal_schema_immutable
BEFORE UPDATE ON cleaning_formal_schema
BEGIN SELECT RAISE(ABORT, 'formal schema identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_model_artifacts_immutable
BEFORE UPDATE ON cleaning_model_artifacts
WHEN OLD.status = 'frozen'
BEGIN SELECT RAISE(ABORT, 'frozen model artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_threshold_policies_immutable
BEFORE UPDATE ON cleaning_threshold_policies
WHEN OLD.policy_status = 'frozen'
BEGIN SELECT RAISE(ABORT, 'frozen threshold policies are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_predictions_immutable
BEFORE UPDATE ON cleaning_predictions
BEGIN SELECT RAISE(ABORT, 'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_manual_evidence_immutable
BEFORE UPDATE ON cleaning_manual_evidence
BEGIN SELECT RAISE(ABORT, 'manual evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_final_decisions_immutable
BEFORE UPDATE ON cleaning_final_decisions
BEGIN SELECT RAISE(ABORT, 'final decisions are immutable'); END;
"""


def migrate_formal_schema(connection: sqlite3.Connection) -> None:
    """在独立连接中建立正式框架表；已有其他修订时拒绝就地改写。"""

    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cleaning_formal_schema'"
    ).fetchone()
    existing = (
        connection.execute(
            "SELECT schema_revision FROM cleaning_formal_schema WHERE schema_name = ?",
            (FORMAL_SCHEMA_NAME,),
        ).fetchone()
        if table_exists is not None
        else None
    )
    if existing is not None and int(existing[0]) != FORMAL_SCHEMA_REVISION:
        raise sqlite3.IntegrityError("formal_schema_revision_mismatch")
    connection.executescript(FORMAL_SCHEMA_SQL)
    if existing is None:
        connection.execute(
            "INSERT INTO cleaning_formal_schema(schema_name, schema_revision, created_at_utc) "
            "VALUES ('cleaning_formal', 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
    connection.commit()
