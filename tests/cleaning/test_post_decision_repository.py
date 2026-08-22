"""帖子决定仓储的显式证据、幂等与非循环集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.post_decision_repository import (
    PostDecisionBuildRequest,
    PostDecisionRepositoryError,
    _bind_final_audit_evaluation,
    build_post_decision_snapshot,
)
from tourism_ugc_study.cleaning.post_decision import (
    HumanTextEvidence,
    PostDecision,
    PostDecisionRequest,
    build_post_decisions,
)
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived
from tourism_ugc_study.cleaning.text_keep_audit_repository import (
    TextKeepAuditAnnotationInput,
    create_text_keep_audit_round,
    evaluate_and_seal_text_keep_audit,
    export_text_keep_audit_tasks,
    import_text_keep_audit_annotations,
)


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_NOW = "2026-08-01T00:00:00+00:00"


def _seed_one_human_keep(
    path: Path,
    *,
    include_unrelated_second: bool = False,
) -> None:
    """建立含人工 related 仲裁及可选 unrelated 第二帖的合成快照。"""

    with connect_derived(path) as connection:
        migrate_derived(connection)
        connection.execute(
            """
            INSERT INTO cleaning_runs(
              run_id, protocol_version, config_sha256, random_seed, status,
              reason_code, code_version, environment_json, created_at_utc, updated_at_utc
                ) VALUES ('run-1', '3.2', ?, 17, 'paused', 'awaiting_quality_gate',
                      'git-test', '{}', ?, ?)
            """,
            (_A, _NOW, _NOW),
        )
        connection.execute(
            """
            INSERT INTO source_snapshots(
              snapshot_id, run_id, source_path, source_identity_sha256,
              source_sha256_before, source_sha256_after, source_size_bytes,
                  snapshot_path, snapshot_sha256, snapshot_size_bytes, post_count,
                  table_counts_json, object_manifest_sha256,
              input_contract_status, input_contract_method,
              input_contract_details_json, manifest_path, created_at_utc,
              created_at_asia_shanghai, code_version, environment_json
            ) VALUES ('snapshot-1', 'run-1', 'source.sqlite', ?, ?, ?, 1,
                          'snapshot.sqlite', ?, 1, ?, '{"posts":1}', ?,
                      'accepted', 'schema_attestation', '{}', 'snapshot.json',
                      ?, ?, 'git-test', '{}')
            """,
            (
                _A,
                _A,
                _A,
                _A,
                2 if include_unrelated_second else 1,
                _A,
                _NOW,
                _NOW,
            ),
        )
        connection.execute(
            "UPDATE cleaning_runs SET source_snapshot_id = 'snapshot-1' WHERE run_id = 'run-1'"
        )
        connection.execute(
            """
            INSERT INTO source_post_inventory(
              source_post_id, platform_key, first_seen_snapshot_id,
              last_seen_snapshot_id, is_present, current_source_version,
              current_text_sha256, current_author_sha256, current_analysis_sha256,
              updated_at_utc, current_author_identity_present
            ) VALUES (1, 'xiaohongshu', 'snapshot-1', 'snapshot-1', 1, 1,
                      ?, ?, ?, ?, 0)
            """,
            (_A, _B, _C, _NOW),
        )
        connection.execute(
            """
            INSERT INTO source_post_versions(
              source_post_id, source_version, effective_snapshot_id, text_sha256,
              author_sha256, analysis_sha256, created_at_utc, author_identity_present
            ) VALUES (1, 1, 'snapshot-1', ?, ?, ?, ?, 0)
            """,
            (_A, _B, _C, _NOW),
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
            INSERT INTO stage_tasks(
              task_id, run_id, stage_name, object_type, source_object_id,
              source_post_id, source_version, stage_version, required, status,
              max_attempts, created_at_utc, updated_at_utc
            ) VALUES ('task-1', 'run-1', 'text_deterministic', 'post', 1, 1, 1,
                      'text-v1', 1, 'succeeded', 3, ?, ?)
            """,
            (_NOW, _NOW),
        )
        connection.execute(
            """
            INSERT INTO text_deterministic_results(
              task_id, run_id, source_snapshot_id, source_post_id, source_version,
              platform_key, stage_version, rules_version, rules_sha256,
              runtime_versions_json, runtime_sha256, structure_status,
              structure_reason_code, structure_evidence_json, normalized_title,
              normalized_body, normalized_model_text, normalized_sha256,
              exact_canonical_sha256, output_sha256, created_at_utc
            ) VALUES ('task-1', 'run-1', 'snapshot-1', 1, 1, 'xiaohongshu',
                      'text-v1', 'rules-v1', ?, '{}', ?, 'usable',
                      'usable_text', '{}', '合成标题', '合成正文', '合成文本', ?, ?, ?, ?)
            """,
            (_A, _B, _A, _A, _C, _NOW),
        )
        connection.execute(
            """
            INSERT INTO text_annotation_imports(
              import_id, record_kind, guide_version, source_sha256, row_count,
              imported_by_hash, created_at_utc
            ) VALUES ('import-1', 'post_final_review', 'guide-1', ?, 1, ?, ?)
            """,
            (_A, _B, _NOW),
        )
        connection.execute(
            """
            INSERT INTO text_post_adjudications(
              adjudication_id, import_id, source_post_id, source_version,
              adjudicator_hash, tourism_label, reason_codes_json,
              evidence_annotation_ids_json, decision_context, guide_version,
              adjudicated_at_utc, created_at_utc
            ) VALUES ('adj-1', 'import-1', 1, 1, ?, 'related',
                      '["human_related"]', '[]', 'manual_review', 'guide-1', ?, ?)
            """,
            (_C, _NOW, _NOW),
        )
        if include_unrelated_second:
            connection.execute(
                """
                INSERT INTO source_post_inventory(
                  source_post_id, platform_key, first_seen_snapshot_id,
                  last_seen_snapshot_id, is_present, current_source_version,
                  current_text_sha256, current_author_sha256,
                  current_analysis_sha256, updated_at_utc,
                  current_author_identity_present
                ) VALUES (2, 'weibo', 'snapshot-1', 'snapshot-1', 1, 1,
                          ?, ?, ?, ?, 0)
                """,
                (_A, _B, _C, _NOW),
            )
            connection.execute(
                """
                INSERT INTO source_post_versions(
                  source_post_id, source_version, effective_snapshot_id,
                  text_sha256, author_sha256, analysis_sha256, created_at_utc,
                  author_identity_present
                ) VALUES (2, 1, 'snapshot-1', ?, ?, ?, ?, 0)
                """,
                (_A, _B, _C, _NOW),
            )
            connection.execute(
                """
                INSERT INTO source_post_observations(
                  snapshot_id, source_post_id, source_version, change_kind,
                  changed_axes_json, observed_at_utc
                ) VALUES ('snapshot-1', 2, 1, 'new', '["text"]', ?)
                """,
                (_NOW,),
            )
            connection.execute(
                """
                INSERT INTO stage_tasks(
                  task_id, run_id, stage_name, object_type, source_object_id,
                  source_post_id, source_version, stage_version, required, status,
                  max_attempts, created_at_utc, updated_at_utc
                ) VALUES ('task-2', 'run-1', 'text_deterministic', 'post', 2,
                          2, 1, 'text-v1', 1, 'succeeded', 3, ?, ?)
                """,
                (_NOW, _NOW),
            )
            connection.execute(
                """
                INSERT INTO text_deterministic_results(
                  task_id, run_id, source_snapshot_id, source_post_id,
                  source_version, platform_key, stage_version, rules_version,
                  rules_sha256, runtime_versions_json, runtime_sha256,
                  structure_status, structure_reason_code, structure_evidence_json,
                  normalized_title, normalized_body, normalized_model_text,
                  normalized_sha256, exact_canonical_sha256, output_sha256,
                  created_at_utc
                ) VALUES ('task-2', 'run-1', 'snapshot-1', 2, 1, 'weibo',
                          'text-v1', 'rules-v1', ?, '{}', ?, 'usable',
                          'usable_text', '{}', '合成标题二', '合成正文二',
                          '合成文本二', ?, ?, ?, ?)
                """,
                (_A, _B, _A, _A, _C, _NOW),
            )
            connection.execute(
                """
                INSERT INTO text_annotation_imports(
                  import_id, record_kind, guide_version, source_sha256, row_count,
                  imported_by_hash, created_at_utc
                ) VALUES ('import-2', 'post_final_review', 'guide-1', ?, 1, ?, ?)
                """,
                (_B, _C, _NOW),
            )
            connection.execute(
                """
                INSERT INTO text_post_adjudications(
                  adjudication_id, import_id, source_post_id, source_version,
                  adjudicator_hash, tourism_label,
                  reason_codes_json, evidence_annotation_ids_json,
                  decision_context, guide_version, adjudicated_at_utc,
                  created_at_utc
                ) VALUES ('adj-2', 'import-2', 2, 1, ?, 'unrelated',
                          '["human_unrelated"]', '[]', 'manual_review',
                          'guide-1', ?, ?)
                """,
                (_C, _NOW, _NOW),
            )
        connection.commit()


def _candidate_request() -> PostDecisionBuildRequest:
    """返回覆盖唯一快照成员的正式候选请求。"""

    return PostDecisionBuildRequest(
        run_id="run-1",
        source_snapshot_id="snapshot-1",
        build_kind="candidate",
        audit_mode="formal",
        decision_version="decision-v1",
        guide_version="guide-1",
        rules_sha256=_A,
        deterministic_result_ids=("task-1",),
        human_adjudication_ids=("adj-1",),
    )




def test_candidate_audit_final_path_is_non_circular_and_idempotent(tmp_path: Path) -> None:
    """候选先冻结审计人口，审计通过后最终构建才可封存。"""

    database = tmp_path / "derived.sqlite"
    _seed_one_human_keep(database)
    candidate = build_post_decision_snapshot(database, _candidate_request())
    assert candidate == build_post_decision_snapshot(database, _candidate_request())
    assert (candidate.expected_post_count, candidate.keep_count) == (1, 1)

    audit = create_text_keep_audit_round(
        database,
        run_id="run-1",
        candidate_decision_build_id=candidate.decision_build_id,
        audit_mode="formal",
        round_number=1,
        seed=17,
    )
    task = export_text_keep_audit_tasks(database, audit_round_id=audit.audit_round_id)[0]
    imported = import_text_keep_audit_annotations(
        database,
        audit_round_id=audit.audit_round_id,
        rows=(
            TextKeepAuditAnnotationInput(
                task.source_post_id,
                task.source_version,
                task.guide_version,
                "related",
                ("audit_usable_related",),
            ),
        ),
    )
    assert len(imported.annotation_ids) == 1
    evaluation = evaluate_and_seal_text_keep_audit(
        database,
        audit_round_id=audit.audit_round_id,
    )
    assert evaluation.evaluation_status == "passed"

    final_request = PostDecisionBuildRequest(
        run_id="run-1",
        source_snapshot_id="snapshot-1",
        build_kind="final",
        audit_mode="formal",
        decision_version="decision-v1",
        guide_version="guide-1",
        rules_sha256=_A,
        source_candidate_decision_build_id=candidate.decision_build_id,
        text_keep_audit_evaluation_id=evaluation.audit_evaluation_id,
    )
    final = build_post_decision_snapshot(database, final_request)
    assert final == build_post_decision_snapshot(database, final_request)
    assert final.build_kind == "final"
    assert final.keep_count == 1
    assert final.input_manifest_sha256 != candidate.input_manifest_sha256
    domain_decision = build_post_decisions(
        (
            PostDecisionRequest(
                1,
                1,
                "decision-v1",
                "final",
                (HumanTextEvidence("adj-1", "related"),),
            ),
        )
    )[0]
    expected = _bind_final_audit_evaluation(
        domain_decision,
        text_keep_audit_evaluation_id=evaluation.audit_evaluation_id,
    )
    with connect_derived(database) as connection:
        stored_sha = connection.execute(
            """
            SELECT decision_sha256 FROM post_decisions
            WHERE decision_build_id = ? AND source_post_id = 1
            """,
            (final.decision_build_id,),
        ).fetchone()[0]
    assert stored_sha == expected.decision_sha256


@pytest.mark.parametrize("evidence_id", ["human-adjudication", "model-run"])
def test_final_audit_hash_binding_is_uniform_for_human_and_model_paths(
    evidence_id: str,
) -> None:
    """人工与模型领域输出都必须绑定实际审计评估身份。"""

    domain = PostDecision(
        1,
        1,
        "keep",
        ("domain_reason",),
        (evidence_id,),
        "decision-v1",
        _A,
    )
    first = _bind_final_audit_evaluation(
        domain,
        text_keep_audit_evaluation_id="audit-evaluation-1",
    )
    second = _bind_final_audit_evaluation(
        domain,
        text_keep_audit_evaluation_id="audit-evaluation-2",
    )
    assert first.decision_sha256 != domain.decision_sha256
    assert first.decision_sha256 != second.decision_sha256


def test_candidate_rejects_incomplete_deterministic_coverage(tmp_path: Path) -> None:
    """显式请求缺少快照成员的确定性证据时不得读取其他 latest 结果。"""

    database = tmp_path / "coverage.sqlite"
    _seed_one_human_keep(database)
    request = PostDecisionBuildRequest(
        **{
            **_candidate_request().__dict__,
            "deterministic_result_ids": (),
        }
    )
    with pytest.raises(PostDecisionRepositoryError) as error:
        build_post_decision_snapshot(database, request)
    assert error.value.reason_code == "deterministic_evidence_does_not_cover_snapshot"
