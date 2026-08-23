#!/usr/bin/env python3
"""训练并封存英文 instruction＋head-tail Qwen 第三层；不打开测试。"""

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
from tourism_ugc_study.models.text.qwen_embedding_artifacts import (
    QwenEmbeddingArtifactError,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingConfigError,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    QwenEmbeddingRuntimeError,
)
from tourism_ugc_study.models.text.qwen_head_challenger import (
    QwenHeadChallengerError,
)
from tourism_ugc_study.models.text.qwen_head_challenger_artifacts import (
    QwenHeadChallengerArtifactError,
)
from tourism_ugc_study.models.text.qwen_head_challenger_config import (
    QwenHeadChallengerConfigError,
)
from tourism_ugc_study.models.text.qwen_head_tail_artifacts import (
    QwenHeadTailArtifactError,
    render_qwen_head_tail_result,
    train_qwen_head_tail_package,
)
from tourism_ugc_study.models.text.qwen_head_tail_config import (
    QwenHeadTailConfigError,
)
from tourism_ugc_study.models.text.sparse_challenger_artifacts import (
    SparseChallengerArtifactError,
)
from tourism_ugc_study.models.text.sparse_challenger_config import (
    SparseChallengerConfigError,
)


def _parser() -> argparse.ArgumentParser:
    """构造不接收验证、测试、平台、阈值或审计参数的 CLI。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/cleaning.yaml"))
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument("--split-anchor-package", type=Path, required=True)
    parser.add_argument(
        "--sparse-plan",
        type=Path,
        default=Path("configs/cleaning-text-challenger.yaml"),
    )
    parser.add_argument("--sparse-package", type=Path, required=True)
    parser.add_argument("--layer2-package", type=Path, required=True)
    parser.add_argument(
        "--qwen-base-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-embedding-baseline.yaml"),
    )
    parser.add_argument(
        "--head-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-head-challenger.yaml"),
    )
    parser.add_argument(
        "--projection-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-english-head-tail.yaml"),
    )
    parser.add_argument(
        "--acceptance-policy",
        type=Path,
        default=Path("configs/cleaning-qwen-model-acceptance.yaml"),
    )
    parser.add_argument(
        "--model-dir", type=Path, default=Path("../models/Qwen3-Embedding-4B")
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
        help="显式确认重新编码训练442条并执行冻结56候选 nested OOF",
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
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_git_version_unavailable"
        ) from exc
    if status.stdout.strip():
        raise QwenHeadTailArtifactError("qwen_head_tail_git_worktree_dirty")
    return process.stdout.strip()


def _validate_storage_paths(model_dir: Path, artifact_root: Path) -> None:
    """要求权重位于仓库外，仓库内 artifact 目标必须已被 Git 忽略。"""

    repository = Path(__file__).resolve().parents[1]
    model = model_dir.expanduser().resolve()
    artifact = artifact_root.expanduser().resolve()
    if repository == model or repository in model.parents:
        raise QwenHeadTailArtifactError(
            "qwen_head_tail_model_directory_inside_repository"
        )
    if repository == artifact or repository in artifact.parents:
        relative = artifact.relative_to(repository)
        process = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(relative)],
            cwd=repository,
            check=False,
        )
        if process.returncode != 0:
            raise QwenHeadTailArtifactError(
                "qwen_head_tail_artifact_root_not_ignored"
            )


def main() -> int:
    """执行显式第三层训练并输出不含路径、正文或成员身份的摘要。"""

    parser = _parser()
    args = parser.parse_args()
    if not args.execute_training:
        parser.error("Qwen head-tail training requires --execute-training")
    try:
        config, normalization_config = load_cleaning_config_bundle(args.config)
        _validate_storage_paths(args.model_dir, args.artifact_root)
        result = train_qwen_head_tail_package(
            args.csv,
            args.manifest,
            args.derived_db,
            args.split_anchor_package,
            args.sparse_plan,
            args.sparse_package,
            args.layer2_package,
            args.qwen_base_plan,
            args.head_plan,
            args.projection_plan,
            args.acceptance_policy,
            args.model_dir,
            args.artifact_root,
            config=config,
            normalization_config=normalization_config,
            code_version=_git_version(),
            expected_existing_manifest_sha256=(
                args.expected_existing_manifest_sha256
            ),
        )
        print(render_qwen_head_tail_result(result, output_format=args.output_format))
    except (
        ConfigurationError,
        FormalBaselineError,
        ModelAcceptanceError,
        QwenEmbeddingArtifactError,
        QwenEmbeddingConfigError,
        QwenEmbeddingRuntimeError,
        QwenHeadChallengerArtifactError,
        QwenHeadChallengerConfigError,
        QwenHeadChallengerError,
        QwenHeadTailArtifactError,
        QwenHeadTailConfigError,
        ReferenceEvidenceError,
        SparseChallengerArtifactError,
        SparseChallengerConfigError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", "qwen_head_tail_training_failed")
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
