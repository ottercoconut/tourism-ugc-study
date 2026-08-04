"""Issue #11 最终决定、审计和发布 schema 的数据库层防绕过测试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

import tourism_ugc_study.cleaning.schema as schema_module
from tourism_ugc_study.cleaning.image_repository import (
    build_image_candidates,
    import_image_manifest,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.schema import (
    ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION,
    DERIVED_SCHEMA_VERSION,
    connect_derived,
    migrate_derived,
)
from tests.cleaning.test_image_repository import (
    _prepared_run,
    _route_map,
    _row,
    _sha,
    _write_manifest,
)
from tests.cleaning.test_analysis_release_repository import (
    _append_exact_content_member,
    _seed_release_fixture,
)


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_NOW = "2026-08-01T00:00:00+00:00"


def _canonical_list_sha256(values: list[str]) -> str:
    """按生产仓储相同的规范 JSON 规则计算字符串列表摘要。"""

    payload = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_release_acceptance_guard_is_private_and_defaults_to_deny(tmp_path: Path) -> None:
    """schema 只注册可变参数恒假 guard，不能用公开 helper 伪造验收。"""

    with connect_derived(tmp_path / "guard.sqlite") as connection:
        migrate_derived(connection)
        assert connection.execute(
            "SELECT release_acceptance_guard('run-1', 'release-1', ?, 'run')",
            (_HASH_A,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT release_acceptance_guard('arbitrary')"
        ).fetchone()[0] == 0
        assert not hasattr(schema_module, "arm_release_acceptance_guard")
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
            "AND name = 'analysis_release_acceptance_attestations'"
        ).fetchone() is not None
        assert DERIVED_SCHEMA_VERSION == 25
        assert ANALYSIS_RELEASE_RECORD_SCHEMA_VERSION == 24


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


def test_v25_fresh_migration_is_idempotent_and_exposes_run_scoped_outputs(
    tmp_path: Path,
) -> None:
    """全新库与重复迁移都停在 v25，输出不含商业清洗字段。"""

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
        ) == (25, 25)
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


def _restore_legacy_v24_post_decision_check(connection: sqlite3.Connection) -> None:
    """把测试库收敛为首版 v24 的帖子 CHECK，同时保留既有决定证据。

    该 helper 只构造升级夹具：首版 v24 不允许人工仲裁确认结构无效。先备份
    evidence 子行再重建父表，可验证正式 v25 迁移是否在不丢行、不破坏 FK 的
    前提下替换内联 CHECK；它不模拟应用层写入或正式研究结果。
    """

    connection.executescript(
        """
        CREATE TEMP TABLE legacy_decisions AS SELECT * FROM post_decisions;
        CREATE TEMP TABLE legacy_links AS SELECT * FROM post_decision_evidence_links;
        DROP TABLE post_decision_evidence_links;
        DROP TABLE post_decisions;
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
          decision_action TEXT NOT NULL CHECK (
            decision_action IN ('keep', 'review', 'exclude')
          ),
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
            OR (structure_label IN ('usable', 'uncertain')
                AND tourism_label IN ('related', 'unrelated', 'uncertain'))
          ),
          CHECK (
            (provenance = 'deterministic_invalid'
             AND structure_label = 'invalid' AND tourism_label = 'not_applicable'
             AND decision_action = 'exclude' AND model_run_id IS NULL)
            OR (provenance = 'human_adjudication'
                AND structure_label = 'usable'
                AND tourism_label IN ('related', 'unrelated')
                AND decision_action = CASE tourism_label
                    WHEN 'related' THEN 'keep' ELSE 'exclude' END
                AND model_run_id IS NULL)
            OR (provenance = 'model_low_risk'
                AND structure_label = 'usable' AND tourism_label = 'related'
                AND decision_action IN ('keep', 'review') AND model_run_id IS NOT NULL)
            OR (provenance = 'model_review_candidate'
                AND structure_label = 'usable' AND tourism_label = 'uncertain'
                AND decision_action = 'review' AND model_run_id IS NOT NULL)
            OR (provenance IN ('insufficient_evidence', 'evidence_conflict')
                AND decision_action = 'review' AND model_run_id IS NULL)
          )
        );
        INSERT INTO post_decisions SELECT * FROM legacy_decisions;
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
        INSERT INTO post_decision_evidence_links SELECT * FROM legacy_links;
        DROP TABLE legacy_decisions;
        DROP TABLE legacy_links;
        """
    )


def test_v24_to_v25_upgrade_preserves_rows_and_enables_human_invalid(
    tmp_path: Path,
) -> None:
    """真实 v24 记录升级后保留决定/FK，并允许人工确认结构无效。"""

    path = tmp_path / "v24-to-v25.sqlite"
    with connect_derived(path) as connection:
        migrate_derived(connection)
        _seed_frozen_post_snapshot(connection)
        _seal_keep_candidate(connection)
        _restore_legacy_v24_post_decision_check(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.execute("DROP TABLE analysis_release_acceptance_attestations")
        connection.commit()

    with connect_derived(path) as connection:
        migrate_derived(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM post_decisions WHERE decision_id = 'decision-1'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM post_decision_evidence_links "
            "WHERE decision_id = 'decision-1'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 25
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
            "AND name = 'analysis_release_acceptance_attestations'"
        ).fetchone() is not None

        connection.execute(
            """
            INSERT INTO post_decision_builds(
              decision_build_id, run_id, source_snapshot_id, build_kind,
              decision_version, guide_version, rules_sha256, input_manifest_sha256,
              expected_post_count, keep_count, review_count, exclude_count,
              decision_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('human-invalid-build', 'run-1', 'snapshot-1', 'candidate',
                      'decision-v2', 'guide-1', ?, ?, 1, 0, 0, 1, ?, 'building', ?)
            """,
            (_HASH_A, _HASH_C, _HASH_B, _NOW),
        )
        connection.execute(
            """
            INSERT INTO post_decisions(
              decision_id, decision_build_id, source_post_id, source_version,
              structure_label, tourism_label, decision_action, reason_code,
              provenance, model_run_id, evidence_manifest_sha256,
              decision_sha256, created_at_utc
            ) VALUES ('human-invalid-decision', 'human-invalid-build', 1, 1,
                      'invalid', 'not_applicable', 'exclude', 'human_invalid',
                      'human_adjudication', NULL, ?, ?, ?)
            """,
            (_HASH_A, _HASH_B, _NOW),
        )


def test_image_decision_subset_cannot_be_sealed_by_lying_about_expected_count(
    tmp_path: Path,
) -> None:
    """直接 SQL 只写 fingerprint 子集时封存失败，building 行原样保留。"""

    derived, image_root, config, snapshot = _prepared_run(tmp_path, "image-subset")
    first = image_root / "first.png"
    second = image_root / "second.png"
    Image.new("RGB", (96, 96), (20, 40, 60)).save(first)
    _route_map(second)
    manifest_path = tmp_path / "image-subset.csv"
    _write_manifest(
        manifest_path,
        [
            _row(1, "content", first.name, _sha(first)),
            _row(2, "content", second.name, _sha(second)),
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id="image-subset",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=image_root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=image_root,
        config=config,
    )
    candidate = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )

    with connect_derived(derived) as connection:
        fingerprints = [
            str(row[0])
            for row in connection.execute(
                "SELECT fingerprint_id FROM image_candidate_build_members "
                "WHERE build_id = ? ORDER BY fingerprint_id",
                (candidate.build_id,),
            )
        ]
        assert len(fingerprints) == 2
        with pytest.raises(sqlite3.IntegrityError, match="rows or evidence"):
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_decision_builds(
                      decision_build_id, candidate_build_id, guide_version,
                      evidence_manifest_sha256, expected_decision_count,
                      decision_manifest_sha256, seal_status, created_at_utc
                    ) VALUES ('subset-decisions', ?, ?, ?, 1, ?, 'building', ?)
                    """,
                    (
                        candidate.build_id,
                        config.image_label_guide_version,
                        _HASH_A,
                        _HASH_B,
                        _NOW,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO image_decisions(
                      decision_id, decision_build_id, fingerprint_id,
                      technical_noise_label, decision_action, provenance,
                      evidence_id, decision_sha256, created_at_utc
                    ) VALUES ('subset-decision', 'subset-decisions', ?, NULL, 'keep',
                              'default_keep_no_candidate', NULL, ?, ?)
                    """,
                    (fingerprints[0], _HASH_C, _NOW),
                )
                connection.execute(
                    "UPDATE image_decision_builds SET seal_status = 'finalized' "
                    "WHERE decision_build_id = 'subset-decisions'"
                )

        # 调用者把构建、子行和封存放在同一事务时，BEFORE trigger 失败会整体
        # 回滚，不留下可被后续流程误认的 building 草稿或孤立决定。
        assert connection.execute(
            "SELECT COUNT(*) FROM image_decision_builds "
            "WHERE decision_build_id = 'subset-decisions'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM image_decisions "
            "WHERE decision_build_id = 'subset-decisions'"
        ).fetchone()[0] == 0


def _prepare_two_member_sha_cluster(
    tmp_path: Path,
) -> tuple[Path, str, str, str, str]:
    """建立两成员精确簇，只旁路与本项无关的决定/来源前置门。"""

    derived, image_root, config, snapshot = _prepared_run(tmp_path, "sha-subset")
    duplicate = image_root / "duplicate.png"
    Image.new("RGB", (96, 96), (20, 40, 60)).save(duplicate)
    manifest_path = tmp_path / "sha-subset.csv"
    _write_manifest(
        manifest_path,
        [
            _row(1, "content", duplicate.name, _sha(duplicate)),
            _row(2, "content", duplicate.name, _sha(duplicate)),
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id="sha-subset",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=image_root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=image_root,
        config=config,
    )
    candidate = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )
    with connect_derived(derived) as connection:
        cluster = connection.execute(
            """
            SELECT cluster_id, representative_fingerprint_id, member_count
            FROM image_exact_clusters
            WHERE build_id = ?
            """,
            (candidate.build_id,),
        ).fetchone()
        assert cluster is not None and cluster["member_count"] == 2
        fingerprints = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT fingerprint_id FROM image_exact_cluster_members
                WHERE build_id = ? AND cluster_id = ? ORDER BY fingerprint_id
                """,
                (candidate.build_id, cluster["cluster_id"]),
            )
        ]

        # 本测试只验证 SHA 父对象封存。先移除决定的人工复核前置门，构造一个
        # 完整且已封存的两成员决定父对象；v25 的 SHA seal trigger 始终保留。
        connection.execute(
            "DROP TRIGGER validate_image_formal_review_evaluations_trusted"
        )
        connection.execute("DROP TRIGGER validate_image_formal_review_gate")
        connection.execute("DROP TRIGGER validate_image_decision_seal")
        connection.execute("DROP TRIGGER require_sha_propagation_building_insert")
        connection.execute(
            """
            INSERT INTO image_decision_builds(
              decision_build_id, candidate_build_id, guide_version,
              evidence_manifest_sha256, expected_decision_count,
              decision_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('image-decisions', ?, ?, ?, 2, ?, 'building', ?)
            """,
            (
                candidate.build_id,
                config.image_label_guide_version,
                _HASH_A,
                _HASH_B,
                _NOW,
            ),
        )
        decision_by_fingerprint: dict[str, str] = {}
        for index, fingerprint_id in enumerate(fingerprints, start=1):
            decision_id = f"image-decision-{index}"
            connection.execute(
                """
                INSERT INTO image_decisions(
                  decision_id, decision_build_id, fingerprint_id,
                  technical_noise_label, decision_action, provenance,
                  evidence_id, decision_sha256, created_at_utc
                ) VALUES (?, 'image-decisions', ?, NULL, 'keep',
                          'default_keep_no_candidate', NULL, ?, ?)
                """,
                (decision_id, fingerprint_id, _HASH_C, _NOW),
            )
            decision_by_fingerprint[fingerprint_id] = decision_id
        connection.execute(
            "UPDATE image_decision_builds SET seal_status = 'finalized' "
            "WHERE decision_build_id = 'image-decisions'"
        )
        connection.commit()
        representative_fingerprint_id = str(cluster["representative_fingerprint_id"])
        representative_decision_id = decision_by_fingerprint[
            representative_fingerprint_id
        ]
    return (
        derived,
        candidate.build_id,
        str(cluster["cluster_id"]),
        representative_fingerprint_id,
        representative_decision_id,
    )


def test_sha_propagation_subset_seal_fails_without_transaction_residue(
    tmp_path: Path,
) -> None:
    """两成员精确簇不得自报一成员封存，失败事务不留下父子行。"""

    (
        derived,
        candidate_build_id,
        cluster_id,
        representative_fingerprint_id,
        representative_decision_id,
    ) = _prepare_two_member_sha_cluster(tmp_path)
    one_member_manifest = _canonical_list_sha256([representative_fingerprint_id])
    with connect_derived(derived) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="rows, cluster or manifest are incomplete",
        ):
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_sha_propagation_runs(
                      propagation_run_id, decision_build_id, candidate_build_id,
                      exact_cluster_id, representative_decision_id,
                      technical_noise_label, expected_member_count,
                      member_manifest_sha256, seal_status, created_at_utc
                    ) VALUES ('subset-propagation', 'image-decisions', ?, ?, ?,
                              'site_ui', 1, ?, 'building', ?)
                    """,
                    (
                        candidate_build_id,
                        cluster_id,
                        representative_decision_id,
                        one_member_manifest,
                        _NOW,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO image_sha_propagation_members(
                      propagation_run_id, fingerprint_id,
                      propagated_label, source_decision_id
                    ) VALUES ('subset-propagation', ?, 'site_ui', ?)
                    """,
                    (representative_fingerprint_id, representative_decision_id),
                )
                connection.execute(
                    """
                    UPDATE image_sha_propagation_runs SET seal_status = 'finalized'
                    WHERE propagation_run_id = 'subset-propagation'
                    """
                )

        assert connection.execute(
            "SELECT COUNT(*) FROM image_sha_propagation_runs "
            "WHERE propagation_run_id = 'subset-propagation'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM image_sha_propagation_members "
            "WHERE propagation_run_id = 'subset-propagation'"
        ).fetchone()[0] == 0


def test_sha_propagation_full_set_rejects_default_keep_source(tmp_path: Path) -> None:
    """完整成员和 manifest 也不能把默认 keep 代表伪造成确认排除来源。"""

    (
        derived,
        candidate_build_id,
        cluster_id,
        _,
        representative_decision_id,
    ) = _prepare_two_member_sha_cluster(tmp_path)
    with connect_derived(derived) as connection:
        fingerprints = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT fingerprint_id FROM image_exact_cluster_members
                WHERE build_id = ? AND cluster_id = ? ORDER BY fingerprint_id
                """,
                (candidate_build_id, cluster_id),
            )
        ]
        assert len(fingerprints) == 2
        with pytest.raises(
            sqlite3.IntegrityError,
            match="rows, cluster or manifest are incomplete",
        ):
            with connection:
                connection.execute(
                    """
                    INSERT INTO image_sha_propagation_runs(
                      propagation_run_id, decision_build_id, candidate_build_id,
                      exact_cluster_id, representative_decision_id,
                      technical_noise_label, expected_member_count,
                      member_manifest_sha256, seal_status, created_at_utc
                    ) VALUES ('forged-keep-source', 'image-decisions', ?, ?, ?,
                              'site_ui', 2, ?, 'building', ?)
                    """,
                    (
                        candidate_build_id,
                        cluster_id,
                        representative_decision_id,
                        _canonical_list_sha256(fingerprints),
                        _NOW,
                    ),
                )
                for fingerprint_id in fingerprints:
                    connection.execute(
                        """
                        INSERT INTO image_sha_propagation_members VALUES
                        ('forged-keep-source', ?, 'site_ui', ?)
                        """,
                        (fingerprint_id, representative_decision_id),
                    )
                connection.execute(
                    """
                    UPDATE image_sha_propagation_runs SET seal_status = 'finalized'
                    WHERE propagation_run_id = 'forged-keep-source'
                    """
                )

        assert connection.execute(
            "SELECT COUNT(*) FROM image_sha_propagation_runs "
            "WHERE propagation_run_id = 'forged-keep-source'"
        ).fetchone()[0] == 0


def test_v24_upgrade_rejects_invalid_finalized_sha_propagation(
    tmp_path: Path,
) -> None:
    """旧 v24 残缺 finalized 传播阻断升级，迁移号和证据行保持原样。"""

    derived, _, _ = _seed_release_fixture(tmp_path, audit_mode="smoke")
    _append_exact_content_member(derived, own_action=None)
    with connect_derived(derived) as connection:
        # 夹具已移除 trigger，可精确复现首版 v24 曾允许的“自报 1、只写 1”。
        connection.execute(
            """
            UPDATE image_decisions
            SET technical_noise_label = 'site_ui', decision_action = 'exclude',
                provenance = 'double_agreement', evidence_id = 'human-confirmed'
            WHERE decision_id = 'image-decision'
            """
        )
        connection.execute(
            """
            INSERT INTO image_sha_propagation_runs(
              propagation_run_id, decision_build_id, candidate_build_id,
              exact_cluster_id, representative_decision_id,
              technical_noise_label, expected_member_count,
              member_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('legacy-incomplete', 'image-decisions', 'image-candidates',
                      'image-cluster', 'image-decision', 'site_ui', 1, ?,
                      'finalized', ?)
            """,
            (_canonical_list_sha256(["fingerprint-1"]), _NOW),
        )
        connection.execute(
            """
            INSERT INTO image_sha_propagation_members VALUES
            ('legacy-incomplete', 'fingerprint-1', 'site_ui', 'image-decision')
            """
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="v25_image_sha_propagation_invalid",
        ):
            migrate_derived(connection)

        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 24
        assert tuple(
            connection.execute(
                """
                SELECT seal_status, expected_member_count
                FROM image_sha_propagation_runs
                WHERE propagation_run_id = 'legacy-incomplete'
                """
            ).fetchone()
        ) == ("finalized", 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM image_sha_propagation_members "
            "WHERE propagation_run_id = 'legacy-incomplete'"
        ).fetchone()[0] == 1


def test_v24_upgrade_rejects_full_set_propagation_from_default_keep(
    tmp_path: Path,
) -> None:
    """旧库完整集合也必须有同簇人工确认排除代表，默认 keep 不得伪造。"""

    derived, _, _ = _seed_release_fixture(tmp_path, audit_mode="smoke")
    with connect_derived(derived) as connection:
        # 发布夹具已移除 trigger；把代表明确改成无标签默认保留，再构造数量、
        # 集合和 manifest 全部自洽的 finalized 传播，唯一非法点就是来源语义。
        connection.execute(
            """
            UPDATE image_decisions
            SET technical_noise_label = NULL, decision_action = 'keep',
                provenance = 'default_keep_no_candidate', evidence_id = NULL
            WHERE decision_id = 'image-decision'
            """
        )
        connection.execute(
            """
            INSERT INTO image_sha_propagation_runs(
              propagation_run_id, decision_build_id, candidate_build_id,
              exact_cluster_id, representative_decision_id,
              technical_noise_label, expected_member_count,
              member_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('legacy-forged-source', 'image-decisions',
                      'image-candidates', 'image-cluster', 'image-decision',
                      'site_ui', 1, ?, 'finalized', ?)
            """,
            (_canonical_list_sha256(["fingerprint-1"]), _NOW),
        )
        connection.execute(
            """
            INSERT INTO image_sha_propagation_members VALUES
            ('legacy-forged-source', 'fingerprint-1', 'site_ui', 'image-decision')
            """
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="v25_image_sha_propagation_invalid",
        ):
            migrate_derived(connection)

        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 24
        assert tuple(
            connection.execute(
                """
                SELECT seal_status, expected_member_count
                FROM image_sha_propagation_runs
                WHERE propagation_run_id = 'legacy-forged-source'
                """
            ).fetchone()
        ) == ("finalized", 1)
        assert connection.execute(
            "SELECT decision_action FROM image_decisions "
            "WHERE decision_id = 'image-decision'"
        ).fetchone()[0] == "keep"


def test_v24_upgrade_rejects_untrusted_accepted_run(tmp_path: Path) -> None:
    """无 v25 attestation 的旧 accepted 运行不能被迁移静默继承。"""

    path = tmp_path / "untrusted-accepted-v24.sqlite"
    with connect_derived(path) as connection:
        migrate_derived(connection)
        _seed_frozen_post_snapshot(connection)
        connection.execute("DROP TRIGGER validate_cleaning_run_acceptance")
        connection.execute(
            "UPDATE cleaning_runs SET status = 'accepted' WHERE run_id = 'run-1'"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="v25_untrusted_accepted_state",
        ):
            migrate_derived(connection)

        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 24
        assert connection.execute(
            "SELECT status FROM cleaning_runs WHERE run_id = 'run-1'"
        ).fetchone()[0] == "accepted"


def test_v24_upgrade_rejects_untrusted_accepted_release(tmp_path: Path) -> None:
    """即使旧运行未 accepted，单独的旧 accepted 发布也必须阻断升级。"""

    derived, _, _ = _seed_release_fixture(tmp_path, audit_mode="formal")
    with connect_derived(derived) as connection:
        # 上游夹具已移除发布 trigger，但保留完整的直接父对象，足以复现首版
        # v24 已落库的 accepted release；v25 不得把它当成新证明。
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
              release_manifest_sha256, seal_status, created_at_utc,
              finalized_at_utc, accepted_at_utc
            ) VALUES ('legacy-accepted-release', 'run-1', 'snapshot-1', 'formal',
                      'post-final', 'dedup-1', 'text-eval', 'image-decisions',
                      'image-eval', '2.4', 24, ?, 'git-test', ?,
                      1, 1, 1, 1, ?, 'accepted', ?, ?, ?)
            """,
            (_HASH_A, _HASH_B, _HASH_C, _NOW, _NOW, _NOW),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="v25_untrusted_accepted_state",
        ):
            migrate_derived(connection)

        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 24
        assert connection.execute(
            "SELECT seal_status FROM analysis_release_builds "
            "WHERE release_id = 'legacy-accepted-release'"
        ).fetchone()[0] == "accepted"


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
