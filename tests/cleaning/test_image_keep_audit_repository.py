"""图片保留集两层审计仓储的合成人口集成测试。"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

import tourism_ugc_study.cleaning.schema as schema_module
from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.image_decision_repository import (
    build_image_decisions,
    propagate_exact_sha_labels,
)
from tourism_ugc_study.cleaning.image_keep_audit_repository import (
    ImageKeepAuditRepositoryError,
    create_keep_audit_round,
    evaluate_keep_audit_round,
    export_keep_audit_tasks,
    import_keep_audit_annotations,
)
from tourism_ugc_study.cleaning.image_keep_audit import (
    AuditPopulationItem,
    AuditSampleMember,
    AuditSamplePlan,
    build_keep_audit_sample,
)
from tourism_ugc_study.cleaning.image_repository import (
    build_image_candidates,
    import_image_manifest,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.image_review_repository import (
    create_double_label_plan,
    create_image_review_run,
    export_image_annotation_tasks,
    import_image_annotations,
)
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.schema import connect_derived, migrate_derived
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tests.cleaning.test_image_decision_repository import (
    _build_with_tiny_duplicate,
    _complete_formal_review_gate,
    _fill_by_fingerprint,
)
from tests.cleaning.test_image_repository import (
    CONFIG_PATH,
    _build_source,
    _route_map,
    _row,
    _sha,
    _write_manifest,
)


def _decisions(tmp_path: Path):
    """完成合成候选人工证据，生成决定与必要 SHA 传播。"""

    derived, config, build = _build_with_tiny_duplicate(tmp_path)
    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    template1 = tmp_path / "review1-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=1,
        output_path=template1,
    )
    with template1.open("r", encoding="utf-8", newline="") as stream:
        ids = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    # 所有候选均明确标为有效，路线图/推荐计划图形式不会自动排除。
    completed1 = tmp_path / "review1.csv"
    _fill_by_fingerprint(
        template1,
        completed1,
        labels={identity: "valid_content" for identity in ids},
        annotator="1" * 64,
    )
    import_image_annotations(derived, csv_path=completed1, imported_by_hash="a" * 64)
    _complete_formal_review_gate(derived, config, build, tmp_path, prefix="audit-gate")
    # valid_content 不会生成动态第二槽；这里不创建空拟排除计划。
    decisions = build_image_decisions(
        derived, candidate_build_id=build.build_id, config=config
    )
    propagate_exact_sha_labels(derived, decision_build_id=decisions.decision_build_id)
    return derived, config, decisions


def _large_non_census_decisions(tmp_path: Path):
    """构造 201 个同 SHA 内容关系，使审计主层必须稳定抽 200 个成员。"""

    source = tmp_path / "large-source.sqlite"
    derived = tmp_path / "processed" / "large-cleaning.sqlite"
    image_root = tmp_path / "large-images"
    image_root.mkdir()
    _build_source(source)
    # 在源快照前补足关系人口；每个关系仍保留独立 fingerprint 身份，但共享
    # 文件 SHA，因而决定只需审核代表图，保留集审计必须展开到全部关系。
    with sqlite3.connect(source) as connection:
        connection.executemany(
            """
            INSERT INTO web_posts(
              id, platform_key, platform_post_id, source_type, source_url,
              title, author_platform_id, captured_at, content_text,
              post_likes_count, post_images_count
            ) VALUES (?, 'xhs', ?, 'search', ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                (
                    identity,
                    f"p{identity}",
                    f"https://invalid/{identity}",
                    f"标题{identity}",
                    f"a{identity}",
                    f"2026-07-{(identity % 28) + 1:02d}",
                    f"正文{identity}",
                    identity,
                )
                for identity in range(5, 202)
            ],
        )
        connection.executemany(
            """
            INSERT INTO web_post_images(
              id, web_post_id, image_index, image_url, image_role
            ) VALUES (?, ?, 0, ?, 'content')
            """,
            [
                (identity, identity, f"https://invalid/i{identity}")
                for identity in range(5, 202)
            ],
        )

    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, "large-audit")
    discover_increment(derived, snapshot.snapshot_id, config)
    route = image_root / "route.png"
    _route_map(route)
    manifest = tmp_path / "large-manifest.csv"
    _write_manifest(
        manifest,
        [
            _row(identity, "content", "route.png", _sha(route))
            for identity in range(1, 202)
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id="large-audit",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest,
        image_root=image_root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=image_root,
        config=config,
    )
    build = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )
    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
    )
    template = tmp_path / "large-review-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=1,
        output_path=template,
    )
    with template.open("r", encoding="utf-8", newline="") as stream:
        ids = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    completed = tmp_path / "large-review.csv"
    _fill_by_fingerprint(
        template,
        completed,
        labels={identity: "valid_content" for identity in ids},
        annotator="1" * 64,
    )
    import_image_annotations(derived, csv_path=completed, imported_by_hash="a" * 64)
    _complete_formal_review_gate(
        derived,
        config,
        build,
        tmp_path,
        prefix="large-audit-gate",
    )
    decisions = build_image_decisions(
        derived,
        candidate_build_id=build.build_id,
        config=config,
    )
    propagate_exact_sha_labels(derived, decision_build_id=decisions.decision_build_id)
    return derived, config, decisions


def _canonical_sha256(value: object) -> str:
    """在负例测试中独立计算协议规范 JSON 摘要，不调用仓储私有函数。"""

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sample_manifest(members: tuple[AuditSampleMember, ...]) -> str:
    """按正式六字段协议计算测试审计成员 manifest。"""

    return _canonical_sha256(
        [
            [
                item.fingerprint_id,
                item.platform_key,
                item.sampling_layer,
                item.stable_rank,
                round(item.inclusion_probability, 15),
                round(item.sampling_weight, 15),
            ]
            for item in members
        ]
    )


def _audit_identity(
    population_rows: list[sqlite3.Row],
    *,
    decision_build_id: str,
    round_number: int,
    seed: int,
    excluded_fingerprint_ids: set[str] | None = None,
) -> tuple[tuple[AuditPopulationItem, ...], AuditSamplePlan, str, str, str, str]:
    """按指定 seed 独立生成完整审计父身份，供 direct SQL 防绕过测试。"""

    population = tuple(
        AuditPopulationItem(str(row["fingerprint_id"]), str(row["platform_key"]))
        for row in population_rows
    )
    plan = build_keep_audit_sample(
        population,
        seed=seed,
        primary_size=200,
        platform_supplement_min=30,
        excluded_fingerprint_ids=excluded_fingerprint_ids or (),
    )
    population_manifest = _canonical_sha256(
        [[item.fingerprint_id, item.platform_key] for item in population]
    )
    primary_manifest = _sample_manifest(plan.primary_members)
    supplement_manifest = _sample_manifest(plan.supplement_members)
    audit_round_id = _canonical_sha256(
        [
            "image-keep-audit-v2",
            decision_build_id,
            round_number,
            population_manifest,
            primary_manifest,
            supplement_manifest,
        ]
    )[:32]
    return (
        population,
        plan,
        population_manifest,
        primary_manifest,
        supplement_manifest,
        audit_round_id,
    )


def _insert_self_consistent_audit_round(
    connection: sqlite3.Connection,
    *,
    decision_build_id: str,
    round_number: int,
    seed: int,
    population: tuple[AuditPopulationItem, ...],
    plan: AuditSamplePlan,
    population_manifest: str,
    primary_manifest: str,
    supplement_manifest: str,
    audit_round_id: str,
) -> None:
    """写入字段、人口和成员完全自洽的 building 轮，不替调用方封存。"""

    connection.execute(
        """
        INSERT INTO image_keep_audit_rounds(
          audit_round_id, decision_build_id, round_number, random_seed,
          population_count, population_manifest_sha256, primary_count,
          supplement_count, primary_manifest_sha256,
          supplement_manifest_sha256, interval_method, seal_status,
          created_at_utc, integrity_status, sampling_algorithm_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building',
                  '2026-08-01T01:00:00Z', 'building',
                  'image-keep-audit-sampling-v1')
        """,
        (
            audit_round_id,
            decision_build_id,
            round_number,
            seed,
            plan.population_count,
            population_manifest,
            len(plan.primary_members),
            len(plan.supplement_members),
            primary_manifest,
            supplement_manifest,
            plan.interval_method,
        ),
    )
    connection.executemany(
        """
        INSERT INTO image_keep_audit_population_members(
          audit_round_id, fingerprint_id, platform_key, population_rank
        ) VALUES (?, ?, ?, ?)
        """,
        [
            (audit_round_id, item.fingerprint_id, item.platform_key, rank)
            for rank, item in enumerate(population, start=1)
        ],
    )
    connection.executemany(
        """
        INSERT INTO image_keep_audit_members(
          audit_round_id, fingerprint_id, platform_key, sampling_layer,
          stable_rank, inclusion_probability, sampling_weight
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                audit_round_id,
                item.fingerprint_id,
                item.platform_key,
                item.sampling_layer,
                item.stable_rank,
                item.inclusion_probability,
                item.sampling_weight,
            )
            for item in (*plan.primary_members, *plan.supplement_members)
        ],
    )


def _two_cluster_candidate_build(tmp_path: Path):
    """构造两个互斥的双成员 SHA 候选簇，供跨轮人口完全替换测试。"""

    source = tmp_path / "replacement-source.sqlite"
    derived = tmp_path / "processed" / "replacement-cleaning.sqlite"
    image_root = tmp_path / "replacement-images"
    image_root.mkdir()
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, "replacement-audit")
    discover_increment(derived, snapshot.snapshot_id, config)
    tiny = image_root / "tiny.png"
    route = image_root / "route.png"
    Image.new("RGBA", (24, 24), (0, 0, 0, 0)).save(tiny)
    _route_map(route)
    manifest = tmp_path / "replacement-manifest.csv"
    _write_manifest(
        manifest,
        [
            _row(1, "content", "tiny.png", _sha(tiny)),
            _row(2, "content", "tiny.png", _sha(tiny)),
            _row(3, "content", "route.png", _sha(route)),
            _row(4, "content", "route.png", _sha(route)),
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id="replacement-audit",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest,
        image_root=image_root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=image_root,
        config=config,
    )
    build = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )
    _complete_formal_review_gate(
        derived,
        config,
        build,
        tmp_path,
        prefix="replacement-audit-gate",
    )
    with connect_derived(derived) as connection:
        representatives = {
            str(row["file_sha256"]): str(row["representative_fingerprint_id"])
            for row in connection.execute(
                """
                SELECT file_sha256, representative_fingerprint_id
                FROM image_exact_clusters WHERE build_id = ?
                """,
                (build.build_id,),
            )
        }
    assert set(representatives) == {_sha(tiny), _sha(route)}
    return derived, config, build, representatives[_sha(tiny)], representatives[_sha(route)]


def _build_labeled_decisions(
    derived: Path,
    config,
    build,
    tmp_path: Path,
    *,
    prefix: str,
    seed: int,
    labels: dict[str, str],
    annotator_pair: tuple[str, str],
    importer_pair: tuple[str, str],
):
    """用独立候选复核运行形成一份可追溯决定快照。"""

    review = create_image_review_run(
        derived,
        candidate_build_id=build.build_id,
        review_kind="candidate_review",
        config=config,
        random_seed=seed,
    )
    slot1_template = tmp_path / f"{prefix}-slot1-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=1,
        output_path=slot1_template,
    )
    slot1 = tmp_path / f"{prefix}-slot1.csv"
    _fill_by_fingerprint(
        slot1_template,
        slot1,
        labels=labels,
        annotator=annotator_pair[0],
    )
    import_image_annotations(
        derived,
        csv_path=slot1,
        imported_by_hash=importer_pair[0],
    )
    create_double_label_plan(
        derived,
        review_run_id=review.review_run_id,
        plan_kind="proposed_exclusion",
    )
    slot2_template = tmp_path / f"{prefix}-slot2-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=review.review_run_id,
        assignment_slot=2,
        output_path=slot2_template,
    )
    with slot2_template.open("r", encoding="utf-8", newline="") as stream:
        excluded_ids = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    assert excluded_ids
    slot2 = tmp_path / f"{prefix}-slot2.csv"
    _fill_by_fingerprint(
        slot2_template,
        slot2,
        labels={identity: labels[identity] for identity in excluded_ids},
        annotator=annotator_pair[1],
    )
    import_image_annotations(
        derived,
        csv_path=slot2,
        imported_by_hash=importer_pair[1],
    )
    decisions = build_image_decisions(
        derived,
        candidate_build_id=build.build_id,
        candidate_review_run_id=review.review_run_id,
        config=config,
    )
    propagate_exact_sha_labels(derived, decision_build_id=decisions.decision_build_id)
    return decisions


def _fill_audit(template: Path, output: Path, *, label: str) -> None:
    with template.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or ())
        rows = list(reader)
    for row in rows:
        row["technical_noise_label"] = label
        row["reason_codes"] = "synthetic_fixture"
        row["annotator_hash"] = "9" * 64
        row["annotated_at_utc"] = "2026-07-31T03:00:00Z"
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_small_keep_population_is_census_and_passes_only_after_complete_labels(
    tmp_path: Path,
) -> None:
    derived, config, decisions = _decisions(tmp_path)
    audit = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    assert audit.interval_method == "census"
    assert audit.primary_count == audit.population_count == 3
    assert audit.supplement_count == 0
    assert create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    ) == audit
    template = tmp_path / "audit-template.csv"
    assert export_keep_audit_tasks(
        derived, audit_round_id=audit.audit_round_id, output_path=template
    ) == 3
    incomplete = evaluate_keep_audit_round(
        derived, audit_round_id=audit.audit_round_id, config=config
    )
    assert incomplete.evaluation_status == "incomplete"

    completed = tmp_path / "audit.csv"
    _fill_audit(template, completed, label="valid_content")
    imported = import_keep_audit_annotations(derived, csv_path=completed)
    assert imported.imported_count == 3
    passed = evaluate_keep_audit_round(
        derived, audit_round_id=audit.audit_round_id, config=config
    )
    assert passed.evaluation_status == "passed"
    assert passed.primary_point_estimate == 0.0
    assert passed.one_sided_upper == 0.0
    with connect_derived(derived) as connection:
        stored = connection.execute(
            """
            SELECT seal_status FROM image_keep_audit_evaluations
            WHERE audit_evaluation_id = ?
            """,
            (passed.audit_evaluation_id,),
        ).fetchone()
        linked = connection.execute(
            """
            SELECT COUNT(*) FROM image_keep_audit_evaluation_annotations
            WHERE audit_evaluation_id = ?
            """,
            (passed.audit_evaluation_id,),
        ).fetchone()[0]
        assert stored["seal_status"] == "finalized"
        assert linked == audit.population_count


def test_any_census_noise_fails_round(tmp_path: Path) -> None:
    derived, config, decisions = _decisions(tmp_path)
    audit = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    template = tmp_path / "audit-fail-template.csv"
    export_keep_audit_tasks(
        derived, audit_round_id=audit.audit_round_id, output_path=template
    )
    completed = tmp_path / "audit-fail.csv"
    _fill_audit(template, completed, label="placeholder_or_error")
    import_keep_audit_annotations(derived, csv_path=completed)
    failed = evaluate_keep_audit_round(
        derived, audit_round_id=audit.audit_round_id, config=config
    )
    assert failed.evaluation_status == "failed"
    assert failed.primary_event_count == 3


def test_direct_sql_cannot_self_attest_failed_audit_without_labels(
    tmp_path: Path,
) -> None:
    """伪造 failed 不能在没有审计标注时打开决定修订后的下一轮。"""

    derived, config, decisions = _decisions(tmp_path)
    audit = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    with connect_derived(derived) as connection:
        connection.execute(
            """
            INSERT INTO image_keep_audit_evaluations(
              audit_evaluation_id, audit_round_id, completed_count,
              primary_event_count, supplement_event_count,
              primary_point_estimate, one_sided_upper, evaluation_status,
              reason_code, evidence_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('fake-audit-failed', ?, ?, ?, 0, 1.0, 1.0, 'failed',
                      'residual_technical_noise_detected', ?, 'building',
                      '2026-07-31T04:30:00Z')
            """,
            (audit.audit_round_id, audit.primary_count, audit.primary_count, "a" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE image_keep_audit_evaluations SET seal_status = 'finalized'
                WHERE audit_evaluation_id = 'fake-audit-failed'
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_evaluations(
                  audit_evaluation_id, audit_round_id, completed_count,
                  primary_event_count, supplement_event_count,
                  primary_point_estimate, one_sided_upper, evaluation_status,
                  reason_code, evidence_manifest_sha256, seal_status,
                  created_at_utc
                ) VALUES ('fake-audit-finalized', ?, ?, ?, 0, 1.0, 1.0,
                          'failed', 'residual_technical_noise_detected', ?,
                          'finalized', '2026-07-31T04:31:00Z')
                """,
                (audit.audit_round_id, audit.primary_count, audit.primary_count, "b" * 64),
            )


def test_v21_rejects_one_primary_claimed_as_census_and_public_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v20 可封存的单张伪 census 升级后不得成为可信轮或公开评估输入。"""

    original_v21 = schema_module._SCHEMA_V21
    original_v22 = schema_module._SCHEMA_V22
    monkeypatch.setattr(schema_module, "_SCHEMA_V21", "")
    monkeypatch.setattr(schema_module, "_SCHEMA_V22", "")
    derived, config, decisions = _decisions(tmp_path)
    with connect_derived(derived) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        population = connection.execute(
            """
            SELECT m.fingerprint_id, p.platform_key
            FROM image_decision_builds b
            JOIN image_decisions d ON d.decision_build_id = b.decision_build_id
              AND d.decision_action IN ('keep', 'review')
            JOIN image_exact_clusters c ON c.build_id = b.candidate_build_id
              AND c.representative_fingerprint_id = d.fingerprint_id
            JOIN image_exact_cluster_members m ON m.build_id = c.build_id
              AND m.cluster_id = c.cluster_id
            JOIN image_candidate_build_members bm ON bm.build_id = m.build_id
              AND bm.fingerprint_id = m.fingerprint_id
            JOIN source_post_inventory p ON p.source_post_id = bm.source_post_id
            WHERE b.decision_build_id = ? ORDER BY m.fingerprint_id
            """,
            (decisions.decision_build_id,),
        ).fetchall()
        assert len(population) > 1
        connection.execute(
            """
            INSERT INTO image_keep_audit_rounds(
              audit_round_id, decision_build_id, round_number, random_seed,
              population_count, population_manifest_sha256, primary_count,
              supplement_count, primary_manifest_sha256,
              supplement_manifest_sha256, interval_method, seal_status,
              created_at_utc
            ) VALUES ('legacy-one-primary-census', ?, 1, ?, ?, ?, 1, 0,
                      ?, ?, 'census', 'building', '2026-08-01T00:00:00Z')
            """,
            (
                decisions.decision_build_id,
                config.random_seed,
                len(population),
                "a" * 64,
                "b" * 64,
                "c" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO image_keep_audit_members(
              audit_round_id, fingerprint_id, platform_key, sampling_layer,
              stable_rank, inclusion_probability, sampling_weight
            ) VALUES ('legacy-one-primary-census', ?, ?, 'primary', 1, 1.0, 1.0)
            """,
            (population[0]["fingerprint_id"], population[0]["platform_key"]),
        )
        connection.execute(
            """
            UPDATE image_keep_audit_rounds SET seal_status = 'finalized'
            WHERE audit_round_id = 'legacy-one-primary-census'
            """
        )
        connection.commit()
        monkeypatch.setattr(schema_module, "_SCHEMA_V21", original_v21)
        monkeypatch.setattr(schema_module, "_SCHEMA_V22", original_v22)
        migrate_derived(connection)
        assert connection.execute(
            """
            SELECT integrity_status FROM image_keep_audit_rounds
            WHERE audit_round_id = 'legacy-one-primary-census'
            """
        ).fetchone()[0] == "untrusted_legacy"
    with pytest.raises(ImageKeepAuditRepositoryError) as rejected:
        evaluate_keep_audit_round(
            derived,
            audit_round_id="legacy-one-primary-census",
            config=config,
        )
    assert rejected.value.reason_code == "image_keep_audit_round_untrusted"


def test_v21_direct_sql_rejects_malformed_population_and_selection(
    tmp_path: Path,
) -> None:
    """人口、层、概率、权重、样本量或 census 任一伪造都不能封存审计轮。"""

    derived, config, decisions = _decisions(tmp_path)
    with connect_derived(derived) as connection:
        population = connection.execute(
            """
            SELECT fingerprint_id, platform_key
            FROM image_keep_audit_actual_population
            WHERE decision_build_id = ? ORDER BY fingerprint_id
            """,
            (decisions.decision_build_id,),
        ).fetchall()
        assert len(population) > 1
        connection.execute(
            """
            INSERT INTO image_keep_audit_rounds(
              audit_round_id, decision_build_id, round_number, random_seed,
              population_count, population_manifest_sha256, primary_count,
              supplement_count, primary_manifest_sha256,
              supplement_manifest_sha256, interval_method, seal_status,
              created_at_utc, integrity_status, sampling_algorithm_version
            ) VALUES ('malformed-v21-round', ?, 1, ?, ?, ?, 1, 0, ?, ?,
                      'census', 'building', '2026-08-01T00:05:00Z',
                      'building', 'image-keep-audit-sampling-v1')
            """,
            (
                decisions.decision_build_id,
                config.random_seed,
                len(population),
                "a" * 64,
                "b" * 64,
                "c" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_population_members(
                  audit_round_id, fingerprint_id, platform_key, population_rank
                ) VALUES ('malformed-v21-round', ?, 'wrong-platform', 1)
                """,
                (population[0]["fingerprint_id"],),
            )
        connection.executemany(
            """
            INSERT INTO image_keep_audit_population_members(
              audit_round_id, fingerprint_id, platform_key, population_rank
            ) VALUES ('malformed-v21-round', ?, ?, ?)
            """,
            [
                (row["fingerprint_id"], row["platform_key"], rank)
                for rank, row in enumerate(population, start=1)
            ],
        )
        expected = connection.execute(
            """
            SELECT * FROM image_keep_audit_expected_members
            WHERE audit_round_id = 'malformed-v21-round'
              AND sampling_layer = 'primary' ORDER BY stable_rank
            """
        ).fetchall()
        assert len(expected) == len(population)

        # 平台补充不能占据本应属于 primary 的身份。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_members(
                  audit_round_id, fingerprint_id, platform_key, sampling_layer,
                  stable_rank, inclusion_probability, sampling_weight
                ) VALUES ('malformed-v21-round', ?, ?, 'platform_supplement',
                          1, 1.0, 1.0)
                """,
                (expected[0]["fingerprint_id"], expected[0]["platform_key"]),
            )
        # 正确成员但错误概率或权重也在逐行写入时拒绝。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_members(
                  audit_round_id, fingerprint_id, platform_key, sampling_layer,
                  stable_rank, inclusion_probability, sampling_weight
                ) VALUES ('malformed-v21-round', ?, ?, 'primary', ?, 0.5, 2.0)
                """,
                (
                    expected[0]["fingerprint_id"],
                    expected[0]["platform_key"],
                    expected[0]["stable_rank"],
                ),
            )
        first = expected[0]
        connection.execute(
            """
            INSERT INTO image_keep_audit_members(
              audit_round_id, fingerprint_id, platform_key, sampling_layer,
              stable_rank, inclusion_probability, sampling_weight
            ) VALUES ('malformed-v21-round', ?, ?, 'primary', ?, ?, ?)
            """,
            (
                first["fingerprint_id"],
                first["platform_key"],
                first["stable_rank"],
                first["inclusion_probability"],
                first["sampling_weight"],
            ),
        )
        # 只有一条主样本、伪造 manifest/ID 且谎称 census 的父行无法封存。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE image_keep_audit_rounds
                SET seal_status = 'finalized', integrity_status = 'finalized'
                WHERE audit_round_id = 'malformed-v21-round'
                """
            )

    with pytest.raises(ImageKeepAuditRepositoryError) as rejected:
        evaluate_keep_audit_round(
            derived, audit_round_id="malformed-v21-round", config=config
        )
    assert rejected.value.reason_code == "image_keep_audit_round_untrusted"


def test_v21_direct_sql_rejects_wrong_non_census_primary_member(
    tmp_path: Path,
) -> None:
    """201 人口中未进入稳定前 200 的成员不能冒充非 census 主样本。"""

    derived, config, decisions = _large_non_census_decisions(tmp_path)
    with connect_derived(derived) as connection:
        population = connection.execute(
            """
            SELECT fingerprint_id, platform_key
            FROM image_keep_audit_actual_population
            WHERE decision_build_id = ? ORDER BY fingerprint_id
            """,
            (decisions.decision_build_id,),
        ).fetchall()
        assert len(population) == 201
        connection.execute(
            """
            INSERT INTO image_keep_audit_rounds(
              audit_round_id, decision_build_id, round_number, random_seed,
              population_count, population_manifest_sha256, primary_count,
              supplement_count, primary_manifest_sha256,
              supplement_manifest_sha256, interval_method, seal_status,
              created_at_utc, integrity_status, sampling_algorithm_version
            ) VALUES ('wrong-wilson-member', ?, 1, ?, 201, ?, 200, 0, ?, ?,
                      'wilson_one_sided_95', 'building',
                      '2026-08-01T00:07:00Z', 'building',
                      'image-keep-audit-sampling-v1')
            """,
            (
                decisions.decision_build_id,
                config.random_seed,
                "a" * 64,
                "b" * 64,
                "c" * 64,
            ),
        )
        connection.executemany(
            """
            INSERT INTO image_keep_audit_population_members(
              audit_round_id, fingerprint_id, platform_key, population_rank
            ) VALUES ('wrong-wilson-member', ?, ?, ?)
            """,
            [
                (row["fingerprint_id"], row["platform_key"], rank)
                for rank, row in enumerate(population, start=1)
            ],
        )
        expected = connection.execute(
            """
            SELECT fingerprint_id FROM image_keep_audit_expected_members
            WHERE audit_round_id = 'wrong-wilson-member'
              AND sampling_layer = 'primary'
            """
        ).fetchall()
        expected_ids = {row["fingerprint_id"] for row in expected}
        assert len(expected_ids) == 200
        outsider = next(
            row for row in population if row["fingerprint_id"] not in expected_ids
        )
        # 该关系属于真实人口，但其 SHA 排名为 201；逐行守卫必须在写入点拒绝，
        # 不能等到最终 manifest 校验才发现抽样者换过样本。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_members(
                  audit_round_id, fingerprint_id, platform_key, sampling_layer,
                  stable_rank, inclusion_probability, sampling_weight
                ) VALUES ('wrong-wilson-member', ?, ?, 'primary',
                          200, 200.0 / 201.0, 201.0 / 200.0)
                """,
                (outsider["fingerprint_id"], outsider["platform_key"]),
            )


def test_v22_direct_sql_rejects_self_consistent_wrong_seed_for_201_population(
    tmp_path: Path,
) -> None:
    """201 人口即使按错误 seed 重算全部父身份，写入仍须被运行谱系拒绝。"""

    derived, config, decisions = _large_non_census_decisions(tmp_path)
    wrong_seed = config.random_seed + 97
    with connect_derived(derived) as connection:
        rows = connection.execute(
            """
            SELECT fingerprint_id, platform_key
            FROM image_keep_audit_actual_population
            WHERE decision_build_id = ? ORDER BY fingerprint_id
            """,
            (decisions.decision_build_id,),
        ).fetchall()
        identity = _audit_identity(
            rows,
            decision_build_id=decisions.decision_build_id,
            round_number=1,
            seed=wrong_seed,
        )
        population, plan, population_manifest, primary_manifest, supplement_manifest, audit_id = identity
        assert len(population) == 201
        assert len(plan.primary_members) == 200
        assert plan.interval_method == "wilson_one_sided_95"
        # audit_id、三份 manifest、200 个成员及其概率/权重都已按错误 seed
        # 自洽重算；v22 必须只相信 candidate build 所属 cleaning run 的基种子。
        with pytest.raises(sqlite3.IntegrityError):
            _insert_self_consistent_audit_round(
                connection,
                decision_build_id=decisions.decision_build_id,
                round_number=1,
                seed=wrong_seed,
                population=population,
                plan=plan,
                population_manifest=population_manifest,
                primary_manifest=primary_manifest,
                supplement_manifest=supplement_manifest,
                audit_round_id=audit_id,
            )


@pytest.mark.parametrize("legacy_status", ["building", "finalized"])
def test_v22_migration_rejects_or_downgrades_self_consistent_wrong_seed_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_status: str,
) -> None:
    """v21 错误 seed 轮升级后不得封存，也不得保留可信 finalized 身份。"""

    original_v22 = schema_module._SCHEMA_V22
    monkeypatch.setattr(schema_module, "_SCHEMA_V22", "")
    derived, config, decisions = _decisions(tmp_path)
    wrong_seed = config.random_seed + 41
    with connect_derived(derived) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        rows = connection.execute(
            """
            SELECT fingerprint_id, platform_key
            FROM image_keep_audit_actual_population
            WHERE decision_build_id = ? ORDER BY fingerprint_id
            """,
            (decisions.decision_build_id,),
        ).fetchall()
        identity = _audit_identity(
            rows,
            decision_build_id=decisions.decision_build_id,
            round_number=1,
            seed=wrong_seed,
        )
        population, plan, population_manifest, primary_manifest, supplement_manifest, audit_id = identity
        _insert_self_consistent_audit_round(
            connection,
            decision_build_id=decisions.decision_build_id,
            round_number=1,
            seed=wrong_seed,
            population=population,
            plan=plan,
            population_manifest=population_manifest,
            primary_manifest=primary_manifest,
            supplement_manifest=supplement_manifest,
            audit_round_id=audit_id,
        )
        if legacy_status == "finalized":
            connection.execute(
                """
                UPDATE image_keep_audit_rounds
                SET seal_status = 'finalized', integrity_status = 'finalized'
                WHERE audit_round_id = ?
                """,
                (audit_id,),
            )
        connection.commit()

        monkeypatch.setattr(schema_module, "_SCHEMA_V22", original_v22)
        migrate_derived(connection)
        migrated = connection.execute(
            """
            SELECT seal_status, integrity_status
            FROM image_keep_audit_rounds WHERE audit_round_id = ?
            """,
            (audit_id,),
        ).fetchone()
        if legacy_status == "finalized":
            assert tuple(migrated) == ("finalized", "untrusted_legacy")
        else:
            assert tuple(migrated) == ("building", "building")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE image_keep_audit_rounds
                    SET seal_status = 'finalized', integrity_status = 'finalized'
                    WHERE audit_round_id = ?
                    """,
                    (audit_id,),
                )

    if legacy_status == "finalized":
        with pytest.raises(ImageKeepAuditRepositoryError) as rejected:
            evaluate_keep_audit_round(
                derived,
                audit_round_id=audit_id,
                config=config,
            )
        assert rejected.value.reason_code == "image_keep_audit_round_untrusted"


def test_v22_migration_preserves_revalidated_protocol_seed_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """符合运行基种子的 v21 可信轮经显式谱系重验后应保持 finalized。"""

    original_v22 = schema_module._SCHEMA_V22
    monkeypatch.setattr(schema_module, "_SCHEMA_V22", "")
    derived, config, decisions = _decisions(tmp_path)
    audit = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    with connect_derived(derived) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        connection.commit()
        monkeypatch.setattr(schema_module, "_SCHEMA_V22", original_v22)
        migrate_derived(connection)
        migrated = connection.execute(
            """
            SELECT seal_status, integrity_status, random_seed
            FROM image_keep_audit_rounds WHERE audit_round_id = ?
            """,
            (audit.audit_round_id,),
        ).fetchone()
        assert tuple(migrated) == (
            "finalized",
            "finalized",
            config.random_seed,
        )


def test_v21_rejects_zero_primary_supplement_round_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧版 0 primary＋补充层 failed 不得封存新评估或打开第二轮。"""

    original_v21 = schema_module._SCHEMA_V21
    original_v22 = schema_module._SCHEMA_V22
    monkeypatch.setattr(schema_module, "_SCHEMA_V21", "")
    monkeypatch.setattr(schema_module, "_SCHEMA_V22", "")
    derived, config, decisions = _decisions(tmp_path)
    with connect_derived(derived) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        member = connection.execute(
            """
            SELECT m.fingerprint_id, p.platform_key
            FROM image_decision_builds b
            JOIN image_decisions d ON d.decision_build_id = b.decision_build_id
              AND d.decision_action IN ('keep', 'review')
            JOIN image_exact_clusters c ON c.build_id = b.candidate_build_id
              AND c.representative_fingerprint_id = d.fingerprint_id
            JOIN image_exact_cluster_members m ON m.build_id = c.build_id
              AND m.cluster_id = c.cluster_id
            JOIN image_candidate_build_members bm ON bm.build_id = m.build_id
              AND bm.fingerprint_id = m.fingerprint_id
            JOIN source_post_inventory p ON p.source_post_id = bm.source_post_id
            WHERE b.decision_build_id = ? ORDER BY m.fingerprint_id LIMIT 1
            """,
            (decisions.decision_build_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO image_keep_audit_rounds(
              audit_round_id, decision_build_id, round_number, random_seed,
              population_count, population_manifest_sha256, primary_count,
              supplement_count, primary_manifest_sha256,
              supplement_manifest_sha256, interval_method, seal_status,
              created_at_utc
            ) VALUES ('legacy-supplement-only', ?, 1, ?, 1, ?, 0, 1, ?, ?,
                      'census', 'building', '2026-08-01T00:10:00Z')
            """,
            (
                decisions.decision_build_id,
                config.random_seed,
                "d" * 64,
                "e" * 64,
                "f" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO image_keep_audit_members(
              audit_round_id, fingerprint_id, platform_key, sampling_layer,
              stable_rank, inclusion_probability, sampling_weight
            ) VALUES ('legacy-supplement-only', ?, ?, 'platform_supplement',
                      1, 1.0, 1.0)
            """,
            (member["fingerprint_id"], member["platform_key"]),
        )
        connection.execute(
            """
            UPDATE image_keep_audit_rounds SET seal_status = 'finalized'
            WHERE audit_round_id = 'legacy-supplement-only'
            """
        )
        connection.commit()
        monkeypatch.setattr(schema_module, "_SCHEMA_V21", original_v21)
        monkeypatch.setattr(schema_module, "_SCHEMA_V22", original_v22)
        migrate_derived(connection)
        assert connection.execute(
            """
            SELECT integrity_status FROM image_keep_audit_rounds
            WHERE audit_round_id = 'legacy-supplement-only'
            """
        ).fetchone()[0] == "untrusted_legacy"
        connection.execute(
            """
            INSERT INTO image_keep_audit_annotations(
              audit_annotation_id, audit_round_id, fingerprint_id,
              annotator_hash, guide_version, technical_noise_label,
              reason_codes_json, annotated_at_utc, row_sha256
            ) VALUES ('supplement-only-annotation', 'legacy-supplement-only', ?,
                      ?, ?, 'site_ui', '["synthetic_fixture"]',
                      '2026-08-01T00:10:30Z', ?)
            """,
            (
                member["fingerprint_id"],
                "9" * 64,
                config.image_label_guide_version,
                "8" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO image_keep_audit_evaluations(
              audit_evaluation_id, audit_round_id, completed_count,
              primary_event_count, supplement_event_count,
              primary_point_estimate, one_sided_upper, evaluation_status,
              reason_code, evidence_manifest_sha256, seal_status, created_at_utc
            ) VALUES ('supplement-only-failed', 'legacy-supplement-only',
                      1, 0, 1, 0.0, 0.0, 'failed',
                      'residual_technical_noise_detected', ?, 'building',
                      '2026-08-01T00:10:31Z')
            """,
            ("7" * 64,),
        )
        connection.execute(
            """
            INSERT INTO image_keep_audit_evaluation_annotations(
              audit_evaluation_id, audit_annotation_id, audit_round_id,
              fingerprint_id, sampling_layer
            ) VALUES ('supplement-only-failed', 'supplement-only-annotation',
                      'legacy-supplement-only', ?, 'platform_supplement')
            """,
            (member["fingerprint_id"],),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE image_keep_audit_evaluations SET seal_status = 'finalized'
                WHERE audit_evaluation_id = 'supplement-only-failed'
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_rounds(
                  audit_round_id, decision_build_id, round_number, random_seed,
                  population_count, population_manifest_sha256, primary_count,
                  supplement_count, primary_manifest_sha256,
                  supplement_manifest_sha256, interval_method, seal_status,
                  created_at_utc, integrity_status, sampling_algorithm_version
                ) VALUES ('direct-round-two', ?, 2, ?, 1, ?, 1, 0, ?, ?,
                          'census', 'building', '2026-08-01T00:11:00Z',
                          'building', 'image-keep-audit-sampling-v1')
                """,
                (
                    decisions.decision_build_id,
                    config.random_seed + 1,
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                ),
            )
    with pytest.raises(ImageKeepAuditRepositoryError) as rejected:
        create_keep_audit_round(
            derived,
            decision_build_id=decisions.decision_build_id,
            round_number=2,
            config=config,
        )
    assert rejected.value.reason_code == "previous_image_keep_audit_not_evaluated"


def test_next_audit_round_requires_completed_failed_previous_round(
    tmp_path: Path,
) -> None:
    """上一轮未评估或已通过时，都不能为了凑轮次继续抽样。"""

    derived, config, decisions = _decisions(tmp_path)
    first = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    with pytest.raises(ImageKeepAuditRepositoryError) as incomplete:
        create_keep_audit_round(
            derived,
            decision_build_id=decisions.decision_build_id,
            round_number=2,
            config=config,
        )
    assert incomplete.value.reason_code == "previous_image_keep_audit_not_evaluated"

    template = tmp_path / "passed-round-template.csv"
    export_keep_audit_tasks(
        derived, audit_round_id=first.audit_round_id, output_path=template
    )
    completed = tmp_path / "passed-round.csv"
    _fill_audit(template, completed, label="valid_content")
    import_keep_audit_annotations(derived, csv_path=completed)
    assert evaluate_keep_audit_round(
        derived, audit_round_id=first.audit_round_id, config=config
    ).evaluation_status == "passed"
    with pytest.raises(ImageKeepAuditRepositoryError) as passed:
        create_keep_audit_round(
            derived,
            decision_build_id=decisions.decision_build_id,
            round_number=2,
            config=config,
        )
    assert passed.value.reason_code == "previous_image_keep_audit_not_failed"


def test_failed_round_requires_changed_decisions_before_nonoverlap_retry(
    tmp_path: Path,
) -> None:
    """失败后必须形成决定内容不同的新 build，且旧样本仍不得复抽。"""

    derived, config, decisions = _decisions(tmp_path)
    first = create_keep_audit_round(
        derived,
        decision_build_id=decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    template = tmp_path / "failed-round-template.csv"
    export_keep_audit_tasks(
        derived, audit_round_id=first.audit_round_id, output_path=template
    )
    completed = tmp_path / "failed-round.csv"
    _fill_audit(template, completed, label="site_ui")
    import_keep_audit_annotations(derived, csv_path=completed)
    assert evaluate_keep_audit_round(
        derived, audit_round_id=first.audit_round_id, config=config
    ).evaluation_status == "failed"

    with pytest.raises(ImageKeepAuditRepositoryError) as unchanged:
        create_keep_audit_round(
            derived,
            decision_build_id=decisions.decision_build_id,
            round_number=2,
            config=config,
        )
    assert unchanged.value.reason_code == "revised_image_decision_build_required"
    with connect_derived(derived) as connection:
        # 即使绕开仓储直接 INSERT，未形成新决定 build 的第二轮也必须失败。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_keep_audit_rounds(
                  audit_round_id, decision_build_id, round_number, random_seed,
                  population_count, population_manifest_sha256, primary_count,
                  supplement_count, primary_manifest_sha256,
                  supplement_manifest_sha256, interval_method, seal_status,
                  created_at_utc
                ) VALUES ('direct-unchanged-round', ?, 2, 20260732, 0, ?, 0, 0,
                          ?, ?, 'census', 'building', '2026-07-31T06:00:00Z')
                """,
                (decisions.decision_build_id, "1" * 64, "2" * 64, "3" * 64),
            )

    revised_review = create_image_review_run(
        derived,
        candidate_build_id=decisions.candidate_build_id,
        review_kind="candidate_review",
        config=config,
        random_seed=config.random_seed + 1,
    )
    revised_slot1_template = tmp_path / "revised-slot1-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=revised_review.review_run_id,
        assignment_slot=1,
        output_path=revised_slot1_template,
    )
    with revised_slot1_template.open("r", encoding="utf-8", newline="") as stream:
        identities = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    excluded_id = identities[0]
    revised_slot1 = tmp_path / "revised-slot1.csv"
    labels = {identity: "valid_content" for identity in identities}
    labels[excluded_id] = "site_ui"
    _fill_by_fingerprint(
        revised_slot1_template,
        revised_slot1,
        labels=labels,
        annotator="b" * 64,
    )
    import_image_annotations(
        derived, csv_path=revised_slot1, imported_by_hash="c" * 64
    )
    create_double_label_plan(
        derived,
        review_run_id=revised_review.review_run_id,
        plan_kind="proposed_exclusion",
    )
    revised_slot2_template = tmp_path / "revised-slot2-template.csv"
    export_image_annotation_tasks(
        derived,
        review_run_id=revised_review.review_run_id,
        assignment_slot=2,
        output_path=revised_slot2_template,
    )
    revised_slot2 = tmp_path / "revised-slot2.csv"
    _fill_by_fingerprint(
        revised_slot2_template,
        revised_slot2,
        labels={excluded_id: "site_ui"},
        annotator="d" * 64,
    )
    import_image_annotations(
        derived, csv_path=revised_slot2, imported_by_hash="e" * 64
    )
    revised = build_image_decisions(
        derived,
        candidate_build_id=decisions.candidate_build_id,
        config=config,
        candidate_review_run_id=revised_review.review_run_id,
    )
    assert revised.decision_manifest_sha256 != decisions.decision_manifest_sha256
    propagate_exact_sha_labels(
        derived, decision_build_id=revised.decision_build_id
    )
    with pytest.raises(ImageKeepAuditRepositoryError) as exhausted:
        create_keep_audit_round(
            derived,
            decision_build_id=revised.decision_build_id,
            round_number=2,
            config=config,
        )
    assert exhausted.value.reason_code == "image_keep_audit_population_exhausted"


def test_revised_decisions_with_fully_replaced_population_allow_second_census(
    tmp_path: Path,
) -> None:
    """旧样本已离开当前人口时，第二轮应全查替换人口并可完成可信评估。"""

    derived, config, build, tiny_rep, route_rep = _two_cluster_candidate_build(
        tmp_path
    )
    first_decisions = _build_labeled_decisions(
        derived,
        config,
        build,
        tmp_path,
        prefix="replacement-first",
        seed=config.random_seed,
        labels={tiny_rep: "valid_content", route_rep: "site_ui"},
        annotator_pair=("1" * 64, "2" * 64),
        importer_pair=("3" * 64, "4" * 64),
    )
    first = create_keep_audit_round(
        derived,
        decision_build_id=first_decisions.decision_build_id,
        round_number=1,
        config=config,
    )
    assert first.interval_method == "census"
    assert first.population_count == first.primary_count == 2
    first_template = tmp_path / "replacement-first-audit-template.csv"
    export_keep_audit_tasks(
        derived,
        audit_round_id=first.audit_round_id,
        output_path=first_template,
    )
    first_completed = tmp_path / "replacement-first-audit.csv"
    _fill_audit(first_template, first_completed, label="site_ui")
    import_keep_audit_annotations(derived, csv_path=first_completed)
    assert evaluate_keep_audit_round(
        derived,
        audit_round_id=first.audit_round_id,
        config=config,
    ).evaluation_status == "failed"

    revised_decisions = _build_labeled_decisions(
        derived,
        config,
        build,
        tmp_path,
        prefix="replacement-second",
        seed=config.random_seed + 1,
        labels={tiny_rep: "site_ui", route_rep: "valid_content"},
        annotator_pair=("5" * 64, "6" * 64),
        importer_pair=("7" * 64, "8" * 64),
    )
    assert (
        revised_decisions.decision_manifest_sha256
        != first_decisions.decision_manifest_sha256
    )
    second = create_keep_audit_round(
        derived,
        decision_build_id=revised_decisions.decision_build_id,
        round_number=2,
        config=config,
    )
    assert second.interval_method == "census"
    assert second.population_count == second.primary_count == 2
    assert second.supplement_count == 0
    with connect_derived(derived) as connection:
        round_members = {
            round_id: {
                str(row["fingerprint_id"])
                for row in connection.execute(
                    """
                    SELECT fingerprint_id FROM image_keep_audit_members
                    WHERE audit_round_id = ?
                    """,
                    (round_id,),
                )
            }
            for round_id in (first.audit_round_id, second.audit_round_id)
        }
        assert round_members[first.audit_round_id].isdisjoint(
            round_members[second.audit_round_id]
        )
        second_design = connection.execute(
            """
            SELECT MIN(inclusion_probability), MAX(inclusion_probability),
                   MIN(sampling_weight), MAX(sampling_weight),
                   r.seal_status, r.integrity_status, r.random_seed
            FROM image_keep_audit_members m
            JOIN image_keep_audit_rounds r
              ON r.audit_round_id = m.audit_round_id
            WHERE m.audit_round_id = ?
            """,
            (second.audit_round_id,),
        ).fetchone()
        assert tuple(second_design) == (
            1.0,
            1.0,
            1.0,
            1.0,
            "finalized",
            "finalized",
            config.random_seed + 1,
        )

    second_template = tmp_path / "replacement-second-audit-template.csv"
    export_keep_audit_tasks(
        derived,
        audit_round_id=second.audit_round_id,
        output_path=second_template,
    )
    second_completed = tmp_path / "replacement-second-audit.csv"
    _fill_audit(second_template, second_completed, label="valid_content")
    import_keep_audit_annotations(derived, csv_path=second_completed)
    assert evaluate_keep_audit_round(
        derived,
        audit_round_id=second.audit_round_id,
        config=config,
    ).evaluation_status == "passed"
