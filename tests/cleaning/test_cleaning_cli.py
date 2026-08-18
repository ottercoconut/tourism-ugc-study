from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tourism_ugc_study.cleaning.config import load_config
from tourism_ugc_study.cleaning.snapshot import snapshot_source
from tests.cleaning.test_incremental_inventory import _build_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v3.2.yaml"


def _run_script(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """从项目根目录运行薄入口并捕获 JSON 输出。"""

    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / name), *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_incremental_commands_form_a_non_sensitive_lifecycle(tmp_path: Path) -> None:
    source = tmp_path / "private-source.sqlite"
    derived = tmp_path / "private-processed" / "cleaning.sqlite"
    _build_source(source)
    snapshot = snapshot_source(source, derived, load_config(CONFIG_PATH), "cli-run")
    common = ("--derived-db", str(derived), "--config", str(CONFIG_PATH))

    discovery = _run_script(
        "cleaning_discover_increment.py",
        *common,
        "--snapshot-id",
        snapshot.snapshot_id,
    )
    assert discovery.returncode == 0
    assert json.loads(discovery.stdout)["tasks_created"] == 12

    created = _run_script(
        "cleaning_create_batch.py",
        *common,
        "--run-id",
        "cli-run",
        "--max-posts",
        "2",
    )
    assert created.returncode == 0
    batch_id = json.loads(created.stdout)["batch_id"]

    claimed = _run_script(
        "cleaning_run_batch.py",
        *common,
        "--batch-id",
        batch_id,
        "--stage",
        "text_deterministic",
    )
    assert claimed.returncode == 0
    claim_payload = json.loads(claimed.stdout)
    assert claim_payload["claimed"] == 2
    task_id = claim_payload["tasks"][0]["task_id"]

    finished = _run_script(
        "cleaning_run_batch.py",
        *common,
        "--batch-id",
        batch_id,
        "--finish-task",
        task_id,
        "--result",
        "succeeded",
        "--output-sha256",
        "a" * 64,
    )
    assert finished.returncode == 0
    assert json.loads(finished.stdout)["status"] == "succeeded"

    status = _run_script(
        "cleaning_run_batch.py",
        *common,
        "--batch-id",
        batch_id,
        "--status",
    )
    assert status.returncode == 0
    assert json.loads(status.stdout)["task_status_counts"]["succeeded"] == 1
    combined_output = discovery.stdout + created.stdout + claimed.stdout + status.stdout
    assert str(tmp_path) not in combined_output
    assert "正文" not in combined_output
    assert "author" not in combined_output


def test_cli_error_redacts_database_path(tmp_path: Path) -> None:
    derived = tmp_path / "private" / "missing.sqlite"
    completed = _run_script(
        "cleaning_create_batch.py",
        "--derived-db",
        str(derived),
        "--config",
        str(CONFIG_PATH),
        "--run-id",
        "missing-run",
    )
    assert completed.returncode == 1
    assert json.loads(completed.stderr) == {"reason_code": "run_not_found", "status": "failed"}
    assert str(tmp_path) not in completed.stderr
    assert "Traceback" not in completed.stderr
