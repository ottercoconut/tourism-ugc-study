"""文本保留集审计仓储的抽样、导入和门禁测试。"""

from __future__ import annotations

import sqlite3
from dataclasses import fields
from pathlib import Path

import pytest

from tests.cleaning.test_post_decision_repository import (
    _C,
    _NOW,
    _candidate_request,
    _seed_one_human_keep,
)
from tourism_ugc_study.cleaning.post_decision_repository import (
    build_post_decision_snapshot,
)
from tourism_ugc_study.cleaning.schema import connect_derived
from tourism_ugc_study.cleaning.text_keep_audit_repository import (
    TextKeepAuditAnnotationInput,
    TextKeepAuditRepositoryError,
    TextKeepAuditTask,
    create_text_keep_audit_round,
    evaluate_and_seal_text_keep_audit,
    export_text_keep_audit_tasks,
    import_text_keep_audit_annotations,
)


def _audit_round(database: Path):
    """建立候选决定并冻结一个单成员 census 审计轮。"""

    _seed_one_human_keep(database)
    candidate = build_post_decision_snapshot(database, _candidate_request())
    round_result = create_text_keep_audit_round(
        database,
        run_id="run-1",
        candidate_decision_build_id=candidate.decision_build_id,
        audit_mode="formal",
        round_number=1,
        seed=17,
    )
    return candidate, round_result


def test_round_export_import_and_evaluation_are_idempotent(tmp_path: Path) -> None:
    """冻结轮、追加式观察和真实证据评估均可完整复验。"""

    database = tmp_path / "audit.sqlite"
    candidate, audit = _audit_round(database)
    repeated = create_text_keep_audit_round(
        database,
        run_id="run-1",
        candidate_decision_build_id=candidate.decision_build_id,
        audit_mode="formal",
        round_number=1,
        seed=17,
    )
    assert repeated == audit
    assert (audit.population_count, audit.sample_count) == (1, 1)

    tasks = export_text_keep_audit_tasks(database, audit_round_id=audit.audit_round_id)
    assert len(tasks) == 1
    assert {field.name for field in fields(TextKeepAuditTask)} == {
        "audit_round_id",
        "source_post_id",
        "source_version",
        "platform_key",
        "stratum_rank",
        "guide_version",
    }
    annotation = TextKeepAuditAnnotationInput(
        tasks[0].source_post_id,
        tasks[0].source_version,
        _C,
        tasks[0].guide_version,
        "related",
        ("audit_usable_related",),
        _NOW,
    )
    first_import = import_text_keep_audit_annotations(
        database,
        audit_round_id=audit.audit_round_id,
        rows=(annotation,),
    )
    assert first_import == import_text_keep_audit_annotations(
        database,
        audit_round_id=audit.audit_round_id,
        rows=(annotation,),
    )
    first_evaluation = evaluate_and_seal_text_keep_audit(
        database,
        audit_round_id=audit.audit_round_id,
    )
    assert first_evaluation == evaluate_and_seal_text_keep_audit(
        database,
        audit_round_id=audit.audit_round_id,
    )
    assert first_evaluation.evaluation_status == "passed"
    assert first_evaluation.event_count == 0
    assert len(first_evaluation.platform_slices) == 1
    assert (
        first_evaluation.platform_slices[0].platform_key,
        first_evaluation.platform_slices[0].population_count,
        first_evaluation.platform_slices[0].sample_count,
        first_evaluation.platform_slices[0].completed_count,
        first_evaluation.platform_slices[0].event_count,
        first_evaluation.platform_slices[0].point_estimate,
    ) == ("xiaohongshu", 1, 1, 1, 0, 0.0)

    # 持久化行必须与应用层从真实 observation 重算的切片逐字段一致；封存后
    # schema 禁止修改，不能把某个平台的事件数另行粉饰。
    with connect_derived(database) as connection:
        stored = connection.execute(
            """
            SELECT platform_key, population_count, sample_count,
                   completed_count, event_count, event_point_estimate
            FROM text_keep_audit_platform_evaluations
            WHERE audit_evaluation_id = ?
            """,
            (first_evaluation.audit_evaluation_id,),
        ).fetchone()
        assert tuple(stored) == ("xiaohongshu", 1, 1, 1, 0, 0.0)
        with pytest.raises(sqlite3.IntegrityError, match="platform slices are immutable"):
            connection.execute(
                """
                UPDATE text_keep_audit_platform_evaluations SET event_count = 1
                WHERE audit_evaluation_id = ? AND platform_key = 'xiaohongshu'
                """,
                (first_evaluation.audit_evaluation_id,),
            )


def test_same_population_cannot_be_reseeded_or_repeated_across_rounds(
    tmp_path: Path,
) -> None:
    """同一人口不能用另一个 seed 新建第二轮挑选结果。"""

    database = tmp_path / "no-retry.sqlite"
    candidate, _ = _audit_round(database)
    with pytest.raises(TextKeepAuditRepositoryError) as error:
        create_text_keep_audit_round(
            database,
            run_id="run-1",
            candidate_decision_build_id=candidate.decision_build_id,
            audit_mode="formal",
            round_number=2,
            seed=18,
        )
    assert error.value.reason_code == "text_keep_audit_contract_rejected"


def test_annotations_require_real_hash_and_complete_sample(tmp_path: Path) -> None:
    """占位研究者身份被拒绝，缺标时不得伪造评估父对象。"""

    database = tmp_path / "annotation-guard.sqlite"
    _, audit = _audit_round(database)
    with pytest.raises(TextKeepAuditRepositoryError) as incomplete:
        evaluate_and_seal_text_keep_audit(
            database,
            audit_round_id=audit.audit_round_id,
        )
    assert incomplete.value.reason_code == "text_keep_audit_annotations_incomplete"

    task = export_text_keep_audit_tasks(database, audit_round_id=audit.audit_round_id)[0]
    with pytest.raises(TextKeepAuditRepositoryError) as invalid_hash:
        import_text_keep_audit_annotations(
            database,
            audit_round_id=audit.audit_round_id,
            rows=(
                TextKeepAuditAnnotationInput(
                    task.source_post_id,
                    task.source_version,
                    "researcher-name",
                    task.guide_version,
                    "related",
                    ("audit_usable_related",),
                    _NOW,
                ),
            ),
        )
    assert invalid_hash.value.reason_code == "text_keep_audit_annotation_contract_invalid"
