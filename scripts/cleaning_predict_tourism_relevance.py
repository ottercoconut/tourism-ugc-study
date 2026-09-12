#!/usr/bin/env python3
"""使用唯一冻结融合模型读取CSV并输出旅游相关性三段判断。"""

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
from tourism_ugc_study.models.text.model_retraining_config import (
    ModelRetrainingConfigError,
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_delivery_config import (
    ModelRetrainingDeliveryConfigError,
    load_model_retraining_delivery_plan,
)
from tourism_ugc_study.models.text.model_retraining_incremental import (
    ModelRetrainingIncrementalError,
    render_incremental_inference_result,
    score_incremental_batch_package,
)
from tourism_ugc_study.models.text.model_retraining_routing_artifacts import (
    ModelRetrainingRoutingArtifactError,
)
from tourism_ugc_study.models.text.model_retraining_snapshot_artifacts import (
    ModelRetrainingSnapshotArtifactError,
)
from tourism_ugc_study.models.text.qwen_complete_chunk_runtime import (
    LocalQwenCompleteChunkEncoder,
)
from tourism_ugc_study.models.text.qwen_embedding_cache import (
    QwenEmbeddingCache,
    QwenEmbeddingCacheError,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingConfigError,
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    QwenEmbeddingRuntimeError,
    validate_qwen_model_directory,
)
from tourism_ugc_study.models.text.qwen_inference_execution import (
    QwenInferenceExecutionError,
    load_inference_execution,
)


DEFAULT_SNAPSHOT_PACKAGE = Path(
    "results/cleaning-model-retraining-snapshot/6f1075ec9b70ca1485208762cb7a6453"
)
DEFAULT_SNAPSHOT_MANIFEST_SHA256 = (
    "bd8142bfe5340895c23c837063bcad6c21a7fa0506ccd914f74f3370562048fa"
)
DEFAULT_POLICY_PACKAGE = Path(
    "results/cleaning-model-retraining-delivery-policy/"
    "8c87b85cd44803b9451c825bacaa3e90"
)
DEFAULT_POLICY_MANIFEST_SHA256 = (
    "5874882125b521271f99b990f7b7b5be38fcf4426aefb4fd1e1e1cb07d07667b"
)


def _parser() -> argparse.ArgumentParser:
    """构造唯一生产推理入口的参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-csv",
        type=Path,
        required=True,
        help=(
            "严格六列CSV：source_post_id,source_version,component_id,"
            "title,body,source_status"
        ),
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    parser.add_argument(
        "--delivery-plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining-delivery.yaml"),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("configs/cleaning.yaml")
    )
    parser.add_argument(
        "--qwen-base-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-embedding-baseline.yaml"),
    )
    parser.add_argument(
        "--snapshot-package", type=Path, default=DEFAULT_SNAPSHOT_PACKAGE
    )
    parser.add_argument(
        "--expected-snapshot-manifest-sha256",
        default=DEFAULT_SNAPSHOT_MANIFEST_SHA256,
    )
    parser.add_argument(
        "--policy-package", type=Path, default=DEFAULT_POLICY_PACKAGE
    )
    parser.add_argument(
        "--expected-policy-manifest-sha256",
        default=DEFAULT_POLICY_MANIFEST_SHA256,
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("../models/Qwen3-Embedding-4B"),
    )
    parser.add_argument(
        "--vector-cache-root",
        type=Path,
        default=Path("results/cleaning-model-inference-vector-cache"),
        help="跨批次按规范正文SHA复用Qwen向量的Git忽略目录",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("results/cleaning-model-inference"),
    )
    parser.add_argument("--expected-existing-manifest-sha256")
    parser.add_argument("--inference-execution-config", type=Path)
    parser.add_argument("--expected-inference-execution-sha256")
    parser.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    parser.add_argument(
        "--execute-prediction",
        action="store_true",
        help="显式确认只做冻结模型预测；不会训练或写入源数据库",
    )
    return parser


def _git_version() -> str:
    """要求已跟踪代码无修改并返回当前完整Git提交身份。

    未跟踪的私有CSV或用户文档不影响代码身份；任何已跟踪文件修改仍会失败关闭。
    """

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        )
        version = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ModelRetrainingIncrementalError(
            "model_inference_git_version_unavailable"
        ) from exc
    if status.stdout.strip():
        raise ModelRetrainingIncrementalError("model_inference_git_worktree_dirty")
    return version.stdout.strip()


def _validate_ignored_root(path: Path) -> None:
    """仓库内私有输出或缓存必须被Git忽略。"""

    repository = Path(__file__).resolve().parents[1]
    resolved = path.expanduser().resolve()
    if repository == resolved or repository in resolved.parents:
        try:
            relative = resolved.relative_to(repository)
            process = subprocess.run(
                ["git", "check-ignore", "-q", "--", str(relative)],
                cwd=repository,
                check=False,
            )
        except (OSError, ValueError) as exc:
            raise ModelRetrainingIncrementalError(
                "model_inference_private_root_check_failed"
            ) from exc
        if process.returncode != 0:
            raise ModelRetrainingIncrementalError(
                "model_inference_private_root_not_ignored"
            )


def _validate_external_model_directory(path: Path) -> None:
    """要求公开权重位于仓库外，防止模型权重误入Git。"""

    repository = Path(__file__).resolve().parents[1]
    resolved = path.expanduser().resolve()
    if repository == resolved or repository in resolved.parents:
        raise ModelRetrainingIncrementalError(
            "model_inference_model_directory_inside_repository"
        )


def main() -> int:
    """校验唯一冻结依赖并执行CSV纯预测。"""

    args = _parser().parse_args()
    try:
        if not args.execute_prediction:
            raise ModelRetrainingIncrementalError(
                "model_retraining_incremental_confirmation_required"
            )
        _validate_ignored_root(args.artifact_root)
        _validate_ignored_root(args.vector_cache_root)
        _validate_external_model_directory(args.model_dir)
        code_version = _git_version()
        if bool(args.inference_execution_config) != bool(args.expected_inference_execution_sha256):
            raise QwenInferenceExecutionError("qwen_execution_config_binding_required")
        execution = (load_inference_execution(args.inference_execution_config,
                     args.expected_inference_execution_sha256)
                     if args.inference_execution_config else None)
        plan = load_model_retraining_plan(args.plan)
        delivery_plan = load_model_retraining_delivery_plan(args.delivery_plan)
        _cleaning, normalization_config = load_cleaning_config_bundle(args.config)
        qwen_plan = load_qwen_embedding_plan(args.qwen_base_plan)
        model_snapshot = validate_qwen_model_directory(
            args.model_dir, plan=qwen_plan, reused=True
        )
        encoder = LocalQwenCompleteChunkEncoder(
            args.model_dir,
            base_plan=qwen_plan,
            retraining_plan=plan,
            inference_execution=execution,
        )
        embedding_cache = QwenEmbeddingCache(
            args.vector_cache_root,
            plan=plan,
            model_snapshot=model_snapshot,
            encoder=encoder,
        )
        result = score_incremental_batch_package(
            args.input_csv,
            args.snapshot_package,
            args.policy_package,
            args.artifact_root,
            plan=plan,
            delivery_plan=delivery_plan,
            normalization_config=normalization_config,
            expected_snapshot_manifest_sha256=(
                args.expected_snapshot_manifest_sha256
            ),
            expected_policy_manifest_sha256=(
                args.expected_policy_manifest_sha256
            ),
            code_version=code_version,
            embedding_cache=embedding_cache,
            expected_existing_manifest_sha256=(
                args.expected_existing_manifest_sha256
            ),
        )
        print(
            render_incremental_inference_result(
                result, output_format=args.output_format
            )
        )
    except (
        ConfigurationError,
        ModelRetrainingConfigError,
        ModelRetrainingDeliveryConfigError,
        ModelRetrainingIncrementalError,
        ModelRetrainingRoutingArtifactError,
        ModelRetrainingSnapshotArtifactError,
        QwenEmbeddingCacheError,
        QwenEmbeddingConfigError,
        QwenEmbeddingRuntimeError,
        QwenInferenceExecutionError,
    ) as exc:
        print(
            json.dumps(
                {"status": "failed", "reason_code": exc.reason_code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
