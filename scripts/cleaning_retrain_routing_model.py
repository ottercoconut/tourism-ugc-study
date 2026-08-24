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

from tourism_ugc_study.cleaning.config import (
    ConfigurationError,
    load_cleaning_config_bundle,
)
from tourism_ugc_study.cleaning.reference_projection import ReferenceProjectionError
from tourism_ugc_study.models.text.model_retraining_audit import (
    ModelRetrainingAuditError,
    assess_blind_audit_package,
    prepare_blind_audit_package,
    render_audit_assessment_result,
    render_audit_task_result,
)
from tourism_ugc_study.models.text.model_retraining_config import (
    ModelRetrainingConfigError,
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_embeddings import (
    ModelRetrainingEmbeddingError,
    encode_retraining_embeddings_package,
    render_retraining_embedding_result,
)
from tourism_ugc_study.models.text.model_retraining_inference import (
    ModelRetrainingInferenceError,
    render_inference_scoring_result,
    score_unlabeled_population_package,
)
from tourism_ugc_study.models.text.model_retraining_decisions import (
    ModelRetrainingDecisionError,
    build_final_decisions_package,
    render_final_decision_result,
)
from tourism_ugc_study.models.text.model_retraining_snapshot import (
    ModelRetrainingSnapshotError,
    build_retraining_snapshot,
)
from tourism_ugc_study.models.text.model_retraining_routing import (
    ModelRetrainingRoutingError,
)
from tourism_ugc_study.models.text.model_retraining_routing_artifacts import (
    ModelRetrainingRoutingArtifactError,
    freeze_retraining_routing_policy_package,
    load_retraining_routing_policy_package,
    render_retraining_routing_result,
)
from tourism_ugc_study.models.text.model_retraining_snapshot_artifacts import (
    ModelRetrainingSnapshotArtifactError,
    freeze_retraining_snapshot_package,
    render_retraining_snapshot_result,
)
from tourism_ugc_study.models.text.model_retraining_training import (
    ModelRetrainingTrainingError,
)
from tourism_ugc_study.models.text.model_retraining_training_artifacts import (
    ModelRetrainingTrainingArtifactError,
    render_model_retraining_result,
    train_retraining_candidates_package,
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
    training = subparsers.add_parser(
        "train-candidates", help="使用1,300条标签训练固定三候选并封存OOF"
    )
    training.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    training.add_argument("--snapshot-package", type=Path, required=True)
    training.add_argument(
        "--expected-snapshot-manifest-sha256", required=True
    )
    training.add_argument("--embedding-package", type=Path, required=True)
    training.add_argument(
        "--expected-embedding-manifest-sha256", required=True
    )
    training.add_argument("--artifact-root", type=Path, required=True)
    training.add_argument("--expected-existing-manifest-sha256")
    training.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    training.add_argument(
        "--execute-candidate-training",
        action="store_true",
        help="显式确认训练计划中唯一三个候选；不会读取历史测试证据",
    )
    routing = subparsers.add_parser(
        "freeze-routing", help="扫描Wave B OOF并冻结唯一模型与双阈值"
    )
    routing.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    routing.add_argument("--training-package", type=Path, required=True)
    routing.add_argument(
        "--expected-training-manifest-sha256", required=True
    )
    routing.add_argument("--artifact-root", type=Path, required=True)
    routing.add_argument("--expected-existing-manifest-sha256")
    routing.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    routing.add_argument(
        "--execute-routing-freeze",
        action="store_true",
        help="显式确认按预注册风险门选择；不会重开历史测试",
    )
    scoring = subparsers.add_parser(
        "score-population", help="纯预测覆盖全部尚未人工标注的候选人口"
    )
    scoring.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    scoring.add_argument(
        "--config", type=Path, default=Path("configs/cleaning.yaml")
    )
    scoring.add_argument(
        "--qwen-base-plan",
        type=Path,
        default=Path("configs/cleaning-qwen-embedding-baseline.yaml"),
    )
    scoring.add_argument("--derived-db", type=Path, required=True)
    scoring.add_argument("--snapshot-package", type=Path, required=True)
    scoring.add_argument(
        "--expected-snapshot-manifest-sha256", required=True
    )
    scoring.add_argument("--policy-package", type=Path, required=True)
    scoring.add_argument(
        "--expected-policy-manifest-sha256", required=True
    )
    scoring.add_argument(
        "--model-dir",
        type=Path,
        default=Path("../models/Qwen3-Embedding-4B"),
    )
    scoring.add_argument("--artifact-root", type=Path, required=True)
    scoring.add_argument("--expected-existing-manifest-sha256")
    scoring.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    scoring.add_argument(
        "--execute-prediction",
        action="store_true",
        help="显式确认只读重建人口并纯预测；fit调用必须为0",
    )
    audit_task = subparsers.add_parser(
        "prepare-audit", help="从两个临时自动尾部各冻结150条盲审任务"
    )
    audit_task.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    audit_task.add_argument("--inference-package", type=Path, required=True)
    audit_task.add_argument(
        "--expected-inference-manifest-sha256", required=True
    )
    audit_task.add_argument("--artifact-root", type=Path, required=True)
    audit_task.add_argument("--expected-existing-manifest-sha256")
    audit_task.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    audit_task.add_argument(
        "--execute-audit-sampling",
        action="store_true",
        help="显式确认一次性无放回抽样；后续禁止补抽",
    )
    audit_assessment = subparsers.add_parser(
        "assess-audit", help="导入300条完成表并独立判读两个自动尾部"
    )
    audit_assessment.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    audit_assessment.add_argument("--completed-csv", type=Path, required=True)
    audit_assessment.add_argument("--task-package", type=Path, required=True)
    audit_assessment.add_argument(
        "--expected-task-manifest-sha256", required=True
    )
    audit_assessment.add_argument("--artifact-root", type=Path, required=True)
    audit_assessment.add_argument("--expected-existing-manifest-sha256")
    audit_assessment.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    audit_assessment.add_argument(
        "--execute-audit-assessment",
        action="store_true",
        help="显式确认以固定3/7事件门判读；不得调阈值或补抽",
    )
    decisions = subparsers.add_parser(
        "build-decisions", help="合并人工覆盖和通过尾部，生成派生最终决定"
    )
    decisions.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-model-retraining.yaml"),
    )
    decisions.add_argument("--snapshot-package", type=Path, required=True)
    decisions.add_argument(
        "--expected-snapshot-manifest-sha256", required=True
    )
    decisions.add_argument("--inference-package", type=Path, required=True)
    decisions.add_argument(
        "--expected-inference-manifest-sha256", required=True
    )
    decisions.add_argument(
        "--audit-assessment-package", type=Path, required=True
    )
    decisions.add_argument(
        "--expected-audit-manifest-sha256", required=True
    )
    decisions.add_argument("--artifact-root", type=Path, required=True)
    decisions.add_argument("--expected-existing-manifest-sha256")
    decisions.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    decisions.add_argument(
        "--execute-final-decisions",
        action="store_true",
        help="显式确认只写派生决定；源数据库保持只读且不删除记录",
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


def _train_candidates(args: argparse.Namespace) -> str:
    """训练并封存固定三候选的分组OOF和最终模型。"""

    if not args.execute_candidate_training:
        raise ModelRetrainingTrainingArtifactError(
            "model_retraining_candidate_training_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    plan = load_model_retraining_plan(args.plan)
    result = train_retraining_candidates_package(
        args.snapshot_package,
        args.embedding_package,
        args.artifact_root,
        plan=plan,
        expected_snapshot_manifest_sha256=(
            args.expected_snapshot_manifest_sha256
        ),
        expected_embedding_manifest_sha256=(
            args.expected_embedding_manifest_sha256
        ),
        code_version=_git_version(),
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
    )
    return render_model_retraining_result(
        result, output_format=args.output_format
    )


def _freeze_routing(args: argparse.Namespace) -> str:
    """在固定Wave B OOF证据上冻结唯一模型和双阈值。"""

    if not args.execute_routing_freeze:
        raise ModelRetrainingRoutingArtifactError(
            "model_retraining_routing_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    plan = load_model_retraining_plan(args.plan)
    result = freeze_retraining_routing_policy_package(
        args.training_package,
        args.artifact_root,
        plan=plan,
        expected_training_manifest_sha256=(
            args.expected_training_manifest_sha256
        ),
        code_version=_git_version(),
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
    )
    return render_retraining_routing_result(
        result, output_format=args.output_format
    )


def _score_population(args: argparse.Namespace) -> str:
    """按唯一冻结模型对尚未标注人口执行checkpoint纯预测。"""

    if not args.execute_prediction:
        raise ModelRetrainingInferenceError(
            "model_retraining_inference_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    plan = load_model_retraining_plan(args.plan)
    _config, normalization_config = load_cleaning_config_bundle(args.config)
    selected_model, _manifest = load_retraining_routing_policy_package(
        args.policy_package,
        plan=plan,
        expected_manifest_sha256=args.expected_policy_manifest_sha256,
    )
    encoder = None
    if selected_model.candidate_name in {"qwen_linear_svc", "logit_fusion"}:
        _validate_model_directory(args.model_dir)
        base_plan = load_qwen_embedding_plan(args.qwen_base_plan)
        encoder = LocalQwenCompleteChunkEncoder(
            args.model_dir,
            base_plan=base_plan,
            retraining_plan=plan,
        )
    result = score_unlabeled_population_package(
        args.derived_db,
        args.snapshot_package,
        args.policy_package,
        args.artifact_root,
        plan=plan,
        normalization_config=normalization_config,
        expected_snapshot_manifest_sha256=(
            args.expected_snapshot_manifest_sha256
        ),
        expected_policy_manifest_sha256=args.expected_policy_manifest_sha256,
        code_version=_git_version(),
        encoder=encoder,
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
    )
    return render_inference_scoring_result(
        result, output_format=args.output_format
    )


def _prepare_audit(args: argparse.Namespace) -> str:
    """冻结一次150+150盲审抽样。"""

    if not args.execute_audit_sampling:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_sampling_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    plan = load_model_retraining_plan(args.plan)
    result = prepare_blind_audit_package(
        args.inference_package,
        args.artifact_root,
        plan=plan,
        expected_inference_manifest_sha256=(
            args.expected_inference_manifest_sha256
        ),
        code_version=_git_version(),
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
    )
    return render_audit_task_result(result, output_format=args.output_format)


def _assess_audit(args: argparse.Namespace) -> str:
    """导入完成表并按3/7硬门独立判读两个尾部。"""

    if not args.execute_audit_assessment:
        raise ModelRetrainingAuditError(
            "model_retraining_audit_assessment_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    plan = load_model_retraining_plan(args.plan)
    result = assess_blind_audit_package(
        args.completed_csv,
        args.task_package,
        args.artifact_root,
        plan=plan,
        expected_task_manifest_sha256=args.expected_task_manifest_sha256,
        code_version=_git_version(),
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
    )
    return render_audit_assessment_result(
        result, output_format=args.output_format
    )


def _build_decisions(args: argparse.Namespace) -> str:
    """按审计尾部状态生成全候选人口的派生最终决定。"""

    if not args.execute_final_decisions:
        raise ModelRetrainingDecisionError(
            "model_retraining_decision_confirmation_required"
        )
    _validate_artifact_root(args.artifact_root)
    plan = load_model_retraining_plan(args.plan)
    result = build_final_decisions_package(
        args.snapshot_package,
        args.inference_package,
        args.audit_assessment_package,
        args.artifact_root,
        plan=plan,
        expected_snapshot_manifest_sha256=(
            args.expected_snapshot_manifest_sha256
        ),
        expected_inference_manifest_sha256=(
            args.expected_inference_manifest_sha256
        ),
        expected_audit_manifest_sha256=args.expected_audit_manifest_sha256,
        code_version=_git_version(),
        expected_existing_manifest_sha256=(
            args.expected_existing_manifest_sha256
        ),
    )
    return render_final_decision_result(
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
        elif args.command == "train-candidates":
            print(_train_candidates(args))
        elif args.command == "freeze-routing":
            print(_freeze_routing(args))
        elif args.command == "score-population":
            print(_score_population(args))
        elif args.command == "prepare-audit":
            print(_prepare_audit(args))
        elif args.command == "assess-audit":
            print(_assess_audit(args))
        elif args.command == "build-decisions":
            print(_build_decisions(args))
        else:  # pragma: no cover - argparse保证不会到达
            parser.error("unknown command")
    except (
        ConfigurationError,
        ModelRetrainingAuditError,
        ModelRetrainingConfigError,
        ModelRetrainingDecisionError,
        ModelRetrainingEmbeddingError,
        ModelRetrainingInferenceError,
        ModelRetrainingRoutingArtifactError,
        ModelRetrainingRoutingError,
        ModelRetrainingSnapshotError,
        ModelRetrainingSnapshotArtifactError,
        ModelRetrainingTrainingArtifactError,
        ModelRetrainingTrainingError,
        QwenEmbeddingConfigError,
        QwenEmbeddingRuntimeError,
        ReferenceProjectionError,
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
