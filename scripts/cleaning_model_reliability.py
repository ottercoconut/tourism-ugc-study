#!/usr/bin/env python3
"""为两个既有冻结模型生成新盲标成对评价证据；不训练。"""

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
from tourism_ugc_study.cleaning.reference_projection import ReferenceProjectionError
from tourism_ugc_study.models.text.model_reliability_artifacts import (
    ModelReliabilityArtifactError,
    prepare_wave_a_package,
    render_model_reliability_result,
    score_model_reliability_frame,
)
from tourism_ugc_study.models.text.model_reliability_config import (
    ModelReliabilityConfigError,
)
from tourism_ugc_study.models.text.model_reliability_study import (
    ModelReliabilityStudyError,
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
from tourism_ugc_study.models.text.qwen_head_tail_artifacts import (
    QwenHeadTailArtifactError,
)
from tourism_ugc_study.models.text.qwen_head_tail_config import (
    QwenHeadTailConfigError,
)
from tourism_ugc_study.models.text.sparse_challenger import SparseChallengerError
from tourism_ugc_study.models.text.sparse_challenger_artifacts import (
    SparseChallengerArtifactError,
)


def _common(parser: argparse.ArgumentParser) -> None:
    """添加两个阶段共享且不包含阈值、测试或平台的参数。"""

    parser.add_argument("--config", type=Path, default=Path("configs/cleaning.yaml"))
    parser.add_argument(
        "--study-plan",
        type=Path,
        default=Path("configs/cleaning-model-reliability-study.yaml"),
    )
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output-format", choices=("human", "json"), default="human")


def _parser() -> argparse.ArgumentParser:
    """构造纯预测人口框与 Wave A 两个显式子命令。"""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    score = subparsers.add_parser("score-frame", help="对10103条合格人口纯预测")
    _common(score)
    score.add_argument("--sparse-package", type=Path, required=True)
    score.add_argument("--qwen-package", type=Path, required=True)
    score.add_argument(
        "--qwen-base-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-embedding-baseline.yaml"),
    )
    score.add_argument(
        "--qwen-projection-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-english-head-tail.yaml"),
    )
    score.add_argument(
        "--model-dir", type=Path, default=Path("../models/Qwen3-Embedding-4B")
    )
    score.add_argument("--expected-existing-manifest-sha256")
    score.add_argument(
        "--execute-prediction",
        action="store_true",
        help="显式确认对合格人口执行两个冻结模型的纯预测",
    )
    wave = subparsers.add_parser("prepare-wave-a", help="生成240条盲标任务")
    _common(wave)
    wave.add_argument("--scored-package", type=Path, required=True)
    wave.add_argument("--expected-scored-manifest-sha256", required=True)
    wave.add_argument("--expected-existing-manifest-sha256")
    return parser


def _git_version() -> str:
    """要求干净工作树并读取当前完整 Git SHA。"""

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], check=True, capture_output=True, text=True
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ModelReliabilityArtifactError(
            "model_reliability_git_version_unavailable"
        ) from exc
    if status.stdout.strip():
        raise ModelReliabilityArtifactError("model_reliability_git_worktree_dirty")
    return head.stdout.strip()


def main() -> int:
    """执行用户明确选择的评价阶段，并只输出去敏摘要。"""

    parser = _parser()
    args = parser.parse_args()
    try:
        config, normalization = load_cleaning_config_bundle(args.config)
        if args.command == "score-frame":
            if not args.execute_prediction:
                parser.error("score-frame requires --execute-prediction")
            result = score_model_reliability_frame(
                args.csv,
                args.derived_db,
                args.sparse_package,
                args.qwen_package,
                args.qwen_base_plan,
                args.qwen_projection_plan,
                args.model_dir,
                args.study_plan,
                args.artifact_root,
                config=config,
                normalization_config=normalization,
                code_version=_git_version(),
                expected_existing_manifest_sha256=args.expected_existing_manifest_sha256,
            )
        else:
            result = prepare_wave_a_package(
                args.csv,
                args.derived_db,
                args.scored_package,
                args.study_plan,
                args.artifact_root,
                normalization_config=normalization,
                expected_scored_manifest_sha256=args.expected_scored_manifest_sha256,
                expected_existing_manifest_sha256=args.expected_existing_manifest_sha256,
            )
        print(render_model_reliability_result(result, output_format=args.output_format))
    except (
        ConfigurationError,
        ModelReliabilityArtifactError,
        ModelReliabilityConfigError,
        ModelReliabilityStudyError,
        QwenEmbeddingConfigError,
        QwenEmbeddingRuntimeError,
        QwenHeadChallengerError,
        QwenHeadTailArtifactError,
        QwenHeadTailConfigError,
        ReferenceProjectionError,
        SparseChallengerArtifactError,
        SparseChallengerError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", "model_reliability_failed")
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
