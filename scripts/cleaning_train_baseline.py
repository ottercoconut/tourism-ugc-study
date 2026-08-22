#!/usr/bin/env python3
"""训练并封存正式清洗 baseline；不打开测试集、不选择路由阈值。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.config import ConfigurationError, load_stable_config
from tourism_ugc_study.cleaning.reference_evidence import ReferenceEvidenceError
from tourism_ugc_study.models.text.formal_baseline import FormalBaselineError
from tourism_ugc_study.models.text.formal_training import (
    FormalTrainingError,
    train_formal_baseline_package,
)


def _parser() -> argparse.ArgumentParser:
    """构造正式 baseline 训练命令行。

    Returns:
        要求显式证据、泄漏构建、artifact 根目录和执行确认的参数解析器。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/cleaning.yaml"))
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument("--leakage-build-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--code-version")
    parser.add_argument(
        "--execute-training",
        action="store_true",
        help="显式确认拟合 baseline；缺少时失败关闭",
    )
    return parser


def _git_version() -> str:
    """读取当前仓库 Git SHA 作为默认代码身份。

    Returns:
        当前 ``HEAD`` 的完整 Git SHA。

    Raises:
        FormalTrainingError: 当前目录不是可解析提交的 Git 仓库。
    """

    try:
        process = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FormalTrainingError("baseline_git_version_unavailable") from exc
    value = process.stdout.strip()
    if len(value) != 40:
        raise FormalTrainingError("baseline_git_version_invalid")
    return value


def main() -> int:
    """执行显式 baseline 训练并输出不含路径和正文的摘要。

    Returns:
        成功或完整复用时为 ``0``；配置、证据或训练失败时为 ``2``。
    """

    parser = _parser()
    args = parser.parse_args()
    if not args.execute_training:
        parser.error("baseline training requires --execute-training")
    try:
        config = load_stable_config(args.config)
        result = train_formal_baseline_package(
            args.csv,
            args.manifest,
            args.derived_db,
            args.artifact_root,
            leakage_build_id=args.leakage_build_id,
            config=config,
            code_version=args.code_version or _git_version(),
        )
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
    except (
        ConfigurationError,
        ReferenceEvidenceError,
        FormalBaselineError,
        FormalTrainingError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", "baseline_configuration_invalid")
        print(
            json.dumps(
                {"status": "failed", "reason_code": reason_code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
