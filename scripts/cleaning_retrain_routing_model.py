#!/usr/bin/env python3
"""重训1,300条标签模型、冻结路由并执行前瞻性双尾审计。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.models.text.model_retraining_config import (
    ModelRetrainingConfigError,
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_embeddings import (
    ModelRetrainingEmbeddingError,
    encode_retraining_embeddings_package,
    render_retraining_embedding_result,
)
from tourism_ugc_study.models.text.model_retraining_snapshot import (
    ModelRetrainingSnapshotError,
    build_retraining_snapshot,
)
from tourism_ugc_study.models.text.model_retraining_snapshot_artifacts import (
    ModelRetrainingSnapshotArtifactError,
    freeze_retraining_snapshot_package,
    render_retraining_snapshot_result,
)
from tourism_ugc_study.models.text.qwen_complete_chunk_runtime import (
    LocalQwenCompleteChunkEncoder,
)
from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingConfigError,
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    QwenEmbeddingRuntimeError,
    validate_qwen_model_directory,
)


def _parser() -> argparse.ArgumentParser:
    """构造按阶段显式授权、默认失败关闭的CLI。"""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser(
        "freeze-snapshot", help="只读合并并封存1,300条训练成员"
    )
    snapshot.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    snapshot.add_argument("--final-reference-csv", type=Path, required=True)
    snapshot.add_argument("--wave-a-completed-csv", type=Path, required=True)
    snapshot.add_argument("--wave-a-private-map", type=Path, required=True)
    snapshot.add_argument("--wave-b-completed-csv", type=Path, required=True)
    snapshot.add_argument("--wave-b-private-map", type=Path, required=True)
    snapshot.add_argument("--derived-db", type=Path, required=True)
    snapshot.add_argument("--split-manifest", type=Path, required=True)
    snapshot.add_argument("--artifact-root", type=Path, required=True)
    snapshot.add_argument("--expected-existing-manifest-sha256")
    snapshot.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    snapshot.add_argument(
        "--execute-snapshot-freeze",
        action="store_true",
        help="显式确认只读校验并封存1,300条私有训练快照",
    )
    encoding = subparsers.add_parser(
        "encode-qwen", help="完整连续分块编码1,300条，支持checkpoint续跑"
    )
    encoding.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    encoding.add_argument(
        "--qwen-base-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-embedding-baseline.yaml"),
    )
    encoding.add_argument("--snapshot-package", type=Path, required=True)
    encoding.add_argument(
        "--expected-snapshot-manifest-sha256", required=True
    )
    encoding.add_argument(
        "--model-dir",
        type=Path,
        default=Path("../models/Qwen3-Embedding-4B"),
    )
    encoding.add_argument("--artifact-root", type=Path, required=True)
    encoding.add_argument("--expected-existing-manifest-sha256")
    encoding.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    encoding.add_argument(
        "--no-progress", action="store_true", help="关闭逐记录视图进度显示"
    )
    encoding.add_argument(
        "--execute-qwen-encoding",
        action="store_true",
        help="显式确认只读取正文并运行完整Qwen编码；不会fit",
    )
    return parser


def _git_version() -> str:
    """要求干净工作树并返回当前完整Git提交身份。"""

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
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
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_git_version_unavailable"
        ) from exc
    if status.stdout.strip():
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_git_worktree_dirty"
        )
    return version.stdout.strip()


def _validate_artifact_root(artifact_root: Path) -> None:
    """仓库内运行包必须被Git忽略，防止私有UGC意外提交。"""

    repository = Path(__file__).resolve().parents[1]
    artifact = artifact_root.expanduser().resolve()
    if repository == artifact or repository in artifact.parents:
        try:
            relative = artifact.relative_to(repository)
            process = subprocess.run(
                ["git", "check-ignore", "-q", "--", str(relative)],
                cwd=repository,
                check=False,
            )
        except (OSError, ValueError) as exc:
            raise ModelRetrainingSnapshotArtifactError(
                "model_retraining_artifact_root_check_failed"
            ) from exc
        if process.returncode != 0:
            raise ModelRetrainingSnapshotArtifactError(
                "model_retraining_artifact_root_not_ignored"
            )


def _validate_model_directory(model_dir: Path) -> None:
    """公开权重目录必须位于仓库外，避免权重进入Git工作树。"""

    repository = Path(__file__).resolve().parents[1]
    model = model_dir.expanduser().resolve()
    if repository == model or repository in model.parents:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_model_directory_inside_repository"
        )


def _freeze_snapshot(args: argparse.Namespace) -> str:
    """执行训练快照的只读构建与内容寻址封存。"""

    if not args.execute_snapshot_freeze:
        raise ModelRetrainingSnapshotArtifactError(
            "model_retraining_snapshot_explicit_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    plan = load_model_retraining_plan(args.plan)
    snapshot = build_retraining_snapshot(
        args.final_reference_csv,
        args.wave_a_completed_csv,
        args.wave_a_private_map,
        args.wave_b_completed_csv,
        args.wave_b_private_map,
        args.derived_db,
        args.split_manifest,
        plan=plan,
    )
    result = freeze_retraining_snapshot_package(
        snapshot,
        args.artifact_root,
        plan=plan,
        code_version=_git_version(),
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
    )
    return render_retraining_snapshot_result(
        result, output_format=args.output_format
    )


def _encode_qwen(args: argparse.Namespace) -> str:
    """运行可恢复的完整token编码并封存向量矩阵。"""

    if not args.execute_qwen_encoding:
        raise ModelRetrainingEmbeddingError(
            "model_retraining_qwen_encoding_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    _validate_model_directory(args.model_dir)
    plan = load_model_retraining_plan(args.plan)
    base_plan = load_qwen_embedding_plan(args.qwen_base_plan)
    model_snapshot = validate_qwen_model_directory(
        args.model_dir, plan=base_plan
    )
    encoder = LocalQwenCompleteChunkEncoder(
        args.model_dir,
        base_plan=base_plan,
        retraining_plan=plan,
    )
    result = encode_retraining_embeddings_package(
        args.snapshot_package,
        args.artifact_root,
        plan=plan,
        base_plan=base_plan,
        encoder=encoder,
        model_snapshot=model_snapshot,
        expected_snapshot_manifest_sha256=(
            args.expected_snapshot_manifest_sha256
        ),
        code_version=_git_version(),
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
        show_progress=not args.no_progress,
    )
    return render_retraining_embedding_result(
        result, output_format=args.output_format
    )


def main() -> int:
    """分派子命令并只向终端暴露稳定失败码。"""

    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "freeze-snapshot":
            print(_freeze_snapshot(args))
        elif args.command == "encode-qwen":
            print(_encode_qwen(args))
        else:  # pragma: no cover - argparse保证不会到达
            parser.error("unknown command")
    except (
        ModelRetrainingConfigError,
        ModelRetrainingEmbeddingError,
        ModelRetrainingSnapshotError,
        ModelRetrainingSnapshotArtifactError,
        QwenEmbeddingConfigError,
        QwenEmbeddingRuntimeError,
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
