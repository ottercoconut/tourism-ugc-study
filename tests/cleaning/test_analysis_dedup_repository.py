"""分析去重仓储的显式 final 谱系和候选隔离测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.cleaning.test_post_decision_repository import (
    _A,
    _B,
    _C,
    _NOW,
    _candidate_request,
    _seed_one_human_keep,
)
from tourism_ugc_study.cleaning.analysis_dedup_repository import (
    AnalysisDedupRepositoryError,
    build_analysis_dedup_snapshot,
)
from tourism_ugc_study.cleaning.post_decision_repository import (
    PostDecisionBuildRequest,
    build_post_decision_snapshot,
)
from tourism_ugc_study.cleaning.schema import connect_derived
from tourism_ugc_study.cleaning.text_keep_audit_repository import (
    TextKeepAuditAnnotationInput,
    create_text_keep_audit_round,
    evaluate_and_seal_text_keep_audit,
    export_text_keep_audit_tasks,
    import_text_keep_audit_annotations,
)


def _seed_text_candidate(database: Path) -> str:
    """为唯一合成帖子建立并封存一个完整 exact 候选构建。"""

    with connect_derived(database) as connection:
        connection.execute(
            """
            INSERT INTO text_candidate_builds(
              build_id, run_id, source_snapshot_id, stage_version,
              rules_version, rules_sha256, runtime_sha256, status,
              corpus_manifest_sha256, expected_post_count, processed_post_count,
              usable_post_count, is_complete_corpus, exact_cluster_count,
              exact_duplicate_cluster_count, exact_cross_platform_cluster_count,
              exact_cross_platform_member_count, near_candidate_pair_count,
              near_cross_platform_candidate_pair_count,
              near_candidate_component_count, library_versions_json,
              output_sha256, created_at_utc
            ) VALUES ('text-candidate-1', 'run-1', 'snapshot-1', 'text-v1',
                      'rules-v1', ?, ?, 'building', ?, 1, 1, 1, 1, 1,
                      0, 0, 0, 0, 0, 1, '{}', ?, ?)
            """,
            (_A, _B, _C, _A, _NOW),
        )
        connection.execute(
            """
            INSERT INTO text_exact_clusters(
              build_id, cluster_id, exact_canonical_sha256,
              representative_source_post_id, member_count, is_cross_platform
            ) VALUES ('text-candidate-1', 'exact-1', ?, 1, 1, 0)
            """,
            (_A,),
        )
        connection.execute(
            """
            INSERT INTO text_candidate_corpus_members(
              build_id, task_id, source_post_id, source_version, platform_key,
              structure_status, exact_cluster_id, is_near_representative
            ) VALUES ('text-candidate-1', 'task-1', 1, 1, 'xiaohongshu',
                      'usable', 'exact-1', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO text_exact_cluster_members(
              build_id, cluster_id, source_post_id, source_version,
              platform_key, is_representative
            ) VALUES ('text-candidate-1', 'exact-1', 1, 1, 'xiaohongshu', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO text_near_candidate_components(
              build_id, component_id, representative_count,
              member_count, is_cross_platform
            ) VALUES ('text-candidate-1', 'component-1', 1, 1, 0)
            """
        )
        connection.execute(
            """
            INSERT INTO text_near_candidate_component_members(
              build_id, component_id, cluster_id, source_post_id,
              source_version, platform_key, is_cluster_representative
            ) VALUES ('text-candidate-1', 'component-1', 'exact-1', 1, 1,
                      'xiaohongshu', 1)
            """
        )
        connection.execute(
            "UPDATE text_candidate_builds SET status = 'finalized' WHERE build_id = 'text-candidate-1'"
        )
        connection.commit()
    return "text-candidate-1"


def _final_decision(database: Path) -> str:
    """通过 candidate→audit→final 非循环路径建立最终帖子决定。"""

    candidate = build_post_decision_snapshot(database, _candidate_request())
    audit = create_text_keep_audit_round(
        database,
        run_id="run-1",
        candidate_decision_build_id=candidate.decision_build_id,
        audit_mode="formal",
        round_number=1,
        seed=17,
    )
    task = export_text_keep_audit_tasks(database, audit_round_id=audit.audit_round_id)[0]
    import_text_keep_audit_annotations(
        database,
        audit_round_id=audit.audit_round_id,
        rows=(
            TextKeepAuditAnnotationInput(
                task.source_post_id,
                task.source_version,
                _C,
                task.guide_version,
                "usable",
                "related",
                ("audit_usable_related",),
                _NOW,
            ),
        ),
    )
    evaluation = evaluate_and_seal_text_keep_audit(
        database,
        audit_round_id=audit.audit_round_id,
    )
    final = build_post_decision_snapshot(
        database,
        PostDecisionBuildRequest(
            run_id="run-1",
            source_snapshot_id="snapshot-1",
            build_kind="final",
            audit_mode="formal",
            decision_version="decision-v1",
            guide_version="guide-1",
            rules_sha256=_A,
            source_candidate_decision_build_id=candidate.decision_build_id,
            text_keep_audit_evaluation_id=evaluation.audit_evaluation_id,
        ),
    )
    return final.decision_build_id


def test_final_keep_population_builds_one_representative_idempotently(
    tmp_path: Path,
) -> None:
    """最终 keep 成员守恒进入簇，且每簇只有一个稳定代表。"""

    database = tmp_path / "dedup.sqlite"
    _seed_one_human_keep(database)
    candidate_build_id = _seed_text_candidate(database)
    final_build_id = _final_decision(database)
    result = build_analysis_dedup_snapshot(
        database,
        run_id="run-1",
        post_decision_build_id=final_build_id,
        candidate_build_id=candidate_build_id,
        dedup_version="analysis-dedup-v1",
    )
    assert result == build_analysis_dedup_snapshot(
        database,
        run_id="run-1",
        post_decision_build_id=final_build_id,
        candidate_build_id=candidate_build_id,
        dedup_version="analysis-dedup-v1",
    )
    assert (
        result.expected_eligible_count,
        result.member_count,
        result.cluster_count,
        result.representative_count,
    ) == (1, 1, 1, 1)
    assert len(result.input_manifest_sha256) == len(result.member_manifest_sha256) == 64

    with connect_derived(database) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS members, SUM(is_representative) AS representatives
            FROM text_dedup_members WHERE dedup_build_id = ?
            """,
            (result.dedup_build_id,),
        ).fetchone()
        assert tuple(row) == (1, 1)


def test_candidate_pair_identity_cannot_enter_confirmed_dedup(tmp_path: Path) -> None:
    """调用方不能把近重复候选身份冒充人工 duplicate 仲裁 ID。"""

    database = tmp_path / "candidate-rejected.sqlite"
    _seed_one_human_keep(database)
    candidate_build_id = _seed_text_candidate(database)
    final_build_id = _final_decision(database)
    with pytest.raises(AnalysisDedupRepositoryError) as error:
        build_analysis_dedup_snapshot(
            database,
            run_id="run-1",
            post_decision_build_id=final_build_id,
            candidate_build_id=candidate_build_id,
            dedup_version="analysis-dedup-v1",
            duplicate_adjudication_ids=("near-candidate-pair-1",),
        )
    assert error.value.reason_code == "analysis_dedup_adjudication_not_found"


def test_candidate_post_decision_build_is_rejected(tmp_path: Path) -> None:
    """分析去重不能直接消费审计前 candidate 帖子决定。"""

    database = tmp_path / "candidate-post.sqlite"
    _seed_one_human_keep(database)
    candidate_build_id = _seed_text_candidate(database)
    post_candidate = build_post_decision_snapshot(database, _candidate_request())
    with pytest.raises(AnalysisDedupRepositoryError) as error:
        build_analysis_dedup_snapshot(
            database,
            run_id="run-1",
            post_decision_build_id=post_candidate.decision_build_id,
            candidate_build_id=candidate_build_id,
            dedup_version="analysis-dedup-v1",
        )
    assert error.value.reason_code == "analysis_dedup_lineage_invalid"
