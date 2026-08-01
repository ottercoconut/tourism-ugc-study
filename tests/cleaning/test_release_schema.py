"""Issue #11 最终决定、审计和发布 schema 的数据库层防绕过测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import tourism_ugc_study.cleaning.schema as schema_module
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_NOW = "2026-08-01T00:00:00+00:00"


def _seed_frozen_post_snapshot(connection: sqlite3.Connection) -> None:
    """建立一个帖子的一致冻结快照，供决定和审计反例复用。"""

    connection.execute(
        """
        INSERT INTO cleaning_runs(
          run_id, protocol_version, config_sha256, random_seed, status,
          reason_code, code_version, environment_json, created_at_utc, updated_at_utc
        ) VALUES ('run-1', '2.4', ?, 17, 'paused', 'awaiting_quality_gate',
                  'git-test', '{}', ?, ?)
        """,
        (_HASH_A, _NOW, _NOW),
    )
    connection.execute(
        """
        INSERT INTO source_snapshots(
          snapshot_id, run_id, source_path, source_identity_sha256,
          source_sha256_before, source_sha256_after, source_size_bytes,
          snapshot_path, snapshot_sha256, snapshot_size_bytes, post_count,
          image_count, table_counts_json, object_manifest_sha256,
          input_contract_status, input_contract_method, input_contract_reason_code,
          input_contract_details_json, manifest_path, created_at_utc,
          created_at_asia_shanghai, code_version, environment_json
        ) VALUES (
          'snapshot-1', 'run-1', 'source.sqlite', ?, ?, ?, 1,
          'snapshot.sqlite', ?, 1, 1, 0, '{"posts":1}', ?,
          'accepted', 'schema_attestation', NULL, '{}', 'snapshot.json', ?, ?,
          'git-test', '{}'
        )
        """,
        (_HASH_A, _HASH_A, _HASH_A, _HASH_A, _HASH_A, _NOW, _NOW),
    )
    connection.execute(
        "UPDATE cleaning_runs SET source_snapshot_id = 'snapshot-1' WHERE run_id = 'run-1'"
    )
    connection.execute(
        """
        INSERT INTO source_post_inventory(
          source_post_id, platform_key, first_seen_snapshot_id, last_seen_snapshot_id,
          missing_since_snapshot_id, is_present, current_source_version,
          current_text_sha256, current_author_sha256, current_analysis_sha256,
          captured_at_sort, updated_at_utc, current_author_identity_present
        ) VALUES (1, 'xiaohongshu', 'snapshot-1', 'snapshot-1', NULL, 1, 1,
                  ?, ?, ?, ?, ?, 0)
        """,
        (_HASH_A, _HASH_B, _HASH_C, _NOW, _NOW),
    )
    connection.execute(
        """
        INSERT INTO source_post_versions(
          source_post_id, source_version, effective_snapshot_id, text_sha256,
          author_sha256, analysis_sha256, created_at_utc, author_identity_present
        ) VALUES (1, 1, 'snapshot-1', ?, ?, ?, ?, 0)
        """,
        (_HASH_A, _HASH_B, _HASH_C, _NOW),
    )
    connection.execute(
        """
        INSERT INTO source_post_observations(
          snapshot_id, source_post_id, source_version, change_kind,
          changed_axes_json, observed_at_utc
        ) VALUES ('snapshot-1', 1, 1, 'new', '["text"]', ?)
        """,
        (_NOW,),
    )
    connection.execute(
        """
        INSERT INTO text_annotation_imports(
          import_id, record_kind, guide_version, source_sha256, row_count,
          imported_by_hash, created_at_utc
        ) VALUES ('import-1', 'post_adjudication', 'guide-1', ?, 1, ?, ?)
        """,
        (_HASH_A, _HASH_B, _NOW),
    )
    connection.execute(
        """
        INSERT INTO text_post_adjudications(
          adjudication_id, import_id, sample_run_id, source_post_id, source_version,
          adjudicator_hash, structure_label, tourism_label, reason_codes_json,
          evidence_annotation_ids_json, decision_context, guide_version,
          adjudicated_at_utc, created_at_utc, model_run_id
        ) VALUES ('adj-1', 'import-1', NULL, 1, 1, ?, 'usable', 'related',
                  '["human_confirmed"]', '[]', 'manual_review', 'guide-1', ?, ?, NULL)
        """,
        (_HASH_C, _NOW, _NOW),
    )


def _seal_keep_candidate(connection: sqlite3.Connection) -> None:
    """写入并封存一个由人工仲裁支持的 keep 候选决定。"""

    connection.execute(
        """
        INSERT INTO post_decision_builds(
          decision_build_id, run_id, source_snapshot_id, build_kind,
          source_candidate_decision_build_id, text_keep_audit_evaluation_id,
          decision_version, guide_version, rules_sha256, input_manifest_sha256,
          expected_post_count, keep_count, review_count, exclude_count,
          decision_manifest_sha256, seal_status, created_at_utc
        ) VALUES ('candidate-1', 'run-1', 'snapshot-1', 'candidate', NULL, NULL,
                  'decision-v1', 'guide-1', ?, ?, 1, 1, 0, 0, ?, 'building', ?)
        """,
        (_HASH_A, _HASH_B, _HASH_C, _NOW),
    )
    connection.execute(
        """
        INSERT INTO post_decisions(
          decision_id, decision_build_id, source_post_id, source_version,
          structure_label, tourism_label, decision_action, reason_code,
          provenance, model_run_id, evidence_manifest_sha256,
          decision_sha256, created_at_utc
        ) VALUES ('decision-1', 'candidate-1', 1, 1, 'usable', 'related', 'keep',
                  'human_related', 'human_adjudication', NULL, ?, ?, ?)
        """,
        (_HASH_A, _HASH_B, _NOW),
    )
    connection.execute(
        """
        INSERT INTO post_decision_evidence_links(
          decision_id, evidence_kind, evidence_id, source_post_id, source_version
        ) VALUES ('decision-1', 'human_adjudication', 'adj-1', 1, 1)
        """
    )
    connection.execute(
        "UPDATE post_decision_builds SET seal_status = 'finalized' "
        "WHERE decision_build_id = 'candidate-1'"
    )


def test_v24_fresh_migration_is_idempotent_and_exposes_run_scoped_outputs(
    tmp_path: Path,
) -> None:
    """全新库与重复迁移都停在 v24，输出不含商业清洗字段。"""

    with connect_derived(tmp_path / "fresh.sqlite") as connection:
        migrate_derived(connection)
        before = connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        migrate_derived(connection)

        assert tuple(
            connection.execute(
                "SELECT MAX(version), COUNT(*) FROM schema_migrations"
            ).fetchone()
        ) == (24, 24)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0] == before

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert {
            "post_decision_builds",
            "post_decisions",
            "post_decision_evidence_links",
            "text_dedup_builds",
            "text_dedup_edges",
            "text_dedup_clusters",
            "text_dedup_members",
            "text_keep_audit_rounds",
            "text_keep_audit_population_members",
            "text_keep_audit_members",
            "text_keep_audit_annotations",
            "text_keep_audit_evaluations",
            "analysis_release_builds",
            "analysis_posts_eligible",
            "analysis_posts_deduplicated",
            "analysis_images_eligible",
            "analysis_images_evidence_only",
        } <= tables

        decision_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(post_decisions)")
        }
        assert "commercial_label" not in decision_columns
        for table in (
            "analysis_posts_eligible",
            "analysis_posts_deduplicated",
            "analysis_images_eligible",
            "analysis_images_evidence_only",
        ):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert {"release_id", "run_id"} <= columns
        for table in ("analysis_images_eligible", "analysis_images_evidence_only"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert {"source_image_version", "source_post_version"} <= columns


def test_v23_upgrade_preserves_existing_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """v23 派生行原样保留，v24 只追加新对象并替换阶段性视图。"""

    path = tmp_path / "upgrade.sqlite"
    original_v24 = schema_module._SCHEMA_V24
    monkeypatch.setattr(schema_module, "_SCHEMA_V24", "")
    with connect_derived(path) as connection:
        migrate_derived(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version = 24")
        connection.execute(
            """
            INSERT INTO cleaning_runs(
              run_id, protocol_version, config_sha256, random_seed, status,
              code_version, environment_json, created_at_utc, updated_at_utc
            ) VALUES ('legacy-v23', '2.4', ?, 1, 'planned', 'old', '{}', ?, ?)
            """,
            (_HASH_A, _NOW, _NOW),
        )
        connection.commit()

    monkeypatch.setattr(schema_module, "_SCHEMA_V24", original_v24)
    with connect_derived(path) as connection:
        migrate_derived(connection)
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'legacy-v23'"
        ).fetchone()[0] == "planned"
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 24"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'view' AND name = 'text_ready'"
        ).fetchone()[0] == 1


def test_post_decision_seal_is_counted_and_text_ready_is_explicitly_provisional(
    tmp_path: Path,
) -> None:
    """帖子封存重算子行，阶段性视图只读取已封存 keep 决定。"""

    with connect_derived(tmp_path / "post.sqlite") as connection:
        migrate_derived(connection)
        _seed_frozen_post_snapshot(connection)

        connection.execute(
            """
            INSERT INTO post_decision_builds(
              decision_build_id, run_id, source_snapshot_id, build_kind,
              decision_version, guide_version, rules_sha256, input_manifest_sha256,
              expected_post_count, keep_count, review_count, exclude_count,
              decision_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('incomplete', 'run-1', 'snapshot-1', 'candidate',
                      'incomplete-v1', 'guide-1', ?, ?, 1, 1, 0, 0, ?, 'building', ?)
            """,
            (_HASH_A, _HASH_A, _HASH_A, _NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="rows or evidence"):
            connection.execute(
                "UPDATE post_decision_builds SET seal_status = 'finalized' "
                "WHERE decision_build_id = 'incomplete'"
            )

        _seal_keep_candidate(connection)
        row = connection.execute(
            """
            SELECT run_id, decision_build_id, source_post_id, source_version,
                   is_text_ready, is_provisional
            FROM text_ready WHERE decision_build_id = 'candidate-1'
            """
        ).fetchone()
        assert tuple(row) == ("run-1", "candidate-1", 1, 1, 1, 1)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE post_decisions SET reason_code = 'rewritten' "
                "WHERE decision_id = 'decision-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO post_decision_evidence_links VALUES "
                "('decision-1', 'human_annotation', 'missing', 1, 1)"
            )


def test_dual_axis_dedup_and_acceptance_checks_reject_direct_sql(tmp_path: Path) -> None:
    """CHECK 与 trigger 阻止非法双轴、无仲裁去重边和无发布 accepted。"""

    with connect_derived(tmp_path / "guards.sqlite") as connection:
        migrate_derived(connection)
        _seed_frozen_post_snapshot(connection)
        connection.execute(
            """
            INSERT INTO post_decision_builds(
              decision_build_id, run_id, source_snapshot_id, build_kind,
              decision_version, guide_version, rules_sha256, input_manifest_sha256,
              expected_post_count, keep_count, review_count, exclude_count,
              decision_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('labels', 'run-1', 'snapshot-1', 'candidate',
                      'labels-v1', 'guide-1', ?, ?, 1, 0, 1, 0, ?, 'building', ?)
            """,
            (_HASH_A, _HASH_B, _HASH_C, _NOW),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO post_decisions(
                  decision_id, decision_build_id, source_post_id, source_version,
                  structure_label, tourism_label, decision_action, reason_code,
                  provenance, model_run_id, evidence_manifest_sha256,
                  decision_sha256, created_at_utc
                ) VALUES ('bad-label', 'labels', 1, 1, 'invalid', 'related', 'review',
                          'bad', 'insufficient_evidence', NULL, ?, ?, ?)
                """,
                (_HASH_A, _HASH_B, _NOW),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO text_dedup_edges(
                  edge_id, dedup_build_id, left_source_post_id, left_source_version,
                  right_source_post_id, right_source_version, relation_kind,
                  exact_cluster_id, adjudication_id, reason_code, edge_sha256
                ) VALUES ('bad-edge', 'missing', 1, 1, 2, 1, 'human_duplicate',
                          NULL, NULL, 'no_human_evidence', ?)
                """,
                (_HASH_A,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="qualified finalized formal release"):
            connection.execute(
                "UPDATE cleaning_runs SET status = 'accepted' WHERE run_id = 'run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot start accepted"):
            connection.execute(
                """
                INSERT INTO cleaning_runs(
                  run_id, protocol_version, config_sha256, random_seed, status,
                  code_version, environment_json, created_at_utc, updated_at_utc
                ) VALUES ('accepted-direct', '2.4', ?, 1, 'accepted', 'x', '{}', ?, ?)
                """,
                (_HASH_A, _NOW, _NOW),
            )


def test_text_keep_audit_recomputes_events_and_freezes_population(tmp_path: Path) -> None:
    """审计封存后人口不可改，自报 passed 与人工事件不符时拒绝封存。"""

    with connect_derived(tmp_path / "audit.sqlite") as connection:
        migrate_derived(connection)
        _seed_frozen_post_snapshot(connection)
        _seal_keep_candidate(connection)
        connection.execute(
            """
            INSERT INTO text_keep_audit_rounds(
              audit_round_id, run_id, candidate_decision_build_id, audit_mode,
              round_number, random_seed, sampling_method, estimator,
              population_count, population_manifest_sha256, sample_count,
              sample_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('audit-1', 'run-1', 'candidate-1', 'smoke', 1, 17,
                      'census', 'census', 1, ?, 1, ?, 'building', ?)
            """,
            (_HASH_A, _HASH_B, _NOW),
        )
        connection.execute(
            "INSERT INTO text_keep_audit_population_members VALUES "
            "('audit-1', 1, 1, 'xiaohongshu')"
        )
        connection.execute(
            "INSERT INTO text_keep_audit_members VALUES "
            "('audit-1', 1, 1, 'xiaohongshu', 1, 1.0, 1.0)"
        )
        connection.execute(
            "UPDATE text_keep_audit_rounds SET seal_status = 'finalized' "
            "WHERE audit_round_id = 'audit-1'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="population is immutable"):
            connection.execute(
                "UPDATE text_keep_audit_population_members SET platform_key = 'other' "
                "WHERE audit_round_id = 'audit-1'"
            )

        connection.execute(
            """
            INSERT INTO text_keep_audit_annotations(
              audit_annotation_id, audit_round_id, source_post_id, source_version,
              annotator_hash, guide_version, structure_label, tourism_label,
              reason_codes_json, row_sha256, annotated_at_utc
            ) VALUES ('audit-ann-1', 'audit-1', 1, 1, ?, 'guide-1', 'usable',
                      'unrelated', '["not_tourism"]', ?, ?)
            """,
            (_HASH_A, _HASH_B, _NOW),
        )
        connection.execute(
            """
            INSERT INTO text_keep_audit_evaluations(
              audit_evaluation_id, audit_round_id, completed_count, event_count,
              event_point_estimate, one_sided_upper, evaluation_status,
              reason_code, evidence_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('evaluation-1', 'audit-1', 1, 0, 0.0, 0.0, 'passed',
                      'caller_claimed_pass', ?, 'building', ?)
            """,
            (_HASH_C, _NOW),
        )
        connection.execute(
            "INSERT INTO text_keep_audit_evaluation_evidence_links VALUES "
            "('evaluation-1', 'audit-ann-1')"
        )
        connection.execute(
            """
            INSERT INTO text_keep_audit_platform_evaluations(
              audit_evaluation_id, platform_key, population_count, sample_count,
              completed_count, event_count, event_point_estimate
            ) VALUES ('evaluation-1', 'xiaohongshu', 1, 1, 1, 0, 0.0)
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="does not match evidence"):
            connection.execute(
                "UPDATE text_keep_audit_evaluations SET seal_status = 'finalized' "
                "WHERE audit_evaluation_id = 'evaluation-1'"
            )


def test_release_parent_rejects_candidate_post_build_before_foreign_key_bypass(
    tmp_path: Path,
) -> None:
    """发布 parent 在 INSERT 即拒绝 candidate，不能等应用层自行判断。"""

    with connect_derived(tmp_path / "release-candidate.sqlite") as connection:
        migrate_derived(connection)
        _seed_frozen_post_snapshot(connection)
        _seal_keep_candidate(connection)
        with pytest.raises(sqlite3.IntegrityError, match="run identity is invalid"):
            connection.execute(
                """
                INSERT INTO analysis_release_builds(
                  release_id, run_id, source_snapshot_id, release_mode,
                  post_decision_build_id, text_dedup_build_id,
                  text_keep_audit_evaluation_id, image_decision_build_id,
                  image_keep_audit_evaluation_id, protocol_version, schema_version,
                  config_sha256, code_version, request_manifest_sha256,
                  posts_eligible_count, posts_deduplicated_count,
                  images_eligible_count, images_evidence_only_count,
                  release_manifest_sha256, seal_status, created_at_utc
                ) VALUES ('release-1', 'run-1', 'snapshot-1', 'smoke', 'candidate-1',
                          'missing-dedup', 'missing-text-audit', 'missing-image-decisions',
                          'missing-image-audit', '2.4', 24, ?, 'git-test', ?,
                          1, 1, 0, 0, ?, 'building', ?)
                """,
                (_HASH_A, _HASH_B, _HASH_C, _NOW),
            )
