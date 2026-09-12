"""用合成文件验证封存边界、媒体字节、保护和登记；不读取真实研究资料。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.research_freeze import seal_freeze, verify_freeze
from tourism_ugc_study.cleaning.research_freeze_files import (
    asset_files, inventory_files, owned_path, protect_assets, verify_inventory, verify_protection,
)
from tourism_ugc_study.cleaning.research_freeze_media import archive_media
from tourism_ugc_study.cleaning.source_snapshot import file_sha256


def _asset(tmp_path: Path) -> tuple[Path, Path]:
    """创建明确可在测试结束解锁的合成资产；不对真实文件调用清理函数。"""
    workspace = tmp_path.resolve()
    asset = workspace / "results/synthetic-run/file.txt"
    asset.parent.mkdir(parents=True)
    asset.write_text("synthetic")
    return workspace, asset


def _unlock_test_files(workspace: Path) -> None:
    """只为pytest自己创建的临时文件撤销标志，生产模块没有解冻入口。"""
    for path in sorted(workspace.rglob("*"), key=lambda p: len(p.parts)):
        if hasattr(os, "chflags"):
            os.chflags(path, 0)
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize("relative", ["/tmp/file", "data", "results", "data/../secret", "src/private/file"])
def test_freeze_rejects_broad_or_escaped_scope(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        owned_path(tmp_path.resolve(), relative)


def test_freeze_rejects_alias_and_hardlinks_and_sqlite_sidecar(tmp_path: Path) -> None:
    workspace, asset = _asset(tmp_path)
    link = asset.parent / "link"
    link.symlink_to(asset)
    with pytest.raises(ValueError, match="nonregular"):
        asset_files(workspace, ["results/synthetic-run"])
    link.unlink()
    os.link(asset, link)
    with pytest.raises(ValueError, match="hardlink"):
        asset_files(workspace, ["results/synthetic-run"])
    link.unlink()
    db = asset.with_suffix(".sqlite")
    db.write_bytes(b"synthetic")
    Path(str(db) + "-wal").write_bytes(b"unmerged")
    with pytest.raises(ValueError, match="sidecar"):
        asset_files(workspace, [str(db.relative_to(workspace))])


def test_inventory_detects_tampering_extra_missing_and_overlap(tmp_path: Path) -> None:
    workspace, asset = _asset(tmp_path)
    roots = ["results/synthetic-run"]
    entries = inventory_files(workspace, roots)
    verify_inventory(workspace, roots, entries)
    with pytest.raises(ValueError, match="overlapping"):
        asset_files(workspace, [*roots, "results/synthetic-run/file.txt"])
    asset.write_text("tampered")
    with pytest.raises(ValueError, match="inventory_mismatch"):
        verify_inventory(workspace, roots, entries)
    asset.write_text("synthetic")
    extra = asset.parent / "extra"
    extra.touch()
    with pytest.raises(ValueError, match="inventory_mismatch"):
        verify_inventory(workspace, roots, entries)
    extra.unlink()
    asset.unlink()
    with pytest.raises(ValueError, match="inventory_mismatch"):
        verify_inventory(workspace, roots, entries)


def test_special_root_and_manifest_alias_are_rejected(tmp_path: Path) -> None:
    workspace, asset = _asset(tmp_path)
    pipe = asset.parent / "synthetic-pipe"
    os.mkfifo(pipe)
    with pytest.raises(ValueError, match="nonregular_asset_root"):
        asset_files(workspace, [str(pipe.relative_to(workspace))])
    alias = asset.parent / "manifest-alias.json"
    alias.symlink_to(asset)
    with pytest.raises(ValueError, match="symlink_forbidden"):
        verify_freeze(workspace, alias, file_sha256(asset))


@pytest.mark.skipif(sys.platform != "darwin", reason="uchg is the explicit macOS protection contract")
def test_protection_is_verified_idempotent_and_source_independent(tmp_path: Path) -> None:
    workspace, asset = _asset(tmp_path)
    roots = ["results/synthetic-run"]
    with pytest.raises(ValueError, match="not_protected"):
        verify_protection(workspace, roots)
    try:
        protect_assets(workspace, roots)
        protect_assets(workspace, roots)
        assert verify_protection(workspace, roots) == 1
        with pytest.raises(PermissionError):
            asset.write_text("should fail")
        os.chflags(asset, 0)
        with pytest.raises(ValueError, match="not_protected"):
            verify_protection(workspace, roots)
    finally:
        _unlock_test_files(workspace)


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS clone required; no hardlink fallback")
def test_media_clone_binds_all_images_without_modifying_source(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source = root / "source/data/media/platform/image.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic-image")
    before = source.stat()
    database = root / "raw.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE web_post_images(id INTEGER, local_path TEXT, sha256 TEXT)")
        conn.executemany("INSERT INTO web_post_images VALUES(?,?,?)", [(i, "data/media/platform/image.jpg", file_sha256(source)) for i in (1, 2)])
    destination = root / "archive/media-root"
    result = archive_media(database, root / "source", destination)
    clone = destination / "data/media/platform/image.jpg"
    assert result["relationship_count"] == 2
    assert result["file_count"] == 1
    assert clone.stat().st_ino != source.stat().st_ino
    assert (source.stat().st_mode, source.stat().st_flags) == (before.st_mode, before.st_flags)
    source.write_bytes(b"changed-source")
    assert clone.read_bytes() == b"synthetic-image"
    with pytest.raises(FileExistsError):
        archive_media(database, root / "source", destination)
    with pytest.raises(ValueError, match="content_hash_mismatch"):
        archive_media(database, root / "source", root / "bad/media-root")


@pytest.mark.parametrize("relative,digest", [("../escape", "a" * 64), ("data/media/img", None)])
def test_media_rejects_invalid_reference_before_clone(tmp_path: Path, relative: str, digest: str | None) -> None:
    database = tmp_path / "raw.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE web_post_images(id INTEGER, local_path TEXT, sha256 TEXT)")
        conn.execute("INSERT INTO web_post_images VALUES(1,?,?)", (relative, digest))
    with pytest.raises(ValueError):
        archive_media(database, tmp_path, tmp_path / "out/media-root")
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="seal requires macOS uchg")
def test_seal_registers_only_after_full_verification_and_preserves_review_state(tmp_path: Path) -> None:
    workspace, asset = _asset(tmp_path)
    metadata = workspace / "data/processed/freeze-test"
    metadata.mkdir(parents=True)
    (workspace / "governance").mkdir()
    entries = inventory_files(workspace, ["results/synthetic-run"])
    inventory = metadata / "frozen-files.json"
    inventory.write_text(json.dumps(entries))
    manifest = metadata / "freeze-manifest.json"
    spec = {"contract": "research-data-freeze-v1", "freeze_id": "test",
            "inventory_filename": inventory.name, "inventory_sha256": file_sha256(inventory),
            "roots": ["results/synthetic-run"], "file_count": 1, "logical_bytes": len(b"synthetic"),
            "raw_post_count": 1, "media": {"file_count": 0}, "cleaning": {"record_count": 1, "decision_counts": {"keep": 1}},
            "freeze_code_version": "synthetic", "candidate_database_sha256": "candidate", "source_snapshot_sha256": "source"}
    manifest.write_text(json.dumps(spec))
    digest = file_sha256(manifest)
    pointer = workspace / "data/processed/current-cleaning.json"
    pointer.write_text(json.dumps({"candidate_database_sha256": "candidate", "status": "CANDIDATES_READY_AWAITING_HUMAN_REVIEW"}))
    (workspace / "data/processed/current-source.json").write_text(json.dumps({"snapshot_sha256": "source"}))
    asset.write_text("tampered")
    with pytest.raises(ValueError, match="inventory_mismatch"):
        seal_freeze(workspace, manifest, digest)
    assert not (workspace / "governance/research-data-freezes.json").exists()
    asset.write_text("synthetic")
    try:
        receipt = seal_freeze(workspace, manifest, digest)
        assert receipt["status"] == "FROZEN" and not receipt["final_research_dataset_frozen"]
        assert seal_freeze(workspace, manifest, digest) == receipt
        assert verify_freeze(workspace, manifest, digest)["file_count"] == 1
        assert json.loads(pointer.read_text())["status"] == "CANDIDATES_READY_AWAITING_HUMAN_REVIEW"
        assert json.loads(pointer.read_text())["inference_resume_authorized"] is False
        with pytest.raises(ValueError, match="hash_mismatch"):
            verify_freeze(workspace, manifest, hashlib.sha256(b"wrong").hexdigest())
    finally:
        _unlock_test_files(workspace)
