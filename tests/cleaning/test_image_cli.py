from __future__ import annotations

import csv
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

from PIL import Image

import tourism_ugc_study.cleaning.image_contract as image_contract_module
from scripts import cleaning_process_images
from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.image_manifest import IMAGE_MANIFEST_COLUMNS
from tourism_ugc_study.cleaning.image_repository import (
    build_image_candidates,
    import_image_manifest,
    load_image_stage_snapshot,
    process_image_fingerprints,
)
from tourism_ugc_study.cleaning.inventory import discover_increment
from tourism_ugc_study.cleaning.scheduler import create_batch, get_batch_status
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tourism_ugc_study.cleaning.state_machine import claim_tasks, finish_task
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _run_script(
    name: str,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / name), *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _network_block_environment(tmp_path: Path) -> dict[str, str]:
    """让 CLI 子进程在启动时封锁两条常用 TCP 连接入口。"""

    guard = tmp_path / "network-guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        """
import socket

def _reject_network(*_args, **_kwargs):
    raise AssertionError("图片清洗框架不得联网")

socket.create_connection = _reject_network
socket.socket.connect = _reject_network
""".lstrip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(guard) if not existing else f"{guard}{os.pathsep}{existing}"
    )
    return environment


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


def _prepare_import_inputs(tmp_path: Path, run_id: str):
    """建立只含合成图片的 CLI manifest 导入夹具。"""

    source = tmp_path / "source.sqlite"
    derived = tmp_path / "processed" / "cleaning.sqlite"
    image_root = tmp_path / "images"
    image_root.mkdir()
    _build_source(source)
    config = load_config(CONFIG_PATH)
    snapshot = snapshot_source(source, derived, config, run_id)
    discover_increment(derived, snapshot.snapshot_id, config)
    image_path = image_root / "content.png"
    Image.new("RGB", (80, 80), "olive").save(image_path)
    manifest_path = tmp_path / "manifest.csv"
    _write_manifest(manifest_path, hashlib.sha256(image_path.read_bytes()).hexdigest())
    return derived, image_root, manifest_path, snapshot


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
    blocked_network = _network_block_environment(tmp_path)

    blocked = _run_script(
        "cleaning_process_images.py",
        *common,
        "roles",
        "--batch-id",
        batch.batch_id,
        env=blocked_network,
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
        env=blocked_network,
    )
    assert imported.returncode == 0
    manifest_id = json.loads(imported.stdout)["manifest_id"]
    assert str(tmp_path) not in imported.stdout + imported.stderr

    resumed = _run_script(
        "cleaning_resume_batch.py",
        *common,
        "--batch-id",
        batch.batch_id,
        env=blocked_network,
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
        env=blocked_network,
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
        env=blocked_network,
    )
    candidates = _run_script(
        "cleaning_process_images.py",
        *common,
        "candidates",
        "--batch-id",
        batch.batch_id,
        "--manifest-id",
        manifest_id,
        env=blocked_network,
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
    """在进程内封锁常用 socket 入口，验证完整证据链不依赖网络。"""

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
    monkeypatch.setattr(socket.socket, "connect", reject_network)
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
    build = build_image_candidates(
        derived,
        manifest_id=imported.manifest_id,
        config=config,
    )
    roles = load_image_stage_snapshot(
        derived,
        manifest_id=imported.manifest_id,
        stage="roles",
        config=config,
    )
    fingerprints = load_image_stage_snapshot(
        derived,
        manifest_id=imported.manifest_id,
        stage="fingerprints",
        config=config,
    )
    candidates = load_image_stage_snapshot(
        derived,
        manifest_id=imported.manifest_id,
        stage="candidates",
        config=config,
        build_id=build.build_id,
    )

    assert result.succeeded_count == 1
    assert roles.outcomes[0].status == "succeeded"
    assert fingerprints.outcomes[0].status == "succeeded"
    assert candidates.outcomes[0].status == "succeeded"


def test_cli_redacts_path_when_frozen_snapshot_file_disappears(tmp_path: Path) -> None:
    """快照文件消失必须返回领域错误 JSON，不能泄露路径或 traceback。"""

    derived, image_root, manifest_path, snapshot = _prepare_import_inputs(
        tmp_path,
        "snapshot-disappeared",
    )
    with sqlite3.connect(derived) as connection:
        snapshot_path = Path(
            connection.execute(
                "SELECT snapshot_path FROM source_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()[0]
        )
    snapshot_path.unlink()

    result = _run_script(
        "cleaning_process_images.py",
        "--derived-db",
        str(derived),
        "--config",
        str(CONFIG_PATH),
        "import-manifest",
        "--run-id",
        "snapshot-disappeared",
        "--snapshot-id",
        snapshot.snapshot_id,
        "--manifest",
        str(manifest_path),
        "--image-root",
        str(image_root),
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "reason_code": "snapshot_unreadable",
        "status": "failed",
    }
    assert str(tmp_path) not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_cli_redacts_path_from_snapshot_read_oserror(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """可移植 OSError 模拟也只能产生固定领域错误和退出码 1。"""

    derived, image_root, manifest_path, snapshot = _prepare_import_inputs(
        tmp_path,
        "snapshot-read-error",
    )

    def raise_private_oserror(_path):
        raise OSError(f"cannot read private snapshot: {tmp_path / 'secret.sqlite'}")

    monkeypatch.setattr(image_contract_module, "sha256_file", raise_private_oserror)
    exit_code = cleaning_process_images.main(
        [
            "--derived-db",
            str(derived),
            "--config",
            str(CONFIG_PATH),
            "import-manifest",
            "--run-id",
            "snapshot-read-error",
            "--snapshot-id",
            snapshot.snapshot_id,
            "--manifest",
            str(manifest_path),
            "--image-root",
            str(image_root),
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 1
    assert output.out == ""
    assert json.loads(output.err) == {
        "reason_code": "snapshot_unreadable",
        "status": "failed",
    }
    assert str(tmp_path) not in output.out + output.err
    assert "Traceback" not in output.err
