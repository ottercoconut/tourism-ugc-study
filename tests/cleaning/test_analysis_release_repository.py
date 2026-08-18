"""纯文本分析发布仓储的构建、复验和显式身份测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.cleaning.test_analysis_dedup_repository import (
    _final_decision,
    _seed_text_candidate,
)
from tests.cleaning.test_post_decision_repository import _seed_one_human_keep
from tourism_ugc_study.cleaning.analysis_dedup_repository import (
    build_analysis_dedup_snapshot,
)
from tourism_ugc_study.cleaning.analysis_release_repository import (
    AnalysisReleaseRepositoryError,
    build_release,
    get_release_status,
    verify_release,
)
from tourism_ugc_study.cleaning.schema import connect_derived


def _release_inputs(database: Path) -> tuple[str, str, str]:
    _seed_one_human_keep(database)
    candidate_id = _seed_text_candidate(database)
    final_id = _final_decision(database)
    dedup = build_analysis_dedup_snapshot(
        database,
        run_id="run-1",
        post_decision_build_id=final_id,
        candidate_build_id=candidate_id,
        dedup_version="analysis-dedup-v1",
    )
    with connect_derived(database) as connection:
        audit_id = connection.execute(
            "SELECT text_keep_audit_evaluation_id FROM post_decision_builds "
            "WHERE decision_build_id = ?",
            (final_id,),
        ).fetchone()[0]
    return final_id, dedup.dedup_build_id, str(audit_id)


def test_formal_build_writes_two_post_sets_and_verified_artifact(tmp_path: Path) -> None:
    database = tmp_path / "release.sqlite"
    output = tmp_path / "results"
    final_id, dedup_id, audit_id = _release_inputs(database)

    result = build_release(
        database,
        run_id="run-1",
        release_id="release-1",
        release_mode="formal",
        post_decision_build_id=final_id,
        text_dedup_build_id=dedup_id,
        text_keep_audit_evaluation_id=audit_id,
        output_root=output,
    )
    assert (result.posts_eligible_count, result.posts_deduplicated_count) == (1, 1)
    assert result.seal_status == "finalized"
    verified = verify_release(
        database, run_id="run-1", release_id="release-1", output_root=output
    )
    assert verified.release_manifest_sha256 == result.release_manifest_sha256
    assert get_release_status(
        database, run_id="run-1", release_id="release-1"
    ).seal_status == "finalized"

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_posts_eligible").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM analysis_posts_deduplicated").fetchone()[0] == 1


def test_build_is_idempotent_for_same_text_evidence(tmp_path: Path) -> None:
    database = tmp_path / "release.sqlite"
    output = tmp_path / "results"
    final_id, dedup_id, audit_id = _release_inputs(database)
    kwargs = dict(
        run_id="run-1",
        release_id="release-1",
        release_mode="formal",
        post_decision_build_id=final_id,
        text_dedup_build_id=dedup_id,
        text_keep_audit_evaluation_id=audit_id,
        output_root=output,
    )
    assert build_release(database, **kwargs).reused is False
    assert build_release(database, **kwargs).reused is True


def test_release_rejects_wrong_audit_mode_and_mutable_alias(tmp_path: Path) -> None:
    database = tmp_path / "release.sqlite"
    final_id, dedup_id, audit_id = _release_inputs(database)
    with pytest.raises(AnalysisReleaseRepositoryError) as mismatch:
        build_release(
            database,
            run_id="run-1",
            release_id="release-smoke",
            release_mode="smoke",
            post_decision_build_id=final_id,
            text_dedup_build_id=dedup_id,
            text_keep_audit_evaluation_id=audit_id,
            output_root=tmp_path / "results",
        )
    assert mismatch.value.reason_code == "text_keep_audit_lineage_mismatch"

    with pytest.raises(AnalysisReleaseRepositoryError) as latest:
        get_release_status(database, run_id="run-1", release_id="latest")
    assert latest.value.reason_code == "mutable_latest_alias_forbidden"
