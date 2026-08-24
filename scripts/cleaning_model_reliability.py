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
    evaluate_wave_a_package,
    prepare_wave_a_package,
    render_model_reliability_result,
    score_model_reliability_frame,
)
from tourism_ugc_study.models.text.model_reliability_config import (
    ModelReliabilityConfigError,
)
from tourism_ugc_study.models.text.model_reliability_evaluation import (
    ModelReliabilityEvaluationError,
)
from tourism_ugc_study.models.text.model_reliability_study import (
    ModelReliabilityStudyError,
)
from tourism_ugc_study.models.text.model_deployment_acceptance_artifacts import (
    ModelDeploymentAcceptanceArtifactError,
    freeze_deployment_acceptance_package,
    render_deployment_acceptance_freeze_result,
)
from tourism_ugc_study.models.text.model_deployment_acceptance_config import (
    ModelDeploymentAcceptanceConfigError,
)
from tourism_ugc_study.models.text.model_routing_selection import (
    ModelRoutingSelectionError,
)
from tourism_ugc_study.models.text.model_routing_selection_artifacts import (
    ModelRoutingSelectionArtifactError,
    analyze_routing_selection_package,
    render_routing_selection_result,
)
from tourism_ugc_study.models.text.model_routing_selection_config import (
    ModelRoutingSelectionConfigError,
)
from tourism_ugc_study.models.text.model_routing_policy_config import (
    ModelRoutingPolicyConfigError,
)
from tourism_ugc_study.models.text.model_wave_b_artifacts import (
    ModelWaveBArtifactError,
    freeze_routing_policy_package,
    prepare_wave_b_package,
    render_wave_b_result,
)
from tourism_ugc_study.models.text.model_wave_b_evaluation import (
    ModelWaveBEvaluationError,
)
from tourism_ugc_study.models.text.model_wave_b_evaluation_artifacts import (
    ModelWaveBEvaluationArtifactError,
    evaluate_wave_b_package,
    render_wave_b_evaluation_result,
)
from tourism_ugc_study.models.text.model_wave_b_study import ModelWaveBStudyError
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
    evaluate = subparsers.add_parser(
        "evaluate-wave-a", help="封存初标并执行探索性设计加权评价"
    )
    evaluate.add_argument(
        "--study-plan",
        type=Path,
        default=Path("configs/cleaning-model-reliability-study.yaml"),
    )
    evaluate.add_argument("--completed-csv", type=Path, required=True)
    evaluate.add_argument("--wave-a-package", type=Path, required=True)
    evaluate.add_argument("--scored-package", type=Path, required=True)
    evaluate.add_argument("--artifact-root", type=Path, required=True)
    evaluate.add_argument("--expected-wave-manifest-sha256", required=True)
    evaluate.add_argument("--expected-scored-manifest-sha256", required=True)
    evaluate.add_argument("--expected-existing-manifest-sha256")
    evaluate.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    selection = subparsers.add_parser(
        "analyze-routing-grid", help="分析Wave A三段式双阈值风险与人工量"
    )
    selection.add_argument(
        "--selection-plan",
        type=Path,
        default=Path("configs/cleaning-model-routing-selection.yaml"),
    )
    selection.add_argument("--scored-package", type=Path, required=True)
    selection.add_argument("--base-evaluation-package", type=Path, required=True)
    selection.add_argument("--artifact-root", type=Path, required=True)
    selection.add_argument("--expected-existing-manifest-sha256")
    selection.add_argument(
        "--execute-selection-analysis",
        action="store_true",
        help="显式确认只复用封存概率和标签执行选择分析",
    )
    selection.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    policy = subparsers.add_parser(
        "freeze-routing-policy", help="封存研究者确认的Wave B评价策略"
    )
    policy.add_argument(
        "--study-plan",
        type=Path,
        default=Path("configs/cleaning-model-reliability-study.yaml"),
    )
    policy.add_argument(
        "--policy-plan",
        type=Path,
        default=Path("configs/cleaning-model-routing-policy.yaml"),
    )
    policy.add_argument("--scored-package", type=Path, required=True)
    policy.add_argument("--wave-a-package", type=Path, required=True)
    policy.add_argument("--selection-package", type=Path, required=True)
    policy.add_argument("--artifact-root", type=Path, required=True)
    policy.add_argument("--expected-existing-manifest-sha256")
    policy.add_argument(
        "--execute-policy-freeze",
        action="store_true",
        help="显式确认只冻结研究者已选择的策略，不授权部署",
    )
    policy.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    wave_b = subparsers.add_parser(
        "prepare-wave-b", help="按冻结策略生成360条Wave B人工任务"
    )
    _common(wave_b)
    wave_b.add_argument(
        "--policy-plan",
        type=Path,
        default=Path("configs/cleaning-model-routing-policy.yaml"),
    )
    wave_b.add_argument("--scored-package", type=Path, required=True)
    wave_b.add_argument("--wave-a-package", type=Path, required=True)
    wave_b.add_argument("--policy-package", type=Path, required=True)
    wave_b.add_argument("--expected-policy-manifest-sha256", required=True)
    wave_b.add_argument("--expected-existing-manifest-sha256")
    wave_b.add_argument(
        "--execute-wave-b-sampling",
        action="store_true",
        help="显式确认生成不含模型答案的Wave B概率样本",
    )
    wave_b_evaluation = subparsers.add_parser(
        "evaluate-wave-b", help="收口完成表并独立评价冻结策略"
    )
    wave_b_evaluation.add_argument(
        "--study-plan",
        type=Path,
        default=Path("configs/cleaning-model-reliability-study.yaml"),
    )
    wave_b_evaluation.add_argument(
        "--policy-plan",
        type=Path,
        default=Path("configs/cleaning-model-routing-policy.yaml"),
    )
    wave_b_evaluation.add_argument(
        "--evidence-plan",
        type=Path,
        default=Path("configs/cleaning-model-routing-selection.yaml"),
    )
    wave_b_evaluation.add_argument("--completed-source", type=Path, required=True)
    wave_b_evaluation.add_argument("--completed-output", type=Path, required=True)
    wave_b_evaluation.add_argument("--wave-b-package", type=Path, required=True)
    wave_b_evaluation.add_argument("--policy-package", type=Path, required=True)
    wave_b_evaluation.add_argument("--scored-package", type=Path, required=True)
    wave_b_evaluation.add_argument("--wave-a-package", type=Path, required=True)
    wave_b_evaluation.add_argument("--artifact-root", type=Path, required=True)
    wave_b_evaluation.add_argument("--expected-wave-id", required=True)
    wave_b_evaluation.add_argument(
        "--expected-wave-manifest-sha256", required=True
    )
    wave_b_evaluation.add_argument(
        "--expected-policy-manifest-sha256", required=True
    )
    wave_b_evaluation.add_argument("--expected-existing-completed-sha256")
    wave_b_evaluation.add_argument("--expected-existing-manifest-sha256")
    wave_b_evaluation.add_argument(
        "--execute-wave-b-evaluation",
        action="store_true",
        help="显式确认保全完成表、恢复模板并评价既定策略",
    )
    wave_b_evaluation.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
    acceptance = subparsers.add_parser(
        "freeze-deployment-acceptance",
        help="在锁定测试前封存最终判读与双尾审计计划",
    )
    acceptance.add_argument(
        "--acceptance-plan",
        type=Path,
        default=Path("configs/cleaning-model-deployment-acceptance.yaml"),
    )
    acceptance.add_argument("--policy-package", type=Path, required=True)
    acceptance.add_argument(
        "--wave-b-evaluation-package", type=Path, required=True
    )
    acceptance.add_argument("--wave-b-completed-csv", type=Path, required=True)
    acceptance.add_argument("--qwen-package", type=Path, required=True)
    acceptance.add_argument("--split-anchor-package", type=Path, required=True)
    acceptance.add_argument("--artifact-root", type=Path, required=True)
    acceptance.add_argument("--expected-existing-manifest-sha256")
    acceptance.add_argument(
        "--execute-acceptance-freeze",
        action="store_true",
        help="显式确认只冻结规则，仍不读取锁定测试",
    )
    acceptance.add_argument(
        "--output-format", choices=("human", "json"), default="human"
    )
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
        if args.command == "evaluate-wave-a":
            result = evaluate_wave_a_package(
                args.completed_csv,
                args.wave_a_package,
                args.scored_package,
                args.study_plan,
                args.artifact_root,
                expected_wave_manifest_sha256=args.expected_wave_manifest_sha256,
                expected_scored_manifest_sha256=args.expected_scored_manifest_sha256,
                expected_existing_manifest_sha256=args.expected_existing_manifest_sha256,
            )
        elif args.command == "analyze-routing-grid":
            if not args.execute_selection_analysis:
                parser.error(
                    "analyze-routing-grid requires --execute-selection-analysis"
                )
            result = analyze_routing_selection_package(
                args.scored_package,
                args.base_evaluation_package,
                args.selection_plan,
                args.artifact_root,
                code_version=_git_version(),
                expected_existing_manifest_sha256=args.expected_existing_manifest_sha256,
            )
        elif args.command == "freeze-routing-policy":
            if not args.execute_policy_freeze:
                parser.error("freeze-routing-policy requires --execute-policy-freeze")
            result = freeze_routing_policy_package(
                args.scored_package,
                args.wave_a_package,
                args.selection_package,
                args.study_plan,
                args.policy_plan,
                args.artifact_root,
                code_version=_git_version(),
                expected_existing_manifest_sha256=args.expected_existing_manifest_sha256,
            )
        elif args.command == "evaluate-wave-b":
            if not args.execute_wave_b_evaluation:
                parser.error("evaluate-wave-b requires --execute-wave-b-evaluation")
            result = evaluate_wave_b_package(
                args.completed_source,
                args.completed_output,
                args.wave_b_package,
                args.policy_package,
                args.scored_package,
                args.wave_a_package,
                args.study_plan,
                args.policy_plan,
                args.evidence_plan,
                args.artifact_root,
                code_version=_git_version(),
                expected_wave_id=args.expected_wave_id,
                expected_wave_manifest_sha256=args.expected_wave_manifest_sha256,
                expected_policy_manifest_sha256=args.expected_policy_manifest_sha256,
                expected_existing_completed_sha256=args.expected_existing_completed_sha256,
                expected_existing_manifest_sha256=args.expected_existing_manifest_sha256,
            )
        elif args.command == "freeze-deployment-acceptance":
            if not args.execute_acceptance_freeze:
                parser.error(
                    "freeze-deployment-acceptance requires "
                    "--execute-acceptance-freeze"
                )
            result = freeze_deployment_acceptance_package(
                args.acceptance_plan,
                args.policy_package,
                args.wave_b_evaluation_package,
                args.wave_b_completed_csv,
                args.qwen_package,
                args.split_anchor_package,
                args.artifact_root,
                code_version=_git_version(),
                expected_existing_manifest_sha256=(
                    args.expected_existing_manifest_sha256
                ),
            )
        else:
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
        elif args.command == "prepare-wave-a":
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
        elif args.command == "prepare-wave-b":
            if not args.execute_wave_b_sampling:
                parser.error("prepare-wave-b requires --execute-wave-b-sampling")
            result = prepare_wave_b_package(
                args.csv,
                args.derived_db,
                args.scored_package,
                args.wave_a_package,
                args.policy_package,
                args.study_plan,
                args.policy_plan,
                args.artifact_root,
                normalization_config=normalization,
                code_version=_git_version(),
                expected_policy_manifest_sha256=args.expected_policy_manifest_sha256,
                expected_existing_manifest_sha256=args.expected_existing_manifest_sha256,
            )
        if args.command == "evaluate-wave-b":
            print(
                render_wave_b_evaluation_result(
                    result, output_format=args.output_format
                )
            )
        elif args.command == "freeze-deployment-acceptance":
            print(
                render_deployment_acceptance_freeze_result(
                    result, output_format=args.output_format
                )
            )
        elif args.command in {"freeze-routing-policy", "prepare-wave-b"}:
            print(render_wave_b_result(result, output_format=args.output_format))
        elif args.command == "analyze-routing-grid":
            print(render_routing_selection_result(result, output_format=args.output_format))
        else:
            print(render_model_reliability_result(result, output_format=args.output_format))
    except (
        ConfigurationError,
        ModelDeploymentAcceptanceArtifactError,
        ModelDeploymentAcceptanceConfigError,
        ModelReliabilityArtifactError,
        ModelReliabilityConfigError,
        ModelReliabilityEvaluationError,
        ModelReliabilityStudyError,
        ModelRoutingSelectionArtifactError,
        ModelRoutingSelectionConfigError,
        ModelRoutingSelectionError,
        ModelRoutingPolicyConfigError,
        ModelWaveBArtifactError,
        ModelWaveBEvaluationArtifactError,
        ModelWaveBEvaluationError,
        ModelWaveBStudyError,
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
