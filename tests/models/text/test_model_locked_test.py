"""唯一 Qwen 锁定测试的三段判读与失败降级测试。"""

from dataclasses import replace
from pathlib import Path

from tourism_ugc_study.models.text.model_deployment_acceptance_config import (
    load_model_deployment_acceptance_plan,
)
from tourism_ugc_study.models.text.model_locked_test import (
    LockedTestObservation,
    evaluate_locked_test,
)


PLAN = load_model_deployment_acceptance_plan(
    Path("configs/cleaning-model-deployment-acceptance.yaml")
)


def _observations() -> tuple[LockedTestObservation, ...]:
    """生成50保留、48人工、50排除且两个自动尾部零错误的148条。"""

    rows: list[LockedTestObservation] = []
    for index in range(148):
        if index < 50:
            label, probability = "related", 0.05
        elif index < 98:
            label = "related" if index % 2 == 0 else "unrelated"
            probability = 0.5
        else:
            label, probability = "unrelated", 0.95
        rows.append(
            LockedTestObservation(
                member_key=f"{index:064x}",
                component_id=f"component-{index}",
                tourism_label=label,
                p_unrelated=probability,
            )
        )
    return tuple(rows)


def test_pass_only_authorizes_provisional_routing() -> None:
    """零方向性错误只允许临时路由，不能直接产生正式决定。"""

    report = evaluate_locked_test(_observations(), plan=PLAN)

    assert report["status"] == "PASSED_FOR_PROVISIONAL_ROUTING"
    assert report["routing"]["auto_keep_unrelated_events"] == 0
    assert report["routing"]["auto_exclude_related_events"] == 0
    assert report["deployment_status"] == "PROVISIONAL_ROUTING_AUTHORIZED"
    assert report["may_generate_provisional_routing"] is True
    assert report["may_generate_formal_auto_decisions"] is False


def test_one_auto_exclude_ugc_event_forces_manual_only() -> None:
    """排除端出现一条真实UGC就必须失败并降级为全人工。"""

    rows = list(_observations())
    rows[-1] = replace(rows[-1], tourism_label="related")

    report = evaluate_locked_test(rows, plan=PLAN)

    assert report["status"] == "FAILED_MANUAL_ONLY"
    assert report["routing"]["auto_exclude_related_events"] == 1
    assert report["deployment_status"] == "MANUAL_ONLY"
    assert report["may_generate_provisional_routing"] is False


def test_insufficient_tail_support_is_inconclusive() -> None:
    """即使没有错误，自动尾部支持不足也不能勉强放行。"""

    rows = list(_observations())
    for index in range(15, 50):
        rows[index] = replace(rows[index], p_unrelated=0.5)

    report = evaluate_locked_test(rows, plan=PLAN)

    assert report["routing"]["auto_keep_count"] == 15
    assert report["status"] == "INCONCLUSIVE_MANUAL_ONLY"
    assert report["deployment_status"] == "MANUAL_ONLY"
