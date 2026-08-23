#!/usr/bin/env python3
"""训练并封存 sparse challenger；只执行训练侧 UGC 安全验收。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.config import (
    ConfigurationError,
    load_cleaning_config_bundle,
)
from tourism_ugc_study.cleaning.reference_evidence import ReferenceEvidenceError
from tourism_ugc_study.models.text.formal_baseline import FormalBaselineError
from tourism_ugc_study.models.text.model_acceptance import ModelAcceptanceError
from tourism_ugc_study.models.text.sparse_challenger import SparseChallengerError
from tourism_ugc_study.models.text.sparse_challenger_artifacts import (
    SparseChallengerArtifactError,
    render_sparse_challenger_result,
    train_sparse_challenger_package,
)
from tourism_ugc_study.models.text.sparse_challenger_config import (
    SparseChallengerConfigError,
)


def _parser() -> argparse.ArgumentParser:
    """构造不接收验证、测试、平台或阈值参数的 CLI。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/cleaning.yaml"))
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-text-challenger.yaml"),
    )
    parser.add_argument(
        "--acceptance-policy",
        type=Path,
        default=Path("configs/cleaning-model-acceptance.yaml"),
    )
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument("--baseline-package", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--code-version")
    parser.add_argument(
        "--output-format",
        choices=("human", "json"),
        default="human",
        help="输出安全优先的中文报告（默认）或稳定机器 JSON",
    )
    parser.add_argument(
        "--execute-training",
        action="store_true",
        help="显式确认执行预登记的54候选嵌套分组拟合；缺少时失败关闭",
    )
    return parser


def _git_version() -> str:
    """读取当前 HEAD 完整 SHA 作为默认训练代码身份。"""

    try:
        process = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SparseChallengerArtifactError(
            "sparse_challenger_git_version_unavailable"
        ) from exc
    return process.stdout.strip()


def main() -> int:
    """执行显式训练并输出不含路径、成员或正文的摘要。"""

    parser = _parser()
    args = parser.parse_args()
    if not args.execute_training:
        parser.error("sparse challenger training requires --execute-training")
    try:
        config, normalization_config = load_cleaning_config_bundle(args.config)
        result = train_sparse_challenger_package(
            args.csv,
            args.manifest,
            args.derived_db,
            args.baseline_package,
            args.plan,
            args.acceptance_policy,
            args.artifact_root,
            config=config,
            normalization_config=normalization_config,
            code_version=args.code_version or _git_version(),
        )
        print(
            render_sparse_challenger_result(
                result, output_format=args.output_format
            )
        )
    except (
        ConfigurationError,
        FormalBaselineError,
        ModelAcceptanceError,
        ReferenceEvidenceError,
        SparseChallengerArtifactError,
        SparseChallengerConfigError,
        SparseChallengerError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", "sparse_challenger_failed")
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

