from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.image_manifest import IMAGE_MANIFEST_COLUMNS
from tourism_ugc_study.cleaning.image_repository import (
    ImageRepositoryError,
    build_image_candidates,
    import_image_manifest,
    load_image_stage_snapshot,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.inventory import discover_increment
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

    assert fingerprints.succeeded_count == 3
    assert fingerprints.blocked_count == 0
    assert fingerprints.skipped_count == 1
    assert candidates.fingerprint_count == 3
    assert candidates.exact_cluster_count == 2
    assert candidates.exact_duplicate_cluster_count == 1
    assert {_sha(route), _sha(near)} == before
    with sqlite3.connect(derived) as connection:
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
