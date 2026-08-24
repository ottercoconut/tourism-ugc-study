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
from tourism_ugc_study.models.text.model_retraining_snapshot import (
    ModelRetrainingSnapshotError,
    build_retraining_snapshot,
)
from tourism_ugc_study.models.text.model_retraining_snapshot_artifacts import (
    ModelRetrainingSnapshotArtifactError,
    freeze_retraining_snapshot_package,
    render_retraining_snapshot_result,
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


def main() -> int:
    """分派子命令并只向终端暴露稳定失败码。"""

    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "freeze-snapshot":
            print(_freeze_snapshot(args))
        else:  # pragma: no cover - argparse保证不会到达
            parser.error("unknown command")
    except (
        ModelRetrainingConfigError,
        ModelRetrainingSnapshotError,
        ModelRetrainingSnapshotArtifactError,
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
