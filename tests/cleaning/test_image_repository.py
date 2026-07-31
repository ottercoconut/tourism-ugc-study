from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import tourism_ugc_study.cleaning.schema as schema_module
from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.fingerprints import canonical_sha256
from tourism_ugc_study.cleaning.image_candidates import (
    CandidateImage,
    build_image_candidate_plan,
)
from tourism_ugc_study.cleaning.image_manifest import IMAGE_MANIFEST_COLUMNS
from tourism_ugc_study.cleaning.image_repository import (
    ImageRepositoryError,
    build_image_candidates,
    import_image_manifest,
    load_image_stage_snapshot,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.schema import connect_derived
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=IMAGE_MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _row(
    image_id: int,
    role: str,
    relative_path: str,
    expected_sha: str,
) -> dict[str, object]:
    return {
        "source_image_id": image_id,
        "source_post_id": image_id,
        "relation_role": role,
        "relative_path": relative_path,
        "file_sha256": expected_sha,
        "parent_file_sha256": "",
        "transform_json": "{}",
    }


def _prepared_run(tmp_path: Path, run_id: str):
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    root = tmp_path / "images"
    root.mkdir()
    _build_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE web_post_images SET image_role = 'author_avatar' WHERE id = 4")
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, run_id)
    discover_increment(derived, snapshot.snapshot_id, config)
    return derived, root, config, snapshot


def _route_map(path: Path, *, variant: bool = False) -> None:
    image = Image.new("RGB", (128, 96), "white")
    draw = ImageDraw.Draw(image)
    draw.line([(8, 80), (48, 40), (112, 16)], fill="red", width=5)
    if variant:
        draw.rectangle((70, 50, 90, 70), fill="blue")
    image.save(path)


def _insert_building_candidate(
    connection: sqlite3.Connection,
    *,
    build_id: str,
    run_id: str,
    manifest_id: str,
    fingerprint_version: str,
    config_sha256: str,
    identity_seed: str,
) -> None:
    """插入只用于数据库负例的空 building 表头。"""

    connection.execute(
        """
        INSERT INTO image_candidate_builds(
            build_id, run_id, manifest_id, candidate_version,
            fingerprint_version, config_sha256, input_manifest_sha256,
            expected_fingerprint_count, exact_cluster_count,
            exact_duplicate_cluster_count, near_pair_count, signal_count,
            seal_status, output_sha256, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 0, 0,
                  'building', ?, '2026-07-31T00:00:00+00:00')
        """,
        (
            build_id,
            run_id,
            manifest_id,
            f"negative-{identity_seed}",
            fingerprint_version,
            config_sha256,
            identity_seed * 64,
            identity_seed * 64,
        ),
    )


def test_repository_fingerprints_content_and_builds_candidate_evidence(tmp_path: Path) -> None:
    derived, root, config, snapshot = _prepared_run(tmp_path, "image-success")
    route = root / "route-plan.png"
    near = root / "near.png"
    _route_map(route)
    _route_map(near, variant=True)
    manifest = tmp_path / "manifest.csv"
    _write_manifest(
        manifest,
        [
            _row(1, "content", "route-plan.png", _sha(route)),
            _row(2, "content", "route-plan.png", _sha(route)),
            _row(3, "content", "near.png", _sha(near)),
            _row(4, "author_avatar", "route-plan.png", _sha(route)),
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id="image-success",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest,
        image_root=root,
        config=config,
    )
    before = {_sha(route), _sha(near)}

    fingerprints = process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=root,
        config=config,
    )
    candidates = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )
    with sqlite3.connect(derived) as connection:
        evidence_before = {
            table: connection.execute(
                f"SELECT * FROM {table} WHERE build_id = ? ORDER BY 1, 2",
                (candidates.build_id,),
            ).fetchall()
            for table in (
                "image_candidate_builds",
                "image_candidate_build_members",
                "image_candidate_signals",
                "image_exact_clusters",
                "image_exact_cluster_members",
                "image_near_candidate_pairs",
            )
        }
    repeated_candidates = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )

    assert fingerprints.succeeded_count == 3
    assert fingerprints.blocked_count == 0
    assert fingerprints.skipped_count == 1
    assert candidates.fingerprint_count == 3
    assert candidates.exact_cluster_count == 2
    assert candidates.exact_duplicate_cluster_count == 1
    assert repeated_candidates == candidates
    assert {_sha(route), _sha(near)} == before
    with sqlite3.connect(derived) as connection:
        evidence_after = {
            table: connection.execute(
                f"SELECT * FROM {table} WHERE build_id = ? ORDER BY 1, 2",
                (candidates.build_id,),
            ).fetchall()
            for table in evidence_before
        }
        assert evidence_after == evidence_before
        assert connection.execute(
            "SELECT seal_status FROM image_candidate_builds WHERE build_id = ?",
            (candidates.build_id,),
        ).fetchone()[0] == "finalized"
        # 路线图只是 content 文件名，不会生成排除标签；表中也不存在最终标签列。
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(image_candidate_build_members)")
        }
        assert "final_label" not in columns


def test_every_image_operation_rejects_changed_frozen_config_before_reuse(
    tmp_path: Path,
) -> None:
    """配置 A 的证据不能被参数不同的配置 B 读取、复用或继续写入。"""

    derived, root, config_a, snapshot = _prepared_run(tmp_path, "image-config-contract")
    image_path = root / "content.png"
    Image.new("RGB", (80, 80), "purple").save(image_path)
    manifest_path = tmp_path / "manifest.csv"
    _write_manifest(
        manifest_path,
        [_row(1, "content", "content.png", _sha(image_path))],
    )
    imported = import_image_manifest(
        derived,
        run_id="image-config-contract",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=root,
        config=config_a,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=root,
        config=config_a,
    )
    build = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config_a,
    )
    config_b = replace(
        config_a,
        image=replace(config_a.image, candidate_hamming_max=9),
        sha256="b" * 64,
    )
    with sqlite3.connect(derived) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "image_fingerprints",
                "image_candidate_builds",
                "image_processing_attempts",
            )
        }

    operations = (
        lambda: process_image_fingerprints(
            derived,
            manifest_id=imported.manifest_id,
            image_root=root,
            config=config_b,
        ),
        lambda: build_image_candidates(
            derived,
            manifest_id=imported.manifest_id,
            config=config_b,
        ),
        lambda: load_image_stage_snapshot(
            derived,
            manifest_id=imported.manifest_id,
            stage="candidates",
            config=config_b,
            build_id=build.build_id,
        ),
    )
    for operation in operations:
        try:
            operation()
        except ImageRepositoryError as exc:
            assert exc.reason_code == "run_config_mismatch"
        else:
            raise AssertionError("冻结运行必须拒绝变更参数后的配置")

    with sqlite3.connect(derived) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


def test_candidate_reuse_is_isolated_from_later_inventory_author_changes(
    tmp_path: Path,
) -> None:
    """旧 manifest 的候选作者身份来自冻结快照，不读取后来更新的库存投影。"""

    derived, root, config, snapshot = _prepared_run(tmp_path, "image-frozen-author")
    image_path = root / "content.png"
    Image.new("RGB", (80, 80), "navy").save(image_path)
    manifest_path = tmp_path / "manifest.csv"
    _write_manifest(
        manifest_path,
        [_row(1, "content", "content.png", _sha(image_path))],
    )
    imported = import_image_manifest(
        derived,
        run_id="image-frozen-author",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=root,
        config=config,
    )
    first = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )
    with sqlite3.connect(derived) as connection:
        connection.execute(
            """
            UPDATE source_post_inventory
            SET current_author_sha256 = ?, current_author_identity_present = 0
            WHERE source_post_id = 1
            """,
            ("f" * 64,),
        )
    repeated = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )

    assert repeated == first


def test_old_snapshot_manifest_imports_after_current_inventory_relationship_changes(
    tmp_path: Path,
) -> None:
    """延迟导入旧快照时，帖子、角色和作者语义不能被当前库存覆盖。"""

    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    root = tmp_path / "images"
    root.mkdir()
    _build_source(source)
    config = load_config(CONFIG_PATH)
    old_snapshot = snapshot_source(source, derived, config, "image-old-snapshot")
    discover_increment(derived, old_snapshot.snapshot_id, config)

    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE web_posts SET author_platform_id = 'new-a1' WHERE id = 1"
        )
        connection.execute(
            """
            UPDATE web_post_images
            SET web_post_id = 2, image_role = 'author_avatar'
            WHERE id = 1
            """
        )
    new_snapshot = snapshot_source(source, derived, config, "image-new-snapshot")
    discover_increment(derived, new_snapshot.snapshot_id, config)

    image_path = root / "old-content.png"
    Image.new("RGB", (80, 80), "maroon").save(image_path)
    manifest_path = tmp_path / "old-manifest.csv"
    _write_manifest(
        manifest_path,
        [_row(1, "content", image_path.name, _sha(image_path))],
    )
    imported = import_image_manifest(
        derived,
        run_id="image-old-snapshot",
        source_snapshot_id=old_snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=root,
        config=config,
    )
    build = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )

    with connect_derived(derived) as connection:
        current = connection.execute(
            """
            SELECT i.source_post_id, p.current_author_sha256
            FROM source_image_inventory AS i
            JOIN source_post_inventory AS p ON p.source_post_id = 1
            WHERE i.source_image_id = 1
            """
        ).fetchone()
        assert int(current["source_post_id"]) == 2
        old_author_sha256 = canonical_sha256(
            {"platform_key": "xhs", "author_platform_id": "a1"}
        )
        assert current["current_author_sha256"] != old_author_sha256
        row = connection.execute(
            """
            SELECT f.fingerprint_id, f.row_identity_sha256, f.file_sha256,
                   f.phash_hex, f.byte_size, f.width_px, f.height_px,
                   f.is_fully_transparent, r.source_image_id, r.source_post_id,
                   d.relation_role, d.handling_action
            FROM image_fingerprints AS f
            JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
            JOIN image_role_results AS d ON d.manifest_row_id = r.manifest_row_id
            WHERE r.manifest_id = ?
            """,
            (imported.manifest_id,),
        ).fetchone()
        assert (row["source_post_id"], row["relation_role"], row["handling_action"]) == (
            1,
            "content",
            "inspect_content",
        )
        expected_plan = build_image_candidate_plan(
            (
                CandidateImage(
                    fingerprint_id=str(row["fingerprint_id"]),
                    row_identity_sha256=str(row["row_identity_sha256"]),
                    source_image_id=int(row["source_image_id"]),
                    source_post_id=int(row["source_post_id"]),
                    author_identity_sha256=old_author_sha256,
                    file_sha256=str(row["file_sha256"]),
                    phash_hex=str(row["phash_hex"]),
                    byte_size=int(row["byte_size"]),
                    width_px=int(row["width_px"]),
                    height_px=int(row["height_px"]),
                    is_fully_transparent=bool(row["is_fully_transparent"]),
                    url_role_hint=False,
                ),
            ),
            config.image,
        )
    assert build.input_manifest_sha256 == expected_plan.input_manifest_sha256


def test_build_members_reject_cross_manifest_and_context_mismatches(tmp_path: Path) -> None:
    """两个 manifest 的指纹不能被直接 SQL 拼接到对方候选构建。"""

    derived, root, config, snapshot = _prepared_run(tmp_path, "image-member-context")
    first_file = root / "first.png"
    second_file = root / "second.png"
    Image.new("RGB", (80, 80), "red").save(first_file)
    Image.new("RGB", (80, 80), "blue").save(second_file)
    imported_manifests = []
    for image_id, image_file in ((1, first_file), (2, second_file)):
        manifest_path = tmp_path / f"manifest-{image_id}.csv"
        _write_manifest(
            manifest_path,
            [_row(image_id, "content", image_file.name, _sha(image_file))],
        )
        imported = import_image_manifest(
            derived,
            run_id="image-member-context",
            source_snapshot_id=snapshot.snapshot_id,
            manifest_path=manifest_path,
            image_root=root,
            config=config,
        )
        process_image_fingerprints(
            derived,
            manifest_id=imported.manifest_id,
            image_root=root,
            config=config,
        )
        imported_manifests.append(imported)

    with connect_derived(derived) as connection:
        fingerprints = connection.execute(
            """
            SELECT f.fingerprint_id, f.fingerprint_version,
                   r.manifest_id, r.source_image_id, r.source_post_id,
                   r.row_identity_sha256
            FROM image_fingerprints AS f
            JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
            ORDER BY r.source_image_id
            """
        ).fetchall()
        for build_seed, target_manifest, foreign_fingerprint in (
            ("a", imported_manifests[0].manifest_id, fingerprints[1]),
            ("b", imported_manifests[1].manifest_id, fingerprints[0]),
        ):
            build_id = build_seed * 32
            _insert_building_candidate(
                connection,
                build_id=build_id,
                run_id="image-member-context",
                manifest_id=target_manifest,
                fingerprint_version=str(foreign_fingerprint["fingerprint_version"]),
                config_sha256=config.sha256,
                identity_seed=build_seed,
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO image_candidate_build_members(
                        build_id, fingerprint_id, source_image_id, source_post_id,
                        row_identity_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        foreign_fingerprint["fingerprint_id"],
                        foreign_fingerprint["source_image_id"],
                        foreign_fingerprint["source_post_id"],
                        foreign_fingerprint["row_identity_sha256"],
                    ),
                )

        local = fingerprints[0]
        _insert_building_candidate(
            connection,
            build_id="c" * 32,
            run_id="image-member-context",
            manifest_id=str(local["manifest_id"]),
            fingerprint_version="wrong-fingerprint-version",
            config_sha256=config.sha256,
            identity_seed="c",
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_candidate_build_members(
                    build_id, fingerprint_id, source_image_id, source_post_id,
                    row_identity_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "c" * 32,
                    local["fingerprint_id"],
                    local["source_image_id"],
                    local["source_post_id"],
                    local["row_identity_sha256"],
                ),
            )

        _insert_building_candidate(
            connection,
            build_id="d" * 32,
            run_id="image-member-context",
            manifest_id=str(local["manifest_id"]),
            fingerprint_version=str(local["fingerprint_version"]),
            config_sha256=config.sha256,
            identity_seed="d",
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_candidate_build_members(
                    build_id, fingerprint_id, source_image_id, source_post_id,
                    row_identity_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "d" * 32,
                    local["fingerprint_id"],
                    999,
                    local["source_post_id"],
                    local["row_identity_sha256"],
                ),
            )


def test_candidate_build_lineage_and_seal_are_enforced_by_sqlite(tmp_path: Path) -> None:
    """组合外键、代表成员和封存触发器必须抵御绕过仓储层的非法写入。"""

    derived, root, config, snapshot = _prepared_run(tmp_path, "image-schema-guards")
    image_path = root / "content.png"
    Image.new("RGB", (80, 80), "gold").save(image_path)
    manifest_path = tmp_path / "manifest.csv"
    _write_manifest(
        manifest_path,
        [_row(1, "content", "content.png", _sha(image_path))],
    )
    imported = import_image_manifest(
        derived,
        run_id="image-schema-guards",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=root,
        config=config,
    )
    finalized = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )

    with connect_derived(derived) as connection:
        original = connection.execute(
            """
            SELECT f.*, r.source_image_id, r.source_post_id
            FROM image_fingerprints AS f
            JOIN image_manifest_rows AS r ON r.manifest_row_id = f.manifest_row_id
            LIMIT 1
            """
        ).fetchone()
        foreign_fingerprint_id = "e" * 32
        connection.execute(
            """
            INSERT INTO image_fingerprints(
                fingerprint_id, manifest_row_id, fingerprint_version,
                row_identity_sha256, file_sha256, mime_type, byte_size,
                width_px, height_px, has_alpha, is_fully_transparent,
                sanitized_exif_json, phash_hex, phash_hash_size,
                phash_highfreq_factor, library_versions_json,
                output_sha256, created_at_utc
            ) VALUES (?, ?, 'foreign-version', ?, ?, 'image/png', 100,
                      80, 80, 0, 0, '{}', '0000000000000000', 8, 4,
                      '{}', ?, '2026-07-31T00:00:00+00:00')
            """,
            (
                foreign_fingerprint_id,
                original["manifest_row_id"],
                original["row_identity_sha256"],
                "e" * 64,
                "d" * 64,
            ),
        )
        building_id = "c" * 32
        connection.execute(
            """
            INSERT INTO image_candidate_builds(
                build_id, run_id, manifest_id, candidate_version,
                fingerprint_version, config_sha256, input_manifest_sha256,
                expected_fingerprint_count, exact_cluster_count,
                exact_duplicate_cluster_count, near_pair_count, signal_count,
                seal_status, output_sha256, created_at_utc
            ) VALUES (?, 'image-schema-guards', ?, 'foreign-candidates',
                      'foreign-version', ?, ?, 1, 1, 0, 0, 0,
                      'building', ?, '2026-07-31T00:00:00+00:00')
            """,
            (building_id, imported.manifest_id, config.sha256, "c" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO image_candidate_build_members(
                build_id, fingerprint_id, source_image_id, source_post_id,
                row_identity_sha256
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                building_id,
                foreign_fingerprint_id,
                original["source_image_id"],
                original["source_post_id"],
                original["row_identity_sha256"],
            ),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_candidate_signals(
                    build_id, fingerprint_id, signal_code, evidence_json
                ) VALUES (?, ?, 'tiny_file', '{}')
                """,
                (building_id, original["fingerprint_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_exact_clusters(
                    build_id, cluster_id, file_sha256,
                    representative_fingerprint_id, member_count
                ) VALUES (?, 'bad-representative', ?, ?, 1)
                """,
                (building_id, "a" * 64, original["fingerprint_id"]),
            )

        connection.execute(
            """
            INSERT INTO image_exact_clusters(
                build_id, cluster_id, file_sha256,
                representative_fingerprint_id, member_count
            ) VALUES (?, 'incomplete-cluster', ?, ?, 1)
            """,
            (building_id, "b" * 64, foreign_fingerprint_id),
        )
        connection.execute(
            """
            INSERT INTO image_exact_cluster_members(
                build_id, cluster_id, fingerprint_id, is_representative
            ) VALUES (?, 'incomplete-cluster', ?, 0)
            """,
            (building_id, foreign_fingerprint_id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE image_candidate_builds SET seal_status = 'finalized' WHERE build_id = ?",
                (building_id,),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE image_exact_clusters SET member_count = member_count WHERE build_id = ?",
                (finalized.build_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM image_exact_clusters WHERE build_id = ?",
                (finalized.build_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_candidate_signals(
                    build_id, fingerprint_id, signal_code, evidence_json
                ) VALUES (?, ?, 'tiny_file', '{}')
                """,
                (finalized.build_id, original["fingerprint_id"]),
            )


@pytest.mark.parametrize("legacy_version", [11, 12])
def test_existing_candidate_rows_upgrade_idempotently(
    tmp_path: Path,
    monkeypatch,
    legacy_version: int,
) -> None:
    """模拟已有候选数据的 v11/v12 本地库，验证升级至 v13 保留行。"""

    original_v12 = schema_module._SCHEMA_V12
    original_v13 = schema_module._SCHEMA_V13
    if legacy_version == 11:
        monkeypatch.setattr(schema_module, "_SCHEMA_V12", "")
    monkeypatch.setattr(schema_module, "_SCHEMA_V13", "")
    run_id = f"image-v{legacy_version}-upgrade"
    derived, root, config, snapshot = _prepared_run(tmp_path, run_id)
    image_path = root / "content.png"
    Image.new("RGB", (80, 80), "lime").save(image_path)
    manifest_path = tmp_path / "manifest.csv"
    _write_manifest(
        manifest_path,
        [_row(1, "content", "content.png", _sha(image_path))],
    )
    imported = import_image_manifest(
        derived,
        run_id=run_id,
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=root,
        config=config,
    )
    process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=root,
        config=config,
    )
    build = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )
    with connect_derived(derived) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "image_candidate_builds",
                "image_candidate_build_members",
                "image_candidate_signals",
                "image_exact_clusters",
                "image_exact_cluster_members",
                "image_near_candidate_pairs",
            )
        }
        if legacy_version == 11:
            connection.execute("DELETE FROM schema_migrations WHERE version = 12")
        connection.execute("DELETE FROM schema_migrations WHERE version = 13")

    monkeypatch.setattr(schema_module, "_SCHEMA_V12", original_v12)
    monkeypatch.setattr(schema_module, "_SCHEMA_V13", original_v13)
    with connect_derived(derived) as connection:
        schema_module.migrate_derived(connection)
        schema_module.migrate_derived(connection)
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        assert after == before
        assert connection.execute(
            "SELECT seal_status FROM image_candidate_builds WHERE build_id = ?",
            (build.build_id,),
        ).fetchone()[0] == "finalized"
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 12"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 13"
        ).fetchone()[0] == 1


def test_missing_hash_conflict_decode_failure_and_role_skip_are_separate(tmp_path: Path) -> None:
    derived, root, config, snapshot = _prepared_run(tmp_path, "image-blocks")
    valid = root / "valid.png"
    Image.new("RGB", (80, 80), "green").save(valid)
    corrupt = root / "corrupt.png"
    corrupt.write_bytes(b"broken-image")
    manifest = tmp_path / "manifest.csv"
    _write_manifest(
        manifest,
        [
            _row(1, "content", "missing.png", "a" * 64),
            _row(2, "content", "valid.png", "b" * 64),
            _row(3, "content", "corrupt.png", _sha(corrupt)),
            _row(4, "author_avatar", "also-missing.png", "c" * 64),
        ],
    )
    imported = import_image_manifest(
        derived,
        run_id="image-blocks",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest,
        image_root=root,
        config=config,
    )

    result = process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=root,
        config=config,
    )

    assert result.blocked_count == 3
    assert result.skipped_count == 1
    assert result.reason_counts == {
        "author_avatar_skipped": 1,
        "image_decode_failed": 1,
        "image_file_missing": 1,
        "image_sha256_mismatch": 1,
    }
    with sqlite3.connect(derived) as connection:
        statuses = connection.execute(
            """
            SELECT status, reason_code FROM image_processing_attempts
            WHERE operation = 'fingerprints' ORDER BY reason_code
            """
        ).fetchall()
        assert {status for status, _ in statuses} == {"blocked", "skipped"}
