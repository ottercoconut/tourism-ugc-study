"""最终700条不重复建模参考集的候选、人工证据与候补调度测试。"""

from __future__ import annotations

import csv
import json
from dataclasses import fields, replace
from pathlib import Path

import numpy as np
import pytest
from scipy.sparse import csr_matrix

from tourism_ugc_study.annotation.reference_candidates import (
    DUPLICATE_SIMILARITY_THRESHOLD,
    compute_duplicate_candidates,
)
from tourism_ugc_study.annotation.reference_artifacts import (
    seal_duplicate_decision_artifact,
    seal_label_resolution_artifact,
    seal_supplemental_label_artifact,
    validate_duplicate_decision_artifact,
    validate_label_resolution_artifact,
    write_candidate_review_artifacts,
    write_label_conflict_review_artifacts,
    write_replacement_queue_artifacts,
    write_supplemental_label_tasks,
)
from tourism_ugc_study.annotation.reference_contract import (
    DuplicateCandidate,
    DuplicateDecision,
    PairIdentity,
    ReferenceDatasetError,
    ReferencePost,
    SourceIdentity,
    canonical_sha256,
)
from tourism_ugc_study.annotation.reference_human_evidence import (
    DUPLICATE_DECISION_FIELDS,
    build_duplicate_component_plan,
    find_duplicate_label_conflicts,
    load_duplicate_decisions,
)
from tourism_ugc_study.annotation.reference_replacements import (
    ReplacementAnnotationPlan,
    ReplacementQueue,
    ReplacementQueueItem,
    build_replacement_queue,
    compute_replacement_duplicate_candidates,
    find_replacement_label_conflicts,
    prepare_replacement_annotation_plan,
    select_replacements,
)


def _post(
    post_id: int,
    text: str,
    *,
    label: str | None = "related",
    frame: str | None = "probability",
    probability: float | None = 0.5,
    weight: float | None = 2.0,
    usable: bool = True,
) -> ReferencePost:
    """构造不含平台字段的最小测试帖子。"""

    if frame != "probability":
        probability = None
        weight = None
    return ReferencePost(
        identity=SourceIdentity(post_id, 1),
        task_id=f"task-{post_id}",
        normalized_model_text=text,
        tourism_label=label,
        sample_frame=frame,
        selection_reason_code="initial",
        selection_rank=post_id,
        inclusion_probability=probability,
        analysis_weight=weight,
        evidence_origin="existing_representative",
        structure_usable=usable,
    )


def _decision(
    first: int,
    second: int,
    decision: str = "duplicate",
    origin: str = "threshold_candidate",
) -> DuplicateDecision:
    """构造稳定无向人工决定。"""

    return DuplicateDecision(
        evidence_id=f"evidence-{first}-{second}",
        pair=PairIdentity.of(SourceIdentity(first, 1), SourceIdentity(second, 1)),
        decision=decision,
        evidence_origin=origin,
    )


def test_all_700_posts_participate_in_244650_unordered_comparisons() -> None:
    posts = [_post(index + 1, f"青岛旅行路线内容{index:04d}独特记录") for index in range(700)]

    result = compute_duplicate_candidates(posts)

    assert result.examined_pair_count == 700 * 699 // 2 == 244_650
    assert result.expected_pair_count == 244_650
    assert result.algorithm_identity["ngram_range"] == [3, 5]
    assert result.algorithm_identity["threshold_role"] == "candidate_only"


def test_similarity_threshold_includes_exact_boundary_and_excludes_next_lower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lower = np.nextafter(DUPLICATE_SIMILARITY_THRESHOLD, 0.0)
    vectors = csr_matrix(
        np.asarray(
            [
                [1.0, 0.0, 0.0],
                [DUPLICATE_SIMILARITY_THRESHOLD, 0.6, 0.0],
                [lower, 0.0, np.sqrt(1.0 - lower**2)],
            ],
            dtype=np.float64,
        )
    )
    monkeypatch.setattr(
        "tourism_ugc_study.annotation.reference_candidates._vectorize",
        lambda _posts: vectors,
    )

    result = compute_duplicate_candidates(
        [_post(1, "文本一"), _post(2, "文本二"), _post(3, "文本三")]
    )

    pairs = {candidate.pair for candidate in result.candidates}
    assert PairIdentity.of(SourceIdentity(1, 1), SourceIdentity(2, 1)) in pairs
    assert PairIdentity.of(SourceIdentity(1, 1), SourceIdentity(3, 1)) not in pairs


def test_exact_hash_duplicate_is_automatic_but_near_candidate_needs_confirmation() -> None:
    posts = [_post(1, "完全相同文本"), _post(2, "完全相同文本"), _post(3, "明显不同文本")]
    candidates = compute_duplicate_candidates(posts).candidates

    preview = build_duplicate_component_plan(
        posts, candidates, (), require_all_candidates_finalized=False
    )

    assert len(preview.representatives) == 2
    assert {post.identity for post in preview.excluded} == {SourceIdentity(2, 1)}
    nonexact_candidates = [item for item in candidates if not item.exact_normalized_hash_match]
    if nonexact_candidates:
        with pytest.raises(ReferenceDatasetError) as error:
            build_duplicate_component_plan(posts, candidates, ())
        assert error.value.reason_code == "duplicate_candidates_require_final_review"


def test_manual_low_similarity_edge_and_transitive_component_are_stable() -> None:
    posts = [
        _post(1, "海边旅行完整长文本"),
        _post(2, "毫不相似的广告内容"),
        _post(3, "另一段完全不同内容"),
    ]
    decisions = (
        _decision(1, 2, origin="manual_low_similarity"),
        _decision(2, 3, origin="manual_low_similarity"),
    )

    first = build_duplicate_component_plan(posts, (), decisions)
    second = build_duplicate_component_plan(list(reversed(posts)), (), tuple(reversed(decisions)))

    assert len(first.representatives) == 1
    assert len(first.excluded) == 2
    assert first.member_sha256 == second.member_sha256
    assert first.decision_sha256 == second.decision_sha256


def test_unconfirmed_candidate_does_not_exclude_in_preview() -> None:
    posts = [_post(1, "青岛旅行攻略甲"), _post(2, "青岛旅行攻略乙")]
    pair = PairIdentity.of(posts[0].identity, posts[1].identity)
    candidate = DuplicateCandidate(pair, 0.80, False, "char_tfidf_similarity")

    plan = build_duplicate_component_plan(
        posts, (candidate,), (), require_all_candidates_finalized=False
    )

    assert len(plan.representatives) == 2
    assert plan.excluded == ()


def test_label_conflict_fails_closed_until_final_resolution() -> None:
    posts = [
        _post(1, "重复文本", label="related"),
        _post(2, "重复文本", label="unrelated"),
    ]
    candidates = compute_duplicate_candidates(posts).candidates

    with pytest.raises(ReferenceDatasetError) as error:
        build_duplicate_component_plan(posts, candidates, ())
    assert error.value.reason_code == "duplicate_component_label_conflict"

    component_id = build_duplicate_component_plan(
        [replace(posts[1], tourism_label="related"), posts[0]], candidates, ()
    ).component_by_identity[posts[0].identity]
    resolved = build_duplicate_component_plan(
        posts, candidates, (), label_resolutions={component_id: "unrelated"}
    )
    assert resolved.representatives[0].tourism_label == "unrelated"


def test_label_conflict_artifact_is_paired_finalized_and_guide_bound(
    tmp_path: Path,
) -> None:
    posts = [
        _post(1, "重复文本", label="related"),
        _post(2, "重复文本", label="unrelated"),
    ]
    candidates = compute_duplicate_candidates(posts).candidates
    conflicts = find_duplicate_label_conflicts(posts, candidates, ())
    pending_csv = tmp_path / "conflicts-pending.csv"
    pending_manifest = tmp_path / "conflicts-pending.json"
    write_label_conflict_review_artifacts(
        conflicts,
        pending_csv,
        pending_manifest,
        duplicate_decision_sha256="d" * 64,
        component_member_sha256="m" * 64,
        label_guide_id="text-cleaning-v1.5",
    )
    with pending_csv.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["tourism_label"] = "related"
    rows[0]["review_status"] = "finalized"
    completed_csv = tmp_path / "conflicts-completed.csv"
    with completed_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    finalized_manifest = tmp_path / "conflicts-finalized.json"
    seal_label_resolution_artifact(
        completed_csv, pending_manifest, finalized_manifest
    )

    artifact = validate_label_resolution_artifact(
        completed_csv,
        finalized_manifest,
        expected_duplicate_decision_sha256="d" * 64,
        expected_component_member_sha256="m" * 64,
        expected_label_guide_id="text-cleaning-v1.5",
    )

    assert artifact.resolutions == {conflicts[0].component_id: "related"}


def test_representative_uses_completeness_then_identity_not_sample_frame() -> None:
    posts = [
        _post(1, "短文本", frame="probability"),
        _post(2, "更完整的长文本内容", frame="probability"),
        _post(3, "最长但属于定向样本框的文本内容", frame="targeted"),
    ]
    decisions = (
        _decision(1, 2, origin="manual_low_similarity"),
        _decision(2, 3, origin="manual_low_similarity"),
    )

    plan = build_duplicate_component_plan(posts, (), decisions)

    assert plan.representatives[0].identity == SourceIdentity(3, 1)

    same_frame_plan = build_duplicate_component_plan(
        posts[:2], (), (_decision(1, 2, origin="manual_low_similarity"),)
    )
    assert same_frame_plan.representatives[0].identity == SourceIdentity(2, 1)


def test_queue_is_deterministic_global_and_never_reads_candidate_labels() -> None:
    first_population = [
        _post(index, f"候补独特文本{index}", label="related", frame="targeted")
        for index in range(1, 20)
    ]
    second_population = [replace(post, tourism_label="unrelated") for post in first_population]

    first = build_replacement_queue(
        first_population, blocked_identities=[SourceIdentity(3, 1)], random_seed=17
    )
    second = build_replacement_queue(
        list(reversed(second_population)),
        blocked_identities=[SourceIdentity(3, 1)],
        random_seed=17,
    )

    assert first.queue_sha256 == second.queue_sha256
    assert [item.post.identity for item in first.items] == [
        item.post.identity for item in second.items
    ]
    assert all(item.post.tourism_label is None for item in first.items)
    assert "platform" not in {field.name for field in fields(ReferencePost)}


def test_replacement_queue_manifest_records_frame_gaps_without_platform_policy(
    tmp_path: Path,
) -> None:
    accepted = [
        *[_post(index, f"概率既有{index}") for index in range(1, 499)],
        *[
            _post(index, f"定向既有{index}", frame="targeted")
            for index in range(1001, 1200)
        ],
    ]
    queue = build_replacement_queue(
        [_post(2001, "冻结候补", label=None, frame=None)],
        blocked_identities=(),
        random_seed=17,
    )
    manifest_path = tmp_path / "queue.manifest.json"

    write_replacement_queue_artifacts(
        queue,
        accepted,
        tmp_path / "queue.csv",
        manifest_path,
        candidate_build_id="candidate-1",
        candidate_build_sha256="c" * 64,
        duplicate_decision_sha256="d" * 64,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["probability_gap"] == 2
    assert manifest["targeted_gap"] == 1
    assert manifest["selection_scope"] == "global"
    assert manifest["platform_quota"] is False
    assert manifest["platform_sort"] is False


def test_replacement_skips_duplicate_and_unusable_then_reaches_500_200() -> None:
    accepted = [
        *[_post(index, f"概率既有独特文本{index}") for index in range(1, 500)],
        *[
            _post(index, f"定向既有独特文本{index}", frame="targeted")
            for index in range(1001, 1200)
        ],
    ]
    population = [
        _post(2001, accepted[0].normalized_model_text, label=None, frame=None),
        _post(2002, "结构不可用候补文本", label=None, frame=None, usable=False),
        _post(2003, "第一条有效候补完全独特内容", label=None, frame=None),
        _post(2004, "第二条有效候补另一独特内容", label=None, frame=None),
    ]
    queue = build_replacement_queue(population, blocked_identities=(), random_seed=3)
    labels = {
        SourceIdentity(2003, 1): "related",
        SourceIdentity(2004, 1): "unrelated",
    }

    selection = select_replacements(accepted, queue, labels=labels)

    assert len(selection.final_posts) == 700
    assert sum(post.sample_frame == "probability" for post in selection.final_posts) == 500
    assert sum(post.sample_frame == "targeted" for post in selection.final_posts) == 200
    selected = [post for post in selection.final_posts if post.evidence_origin == "supplemental_annotation"]
    assert len(selected) == 2
    assert all(post.inclusion_probability is None and post.analysis_weight is None for post in selected)
    assert selection.probability_estimation_status == "unavailable_after_replacement"
    statuses = {item.status for item in selection.dispositions}
    assert "confirmed_duplicate" in statuses


def test_replacement_queue_exhaustion_fails_closed() -> None:
    accepted = [
        *[_post(index, f"概率文本{index}") for index in range(1, 500)],
        *[
            _post(index, f"定向文本{index}", frame="targeted")
            for index in range(1001, 1201)
        ],
    ]
    queue = build_replacement_queue(
        [_post(3001, "唯一候补但没有最终标签", label=None, frame=None)],
        blocked_identities=(),
        random_seed=9,
    )

    with pytest.raises(ReferenceDatasetError) as error:
        select_replacements(accepted, queue, labels={})
    assert error.value.reason_code == "replacement_queue_exhausted"


def test_same_round_replacement_duplicate_is_skipped_before_counting() -> None:
    accepted = [
        *[_post(index, f"概率框既有内容{index}") for index in range(1, 498)],
        *[
            _post(index, f"定向框既有内容{index}", frame="targeted")
            for index in range(1001, 1200)
        ],
    ]
    population = [
        _post(4001, "同轮候补精确重复文本", label=None, frame=None),
        _post(4002, "同轮候补精确重复文本", label=None, frame=None),
        _post(4003, "同轮候补独特甲内容", label=None, frame=None),
        _post(4004, "同轮候补独特乙内容", label=None, frame=None),
        _post(4005, "同轮候补独特丙内容", label=None, frame=None),
    ]
    queue = build_replacement_queue(population, blocked_identities=(), random_seed=11)
    labels = {
        post.identity: "related" for post in population
    }

    selection = select_replacements(accepted, queue, labels=labels)

    assert len(selection.final_posts) == 700
    assert sum(item.status == "confirmed_duplicate" for item in selection.dispositions) == 1
    assert sum(item.status == "selected" for item in selection.dispositions) == 4


def test_reviewed_prefix_retains_exact_duplicate_tail_after_gap_is_filled() -> None:
    accepted = [
        *[_post(index, f"概率框成员{index}") for index in range(1, 500)],
        *[
            _post(index, f"定向框成员{index}", frame="targeted")
            for index in range(1001, 1201)
        ],
    ]
    first = _post(6001, "候补尾部精确重复文本", label=None, frame=None)
    exact_tail = _post(6002, "候补尾部精确重复文本", label=None, frame=None)
    queue = _queue([first, exact_tail])

    selection = select_replacements(
        accepted,
        queue,
        labels={first.identity: "related"},
        max_queue_rank=2,
    )

    assert len(selection.final_posts) == 700
    assert selection.reviewed_max_queue_rank == 2
    assert selection.dispositions[-1].status == "confirmed_duplicate"


def test_pending_duplicate_review_is_sealed_as_new_immutable_artifact(
    tmp_path: Path,
) -> None:
    posts = [
        _post(1, "这是一个非常完整的青岛旅游攻略和路线推荐文本甲"),
        _post(2, "这是一个非常完整的青岛旅游攻略和路线推荐文本乙"),
    ]
    pair = PairIdentity.of(posts[0].identity, posts[1].identity)
    computation = compute_duplicate_candidates(posts)
    pending_csv = tmp_path / "pending.csv"
    pending_manifest = tmp_path / "pending.json"
    write_candidate_review_artifacts(
        computation,
        posts,
        pending_csv,
        pending_manifest,
        input_hashes={"input_csv_sha256": "a" * 64, "input_manifest_sha256": "b" * 64},
        normalization_rule_id="text-normalization-test",
    )
    completed_csv = tmp_path / "completed.csv"
    with pending_csv.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["decision"] = "not_duplicate"
    rows[0]["review_status"] = "finalized"
    with completed_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    finalized_manifest = tmp_path / "finalized.json"

    first = seal_duplicate_decision_artifact(
        completed_csv, pending_manifest, finalized_manifest
    )
    second = seal_duplicate_decision_artifact(
        completed_csv, pending_manifest, finalized_manifest
    )
    decisions = validate_duplicate_decision_artifact(
        completed_csv, finalized_manifest
    )

    assert first.reused is False
    assert second.reused is True
    assert decisions[0].pair == pair
    assert decisions[0].decision == "not_duplicate"
    with pytest.raises(ReferenceDatasetError) as normalization_error:
        validate_duplicate_decision_artifact(
            completed_csv,
            finalized_manifest,
            expected_normalization_rule_id="different-normalization-rule",
        )
    assert (
        normalization_error.value.reason_code
        == "duplicate_decision_normalization_rule_mismatch"
    )

    changed_csv = tmp_path / "changed.csv"
    rows[0]["left_normalized_model_text"] = "被篡改的候选文本"
    with changed_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ReferenceDatasetError) as error:
        seal_duplicate_decision_artifact(
            changed_csv, pending_manifest, tmp_path / "changed.json"
        )
    assert error.value.reason_code == "duplicate_completed_candidates_changed"


def test_manual_low_similarity_row_can_be_appended_to_pending_review(
    tmp_path: Path,
) -> None:
    posts = [_post(1, "海边日出路线"), _post(2, "完全不同的住宿体验")]
    computation = compute_duplicate_candidates(posts)
    pending_csv = tmp_path / "manual-pending.csv"
    pending_manifest = tmp_path / "manual-pending.json"
    write_candidate_review_artifacts(
        computation,
        posts,
        pending_csv,
        pending_manifest,
        input_hashes={"input_csv_sha256": "a" * 64},
        normalization_rule_id="text-normalization-test",
    )
    completed_csv = tmp_path / "manual-completed.csv"
    manual_row = {
        "evidence_id": "manual-evidence-1",
        "left_source_post_id": "1",
        "left_source_version": "1",
        "right_source_post_id": "2",
        "right_source_version": "1",
        "similarity": "0.1",
        "exact_normalized_hash_match": "false",
        "candidate_reason_code": "manual_low_similarity",
        "left_normalized_model_text": posts[0].normalized_model_text,
        "right_normalized_model_text": posts[1].normalized_model_text,
        "decision": "duplicate",
        "review_status": "finalized",
        "evidence_origin": "manual_low_similarity",
    }
    with completed_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(manual_row))
        writer.writeheader()
        writer.writerow(manual_row)
    finalized_manifest = tmp_path / "manual-finalized.json"
    seal_duplicate_decision_artifact(
        completed_csv, pending_manifest, finalized_manifest
    )
    decisions = validate_duplicate_decision_artifact(
        completed_csv,
        finalized_manifest,
        expected_input_member_sha256=computation.input_member_sha256,
        expected_candidate_sha256=computation.candidate_sha256,
    )
    plan = build_duplicate_component_plan(posts, computation.candidates, decisions)

    assert len(plan.representatives) == 1

    tampered_csv = tmp_path / "manual-tampered.csv"
    manual_row["right_normalized_model_text"] = "被替换的私有正文"
    with tampered_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(manual_row))
        writer.writeheader()
        writer.writerow(manual_row)
    with pytest.raises(ReferenceDatasetError) as error:
        seal_duplicate_decision_artifact(
            tampered_csv, pending_manifest, tmp_path / "manual-tampered.json"
        )
    assert error.value.reason_code == "duplicate_manual_edge_invalid"

    malformed_manifest = tmp_path / "manual-malformed-pending.json"
    manifest = json.loads(pending_manifest.read_text(encoding="utf-8"))
    manifest["automatic_candidate_pairs"] = [[]]
    malformed_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReferenceDatasetError) as malformed_error:
        seal_duplicate_decision_artifact(
            completed_csv, malformed_manifest, tmp_path / "manual-malformed.json"
        )
    assert (
        malformed_error.value.reason_code
        == "duplicate_manual_input_lineage_invalid"
    )


def _queue(items: list[ReferencePost]) -> ReplacementQueue:
    """按输入顺序构造冻结秩明确的测试候补队列。"""

    queue_items = tuple(
        ReplacementQueueItem(post, rank, f"order-{rank}")
        for rank, post in enumerate(items, start=1)
    )
    return ReplacementQueue(queue_items, 17, 0, "q" * 64)


def test_replacement_annotation_plan_uses_transitive_components_before_labels() -> None:
    accepted = [
        *[_post(index, f"概率既有独立内容{index}") for index in range(1, 500)],
        *[
            _post(index, f"定向既有独立内容{index}", frame="targeted")
            for index in range(1001, 1201)
        ],
    ]
    first = _post(3001, "候补传递边第一端独特内容", label=None, frame=None)
    second = _post(3002, "候补传递边第二端另一内容", label=None, frame=None)
    safe = _post(3003, "可以进入标注的安全候补内容", label=None, frame=None)
    queue = _queue([first, second, safe])
    decisions = (
        _decision(3001, 3002, origin="manual_low_similarity"),
        DuplicateDecision(
            "evidence-accepted-bridge",
            PairIdentity.of(second.identity, accepted[0].identity),
            "duplicate",
            "manual_low_similarity",
        ),
    )

    plan = prepare_replacement_annotation_plan(
        accepted,
        queue,
        duplicate_decisions=decisions,
        max_queue_rank=3,
    )

    assert [item.post.identity for item in plan.tasks] == [safe.identity]
    assert all(item.post.tourism_label is None for item in plan.tasks)


def test_replacement_component_conflicts_include_prior_and_accepted_labels() -> None:
    accepted = [_post(1, "既有相关短文本", label="related")]
    first = _post(2, "候补不相关完整长文本", label=None, frame=None)
    second = _post(3, "另一候补相关内容", label=None, frame=None)
    queue = _queue([first, second])
    accepted_edge = DuplicateDecision(
        "accepted-edge",
        PairIdentity.of(accepted[0].identity, first.identity),
        "duplicate",
        "manual_low_similarity",
    )

    accepted_conflicts = find_replacement_label_conflicts(
        accepted,
        queue,
        duplicate_decisions=(accepted_edge,),
        max_queue_rank=2,
        prior_labels={first.identity: "unrelated"},
    )
    assert len(accepted_conflicts) == 1
    assert {post.tourism_label for post in accepted_conflicts[0].members} == {
        "related",
        "unrelated",
    }
    with pytest.raises(ReferenceDatasetError) as accepted_error:
        prepare_replacement_annotation_plan(
            accepted,
            queue,
            duplicate_decisions=(accepted_edge,),
            max_queue_rank=2,
            prior_labels={first.identity: "unrelated"},
        )
    assert accepted_error.value.reason_code == "replacement_component_label_conflict"

    prior_edge = _decision(2, 3, origin="manual_low_similarity")
    prior_conflicts = find_replacement_label_conflicts(
        (),
        queue,
        duplicate_decisions=(prior_edge,),
        max_queue_rank=2,
        prior_labels={first.identity: "related", second.identity: "unrelated"},
    )
    assert len(prior_conflicts) == 1
    with pytest.raises(ReferenceDatasetError) as prior_error:
        prepare_replacement_annotation_plan(
            (),
            queue,
            duplicate_decisions=(prior_edge,),
            max_queue_rank=2,
            prior_labels={first.identity: "related", second.identity: "unrelated"},
        )
    assert prior_error.value.reason_code == "replacement_component_label_conflict"


def test_prior_label_is_not_transferred_to_a_new_replacement_representative() -> None:
    accepted = [
        *[_post(index, f"概率既有独立内容{index}") for index in range(1, 500)],
        *[
            _post(index, f"定向既有独立内容{index}", frame="targeted")
            for index in range(1001, 1201)
        ],
    ]
    shorter = _post(7001, "较短候补", label=None, frame=None)
    longer = _post(7002, "更完整的候补代表文本内容", label=None, frame=None)
    queue = _queue([shorter, longer])
    decision = _decision(7001, 7002, origin="manual_low_similarity")

    plan = prepare_replacement_annotation_plan(
        accepted,
        queue,
        duplicate_decisions=(decision,),
        max_queue_rank=2,
        prior_labels={shorter.identity: "related"},
    )

    assert [item.post.identity for item in plan.tasks] == [longer.identity]
    assert plan.carried_labels == ((queue.items[0], "related"),)
    with pytest.raises(ReferenceDatasetError) as error:
        select_replacements(
            accepted,
            queue,
            labels={shorter.identity: "related"},
            duplicate_decisions=(decision,),
            max_queue_rank=2,
        )
    assert error.value.reason_code == "replacement_queue_exhausted"


def test_candidate_duplicate_of_accepted_is_ineligible_even_when_more_complete() -> None:
    accepted = [_post(1, "短文本", label="related")]
    candidate = _post(
        2,
        "比既有代表更完整但在候补阶段确认重复的长文本",
        label=None,
        frame=None,
    )
    queue = _queue([candidate])
    decision = DuplicateDecision(
        "accepted-candidate-edge",
        PairIdentity.of(accepted[0].identity, candidate.identity),
        "duplicate",
        "manual_low_similarity",
    )

    with pytest.raises(ReferenceDatasetError) as error:
        prepare_replacement_annotation_plan(
            accepted,
            queue,
            duplicate_decisions=(decision,),
            max_queue_rank=1,
        )

    assert error.value.reason_code == "replacement_annotation_pool_exhausted"


def test_candidate_connected_through_excluded_initial_member_is_ineligible() -> None:
    """候补连接历史排除成员时仍必须传播到既有代表分量。"""

    accepted = _post(1, "与候补完全不同的既有代表", label="related")
    excluded = _post(2, "候补精确重复文本", label="related")
    candidate = _post(3, "候补精确重复文本", label=None, frame=None)
    queue = _queue([candidate])
    lineage = (accepted, excluded)
    initial_component = {
        accepted.identity: "initial-component",
        excluded.identity: "initial-component",
    }

    computation = compute_replacement_duplicate_candidates(
        (accepted,),
        queue,
        max_queue_rank=1,
        lineage_posts=lineage,
    )
    assert any(
        item.pair == PairIdentity.of(excluded.identity, candidate.identity)
        and item.exact_normalized_hash_match
        for item in computation.candidates
    )
    with pytest.raises(ReferenceDatasetError) as error:
        select_replacements(
            (accepted,),
            queue,
            labels={candidate.identity: "related"},
            max_queue_rank=1,
            lineage_posts=lineage,
            initial_component_by_identity=initial_component,
        )

    assert error.value.reason_code == "replacement_queue_exhausted"


def test_duplicate_decision_short_row_uses_stable_reason_code(tmp_path: Path) -> None:
    """人工 CSV 短行不得抛出带私有路径的底层异常。"""

    csv_path = tmp_path / "short-row.csv"
    csv_path.write_text(
        ",".join(DUPLICATE_DECISION_FIELDS) + "\nonly-one-cell\n",
        encoding="utf-8",
    )
    with pytest.raises(ReferenceDatasetError) as error:
        load_duplicate_decisions(csv_path)

    assert error.value.reason_code == "duplicate_decision_row_structure_invalid"


def test_replacement_near_candidate_must_be_final_before_label_tasks() -> None:
    accepted = [_post(1, "这是一个非常完整的青岛旅游攻略和路线推荐文本甲")]
    candidate = _post(
        2,
        "这是一个非常完整的青岛旅游攻略和路线推荐文本乙",
        label=None,
        frame=None,
    )

    with pytest.raises(ReferenceDatasetError) as error:
        prepare_replacement_annotation_plan(
            accepted,
            _queue([candidate]),
            duplicate_decisions=(),
            max_queue_rank=1,
        )
    assert (
        error.value.reason_code
        == "replacement_duplicate_candidates_require_final_review"
    )


def test_supplemental_label_tasks_bind_queue_rank_and_seal_append_only(
    tmp_path: Path,
) -> None:
    post = _post(5001, "补充人工标签任务内容", label=None, frame=None)
    item = ReplacementQueueItem(post, 7, "order-7")
    task_hash = canonical_sha256(
        [
            {
                "identity": post.identity.as_list(),
                "task_id": post.task_id,
                "queue_rank": 7,
                "normalized_sha256": post.normalized_sha256,
            }
        ]
    )
    plan = ReplacementAnnotationPlan((item,), (), 1, 0, task_hash, "d" * 64, 7)
    pending_csv = tmp_path / "labels-pending.csv"
    pending_manifest = tmp_path / "labels-pending.json"
    write_supplemental_label_tasks(
        plan,
        pending_csv,
        pending_manifest,
        queue_sha256="q" * 64,
        label_guide_id="text-cleaning-v1.5",
    )
    with pending_csv.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["selection_rank"] == "7"
    assert rows[0]["tourism_label"] == ""
    rows[0]["tourism_label"] = "related"
    rows[0]["review_status"] = "finalized"
    completed_csv = tmp_path / "labels-completed.csv"
    with completed_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    first = seal_supplemental_label_artifact(
        completed_csv, pending_manifest, tmp_path / "labels-finalized.json"
    )
    second = seal_supplemental_label_artifact(
        completed_csv, pending_manifest, tmp_path / "labels-finalized.json"
    )

    assert first.reused is False
    assert second.reused is True


def test_cumulative_uncertain_labels_expand_frozen_prefix_and_later_fill_gap(
    tmp_path: Path,
) -> None:
    accepted = [
        *[_post(index, f"概率既有独立内容{index}") for index in range(1, 500)],
        *[
            _post(index, f"定向既有独立内容{index}", frame="targeted")
            for index in range(1001, 1201)
        ],
    ]
    first = _post(8001, "候补甲火山徒步住宿交通综合记录", label=None, frame=None)
    second = _post(8002, "候补乙海岛潜水餐饮天气独立内容", label=None, frame=None)
    queue = _queue([first, second])
    round_one = prepare_replacement_annotation_plan(
        accepted, queue, duplicate_decisions=(), max_queue_rank=1
    )
    round_one_pending_csv = tmp_path / "round-one-pending.csv"
    round_one_pending_manifest = tmp_path / "round-one-pending.json"
    write_supplemental_label_tasks(
        round_one,
        round_one_pending_csv,
        round_one_pending_manifest,
        queue_sha256=queue.queue_sha256,
        label_guide_id="text-cleaning-v1.5",
    )
    with round_one_pending_csv.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["tourism_label"] = "uncertain"
    rows[0]["review_status"] = "finalized"
    round_one_completed = tmp_path / "round-one-completed.csv"
    with round_one_completed.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    seal_supplemental_label_artifact(
        round_one_completed,
        round_one_pending_manifest,
        tmp_path / "round-one-finalized.json",
    )

    round_two = prepare_replacement_annotation_plan(
        accepted,
        queue,
        duplicate_decisions=(),
        max_queue_rank=2,
        prior_labels={first.identity: "uncertain"},
    )
    round_two_pending_csv = tmp_path / "round-two-pending.csv"
    round_two_pending_manifest = tmp_path / "round-two-pending.json"
    write_supplemental_label_tasks(
        round_two,
        round_two_pending_csv,
        round_two_pending_manifest,
        queue_sha256=queue.queue_sha256,
        label_guide_id="text-cleaning-v1.5",
    )
    with round_two_pending_csv.open("r", encoding="utf-8", newline="") as stream:
        round_two_rows = list(csv.DictReader(stream))
    assert [row["review_status"] for row in round_two_rows] == ["finalized", "pending"]
    assert round_two_rows[0]["tourism_label"] == "uncertain"
    round_two_rows[1]["tourism_label"] = "related"
    round_two_rows[1]["review_status"] = "finalized"
    round_two_completed = tmp_path / "round-two-completed.csv"
    with round_two_completed.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(round_two_rows[0]))
        writer.writeheader()
        writer.writerows(round_two_rows)
    seal_supplemental_label_artifact(
        round_two_completed,
        round_two_pending_manifest,
        tmp_path / "round-two-finalized.json",
    )

    selection = select_replacements(
        accepted,
        queue,
        labels={first.identity: "uncertain", second.identity: "related"},
        max_queue_rank=2,
    )

    assert len(selection.final_posts) == 700
    assert any(item.status == "label_not_final" for item in selection.dispositions)


def test_zero_gap_has_canonical_empty_supplemental_artifact(tmp_path: Path) -> None:
    accepted = [
        *[_post(index, f"概率完整成员{index}") for index in range(1, 501)],
        *[
            _post(index, f"定向完整成员{index}", frame="targeted")
            for index in range(1001, 1201)
        ],
    ]
    queue = _queue([])
    plan = prepare_replacement_annotation_plan(
        accepted, queue, duplicate_decisions=(), max_queue_rank=0
    )
    pending_csv = tmp_path / "empty-pending.csv"
    pending_manifest = tmp_path / "empty-pending.json"
    result = write_supplemental_label_tasks(
        plan,
        pending_csv,
        pending_manifest,
        queue_sha256=queue.queue_sha256,
        label_guide_id="text-cleaning-v1.5",
    )
    finalized = seal_supplemental_label_artifact(
        pending_csv, pending_manifest, tmp_path / "empty-finalized.json"
    )

    assert result.row_count == 0
    assert result.status == "complete"
    assert len(finalized.sha256) == 64
