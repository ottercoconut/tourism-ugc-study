from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.image_manifest import (
    IMAGE_MANIFEST_COLUMNS,
    ImageManifestError,
    parse_image_manifest,
)
from tourism_ugc_study.cleaning.image_repository import import_image_manifest
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    """按公开列契约生成测试清单；测试数据只包含合成哈希和相对路径。"""

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=IMAGE_MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _row(image_id: int, post_id: int, role: str, relative_path: str) -> dict[str, object]:
    return {
        "source_image_id": image_id,
        "source_post_id": post_id,
        "relation_role": role,
        "relative_path": relative_path,
        "file_sha256": f"{image_id:x}" * 64,
        "parent_file_sha256": "",
        "transform_json": "{}",
    }


def test_manifest_allows_shared_path_but_marks_source_id_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    manifest = tmp_path / "manifest.csv"
    rows = [
        _row(1, 1, "content", "shared/image.png"),
        _row(2, 2, "content", "shared/image.png"),
        _row(3, 3, "page", "page.png"),
        _row(3, 3, "content", "other.png"),
    ]
    _write_manifest(manifest, rows)

    parsed = parse_image_manifest(manifest, root)

    assert [row.validation_status for row in parsed.rows[:2]] == ["accepted", "accepted"]
    assert [row.validation_status for row in parsed.rows[2:]] == [
        "source_conflict",
        "source_conflict",
    ]
    assert {row.reason_code for row in parsed.rows[2:]} == {"source_image_mapping_conflict"}


@pytest.mark.parametrize(
    ("relative_path", "reason_code"),
    [
        ("/tmp/private.png", "absolute_path_forbidden"),
        ("../outside.png", "relative_path_not_normalized"),
        (".", "relative_path_is_root"),
        ("folder\\image.png", "relative_path_not_portable"),
    ],
)
def test_manifest_rejects_unsafe_paths(
    tmp_path: Path,
    relative_path: str,
    reason_code: str,
) -> None:
    root = tmp_path / "images"
    root.mkdir()
    manifest = tmp_path / "manifest.csv"
    _write_manifest(manifest, [_row(1, 1, "content", relative_path)])

    with pytest.raises(ImageManifestError) as error:
        parse_image_manifest(manifest, root)
    assert error.value.reason_code == reason_code


def test_manifest_rejects_symlink_escape_and_incomplete_crop_lineage(tmp_path: Path) -> None:
    root = tmp_path / "images"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    manifest = tmp_path / "manifest.csv"
    _write_manifest(manifest, [_row(1, 1, "content", "escape/image.png")])
    with pytest.raises(ImageManifestError) as escape:
        parse_image_manifest(manifest, root)
    assert escape.value.reason_code == "relative_path_escapes_root"

    crop = _row(1, 1, "content", "crop.png")
    crop["transform_json"] = '{"crop":[0,0,10,10]}'
    _write_manifest(manifest, [crop])
    with pytest.raises(ImageManifestError) as lineage:
        parse_image_manifest(manifest, root)
    assert lineage.value.reason_code == "derived_lineage_incomplete"


def test_import_persists_role_actions_without_absolute_paths(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    root = tmp_path / "private-images"
    root.mkdir()
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, "image-manifest-run")
    discover_increment(derived, snapshot.snapshot_id, config)
    manifest = tmp_path / "manifest.csv"
    _write_manifest(
        manifest,
        [
            _row(1, 1, "author_avatar", "avatar.png"),
            _row(2, 2, "page", "page.png"),
            _row(3, 3, "content", "route-plan.png"),
        ],
    )

    result = import_image_manifest(
        derived,
        run_id="image-manifest-run",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest,
        image_root=root,
        config=config,
    )
    repeated = import_image_manifest(
        derived,
        run_id="image-manifest-run",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest,
        image_root=root,
        config=config,
    )

    assert result == repeated
    assert result.accepted_row_count == 3
    assert len(result.accepted_row_identities) == 3
    with sqlite3.connect(derived) as connection:
        actions = connection.execute(
            "SELECT relation_role, handling_action FROM image_role_results ORDER BY relation_role"
        ).fetchall()
        assert actions == [
            ("author_avatar", "exclude_from_content"),
            ("content", "inspect_content"),
            ("page", "evidence_only"),
        ]
        stored = "\n".join(
            str(value)
            for row in connection.execute(
                "SELECT relative_path, root_identity_sha256 FROM image_manifest_rows JOIN image_manifest_imports USING(manifest_id)"
            )
            for value in row
        )
        assert str(root.resolve()) not in stored
