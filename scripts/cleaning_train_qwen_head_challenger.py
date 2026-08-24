#!/usr/bin/env python3
"""复用4B训练 embedding 搜索并封存分类头 challenger；不读取正文或测试。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.models.text.formal_baseline import FormalBaselineError
from tourism_ugc_study.models.text.model_acceptance import ModelAcceptanceError
from tourism_ugc_study.models.text.qwen_embedding_artifacts import (
    QwenEmbeddingArtifactError,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingConfigError,
)
from tourism_ugc_study.models.text.qwen_head_challenger import (
    QwenHeadChallengerError,
)
from tourism_ugc_study.models.text.qwen_head_challenger_artifacts import (
    QwenHeadChallengerArtifactError,
    render_qwen_head_challenger_result,
    train_qwen_head_challenger_package,
)
from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    QwenHeadChallengerConfigError,
)


def _parser() -> argparse.ArgumentParser:
    """构造不接收正文、验证、测试、平台、阈值或审计参数的 CLI。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-package",
        type=Path,
        required=True,
        help="已封存的 Qwen3-Embedding-4B baseline 运行目录",
    )
    parser.add_argument(
        "--comparator-package",
        type=Path,
        required=True,
        help="已封存的 sparse challenger 运行目录",
    )
    parser.add_argument(
        "--base-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-embedding-baseline.yaml"),
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-qwen-head-challenger.yaml"),
    )
    parser.add_argument(
        "--acceptance-policy",
        type=Path,
        default=Path("configs/cleaning-qwen-model-acceptance.yaml"),
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument(
        "--expected-existing-manifest-sha256",
        help="仅复用既有 run 时必填的外部冻结 manifest SHA-256",
    )
    parser.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    parser.add_argument(
        "--execute-training",
        action="store_true",
        help="显式确认仅在已封存训练 embedding 上执行 nested OOF",
    )
    return parser


def _git_version() -> str:
    """要求干净工作树并读取当前 HEAD 完整 SHA。"""

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        process = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise QwenHeadChallengerArtifactError(
            "qwen_head_git_version_unavailable"
        ) from exc
    if status.stdout.strip():
        raise QwenHeadChallengerArtifactError(
            "qwen_head_git_worktree_dirty"
        )
    return process.stdout.strip()


def _validate_artifact_root(artifact_root: Path) -> None:
    """仓库内 artifact 目标必须已由 Git ignore 规则保护。"""

    repository = Path(__file__).resolve().parents[1]
    artifact = artifact_root.expanduser().resolve()
    if repository == artifact or repository in artifact.parents:
        relative = artifact.relative_to(repository)
        process = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(relative)],
            cwd=repository,
            check=False,
        )
        if process.returncode != 0:
            raise QwenHeadChallengerArtifactError(
                "qwen_head_artifact_root_not_ignored"
            )


def main() -> int:
    """执行显式第一层训练并输出不含路径、正文或成员身份的摘要。"""

    parser = _parser()
    args = parser.parse_args()
    if not args.execute_training:
        parser.error("Qwen head challenger training requires --execute-training")
    try:
        _validate_artifact_root(args.artifact_root)
        result = train_qwen_head_challenger_package(
            args.source_package,
            args.comparator_package,
            args.base_plan,
            args.plan,
            args.acceptance_policy,
            args.artifact_root,
            code_version=_git_version(),
            expected_existing_manifest_sha256=(
                args.expected_existing_manifest_sha256
            ),
        )
        print(
            render_qwen_head_challenger_result(
                result, output_format=args.output_format
            )
        )
    except (
        FormalBaselineError,
        ModelAcceptanceError,
        QwenEmbeddingArtifactError,
        QwenEmbeddingConfigError,
        QwenHeadChallengerArtifactError,
        QwenHeadChallengerConfigError,
        QwenHeadChallengerError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", "qwen_head_training_failed")
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
