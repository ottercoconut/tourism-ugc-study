"""唯一CSV生产推理入口及历史研究CLI收口测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _help(script: str) -> str:
    """读取脚本帮助并要求入口可正常解析。"""

    process = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout


def test_production_inference_has_one_csv_entry_without_subcommands() -> None:
    """生产推理应只保留一个脚本和一个必填CSV参数。"""

    output = _help("cleaning_predict_tourism_relevance.py")
    assert "--input-csv INPUT_CSV" in output
    assert "score-population" not in output
    assert "score-new-batch" not in output
    assert "--vector-cache-root" in output


def test_historical_retraining_cli_no_longer_exposes_prediction() -> None:
    """已完成训练研究入口不得继续并列暴露模型推理子命令。"""

    output = _help("cleaning_retrain_routing_model.py")
    assert "复现已完成" in output
    assert "score-population" not in output
    assert "score-new-batch" not in output


def test_production_inference_requires_explicit_prediction_confirmation() -> None:
    """生产入口必须先确认纯预测，不应提前读取输入或权重。"""

    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "cleaning_predict_tourism_relevance.py"),
            "--input-csv",
            str(ROOT / "does-not-exist.csv"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 2
    assert (
        "model_retraining_incremental_confirmation_required"
        in process.stderr
    )
