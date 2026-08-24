"""三段式双阈值风险—工作量分析测试。"""

from dataclasses import replace
from pathlib import Path

from tourism_ugc_study.models.text.model_reliability_evaluation import (
    LabeledWaveMember,
)
from tourism_ugc_study.models.text.model_reliability_study import (
    ScoredEvaluationMember,
)
from tourism_ugc_study.models.text.model_routing_selection import (
    analyze_model_routing_grid,
)
from tourism_ugc_study.models.text.model_routing_selection_config import (
    load_model_routing_selection_plan,
)


def _member(index: int, label: str, sparse: float, qwen: float) -> LabeledWaveMember:
    """生成权重为1且分量唯一的合成 Wave A 成员。"""

    return LabeledWaveMember(
        task_id=f"task-{index}",
        source_post_id=index,
        source_version=1,
        component_id=f"component-{index}",
        stratum="synthetic",
        inclusion_probability=1.0,
        analysis_weight=1.0,
        sparse_p_unrelated=sparse,
        qwen_p_unrelated=qwen,
        tourism_label=label,
    )


def test_analyze_complete_three_way_routing_grid() -> None:
    """每个模型必须输出完整256点、两端风险和等工作量比较。"""

    members = (
        _member(1, "related", 0.02, 0.01),
        _member(2, "related", 0.10, 0.06),
        _member(3, "related", 0.88, 0.30),
        _member(4, "unrelated", 0.12, 0.04),
        _member(5, "unrelated", 0.82, 0.91),
        _member(6, "unrelated", 0.96, 0.98),
    )
    population = tuple(
        ScoredEvaluationMember(
            source_post_id=item.source_post_id,
            source_version=1,
            component_id=item.component_id,
            normalized_sha256=f"{item.source_post_id:064x}",
            sparse_p_unrelated=item.sparse_p_unrelated,
            qwen_p_unrelated=item.qwen_p_unrelated,
        )
        for item in members
    )
    plan = replace(
        load_model_routing_selection_plan(
            Path("configs/cleaning-model-routing-selection.yaml")
        ),
        bootstrap_repetitions=100,
        minimum_raw_tail_count=1,
        minimum_effective_tail_count=1.0,
    )

    report = analyze_model_routing_grid(
        members,
        population,
        plan=plan,
        overall_metrics={"sparse": {}, "qwen": {}},
    )

    assert report["status"] == "WAVE_A_SELECTION_READY"
    assert len(report["strategies"]["sparse"]) == 256
    assert len(report["strategies"]["qwen"]) == 256
    assert len(report["equal_workload_comparison"]) == 5
    assert report["pareto_frontier_point_ids"]
    assert report["selected_model"] is None
    assert report["threshold_status"] == "UNSET"
    assert report["labels_entered_fit"] is False
    assert report["test_status"] == "locked_not_opened"

    qwen_point = next(
        point
        for point in report["strategies"]["qwen"]
        if point["t_keep"] == 0.10 and point["t_exclude"] == 0.90
    )
    assert qwen_point["auto_keep_count"] == 3
    assert qwen_point["manual_review_count"] == 1
    assert qwen_point["auto_exclude_count"] == 2
    assert qwen_point["auto_keep_unrelated_retention"]["raw_error_count"] == 1
    assert qwen_point["auto_exclude_related_loss"]["raw_error_count"] == 0
