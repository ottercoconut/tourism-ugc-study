"""图片人工复核 schema 的迁移、封存与追加式约束测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import tourism_ugc_study.cleaning.schema as schema_module
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived


_REVIEW_TABLES = {
    "image_review_runs",
    "image_review_members",
    "image_phash_review_groups",
    "image_phash_review_group_members",
    "image_double_label_plans",
    "image_double_label_plan_members",
    "image_annotation_imports",
    "image_review_annotations",
    "image_review_adjudications",
    "image_agreement_evaluations",
    "image_agreement_evaluation_annotations",
    "image_decision_builds",
    "image_decisions",
    "image_decision_evidence_links",
    "image_sha_propagation_runs",
    "image_sha_propagation_members",
    "image_keep_audit_rounds",
    "image_keep_audit_members",
    "image_keep_audit_annotations",
    "image_keep_audit_evaluations",
    "image_keep_audit_evaluation_annotations",
}


def test_review_schema_fresh_and_idempotent(tmp_path: Path) -> None:
    """fresh 数据库应一次到当前版本，重复迁移不得重建或遗漏复核表。"""

    with connect_derived(tmp_path / "fresh.sqlite") as connection:
        migrate_derived(connection)
        migrate_derived(connection)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert _REVIEW_TABLES <= tables
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 20
        assert list(connection.execute("PRAGMA foreign_key_check")) == []


def test_existing_v15_database_upgrades_and_preserves_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 v15 形态升级后须保留旧运行，并可重复执行 v16 迁移。"""

    database = tmp_path / "upgrade-v15.sqlite"
    original = schema_module._SCHEMA_V16
    original_v17 = schema_module._SCHEMA_V17
    original_v18 = schema_module._SCHEMA_V18
    original_v19 = schema_module._SCHEMA_V19
    original_v20 = schema_module._SCHEMA_V20
    monkeypatch.setattr(schema_module, "_SCHEMA_V16", "")
    monkeypatch.setattr(schema_module, "_SCHEMA_V17", "")
    monkeypatch.setattr(schema_module, "_SCHEMA_V18", "")
    monkeypatch.setattr(schema_module, "_SCHEMA_V19", "")
    monkeypatch.setattr(schema_module, "_SCHEMA_V20", "")
    with connect_derived(database) as connection:
        migrate_derived(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        connection.execute("DELETE FROM schema_migrations WHERE version = 17")
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        connection.execute(
            """
            INSERT INTO cleaning_runs(
                run_id, protocol_version, config_sha256, random_seed, status,
                code_version, environment_json, created_at_utc, updated_at_utc
            ) VALUES ('legacy-v15', '2.4', ?, 20260728, 'planned',
                      'test', '{}', '2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z')
            """,
            ("a" * 64,),
        )
        connection.commit()

    monkeypatch.setattr(schema_module, "_SCHEMA_V16", original)
    monkeypatch.setattr(schema_module, "_SCHEMA_V17", original_v17)
    monkeypatch.setattr(schema_module, "_SCHEMA_V18", original_v18)
    monkeypatch.setattr(schema_module, "_SCHEMA_V19", original_v19)
    monkeypatch.setattr(schema_module, "_SCHEMA_V20", original_v20)
    with connect_derived(database) as connection:
        migrate_derived(connection)
        migrate_derived(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM cleaning_runs WHERE run_id = 'legacy-v15'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 16"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 17"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 18"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 19"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 20"
        ).fetchone()[0] == 1
        assert list(connection.execute("PRAGMA foreign_key_check")) == []


def test_v19_derived_evaluations_upgrade_as_untrusted_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧版单行派生结论只保留审计，不得自动取得 v20 放行资格。"""

    database = tmp_path / "upgrade-v19-evaluations.sqlite"
    original_v20 = schema_module._SCHEMA_V20
    monkeypatch.setattr(schema_module, "_SCHEMA_V20", "")
    with connect_derived(database) as connection:
        migrate_derived(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        connection.commit()
        # 仅构造 v19 派生表的历史形态；v20 迁移的职责是保留且降级这些单行
        # 结论，不在迁移时猜测或补造其已缺失的底层人工证据。
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO image_agreement_evaluations(
              evaluation_id, review_run_id, plan_manifest_sha256,
              annotation_manifest_sha256, planned_pair_count,
              complete_pair_count, agreement_count, raw_agreement, cohen_kappa,
              kappa_status, evaluation_status, label_disagreements_json,
              created_at_utc
            ) VALUES ('legacy-passed', 'legacy-run', ?, ?, 1, 1, 1, 1.0,
                      NULL, 'undefined_single_category', 'passed', '[]',
                      '2026-07-31T00:00:00Z')
            """,
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO image_keep_audit_evaluations(
              audit_evaluation_id, audit_round_id, completed_count,
              primary_event_count, supplement_event_count,
              primary_point_estimate, one_sided_upper, evaluation_status,
              reason_code, evidence_manifest_sha256, created_at_utc
            ) VALUES ('legacy-failed', 'legacy-audit', 1, 1, 0, 1.0, 1.0,
                      'failed', 'residual_technical_noise_detected', ?,
                      '2026-07-31T00:00:00Z')
            """,
            ("c" * 64,),
        )
        connection.commit()
        monkeypatch.setattr(schema_module, "_SCHEMA_V20", original_v20)
        migrate_derived(connection)
        assert connection.execute(
            """
            SELECT seal_status FROM image_agreement_evaluations
            WHERE evaluation_id = 'legacy-passed'
            """
        ).fetchone()[0] == "untrusted_legacy"
        assert connection.execute(
            """
            SELECT seal_status FROM image_keep_audit_evaluations
            WHERE audit_evaluation_id = 'legacy-failed'
            """
        ).fetchone()[0] == "untrusted_legacy"


def test_review_evidence_tables_have_update_and_delete_guards(tmp_path: Path) -> None:
    """人工证据和决策结果必须由 SQLite 层保证不可覆盖与不可删除。"""

    guarded_tables = {
        "image_review_members",
        "image_phash_review_groups",
        "image_phash_review_group_members",
        "image_double_label_plan_members",
        "image_annotation_imports",
        "image_review_annotations",
        "image_review_adjudications",
        "image_agreement_evaluation_annotations",
        "image_decisions",
        "image_decision_evidence_links",
        "image_sha_propagation_members",
        "image_keep_audit_members",
        "image_keep_audit_annotations",
        "image_keep_audit_evaluation_annotations",
    }
    with connect_derived(tmp_path / "guards.sqlite") as connection:
        migrate_derived(connection)
        update_guards = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT tbl_name FROM sqlite_schema
                WHERE type = 'trigger' AND sql LIKE '%BEFORE UPDATE ON%'
                """
            )
        }
        delete_guards = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT tbl_name FROM sqlite_schema
                WHERE type = 'trigger' AND sql LIKE '%BEFORE DELETE ON%'
                """
            )
        }
        assert guarded_tables <= update_guards
        assert guarded_tables <= delete_guards
        evaluation_triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema WHERE type = 'trigger'
                  AND name IN (
                    'freeze_image_agreement_status',
                    'immutable_image_agreements_update',
                    'freeze_keep_audit_evaluation_status',
                    'immutable_audit_evaluations_update'
                  )
                """
            )
        }
        assert evaluation_triggers == {
            "freeze_image_agreement_status",
            "immutable_image_agreements_update",
            "freeze_keep_audit_evaluation_status",
            "immutable_audit_evaluations_update",
        }

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_review_runs(
                    review_run_id, candidate_build_id, review_kind, guide_version,
                    config_sha256, random_seed, code_version, planned_count,
                    member_count, member_manifest_sha256, seal_status, created_at_utc
                ) VALUES ('bad', 'missing', 'pilot', 'image-noise-v1.0', ?, 1,
                          'test', 0, 0, ?, 'building', '2026-07-31T00:00:00Z')
                """,
                ("b" * 64, "c" * 64),
            )
