"""三段式选择分析 artifact 与可读摘要测试。"""

from dataclasses import replace
from pathlib import Path

import tourism_ugc_study.models.text.model_routing_selection_artifacts as artifacts
from tourism_ugc_study.models.text.model_reliability_evaluation import (
    LabeledWaveMember,
)
from tourism_ugc_study.models.text.model_reliability_study import (
    ScoredEvaluationMember,
)
from tourism_ugc_study.models.text.model_routing_selection_config import (
    load_model_routing_selection_plan,
)


def _inputs() -> artifacts._RoutingSelectionInputs:
    """生成不含正文且同时支持两类的合成封存输入。"""

    labeled = tuple(
        LabeledWaveMember(
            task_id=f"task-{index}",
            source_post_id=index,
            source_version=1,
            component_id=f"component-{index}",
            stratum="synthetic",
            inclusion_probability=1.0,
            analysis_weight=1.0,
            sparse_p_unrelated=probability,
            qwen_p_unrelated=min(0.99, max(0.01, probability + delta)),
            tourism_label=label,
        )
        for index, (label, probability, delta) in enumerate(
            [
                ("related", 0.02, -0.01),
                ("related", 0.10, -0.04),
                ("related", 0.88, -0.50),
                ("unrelated", 0.12, -0.08),
                ("unrelated", 0.82, 0.09),
                ("unrelated", 0.96, 0.02),
            ],
            start=1,
        )
    )
    scored = tuple(
        ScoredEvaluationMember(
            source_post_id=item.source_post_id,
            source_version=1,
            component_id=item.component_id,
            normalized_sha256=f"{item.source_post_id:064x}",
            sparse_p_unrelated=item.sparse_p_unrelated,
            qwen_p_unrelated=item.qwen_p_unrelated,
        )
        for item in labeled
    )
    metrics = {
        model: {
            "weighted_accuracy": 0.9,
            "weighted_related_to_unrelated_rate": 0.05,
            "weighted_unrelated_to_related_rate": 0.10,
            "weighted_log_loss": 0.25,
            "weighted_brier_score": 0.08,
            "weighted_pr_auc_unrelated": 0.95,
        }
        for model in ("sparse", "qwen")
    }
    return artifacts._RoutingSelectionInputs(
        members=labeled,
        scored_population=scored,
        overall_metrics=metrics,
        scored_manifest_sha256="1" * 64,
        base_evaluation_manifest_sha256="2" * 64,
        completed_csv_sha256="3" * 64,
    )


def test_package_is_immutable_and_does_not_freeze_policy(
    tmp_path: Path, monkeypatch
) -> None:
    """分析包必须保存完整报告，同时维持测试、阈值和训练锁。"""

    plan = replace(
        load_model_routing_selection_plan(
            Path("configs/cleaning-model-routing-selection.yaml")
        ),
        bootstrap_repetitions=50,
        minimum_raw_tail_count=1,
        minimum_effective_tail_count=1.0,
    )
    monkeypatch.setattr(
        artifacts, "load_model_routing_selection_plan", lambda _path: plan
    )
    monkeypatch.setattr(
        artifacts, "_load_routing_selection_inputs", lambda *args: _inputs()
    )
    result = artifacts.analyze_routing_selection_package(
        tmp_path / "scored",
        tmp_path / "evaluation",
        Path("configs/cleaning-model-routing-selection.yaml"),
        tmp_path / "output",
        code_version="a" * 40,
    )

    package = tmp_path / "output" / result.analysis_id
    assert result.status == "WAVE_A_SELECTION_READY"
    assert result.threshold_pair_count_per_model == 256
    assert result.pareto_point_count > 0
    assert result.selected_model is None
    assert result.threshold_status == "UNSET"
    assert result.test_status == "locked_not_opened"
    assert result.labels_entered_fit is False
    assert (package / "routing-selection-report.json").is_file()
    summary = (package / "routing-selection-summary.md").read_text(encoding="utf-8")
    assert "相同目标人工率比较" in summary
    assert "不自动冻结策略" in summary
