"""图片复核代表、边界样本与 pHash complete-linkage 的纯算法测试。"""

from __future__ import annotations

from tourism_ugc_study.cleaning.image_review_sampling import (
    ReviewCandidate,
    build_complete_linkage_groups,
    select_boundary_members,
    select_candidate_review_members,
    select_pilot_members,
    selection_manifest,
)


def _record(
    index: int,
    *,
    phash: str | None = None,
    signal: bool = False,
    exact: bool = False,
    near: bool = False,
) -> ReviewCandidate:
    return ReviewCandidate(
        fingerprint_id=f"fp-{index:03d}",
        exact_cluster_id=f"cluster-{index:03d}",
        phash_hex=phash or f"{index:016x}",
        platform_key="xiaohongshu" if index % 2 else "douyin",
        has_technical_signal=signal,
        has_exact_duplicate=exact,
        has_phash_candidate=near,
    )


def test_pilot_and_candidate_review_only_use_candidate_representatives() -> None:
    records = [
        _record(1, signal=True),
        _record(2, exact=True),
        _record(3, near=True),
        _record(4),
    ]
    pilot = select_pilot_members(records, seed=7, size=30)
    review = select_candidate_review_members(records, seed=7)

    assert {item.fingerprint_id for item in pilot} == {"fp-001", "fp-002", "fp-003"}
    assert all(item.requires_double_label for item in pilot)
    assert {item.fingerprint_id for item in review} == {"fp-001", "fp-002", "fp-003"}
    assert not any(item.requires_double_label for item in review)


def test_boundary_sample_balances_candidate_and_noncandidate_then_fills() -> None:
    records = [_record(index, signal=index <= 20) for index in range(1, 101)]
    first = select_boundary_members(records, seed=20260728, size=50)
    repeated = select_boundary_members(list(reversed(records)), seed=20260728, size=50)

    assert first == repeated
    assert len(first) == 50
    assert sum(item.review_reason == "candidate_boundary" for item in first) == 20
    assert sum(item.review_reason == "noncandidate_boundary" for item in first) == 30
    assert all(item.requires_double_label for item in first)


def test_boundary_supplement_excludes_existing_members() -> None:
    records = [_record(index, signal=index % 2 == 0) for index in range(1, 121)]
    first = select_boundary_members(records, seed=5, size=50)
    supplement = select_boundary_members(
        records,
        seed=6,
        size=50,
        exclude_fingerprint_ids={item.fingerprint_id for item in first},
    )
    assert len(supplement) == 50
    assert not ({item.fingerprint_id for item in first} & {item.fingerprint_id for item in supplement})


def test_complete_linkage_does_not_merge_chained_pair() -> None:
    # A-B=6、B-C=6，但 A-C=12；single-link 会误合并，complete-link 不会。
    records = [
        _record(1, phash="0000000000000000", near=True),
        _record(2, phash="000000000000003f", near=True),
        _record(3, phash="0000000000000fff", near=True),
    ]
    groups = build_complete_linkage_groups(records, seed=1, maximum_distance=10)
    assert sorted(len(group.member_fingerprint_ids) for group in groups) == [1, 2]
    assert all(group.maximum_pair_distance <= 10 for group in groups)


def test_phash_groups_and_manifest_are_stable_and_have_no_propagation_semantics() -> None:
    records = [
        _record(1, phash="0" * 16, near=True),
        _record(2, phash="0" * 15 + "3", near=True),
        _record(3, signal=True),
    ]
    groups = build_complete_linkage_groups(records, seed=8, maximum_distance=10)
    selections = select_candidate_review_members(records, seed=8)
    assert groups == build_complete_linkage_groups(list(reversed(records)), seed=8, maximum_distance=10)
    assert selection_manifest(selections, groups) == selection_manifest(tuple(reversed(selections)), groups)
    assert not hasattr(groups[0], "propagated_label")
