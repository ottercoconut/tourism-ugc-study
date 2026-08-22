"""唯一最终参考 artifact、验证器与训练输入拒绝边界测试。"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from tourism_ugc_study.annotation.reference_artifacts import (
    LegacyReferenceInput,
    write_final_reference_artifacts,
)
from tourism_ugc_study.annotation.reference_contract import (
    DuplicateDecision,
    FINAL_REFERENCE_FIELDS,
    PairIdentity,
    ReferenceDatasetError,
    ReferencePost,
    SourceIdentity,
    canonical_sha256,
)
from tourism_ugc_study.annotation.reference_candidates import (
    CandidateComputation,
    duplicate_algorithm_identity,
)
from tourism_ugc_study.annotation.reference_human_evidence import (
    build_duplicate_component_plan,
)
from tourism_ugc_study.annotation.reference_replacements import ReplacementSelection
from tourism_ugc_study.cleaning.reference_evidence import (
    ReferenceEvidenceError,
    _load_rows,
    _row_projection,
    _sample_member_manifest,
    validate_reference_evidence as _raw_validate_reference_evidence,
)
from tourism_ugc_study.cleaning.reference_projection import (
    ProjectedReferenceText,
    ReferenceTextProjection,
)
from tourism_ugc_study.cleaning.config import load_stable_config
from tourism_ugc_study.cleaning.text_config import load_text_config
from tourism_ugc_study.models.text.formal_training import load_baseline_evidence


GUIDE_ID = "text-cleaning-v1.5"
NORMALIZATION_ID = "text-normalization-v1+sha256:" + "a" * 64
HASH = "b" * 64
ROOT = Path(__file__).resolve().parents[2]
TEXT_CONFIG_PATH = ROOT / "configs/cleaning-text-normalization-v1.yaml"
COMPUTATION = CandidateComputation(
    (), 244650, 244650, HASH, HASH, duplicate_algorithm_identity()
)


def test_final_reference_short_row_uses_stable_reason_code(tmp_path: Path) -> None:
    """最终 CSV 短行必须在字段投影前稳定失败关闭。"""

    csv_path = tmp_path / "short-final.csv"
    csv_path.write_text(
        ",".join(FINAL_REFERENCE_FIELDS) + "\nonly-one-cell\n",
        encoding="utf-8",
    )

    with pytest.raises(ReferenceEvidenceError) as error:
        _load_rows(csv_path)

    assert error.value.reason_code == "final_reference_csv_row_structure_invalid"


def _final_posts() -> tuple[ReferencePost, ...]:
    """构造500/200、标签完整且身份唯一的最终成员。"""

    rows: list[ReferencePost] = []
    for index in range(1, 701):
        probability = index <= 500
        rows.append(
            ReferencePost(
                identity=SourceIdentity(index, 1),
                task_id=canonical_sha256(["sample-1", index])[:32],
                normalized_model_text=f"[TITLE]\n青岛参考文本{index:04d}\n[BODY]\n唯一内容{index:04d}",
                tourism_label="related" if index % 2 else "unrelated",
                sample_frame="probability" if probability else "targeted",
                selection_reason_code="initial_probability" if probability else "initial_targeted",
                selection_rank=index if probability else index - 500,
                inclusion_probability=0.5 if probability else None,
                analysis_weight=2.0 if probability else None,
                evidence_origin="existing_representative",
            )
        )
    return tuple(rows)


def _database(path: Path, posts: tuple[ReferencePost, ...]) -> None:
    """建立最终验证器所需的最小只读派生库契约。"""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE text_candidate_builds(
            build_id TEXT PRIMARY KEY, rules_version TEXT, rules_sha256 TEXT,
            status TEXT, is_complete_corpus INTEGER
        );
        CREATE TABLE text_candidate_corpus_members(
            build_id TEXT, task_id TEXT, source_post_id INTEGER,
            source_version INTEGER, structure_status TEXT
        );
        CREATE TABLE text_deterministic_results(
            task_id TEXT PRIMARY KEY, normalized_model_text TEXT
        );
        CREATE TABLE text_post_annotations(annotation_id TEXT);
        CREATE TABLE text_sampling_runs(
            sample_run_id TEXT PRIMARY KEY, candidate_build_id TEXT,
            guide_version TEXT, probability_count INTEGER,
            targeted_count INTEGER, seal_status TEXT,
            member_manifest_sha256 TEXT
        );
        CREATE TABLE text_sample_members(
            sample_run_id TEXT, source_post_id INTEGER, source_version INTEGER,
            platform_key TEXT, sample_frame TEXT, selection_reason_code TEXT,
            selection_rank INTEGER, inclusion_probability_ppm INTEGER,
            analysis_weight REAL
        );
        CREATE TABLE source_post_inventory(
            source_post_id INTEGER PRIMARY KEY, captured_at_sort TEXT
        );
        CREATE TABLE text_leakage_builds(
            leakage_build_id TEXT PRIMARY KEY, candidate_build_id TEXT,
            output_sha256 TEXT, seal_status TEXT, input_post_count INTEGER
        );
        CREATE TABLE text_leakage_members(
            leakage_build_id TEXT, component_id TEXT, source_post_id INTEGER,
            source_version INTEGER, author_edge_used INTEGER,
            exact_edge_used INTEGER, confirmed_near_edge_used INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO text_candidate_builds VALUES (?,?,?,?,?)",
        ("candidate-1", "text-normalization-v1", "a" * 64, "finalized", 1),
    )
    sample_members = [
        {
            "source_post_id": post.identity.source_post_id,
            "source_version": post.identity.source_version,
            "platform_key": f"platform-{post.identity.source_post_id % 5}",
            "sample_frame": post.sample_frame,
            "selection_reason_code": post.selection_reason_code,
            "selection_rank": post.selection_rank,
            "inclusion_probability_ppm": (
                500_000 if post.sample_frame == "probability" else None
            ),
            "analysis_weight": post.analysis_weight,
        }
        for post in posts
    ]
    ordered_sample_members = sorted(
        sample_members,
        key=lambda row: (
            1 if row["sample_frame"] == "probability" else 2,
            row["selection_rank"],
            row["source_post_id"],
            row["source_version"],
        ),
    )
    sample_hash = canonical_sha256(ordered_sample_members)
    connection.execute(
        "INSERT INTO text_sampling_runs VALUES (?,?,?,?,?,?,?)",
        ("sample-1", "candidate-1", GUIDE_ID, 500, 200, "finalized", sample_hash),
    )
    connection.executemany(
        "INSERT INTO text_sample_members VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "sample-1",
                row["source_post_id"],
                row["source_version"],
                row["platform_key"],
                row["sample_frame"],
                row["selection_reason_code"],
                row["selection_rank"],
                row["inclusion_probability_ppm"],
                row["analysis_weight"],
            )
            for row in sample_members
        ],
    )
    connection.executemany(
        "INSERT INTO text_candidate_corpus_members VALUES (?,?,?,?,?)",
        [
            (
                "candidate-1",
                post.task_id,
                post.identity.source_post_id,
                post.identity.source_version,
                "usable",
            )
            for post in posts
        ],
    )
    connection.executemany(
        "INSERT INTO text_deterministic_results VALUES (?,?)",
        [(post.task_id, post.normalized_model_text) for post in posts],
    )
    connection.executemany(
        "INSERT INTO source_post_inventory VALUES (?,?)",
        [
            (post.identity.source_post_id, f"2026-01-{post.identity.source_post_id:04d}")
            for post in posts
        ],
    )
    leakage_members = [
        {
            "component_id": f"leakage-{post.identity.source_post_id}",
            "source_post_id": post.identity.source_post_id,
            "source_version": post.identity.source_version,
            "author_edge_used": False,
            "exact_edge_used": False,
            "confirmed_near_edge_used": False,
        }
        for post in posts
    ]
    leakage_hash = hashlib.sha256(
        json.dumps(
            leakage_members,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    connection.execute(
        "INSERT INTO text_leakage_builds VALUES (?,?,?,?,?)",
        ("leakage-1", "candidate-1", leakage_hash, "finalized", len(posts)),
    )
    connection.executemany(
        "INSERT INTO text_leakage_members VALUES (?,?,?,?,?,?,?)",
        [
            (
                "leakage-1",
                member["component_id"],
                member["source_post_id"],
                member["source_version"],
                0,
                0,
                0,
            )
            for member in leakage_members
        ],
    )
    connection.commit()
    connection.close()


def _artifacts(tmp_path: Path) -> tuple[Path, Path, Path, ReplacementSelection]:
    """写出可由正式验证器接受的最终配对 artifact。"""

    posts = _final_posts()
    plan = build_duplicate_component_plan(posts, (), ())
    selection = ReplacementSelection(
        final_posts=posts,
        dispositions=(),
        probability_estimation_status="valid",
        probability_estimation_reason_code=None,
        probability_replacement_count=0,
        targeted_replacement_count=0,
        confirmed_duplicate_pairs=(),
        duplicate_decision_sha256=canonical_sha256([]),
        reviewed_max_queue_rank=0,
        component_by_identity={},
    )
    legacy = LegacyReferenceInput(
        posts=posts,
        csv_sha256=HASH,
        manifest_sha256=HASH,
        input_member_sha256=HASH,
        sample_run_id="sample-1",
        candidate_build_id="candidate-1",
        candidate_build_sha256=HASH,
        label_guide_id=GUIDE_ID,
        normalization_rule_id=NORMALIZATION_ID,
        legacy_normalization_rule_id=NORMALIZATION_ID,
        projection_member_sha256=HASH,
        source_snapshot_sha256=HASH,
    )
    csv_path = tmp_path / "final-reference.csv"
    manifest_path = tmp_path / "final-reference.manifest.json"
    write_final_reference_artifacts(
        selection,
        plan,
        csv_path,
        manifest_path,
        legacy_input=legacy,
        initial_candidate_computation=COMPUTATION,
        initial_duplicate_decision_manifest_sha256=HASH,
        replacement_queue_sha256=HASH,
        label_guide_id=GUIDE_ID,
        candidate_build_id="candidate-1",
        normalization_rule_id=NORMALIZATION_ID,
        supplemental_label_csv_sha256=HASH,
        supplemental_label_manifest_sha256=HASH,
        label_resolution_evidence_sha256=HASH,
        label_resolution_manifest_sha256=HASH,
        replacement_label_resolution_evidence_sha256=HASH,
        replacement_label_resolution_manifest_sha256=HASH,
        replacement_candidate_computation=COMPUTATION,
        replacement_duplicate_decision_manifest_sha256=HASH,
    )
    database = tmp_path / "derived.sqlite"
    _database(database, posts)
    return csv_path, manifest_path, database, selection


def _current_normalization_config():
    """构造与测试 manifest 身份一致的结构化文本配置。"""

    return replace(load_text_config(TEXT_CONFIG_PATH), sha256="a" * 64)


def _source_projection(
    posts: tuple[ReferencePost, ...] | None = None,
) -> ReferenceTextProjection:
    """构造不依赖真实私有快照的冻结源投影测试替身。"""

    selected = _final_posts() if posts is None else posts
    by_identity = {
        (post.identity.source_post_id, post.identity.source_version): ProjectedReferenceText(
            source_post_id=post.identity.source_post_id,
            source_version=post.identity.source_version,
            normalized_model_text=post.normalized_model_text,
            normalized_sha256=post.normalized_sha256,
            structure_status="usable",
            structure_reason_code="structure_usable",
            output_sha256=HASH,
        )
        for post in selected
    }
    return ReferenceTextProjection(
        by_identity=MappingProxyType(by_identity),
        source_snapshot_id="snapshot-1",
        source_snapshot_sha256=HASH,
        normalization_rule_id=NORMALIZATION_ID,
        member_sha256=HASH,
        format_counts=MappingProxyType({"plain_text": len(selected)}),
        invalid_reason_counts=MappingProxyType({}),
    )


@pytest.fixture(autouse=True)
def _freeze_source_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """让最小测试库通过与生产等价的冻结源投影验证边界。"""

    monkeypatch.setattr(
        "tourism_ugc_study.cleaning.reference_evidence.load_reference_text_projection",
        lambda *_args, **_kwargs: _source_projection(),
    )


def _validate_reference_evidence(*args: object, **kwargs: object):
    """为最终验证器测试统一注入必需的当前规范化规则。"""

    kwargs.setdefault("normalization_config", _current_normalization_config())
    return _raw_validate_reference_evidence(*args, **kwargs)


def _bind_csv(manifest_path: Path, csv_path: Path) -> None:
    """同步负面测试中被修改 CSV 的配对哈希。"""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest["csv"]["sha256"] = digest
    manifest["hashes"]["output_csv_sha256"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _bind_member_hash(manifest_path: Path, csv_path: Path) -> None:
    """同步负面测试中已修改成员内容的稳定摘要。"""

    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    projections = [_row_projection(row) for row in rows]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hashes"]["final_member_sha256"] = canonical_sha256(
        sorted(projections, key=lambda item: item["identity"])
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_final_reference_is_unique_finalized_readonly_and_idempotent(tmp_path: Path) -> None:
    csv_path, manifest_path, database, selection = _artifacts(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    result = _validate_reference_evidence(
        csv_path,
        manifest_path,
        database,
        expected_label_guide_version=GUIDE_ID,
        expected_normalization_rule_id=NORMALIZATION_ID,
    )
    after = hashlib.sha256(database.read_bytes()).hexdigest()

    assert result.row_count == 700
    assert result.frame_counts == {"probability": 500, "targeted": 200}
    assert result.probability_estimation_status == "valid"
    assert before == after

    plan = build_duplicate_component_plan(selection.final_posts, (), ())
    legacy = LegacyReferenceInput(
        selection.final_posts,
        HASH,
        HASH,
        HASH,
        "sample-1",
        "candidate-1",
        HASH,
        GUIDE_ID,
        NORMALIZATION_ID,
        NORMALIZATION_ID,
        HASH,
        HASH,
    )
    repeated = write_final_reference_artifacts(
        selection,
        plan,
        csv_path,
        manifest_path,
        legacy_input=legacy,
        initial_candidate_computation=COMPUTATION,
        initial_duplicate_decision_manifest_sha256=HASH,
        replacement_queue_sha256=HASH,
        label_guide_id=GUIDE_ID,
        candidate_build_id="candidate-1",
        normalization_rule_id=NORMALIZATION_ID,
        supplemental_label_csv_sha256=HASH,
        supplemental_label_manifest_sha256=HASH,
        label_resolution_evidence_sha256=HASH,
        label_resolution_manifest_sha256=HASH,
        replacement_label_resolution_evidence_sha256=HASH,
        replacement_label_resolution_manifest_sha256=HASH,
        replacement_candidate_computation=COMPUTATION,
        replacement_duplicate_decision_manifest_sha256=HASH,
    )
    assert repeated.reused is True


def test_training_loader_accepts_only_final_reference_with_finalized_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    config = load_stable_config(ROOT / "configs/cleaning.yaml")
    config = replace(
        config,
        artifacts={
            **config.artifacts,
            "normalization_version_lock": NORMALIZATION_ID,
        },
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE text_deterministic_results SET normalized_model_text = ? "
        "WHERE task_id = ?",
        ('[TITLE]\n旧派生\n[BODY]\n{"ops":[{"insert":"污染"}]}', _final_posts()[0].task_id),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        "tourism_ugc_study.cleaning.reference_evidence.load_reference_text_projection",
        lambda *_args, **_kwargs: _source_projection(),
    )

    bundle = load_baseline_evidence(
        csv_path,
        manifest_path,
        database,
        leakage_build_id="leakage-1",
        config=config,
        normalization_config=_current_normalization_config(),
    )

    assert len(bundle.documents) == 700
    assert bundle.leakage_build_id == "leakage-1"
    assert bundle.documents[0].normalized_model_text == _final_posts()[0].normalized_model_text


def test_final_validator_binds_csv_text_to_frozen_source_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步篡改 CSV 与成员哈希也不得绕过冻结源正文投影。"""

    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    monkeypatch.setattr(
        "tourism_ugc_study.cleaning.reference_evidence.load_reference_text_projection",
        lambda *_args, **_kwargs: _source_projection(),
    )
    _validate_reference_evidence(
        csv_path,
        manifest_path,
        database,
        expected_label_guide_version=GUIDE_ID,
        expected_normalization_rule_id=NORMALIZATION_ID,
        normalization_config=_current_normalization_config(),
    )

    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["normalized_model_text"] = "[TITLE]\n伪造标题\n[BODY]\n伪造正文"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _bind_csv(manifest_path, csv_path)
    _bind_member_hash(manifest_path, csv_path)

    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path,
            manifest_path,
            database,
            expected_label_guide_version=GUIDE_ID,
            expected_normalization_rule_id=NORMALIZATION_ID,
            normalization_config=_current_normalization_config(),
        )

    assert error.value.reason_code == "final_reference_normalized_text_mismatch"


def test_final_validator_rejects_projection_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """manifest 不得伪造冻结源快照或全人口投影摘要。"""

    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    forged = replace(_source_projection(), member_sha256="c" * 64)
    monkeypatch.setattr(
        "tourism_ugc_study.cleaning.reference_evidence.load_reference_text_projection",
        lambda *_args, **_kwargs: forged,
    )

    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path,
            manifest_path,
            database,
            expected_label_guide_version=GUIDE_ID,
            normalization_config=_current_normalization_config(),
        )

    assert (
        error.value.reason_code
        == "final_reference_projection_member_hash_mismatch"
    )


def test_final_validator_rejects_unexpanded_structured_text(tmp_path: Path) -> None:
    """最终模型字段不得重新夹带 Delta JSON。"""

    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["normalized_model_text"] = (
        '[TITLE]\n标题\n[BODY]\n{"ops":[{"insert":"正文"}]}'
    )
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _bind_csv(manifest_path, csv_path)
    _bind_member_hash(manifest_path, csv_path)

    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path,
            manifest_path,
            database,
            expected_label_guide_version=GUIDE_ID,
        )

    assert error.value.reason_code == "final_reference_structured_text_residue"


def test_final_validator_rejects_delta_after_long_top_level_metadata(
    tmp_path: Path,
) -> None:
    """Delta 的 ops 键位置不得绕过最终结构残留检查。"""

    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["normalized_model_text"] = (
        '[TITLE]\n标题\n[BODY]\n{"metadata":"'
        + "x" * 400
        + '","ops":[{"insert":"正文"}]}'
    )
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _bind_csv(manifest_path, csv_path)
    _bind_member_hash(manifest_path, csv_path)

    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path,
            manifest_path,
            database,
            expected_label_guide_version=GUIDE_ID,
        )

    assert error.value.reason_code == "final_reference_structured_text_residue"


@pytest.mark.parametrize("row_delta", [-1, 1])
def test_final_reference_rejects_less_or_more_than_700(
    tmp_path: Path, row_delta: int
) -> None:
    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    if row_delta < 0:
        rows.pop()
    else:
        rows.append(rows[-1].copy())
        rows[-1][1] = "701"
        rows[-1][0] = "task-701"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)
    _bind_csv(manifest_path, csv_path)

    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path,
            manifest_path,
            database,
            expected_label_guide_version=GUIDE_ID,
        )
    assert error.value.reason_code == "final_reference_csv_row_count_invalid"


def test_final_reference_rejects_old_csv_intermediate_and_nonfinal_manifest(
    tmp_path: Path,
) -> None:
    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest["status"] = "pending_human_review"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert error.value.reason_code == "final_reference_manifest_not_finalized"

    manifest["status"] = "finalized"
    manifest["artifact_contract"] = "final-reference-replacement-queue"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert error.value.reason_code == "final_reference_manifest_contract_mismatch"

    legacy_csv = tmp_path / "legacy.csv"
    legacy_csv.write_text(
        "task_id,sample_run_id,source_post_id,source_version,platform_key,normalized_model_text,tourism_label\n",
        encoding="utf-8",
    )
    manifest["artifact_contract"] = "final-nonduplicate-model-reference"
    manifest["csv"]["logical_name"] = legacy_csv.name
    digest = hashlib.sha256(legacy_csv.read_bytes()).hexdigest()
    manifest["csv"]["sha256"] = digest
    manifest["hashes"]["output_csv_sha256"] = digest
    config = load_stable_config(ROOT / "configs/cleaning.yaml")
    manifest["identities"]["normalization_rule_id"] = str(
        config.artifacts["normalization_version_lock"]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            legacy_csv, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert error.value.reason_code == "final_reference_csv_field_contract_mismatch"
    with pytest.raises(ReferenceEvidenceError) as training_error:
        load_baseline_evidence(
            legacy_csv,
            manifest_path,
            database,
            leakage_build_id="must-not-be-read",
            config=config,
            normalization_config=_current_normalization_config(),
        )
    assert (
        training_error.value.reason_code
        == "final_reference_csv_field_contract_mismatch"
    )


def test_final_writer_rejects_confirmed_duplicate_members_and_content_conflict(
    tmp_path: Path,
) -> None:
    posts = _final_posts()
    decision = (
        # 人工显式低阈值边足以证明最终集不能同时保留两端。
        DuplicateDecision(
            "evidence-1",
            PairIdentity.of(posts[0].identity, posts[2].identity),
            "duplicate",
            "manual_low_similarity",
        ),
    )
    plan = build_duplicate_component_plan(posts, (), decision)
    selection = ReplacementSelection(
        posts, (), "valid", None, 0, 0, (), canonical_sha256([]), 0, {}
    )
    legacy = LegacyReferenceInput(
        posts,
        HASH,
        HASH,
        HASH,
        "sample-1",
        "candidate-1",
        HASH,
        GUIDE_ID,
        NORMALIZATION_ID,
        NORMALIZATION_ID,
        HASH,
        HASH,
    )

    with pytest.raises(ReferenceDatasetError) as error:
        write_final_reference_artifacts(
            selection,
            plan,
            tmp_path / "final.csv",
            tmp_path / "final.json",
            legacy_input=legacy,
            initial_candidate_computation=COMPUTATION,
            initial_duplicate_decision_manifest_sha256=HASH,
            replacement_queue_sha256=HASH,
            label_guide_id=GUIDE_ID,
            candidate_build_id="candidate-1",
            normalization_rule_id=NORMALIZATION_ID,
            supplemental_label_csv_sha256=HASH,
            supplemental_label_manifest_sha256=HASH,
            label_resolution_evidence_sha256=HASH,
            label_resolution_manifest_sha256=HASH,
            replacement_label_resolution_evidence_sha256=HASH,
            replacement_label_resolution_manifest_sha256=HASH,
            replacement_candidate_computation=COMPUTATION,
            replacement_duplicate_decision_manifest_sha256=HASH,
        )
    assert error.value.reason_code == "final_reference_confirmed_duplicate_present"

    output = tmp_path / "immutable.csv"
    output.write_text("existing\n", encoding="utf-8")
    clean_plan = build_duplicate_component_plan(posts, (), ())
    with pytest.raises(ReferenceDatasetError) as error:
        write_final_reference_artifacts(
            selection,
            clean_plan,
            output,
            tmp_path / "immutable.json",
            legacy_input=legacy,
            initial_candidate_computation=COMPUTATION,
            initial_duplicate_decision_manifest_sha256=HASH,
            replacement_queue_sha256=HASH,
            label_guide_id=GUIDE_ID,
            candidate_build_id="candidate-1",
            normalization_rule_id=NORMALIZATION_ID,
            supplemental_label_csv_sha256=HASH,
            supplemental_label_manifest_sha256=HASH,
            label_resolution_evidence_sha256=HASH,
            label_resolution_manifest_sha256=HASH,
            replacement_label_resolution_evidence_sha256=HASH,
            replacement_label_resolution_manifest_sha256=HASH,
            replacement_candidate_computation=COMPUTATION,
            replacement_duplicate_decision_manifest_sha256=HASH,
        )
    assert error.value.reason_code == "artifact_identity_content_conflict"


def test_final_validator_rejects_exact_and_transitive_duplicate_members(
    tmp_path: Path,
) -> None:
    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["confirmed_duplicate_pairs"] = [
        [[1, 1], [9001, 1]],
        [[2, 1], [9001, 1]],
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReferenceEvidenceError) as transitive_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert (
        transitive_error.value.reason_code
        == "final_reference_confirmed_duplicate_present"
    )

    csv_path, manifest_path, database, _ = _artifacts(tmp_path / "exact")
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[1]["normalized_model_text"] = rows[0]["normalized_model_text"]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE text_deterministic_results SET normalized_model_text = ? WHERE task_id = ?",
        (rows[0]["normalized_model_text"], rows[1]["task_id"]),
    )
    connection.commit()
    connection.close()
    _bind_csv(manifest_path, csv_path)
    _bind_member_hash(manifest_path, csv_path)
    with pytest.raises(ReferenceEvidenceError) as exact_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert exact_error.value.reason_code == "final_reference_confirmed_duplicate_present"


def test_final_validator_binds_task_and_projected_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_path, manifest_path, database, _ = _artifacts(tmp_path / "task")
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["task_id"] = "forged-task"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _bind_csv(manifest_path, csv_path)
    _bind_member_hash(manifest_path, csv_path)
    with pytest.raises(ReferenceEvidenceError) as task_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert task_error.value.reason_code == "final_reference_task_identity_mismatch"

    csv_path, manifest_path, database, _ = _artifacts(tmp_path / "structure")
    projection = _source_projection()
    changed = dict(projection.by_identity)
    changed[(1, 1)] = replace(
        changed[(1, 1)],
        structure_status="invalid",
        structure_reason_code="structured_text_no_text",
    )
    monkeypatch.setattr(
        "tourism_ugc_study.cleaning.reference_evidence.load_reference_text_projection",
        lambda *_args, **_kwargs: replace(
            projection, by_identity=MappingProxyType(changed)
        ),
    )
    with pytest.raises(ReferenceEvidenceError) as structure_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert (
        structure_error.value.reason_code
        == "final_reference_projected_structure_not_usable"
    )


def test_final_validator_binds_algorithm_sample_and_normalization_lineage(
    tmp_path: Path,
) -> None:
    csv_path, manifest_path, database, _ = _artifacts(tmp_path / "algorithm")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["duplicate_candidate_algorithm"]["lowercase"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReferenceEvidenceError) as algorithm_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert (
        algorithm_error.value.reason_code
        == "final_reference_duplicate_algorithm_invalid"
    )

    csv_path, manifest_path, database, _ = _artifacts(tmp_path / "representative")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["representative_selection_rule"]["sample_frame_used"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReferenceEvidenceError) as representative_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert (
        representative_error.value.reason_code
        == "final_reference_representative_rule_invalid"
    )

    csv_path, manifest_path, database, _ = _artifacts(tmp_path / "sample")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "UPDATE text_sample_members SET selection_rank = 99 "
        "WHERE sample_run_id = 'sample-1' AND source_post_id = 1"
    )
    forged_sample_hash = _sample_member_manifest(connection, "sample-1")
    connection.execute(
        "UPDATE text_sampling_runs SET member_manifest_sha256 = ? "
        "WHERE sample_run_id = 'sample-1'",
        (forged_sample_hash,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ReferenceEvidenceError) as sample_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert sample_error.value.reason_code == "existing_member_sample_lineage_mismatch"

    csv_path, manifest_path, database, _ = _artifacts(tmp_path / "normalization")
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE text_candidate_builds SET rules_sha256 = ? WHERE build_id = ?",
        ("c" * 64, "candidate-1"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ReferenceEvidenceError) as normalization_error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )
    assert (
        normalization_error.value.reason_code
        == "final_reference_database_normalization_mismatch"
    )


def test_final_validator_requires_complete_replacement_lineage_hashes(
    tmp_path: Path,
) -> None:
    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hashes"].pop("replacement_duplicate_candidate_sha256")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )

    assert error.value.reason_code == "final_reference_hash_contract_invalid"


def test_final_validator_rejects_non_hex_lineage_hash(tmp_path: Path) -> None:
    """64字符但非 SHA-256 十六进制的谱系值必须失败关闭。"""

    csv_path, manifest_path, database, _ = _artifacts(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hashes"]["replacement_queue_sha256"] = "z" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReferenceEvidenceError) as error:
        _validate_reference_evidence(
            csv_path, manifest_path, database, expected_label_guide_version=GUIDE_ID
        )

    assert error.value.reason_code == "final_reference_hash_contract_invalid"


def test_final_csv_field_contract_has_all_required_lineage_and_no_platform() -> None:
    assert FINAL_REFERENCE_FIELDS == (
        "task_id",
        "source_post_id",
        "source_version",
        "normalized_model_text",
        "tourism_label",
        "sample_frame",
        "selection_reason_code",
        "selection_rank",
        "inclusion_probability",
        "analysis_weight",
        "evidence_origin",
        "duplicate_component_id",
    )
    assert "platform_key" not in FINAL_REFERENCE_FIELDS
    assert canonical_sha256(list(FINAL_REFERENCE_FIELDS))
