from __future__ import annotations

import csv
import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

from PIL import Image

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.image_manifest import IMAGE_MANIFEST_COLUMNS
from tourism_ugc_study.cleaning.image_repository import (
    import_image_manifest,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.scheduler import create_batch, get_batch_status
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tourism_ugc_study.cleaning.state_machine import claim_tasks, finish_task
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _run_script(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / name), *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_manifest(path: Path, file_sha256: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=IMAGE_MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "source_image_id": 1,
                "source_post_id": 1,
                "relation_role": "content",
                "relative_path": "content.png",
                "file_sha256": file_sha256,
                "parent_file_sha256": "",
                "transform_json": "{}",
            }
        )


def _finish_text_chain(derived: Path, batch_id: str, config) -> None:
    """完成单帖文本链，证明图片阻塞不会阻止文本形成终态。"""

    for stage in ("text_deterministic", "text_relevance", "finalize"):
        claims = [
            claim
            for claim in claim_tasks(derived, batch_id, config, stage_name=stage)
            if claim.object_type == "post"
        ]
        for claim in claims:
            finish_task(
                derived,
                claim.task_id,
                "succeeded",
                config=config,
                output_sha256="d" * 64,
            )


def test_cli_blocks_without_manifest_then_explicitly_recovers(tmp_path: Path) -> None:
    source = tmp_path / "private-source.sqlite"
    derived = tmp_path / "private-processed" / "cleaning.sqlite"
    image_root = tmp_path / "private-images"
    image_root.mkdir()
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, "image-cli-run")
    discover_increment(derived, snapshot.snapshot_id, config)
    batch = create_batch(derived, "image-cli-run", config, max_posts=1)
    common = ("--derived-db", str(derived), "--config", str(CONFIG_PATH))

    blocked = _run_script(
        "cleaning_process_images.py",
        *common,
        "roles",
        "--batch-id",
        batch.batch_id,
    )
    assert blocked.returncode == 0
    assert json.loads(blocked.stdout)["status"] == "blocked"
    assert str(tmp_path) not in blocked.stdout + blocked.stderr
    _finish_text_chain(derived, batch.batch_id, config)
    assert get_batch_status(derived, batch.batch_id).batch.status == "completed_with_blocks"

    image_path = image_root / "content.png"
    Image.new("RGB", (96, 96), "teal").save(image_path)
    original_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "image-manifest.csv"
    _write_manifest(manifest_path, original_sha)
    imported = _run_script(
        "cleaning_process_images.py",
        *common,
        "import-manifest",
        "--run-id",
        "image-cli-run",
        "--snapshot-id",
        snapshot.snapshot_id,
        "--manifest",
        str(manifest_path),
        "--image-root",
        str(image_root),
    )
    assert imported.returncode == 0
    manifest_id = json.loads(imported.stdout)["manifest_id"]
    assert str(tmp_path) not in imported.stdout + imported.stderr

    resumed = _run_script(
        "cleaning_resume_batch.py",
        *common,
        "--batch-id",
        batch.batch_id,
    )
    assert resumed.returncode == 0
    assert json.loads(resumed.stdout)["requeued"] == 4

    roles = _run_script(
        "cleaning_process_images.py",
        *common,
        "roles",
        "--batch-id",
        batch.batch_id,
        "--manifest-id",
        manifest_id,
    )
    fingerprints = _run_script(
        "cleaning_process_images.py",
        *common,
        "fingerprints",
        "--batch-id",
        batch.batch_id,
        "--manifest-id",
        manifest_id,
        "--image-root",
        str(image_root),
    )
    candidates = _run_script(
        "cleaning_process_images.py",
        *common,
        "candidates",
        "--batch-id",
        batch.batch_id,
        "--manifest-id",
        manifest_id,
    )

    assert roles.returncode == fingerprints.returncode == candidates.returncode == 0
    assert json.loads(roles.stdout)["succeeded_count"] == 1
    assert json.loads(fingerprints.stdout)["tasks"]["succeeded_count"] == 1
    assert json.loads(candidates.stdout)["tasks"]["succeeded_count"] == 1
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == original_sha
    all_output = roles.stdout + fingerprints.stdout + candidates.stdout
    assert str(tmp_path) not in all_output
    assert "https://" not in all_output


def test_image_operations_do_not_open_network_connections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """在进程内封锁 socket，验证 manifest 和指纹路径不依赖网络。"""

    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    image_root = tmp_path / "images"
    image_root.mkdir()
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, "no-network-run")
    discover_increment(derived, snapshot.snapshot_id, config)
    image_path = image_root / "content.png"
    Image.new("RGB", (80, 80), "orange").save(image_path)
    manifest_path = tmp_path / "manifest.csv"
    _write_manifest(manifest_path, hashlib.sha256(image_path.read_bytes()).hexdigest())

    def reject_network(*_args, **_kwargs):
        raise AssertionError("图片清洗框架不得联网")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    imported = import_image_manifest(
        derived,
        run_id="no-network-run",
        source_snapshot_id=snapshot.snapshot_id,
        manifest_path=manifest_path,
        image_root=image_root,
        config=config,
    )
    result = process_image_fingerprints(
        derived,
        manifest_id=imported.manifest_id,
        image_root=image_root,
        config=config,
    )

    assert result.succeeded_count == 1
