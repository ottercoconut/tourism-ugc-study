"""分析发布仓储的合成 SQLite 与不可变包测试。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.analysis_release_repository import (
    AnalysisReleaseRepositoryError,
    accept_release,
    build_release,
    get_release_status,
    verify_release,
)
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_NOW = "2026-08-01T00:00:00+00:00"


def _sha256(path: Path) -> str:
    """计算合成 snapshot 文件哈希。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_release_fixture(tmp_path: Path, *, audit_mode: str) -> tuple[Path, Path, Path]:
    """建立一帖、三种图片角色和完整双审计的最小合成谱系。

    测试只验证仓储工作流，不重复 schema 文件已有的大量触发器负例。因此先
    正常迁移获得 v24 表，再移除触发器并关闭外键以直接注入不可变上游事实；
    发布仓储自身仍必须从这些表重读身份、计数和状态，不能接收自报投影。
    """

    derived_db = tmp_path / "derived.sqlite"
    output_root = tmp_path / "results"
    snapshot = tmp_path / "snapshot.sqlite"
    snapshot.write_bytes(b"synthetic-frozen-snapshot")
    snapshot_hash = _sha256(snapshot)
    with connect_derived(derived_db) as connection:
        migrate_derived(connection)
        connection.commit()
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{row[0]}"')
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO cleaning_runs(
              run_id, protocol_version, config_sha256, random_seed, status,
              reason_code, code_version, environment_json, created_at_utc,
              updated_at_utc, source_snapshot_id
            ) VALUES ('run-1', '2.4', ?, 17, 'paused', 'quality_gate_pending',
                      'git-test', '{}', ?, ?, 'snapshot-1')
            """,
            (_A, _NOW, _NOW),
        )
        connection.execute(
            """
            INSERT INTO source_snapshots(
              snapshot_id, run_id, source_path, source_identity_sha256,
              source_sha256_before, source_sha256_after, source_size_bytes,
              snapshot_path, snapshot_sha256, snapshot_size_bytes, post_count,
              image_count, table_counts_json, object_manifest_sha256,
              input_contract_status, input_contract_method,
              input_contract_reason_code, input_contract_details_json,
              manifest_path, created_at_utc, created_at_asia_shanghai,
              code_version, environment_json
            ) VALUES ('snapshot-1', 'run-1', 'source.sqlite', ?, ?, ?, 1,
                      ?, ?, ?, 1, 3, '{}', ?, 'accepted', 'synthetic', NULL,
                      '{}', 'manifest.json', ?, ?, 'git-test', '{}')
            """,
            (
                _A,
                _A,
                _A,
                str(snapshot),
                snapshot_hash,
                snapshot.stat().st_size,
                _B,
                _NOW,
                _NOW,
            ),
        )
        connection.execute(
            "INSERT INTO source_post_observations VALUES "
            "('snapshot-1', 1, 1, 'new', '[]', ?)",
            (_NOW,),
        )
        for image_id in (10, 11, 12):
            connection.execute(
                "INSERT INTO source_image_observations VALUES "
                "('snapshot-1', ?, 1, 'new', '[]', ?)",
                (image_id, _NOW),
            )

        connection.execute(
            """
            INSERT INTO text_keep_audit_rounds(
              audit_round_id, run_id, candidate_decision_build_id, audit_mode,
              round_number, random_seed, sampling_method, estimator,
              population_count, population_manifest_sha256, sample_count,
              sample_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('text-round', 'run-1', 'candidate-1', ?, 1, 17,
                      'census', 'census', 1, ?, 1, ?, 'finalized', ?)
            """,
            (audit_mode, _A, _B, _NOW),
        )
        connection.execute(
            """
            INSERT INTO text_keep_audit_evaluations(
              audit_evaluation_id, audit_round_id, completed_count, event_count,
              event_point_estimate, one_sided_upper, evaluation_status,
              reason_code, evidence_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('text-eval', 'text-round', 1, 0, 0.0, 0.0, 'passed',
                      'audit_passed', ?, 'finalized', ?)
            """,
            (_C, _NOW),
        )
        connection.execute(
            """
            INSERT INTO post_decision_builds(
              decision_build_id, run_id, source_snapshot_id, build_kind,
              source_candidate_decision_build_id, text_keep_audit_evaluation_id,
              decision_version, guide_version, rules_sha256,
              input_manifest_sha256, expected_post_count, keep_count,
              review_count, exclude_count, decision_manifest_sha256,
              seal_status, created_at_utc
            ) VALUES ('post-final', 'run-1', 'snapshot-1', 'final',
                      'candidate-1', 'text-eval', 'decision-v1', 'guide-v1',
                      ?, ?, 1, 1, 0, 0, ?, 'finalized', ?)
            """,
            (_A, _B, _C, _NOW),
        )
        connection.execute(
            """
            INSERT INTO post_decisions(
              decision_id, decision_build_id, source_post_id, source_version,
              structure_label, tourism_label, decision_action, reason_code,
              provenance, model_run_id, evidence_manifest_sha256,
              decision_sha256, created_at_utc
            ) VALUES ('post-decision', 'post-final', 1, 1, 'usable', 'related',
                      'keep', 'human_related', 'human_adjudication', NULL,
                      ?, ?, ?)
            """,
            (_A, _B, _NOW),
        )
        connection.execute(
            "INSERT INTO post_decision_evidence_links VALUES "
            "('post-decision', 'human_adjudication', 'human-1', 1, 1)"
        )
        connection.execute(
            """
            INSERT INTO text_dedup_builds(
              dedup_build_id, run_id, post_decision_build_id, candidate_build_id,
              dedup_version, selection_strategy, expected_eligible_count,
              edge_count, cluster_count, member_count, representative_count,
              input_manifest_sha256, member_manifest_sha256, seal_status,
              created_at_utc
            ) VALUES ('dedup-1', 'run-1', 'post-final', 'text-candidates',
                      'dedup-v1', 'stable_min_identity', 1, 0, 1, 1, 1,
                      ?, ?, 'finalized', ?)
            """,
            (_A, _B, _NOW),
        )
        connection.execute(
            "INSERT INTO text_dedup_clusters VALUES "
            "('dedup-1', 'text-cluster', 1, 1, 1, 'stable_min_identity', ?)",
            (_C,),
        )
        connection.execute(
            "INSERT INTO text_dedup_members VALUES "
            "('dedup-1', 'text-cluster', 1, 1, 1, 'stable_min_identity')"
        )

        connection.execute(
            """
            INSERT INTO image_manifest_imports(
              manifest_id, run_id, source_snapshot_id, manifest_version,
              source_sha256, root_identity_sha256, row_count,
              accepted_row_count, rejected_row_count, status, reason_code,
              created_at_utc
            ) VALUES ('image-manifest', 'run-1', 'snapshot-1', 'manifest-v1',
                      ?, ?, 3, 3, 0, 'accepted', NULL, ?)
            """,
            (_A, _B, _NOW),
        )
        image_rows = (
            ("row-content", 1, 10, "content"),
            ("row-page", 2, 11, "page"),
            ("row-avatar", 3, 12, "author_avatar"),
        )
        for row_id, row_number, image_id, role in image_rows:
            connection.execute(
                """
                INSERT INTO image_manifest_rows(
                  manifest_row_id, manifest_id, row_number, source_image_id,
                  source_post_id, relation_role, relative_path,
                  expected_file_sha256, parent_file_sha256, transform_json,
                  row_identity_sha256, validation_status, reason_code,
                  created_at_utc
                ) VALUES (?, 'image-manifest', ?, ?, 1, ?, ?, ?, NULL, '{}',
                          ?, 'accepted', NULL, ?)
                """,
                (row_id, row_number, image_id, role, f"{row_id}.png", _A, _B, _NOW),
            )
        role_rows = (
            ("role-content", "row-content", "content", "inspect_content"),
            ("role-page", "row-page", "page", "evidence_only"),
            ("role-avatar", "row-avatar", "author_avatar", "exclude_from_content"),
        )
        for role_id, row_id, role, action in role_rows:
            connection.execute(
                "INSERT INTO image_role_results VALUES (?, ?, 'role-v1', ?, ?, 'role', ?, ?)",
                (role_id, row_id, role, action, _C, _NOW),
            )
        connection.execute(
            """
            INSERT INTO image_fingerprints(
              fingerprint_id, manifest_row_id, fingerprint_version,
              row_identity_sha256, file_sha256, mime_type, byte_size,
              width_px, height_px, has_alpha, is_fully_transparent,
              sanitized_exif_json, phash_hex, phash_hash_size,
              phash_highfreq_factor, library_versions_json, output_sha256,
              created_at_utc
            ) VALUES ('fingerprint-1', 'row-content', 'fp-v1', ?, ?, 'image/png',
                      10, 10, 10, 0, 0, '{}', '0000000000000000', 8, 4,
                      '{}', ?, ?)
            """,
            (_A, _B, _C, _NOW),
        )
        connection.execute(
            """
            INSERT INTO image_candidate_builds(
              build_id, run_id, manifest_id, candidate_version,
              fingerprint_version, config_sha256, input_manifest_sha256,
              expected_fingerprint_count, exact_cluster_count,
              exact_duplicate_cluster_count, near_pair_count, signal_count,
              seal_status, output_sha256, created_at_utc
            ) VALUES ('image-candidates', 'run-1', 'image-manifest', 'candidate-v1',
                      'fp-v1', ?, ?, 1, 1, 0, 0, 0, 'finalized', ?, ?)
            """,
            (_A, _B, _C, _NOW),
        )
        connection.execute(
            "INSERT INTO image_candidate_build_members VALUES "
            "('image-candidates', 'fingerprint-1', 10, 1, ?)",
            (_A,),
        )
        connection.execute(
            "INSERT INTO image_exact_clusters VALUES "
            "('image-candidates', 'image-cluster', ?, 'fingerprint-1', 1)",
            (_B,),
        )
        connection.execute(
            "INSERT INTO image_exact_cluster_members VALUES "
            "('image-candidates', 'image-cluster', 'fingerprint-1', 1)"
        )
        connection.execute(
            """
            INSERT INTO image_decision_builds(
              decision_build_id, candidate_build_id, guide_version,
              evidence_manifest_sha256, expected_decision_count,
              decision_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('image-decisions', 'image-candidates', 'guide-v1', ?, 1,
                      ?, 'finalized', ?)
            """,
            (_A, _B, _NOW),
        )
        connection.execute(
            """
            INSERT INTO image_decisions(
              decision_id, decision_build_id, fingerprint_id,
              technical_noise_label, decision_action, provenance, evidence_id,
              decision_sha256, created_at_utc
            ) VALUES ('image-decision', 'image-decisions', 'fingerprint-1',
                      'valid_content', 'keep', 'single_valid_content',
                      'image-human-1', ?, ?)
            """,
            (_C, _NOW),
        )
        connection.execute(
            """
            INSERT INTO image_keep_audit_rounds(
              audit_round_id, decision_build_id, round_number, random_seed,
              population_count, population_manifest_sha256, primary_count,
              supplement_count, primary_manifest_sha256,
              supplement_manifest_sha256, interval_method, seal_status,
              created_at_utc, integrity_status, sampling_algorithm_version
            ) VALUES ('image-round', 'image-decisions', 1, 17, 1, ?, 1, 0,
                      ?, ?, 'census', 'finalized', ?, 'finalized',
                      'image-keep-audit-sampling-v1')
            """,
            (_A, _B, _C, _NOW),
        )
        connection.execute(
            """
            INSERT INTO image_keep_audit_evaluations(
              audit_evaluation_id, audit_round_id, completed_count,
              primary_event_count, supplement_event_count,
              primary_point_estimate, one_sided_upper, evaluation_status,
              reason_code, evidence_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('image-eval', 'image-round', 1, 0, 0, 0.0, 0.0,
                      'passed', 'audit_passed', ?, 'finalized', ?)
            """,
            (_C, _NOW),
        )
        connection.commit()
    return derived_db, output_root, snapshot


def _build(derived_db: Path, output_root: Path, *, mode: str = "smoke"):
    """用固定显式身份调用发布构建。"""

    return build_release(
        derived_db,
        run_id="run-1",
        release_id="release-1",
        release_mode=mode,
        post_decision_build_id="post-final",
        text_dedup_build_id="dedup-1",
        text_keep_audit_evaluation_id="text-eval",
        image_decision_build_id="image-decisions",
        image_keep_audit_evaluation_id="image-eval",
        output_root=output_root,
    )


def test_smoke_build_writes_four_sets_and_verified_artifact(tmp_path: Path) -> None:
    """smoke 证据完整时可 finalized，但不会自动接受运行。"""

    derived_db, output_root, _ = _seed_release_fixture(tmp_path, audit_mode="smoke")
    result = _build(derived_db, output_root)

    assert result.seal_status == "finalized"
    assert result.reused is False
    assert (
        result.posts_eligible_count,
        result.posts_deduplicated_count,
        result.images_eligible_count,
        result.images_evidence_only_count,
    ) == (1, 1, 1, 1)
    verified = verify_release(
        derived_db,
        run_id="run-1",
        release_id="release-1",
        output_root=output_root,
    )
    assert verified.seal_status == "finalized"
    assert (output_root / "run-1" / "release-1" / "artifact-manifest.json").is_file()
    status = get_release_status(
        derived_db, run_id="run-1", release_id="release-1"
    )
    assert (status.run_status, status.seal_status) == ("paused", "finalized")


def test_complete_request_is_idempotent_but_output_root_conflict_is_rejected(
    tmp_path: Path,
) -> None:
    """相同完整请求复验复用，改变输出根身份不能覆盖旧发布。"""

    derived_db, output_root, _ = _seed_release_fixture(tmp_path, audit_mode="smoke")
    _build(derived_db, output_root)
    assert _build(derived_db, output_root).reused is True

    with pytest.raises(
        AnalysisReleaseRepositoryError, match="analysis release repository"
    ) as caught:
        _build(derived_db, tmp_path / "other-results")
    assert caught.value.reason_code == "analysis_release_request_conflict"


def test_formal_build_rejects_smoke_text_audit_and_smoke_accept_is_impossible(
    tmp_path: Path,
) -> None:
    """模式不一致不能构建，smoke finalized 也不能进入 accepted。"""

    derived_db, output_root, _ = _seed_release_fixture(tmp_path, audit_mode="smoke")
    with pytest.raises(AnalysisReleaseRepositoryError) as mismatch:
        _build(derived_db, output_root, mode="formal")
    assert mismatch.value.reason_code == "text_keep_audit_mode_mismatch"

    _build(derived_db, output_root)
    with pytest.raises(AnalysisReleaseRepositoryError) as smoke:
        accept_release(
            derived_db,
            run_id="run-1",
            release_id="release-1",
            output_root=output_root,
        )
    assert smoke.value.reason_code == "smoke_release_cannot_be_accepted"


def test_artifact_tamper_and_snapshot_hash_change_block_verification_or_acceptance(
    tmp_path: Path,
) -> None:
    """本地包篡改由 verify 阻断，snapshot 变化由 formal accept 阻断。"""

    derived_db, output_root, snapshot = _seed_release_fixture(
        tmp_path, audit_mode="formal"
    )
    _build(derived_db, output_root, mode="formal")
    report = output_root / "run-1" / "release-1" / "report.json"
    original = report.read_bytes()
    report.write_bytes(original + b" ")
    with pytest.raises(AnalysisReleaseRepositoryError) as tampered:
        verify_release(
            derived_db,
            run_id="run-1",
            release_id="release-1",
            output_root=output_root,
        )
    assert tampered.value.reason_code == "release_artifact_invalid"
    report.write_bytes(original)

    snapshot.write_bytes(b"changed-after-release")
    with pytest.raises(AnalysisReleaseRepositoryError) as changed:
        accept_release(
            derived_db,
            run_id="run-1",
            release_id="release-1",
            output_root=output_root,
        )
    assert changed.value.reason_code == "snapshot_sha256_mismatch"


def test_formal_acceptance_updates_run_before_release_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """formal 完整证据通过时显式接受，并保存运行完成理由与时间。"""

    derived_db, output_root, _ = _seed_release_fixture(
        tmp_path, audit_mode="formal"
    )
    _build(derived_db, output_root, mode="formal")

    accepted = accept_release(
        derived_db,
        run_id="run-1",
        release_id="release-1",
        output_root=output_root,
    )
    assert (accepted.run_status, accepted.seal_status, accepted.reused) == (
        "accepted",
        "accepted",
        False,
    )
    with connect_derived(derived_db) as connection:
        run = connection.execute(
            "SELECT status, reason_code, finished_at_utc FROM cleaning_runs WHERE run_id = 'run-1'"
        ).fetchone()
        release = connection.execute(
            "SELECT seal_status, accepted_at_utc FROM analysis_release_builds "
            "WHERE release_id = 'release-1'"
        ).fetchone()
    assert tuple(run[:2]) == ("accepted", "analysis_release_accepted")
    assert run["finished_at_utc"] is not None
    assert release["seal_status"] == "accepted"
    assert release["accepted_at_utc"] is not None
    assert accept_release(
        derived_db,
        run_id="run-1",
        release_id="release-1",
        output_root=output_root,
    ).reused is True


@pytest.mark.parametrize("field", ["run_id", "release_id"])
def test_mutable_latest_alias_is_rejected_before_database_access(
    tmp_path: Path, field: str
) -> None:
    """所有查询入口显式拒绝 latest，且无需创建数据库。"""

    values = {"run_id": "run-1", "release_id": "release-1"}
    values[field] = "latest"
    with pytest.raises(AnalysisReleaseRepositoryError) as caught:
        get_release_status(tmp_path / "never-created.sqlite", **values)
    assert caught.value.reason_code == "mutable_latest_alias_forbidden"
    assert not (tmp_path / "never-created.sqlite").exists()
