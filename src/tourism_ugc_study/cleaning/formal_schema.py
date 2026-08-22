"""正式文本清洗框架的独立内部 schema 契约。

该 schema 只描述模型、策略、推理、预测、人工证据和追加式决定之间的引用
关系，不替换当前派生 schema。模型只绑定唯一最终参考 CSV、finalized manifest、
最终成员摘要和 leakage build；所有证据表均禁止删除，跨表谱系依赖强制启用的
SQLite 外键和触发器。
"""

from __future__ import annotations

import sqlite3


FORMAL_SCHEMA_NAME = "cleaning_formal"
FORMAL_SCHEMA_REVISION = 2

FORMAL_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS cleaning_formal_schema (
    schema_name TEXT PRIMARY KEY CHECK (schema_name = 'cleaning_formal'),
    schema_revision INTEGER NOT NULL CHECK (schema_revision = 2),
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
    reference_member_manifest_sha256 TEXT NOT NULL CHECK (length(reference_member_manifest_sha256) = 64),
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
    audit_event_definition TEXT,
    audit_sample_size INTEGER CHECK (audit_sample_size IS NULL OR audit_sample_size > 0),
    audit_max_event_rate REAL CHECK (
        audit_max_event_rate IS NULL
        OR (audit_max_event_rate >= 0.0 AND audit_max_event_rate <= 1.0)
    ),
    audit_upper_confidence_limit REAL CHECK (
        audit_upper_confidence_limit IS NULL
        OR (audit_upper_confidence_limit >= 0.0 AND audit_upper_confidence_limit <= 1.0)
    ),
    minimum_test_metrics_json TEXT CHECK (
        minimum_test_metrics_json IS NULL OR json_valid(minimum_test_metrics_json)
    ),
    minimum_automatic_coverage REAL CHECK (
        minimum_automatic_coverage IS NULL
        OR (minimum_automatic_coverage >= 0.0 AND minimum_automatic_coverage <= 1.0)
    ),
    test_acceptance_manifest_sha256 TEXT CHECK (
        test_acceptance_manifest_sha256 IS NULL
        OR length(test_acceptance_manifest_sha256) = 64
    ),
    test_acceptance_status TEXT NOT NULL CHECK (
        test_acceptance_status IN ('UNSET', 'passed', 'failed')
    ),
    recovery_gate TEXT,
    automatic_routing_enabled INTEGER NOT NULL CHECK (automatic_routing_enabled IN (0, 1)),
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    CHECK (
        (policy_status = 'UNSET'
         AND t_keep IS NULL AND t_exclude IS NULL
         AND audit_event_definition IS NULL
         AND audit_sample_size IS NULL
         AND audit_max_event_rate IS NULL
         AND audit_upper_confidence_limit IS NULL
         AND minimum_test_metrics_json IS NULL
         AND minimum_automatic_coverage IS NULL
         AND test_acceptance_manifest_sha256 IS NULL
         AND test_acceptance_status = 'UNSET'
         AND recovery_gate IS NULL
         AND automatic_routing_enabled = 0)
        OR
        (policy_status = 'frozen'
         AND t_keep IS NOT NULL AND t_exclude IS NOT NULL
         AND t_keep < t_exclude
         AND audit_event_definition IS NOT NULL
         AND length(trim(audit_event_definition)) > 0
         AND audit_sample_size IS NOT NULL
         AND audit_max_event_rate IS NOT NULL
         AND audit_upper_confidence_limit IS NOT NULL
         AND minimum_test_metrics_json IS NOT NULL
         AND minimum_automatic_coverage IS NOT NULL
         AND test_acceptance_manifest_sha256 IS NOT NULL
         AND test_acceptance_status IN ('passed', 'failed')
         AND recovery_gate IS NOT NULL
         AND length(trim(recovery_gate)) > 0
         AND (automatic_routing_enabled = 0 OR test_acceptance_status = 'passed'))
    )
);

CREATE TABLE IF NOT EXISTS cleaning_inference_runs (
    inference_run_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES cleaning_model_artifacts(model_id) ON DELETE RESTRICT,
    policy_id TEXT NOT NULL REFERENCES cleaning_threshold_policies(policy_id) ON DELETE RESTRICT,
    target_batch_id TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL CHECK (length(input_manifest_sha256) = 64),
    expected_prediction_count INTEGER NOT NULL CHECK (expected_prediction_count >= 0),
    output_manifest_sha256 TEXT CHECK (
        output_manifest_sha256 IS NULL OR length(output_manifest_sha256) = 64
    ),
    fit_called INTEGER NOT NULL CHECK (fit_called = 0),
    status TEXT NOT NULL CHECK (status IN ('building', 'completed', 'failed')),
    created_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    UNIQUE (model_id, policy_id, target_batch_id, input_manifest_sha256),
    CHECK (
        (status = 'building' AND output_manifest_sha256 IS NULL AND completed_at_utc IS NULL)
        OR
        (status = 'completed' AND output_manifest_sha256 IS NOT NULL
         AND completed_at_utc IS NOT NULL)
        OR
        (status = 'failed' AND completed_at_utc IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS cleaning_predictions (
    inference_run_id TEXT NOT NULL
        REFERENCES cleaning_inference_runs(inference_run_id) ON DELETE RESTRICT,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    p_unrelated REAL NOT NULL CHECK (p_unrelated >= 0.0 AND p_unrelated <= 1.0),
    route_action TEXT NOT NULL CHECK (
        route_action IN ('auto_keep', 'manual_review', 'auto_exclude')
    ),
    model_id TEXT NOT NULL REFERENCES cleaning_model_artifacts(model_id) ON DELETE RESTRICT,
    policy_id TEXT NOT NULL REFERENCES cleaning_threshold_policies(policy_id) ON DELETE RESTRICT,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (inference_run_id, source_post_id, source_version)
);

CREATE TABLE IF NOT EXISTS cleaning_manual_evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    csv_sha256 TEXT NOT NULL CHECK (length(csv_sha256) = 64),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    tourism_label TEXT NOT NULL CHECK (
        tourism_label IN ('related', 'unrelated', 'uncertain')
    ),
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN ('reference', 'manual_review', 'retained_audit')
    ),
    guide_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (manifest_sha256, task_id, source_post_id, source_version)
);

CREATE TABLE IF NOT EXISTS cleaning_final_decisions (
    decision_id TEXT PRIMARY KEY,
    source_post_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    decision_action TEXT NOT NULL CHECK (decision_action IN ('keep', 'exclude', 'review')),
    decision_source TEXT NOT NULL CHECK (
        decision_source IN ('manual_evidence', 'model_route')
    ),
    inference_run_id TEXT
        REFERENCES cleaning_inference_runs(inference_run_id) ON DELETE RESTRICT,
    evidence_id TEXT UNIQUE
        REFERENCES cleaning_manual_evidence(evidence_id) ON DELETE RESTRICT,
    supersedes_decision_id TEXT UNIQUE
        REFERENCES cleaning_final_decisions(decision_id) ON DELETE RESTRICT,
    decision_sha256 TEXT NOT NULL UNIQUE CHECK (length(decision_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    CHECK (supersedes_decision_id IS NULL OR supersedes_decision_id != decision_id),
    CHECK (
        (decision_source = 'manual_evidence'
         AND evidence_id IS NOT NULL AND inference_run_id IS NULL)
        OR
        (decision_source = 'model_route'
         AND inference_run_id IS NOT NULL AND evidence_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cleaning_decision_single_root
ON cleaning_final_decisions(source_post_id, source_version)
WHERE supersedes_decision_id IS NULL;

CREATE VIEW IF NOT EXISTS cleaning_current_decisions AS
SELECT decision.*
FROM cleaning_final_decisions AS decision
WHERE NOT EXISTS (
    SELECT 1 FROM cleaning_final_decisions AS successor
    WHERE successor.supersedes_decision_id = decision.decision_id
);

CREATE TRIGGER IF NOT EXISTS cleaning_policy_model_frozen
BEFORE INSERT ON cleaning_threshold_policies
WHEN NEW.policy_status = 'frozen'
 AND NOT EXISTS (
     SELECT 1 FROM cleaning_model_artifacts AS model
     WHERE model.model_id = NEW.model_id AND model.status = 'frozen'
 )
BEGIN SELECT RAISE(ABORT, 'frozen policy requires a frozen model'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_inference_lineage_check
BEFORE INSERT ON cleaning_inference_runs
WHEN NOT EXISTS (
    SELECT 1 FROM cleaning_model_artifacts AS model
    JOIN cleaning_threshold_policies AS policy
      ON policy.model_id = model.model_id
    WHERE model.model_id = NEW.model_id
      AND model.status = 'frozen'
      AND policy.policy_id = NEW.policy_id
)
BEGIN SELECT RAISE(ABORT, 'inference lineage requires a frozen model and matching policy'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_prediction_building_check
BEFORE INSERT ON cleaning_predictions
WHEN NOT EXISTS (
    SELECT 1 FROM cleaning_inference_runs AS run
    WHERE run.inference_run_id = NEW.inference_run_id
      AND run.status = 'building'
)
BEGIN SELECT RAISE(ABORT, 'predictions may only be appended to a building run'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_prediction_lineage_and_route_check
BEFORE INSERT ON cleaning_predictions
WHEN NOT EXISTS (
    SELECT 1 FROM cleaning_inference_runs AS run
    JOIN cleaning_threshold_policies AS policy
      ON policy.policy_id = run.policy_id
    WHERE run.inference_run_id = NEW.inference_run_id
      AND run.model_id = NEW.model_id
      AND run.policy_id = NEW.policy_id
      AND (
          ((policy.policy_status = 'UNSET' OR policy.automatic_routing_enabled = 0)
           AND NEW.route_action = 'manual_review')
          OR
          (policy.policy_status = 'frozen'
           AND policy.automatic_routing_enabled = 1
           AND (
               (NEW.p_unrelated <= policy.t_keep AND NEW.route_action = 'auto_keep')
               OR
               (NEW.p_unrelated > policy.t_keep
                AND NEW.p_unrelated < policy.t_exclude
                AND NEW.route_action = 'manual_review')
               OR
               (NEW.p_unrelated >= policy.t_exclude
                AND NEW.route_action = 'auto_exclude')
           ))
      )
)
BEGIN SELECT RAISE(ABORT, 'prediction lineage or route is invalid'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_inference_completion_check
BEFORE UPDATE OF status ON cleaning_inference_runs
WHEN NEW.status = 'completed'
 AND (
     NEW.output_manifest_sha256 IS NULL
     OR NEW.completed_at_utc IS NULL
     OR (SELECT COUNT(*) FROM cleaning_predictions AS prediction
         WHERE prediction.inference_run_id = NEW.inference_run_id)
        != NEW.expected_prediction_count
 )
BEGIN SELECT RAISE(ABORT, 'completed inference run is incomplete'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_final_decision_evidence_check
BEFORE INSERT ON cleaning_final_decisions
WHEN NOT (
    (NEW.decision_source = 'manual_evidence'
     AND EXISTS (
         SELECT 1 FROM cleaning_manual_evidence AS evidence
         WHERE evidence.evidence_id = NEW.evidence_id
           AND evidence.source_post_id = NEW.source_post_id
           AND evidence.source_version = NEW.source_version
           AND NEW.decision_action = CASE evidence.tourism_label
               WHEN 'related' THEN 'keep'
               WHEN 'unrelated' THEN 'exclude'
               ELSE 'review' END
     ))
    OR
    (NEW.decision_source = 'model_route'
     AND EXISTS (
         SELECT 1 FROM cleaning_predictions AS prediction
         JOIN cleaning_inference_runs AS run
           ON run.inference_run_id = prediction.inference_run_id
         WHERE prediction.inference_run_id = NEW.inference_run_id
           AND prediction.source_post_id = NEW.source_post_id
           AND prediction.source_version = NEW.source_version
           AND run.status = 'completed'
           AND NEW.decision_action = CASE prediction.route_action
               WHEN 'auto_keep' THEN 'keep'
               WHEN 'auto_exclude' THEN 'exclude'
               ELSE 'review' END
     ))
)
BEGIN SELECT RAISE(ABORT, 'final decision evidence identity or action is invalid'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_final_decision_supersedes_check
BEFORE INSERT ON cleaning_final_decisions
WHEN NEW.supersedes_decision_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM cleaning_final_decisions AS prior
     WHERE prior.decision_id = NEW.supersedes_decision_id
       AND prior.source_post_id = NEW.source_post_id
       AND prior.source_version = NEW.source_version
 )
BEGIN SELECT RAISE(ABORT, 'superseded decision identity is invalid'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_formal_schema_update_forbidden
BEFORE UPDATE ON cleaning_formal_schema
BEGIN SELECT RAISE(ABORT, 'formal schema identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_formal_schema_delete_forbidden
BEFORE DELETE ON cleaning_formal_schema
BEGIN SELECT RAISE(ABORT, 'formal schema identity is immutable'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_model_identity_update_forbidden
BEFORE UPDATE OF model_id, algorithm_id, feature_contract, calibration_method,
                 artifact_path, artifact_sha256, calibration_artifact_path,
                 calibration_artifact_sha256, reference_csv_sha256,
                 reference_manifest_sha256, reference_member_manifest_sha256,
                 leakage_manifest_sha256, test_manifest_sha256, created_at_utc
ON cleaning_model_artifacts
BEGIN SELECT RAISE(ABORT, 'model artifact identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_model_status_transition_check
BEFORE UPDATE OF status ON cleaning_model_artifacts
WHEN NOT (OLD.status = 'draft' AND NEW.status = 'frozen')
BEGIN SELECT RAISE(ABORT, 'model status transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_model_delete_forbidden
BEFORE DELETE ON cleaning_model_artifacts
BEGIN SELECT RAISE(ABORT, 'model artifacts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_policy_update_forbidden
BEFORE UPDATE ON cleaning_threshold_policies
BEGIN SELECT RAISE(ABORT, 'threshold policies are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_policy_delete_forbidden
BEFORE DELETE ON cleaning_threshold_policies
BEGIN SELECT RAISE(ABORT, 'threshold policies are immutable'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_inference_identity_update_forbidden
BEFORE UPDATE OF inference_run_id, model_id, policy_id, target_batch_id,
                 input_manifest_sha256, expected_prediction_count,
                 fit_called, created_at_utc
ON cleaning_inference_runs
BEGIN SELECT RAISE(ABORT, 'inference run identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_inference_terminal_update_forbidden
BEFORE UPDATE ON cleaning_inference_runs
WHEN OLD.status IN ('completed', 'failed')
BEGIN SELECT RAISE(ABORT, 'terminal inference runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_inference_delete_forbidden
BEFORE DELETE ON cleaning_inference_runs
BEGIN SELECT RAISE(ABORT, 'inference runs are immutable'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_prediction_update_forbidden
BEFORE UPDATE ON cleaning_predictions
BEGIN SELECT RAISE(ABORT, 'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_prediction_delete_forbidden
BEFORE DELETE ON cleaning_predictions
BEGIN SELECT RAISE(ABORT, 'predictions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_manual_evidence_update_forbidden
BEFORE UPDATE ON cleaning_manual_evidence
BEGIN SELECT RAISE(ABORT, 'manual evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_manual_evidence_delete_forbidden
BEFORE DELETE ON cleaning_manual_evidence
BEGIN SELECT RAISE(ABORT, 'manual evidence is immutable'); END;

CREATE TRIGGER IF NOT EXISTS cleaning_final_decision_update_forbidden
BEFORE UPDATE ON cleaning_final_decisions
BEGIN SELECT RAISE(ABORT, 'final decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cleaning_final_decision_delete_forbidden
BEFORE DELETE ON cleaning_final_decisions
BEGIN SELECT RAISE(ABORT, 'final decisions are immutable'); END;
"""


def migrate_formal_schema(connection: sqlite3.Connection) -> None:
    """建立正式框架 schema，并强制启用引用完整性。

    Args:
        connection: 指向独立派生 SQLite 的可写连接。调用时不得处于事务中。

    Raises:
        sqlite3.IntegrityError: 连接已有其他 schema 修订、外键无法启用，或调用
            时已有未提交事务。旧原型必须归档后重建，不做就地改写。
    """

    if connection.in_transaction:
        raise sqlite3.IntegrityError("formal_schema_requires_transaction_boundary")
    connection.execute("PRAGMA foreign_keys = ON")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise sqlite3.IntegrityError("formal_schema_foreign_keys_required")
    connection.execute("PRAGMA trusted_schema = OFF")
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
            "VALUES ('cleaning_formal', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (FORMAL_SCHEMA_REVISION,),
        )
    connection.commit()
