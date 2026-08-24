"""研究者确认的双阈值策略与 Wave B 抽样配置解析。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ModelRoutingPolicyConfigError(RuntimeError):
    """策略字段、证据绑定或安全边界无效时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含正文、身份、标签或私有路径的稳定失败码。"""

        super().__init__("formal model routing policy configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class RoutingStrategy:
    """一个冻结模型与其三段式路由阈值。"""

    model: str
    model_id: str
    point_id: str
    t_keep: float
    t_exclude: float
    population_count: int
    auto_keep_count: int
    manual_review_count: int
    auto_exclude_count: int


@dataclass(frozen=True)
class WaveBStratumPlan:
    """一个 selected×comparator 动作交叉层的容量与分配。"""

    name: str
    selected_action: str
    comparator_action: str
    population_count: int
    sample_count: int


@dataclass(frozen=True)
class ModelRoutingPolicyPlan:
    """供 Wave B 独立评价使用、但不授权部署的不可变策略。"""

    policy_id: str
    policy_sha256: str
    random_seed: int
    decision_comment_url: str
    scored_frame_id: str
    scored_manifest_sha256: str
    wave_a_id: str
    wave_a_manifest_sha256: str
    routing_selection_id: str
    routing_selection_manifest_sha256: str
    routing_selection_report_sha256: str
    selected: RoutingStrategy
    comparator: RoutingStrategy
    wave_b_target_sample_count: int
    wave_b_eligible_population_count: int
    wave_b_eligible_component_count: int
    wave_b_strata: tuple[WaveBStratumPlan, ...]


_ACTIONS = ("auto_keep", "manual_review", "auto_exclude")
_EXPECTED_STRATA = tuple(
    f"qwen_{selected}__sparse_{comparator}"
    for selected in _ACTIONS
    for comparator in _ACTIONS
)


def _mapping(value: Any, fields: set[str], reason_code: str) -> Mapping[str, Any]:
    """要求映射拥有精确键集合，禁止配置静默漂移。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelRoutingPolicyConfigError(reason_code)
    return value


def _hex(value: Any, length: int, reason_code: str) -> str:
    """校验固定长度的小写十六进制身份。"""

    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelRoutingPolicyConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算拒绝 NaN 的稳定配置摘要。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strategy(value: Any, *, expected_model: str) -> RoutingStrategy:
    """解析并核对一个冻结路由策略。"""

    fields = {
        "model",
        "model_id",
        "point_id",
        "T_keep",
        "T_exclude",
        "population_count",
        "auto_keep_count",
        "manual_review_count",
        "auto_exclude_count",
    }
    item = _mapping(value, fields, "model_routing_policy_strategy_invalid")
    try:
        t_keep = float(item["T_keep"])
        t_exclude = float(item["T_exclude"])
        counts = tuple(
            int(item[key])
            for key in (
                "population_count",
                "auto_keep_count",
                "manual_review_count",
                "auto_exclude_count",
            )
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelRoutingPolicyConfigError(
            "model_routing_policy_strategy_invalid"
        ) from exc
    if (
        item["model"] != expected_model
        or not 0.0 <= t_keep < t_exclude <= 1.0
        or any(count < 0 for count in counts)
        or sum(counts[1:]) != counts[0]
    ):
        raise ModelRoutingPolicyConfigError("model_routing_policy_strategy_invalid")
    return RoutingStrategy(
        model=expected_model,
        model_id=_hex(
            item["model_id"], 32, "model_routing_policy_strategy_invalid"
        ),
        point_id=_hex(
            item["point_id"], 32, "model_routing_policy_strategy_invalid"
        ),
        t_keep=t_keep,
        t_exclude=t_exclude,
        population_count=counts[0],
        auto_keep_count=counts[1],
        manual_review_count=counts[2],
        auto_exclude_count=counts[3],
    )


def load_model_routing_policy_plan(path: str | Path) -> ModelRoutingPolicyPlan:
    """加载研究者确认、只供 Wave B 评价的策略配置。

    Args:
        path: 冻结 YAML 配置路径。

    Returns:
        同时绑定 selected、等人工量 comparator 和九个抽样层的策略计划。

    Raises:
        ModelRoutingPolicyConfigError: 文件、身份、阈值、容量或安全门无效。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelRoutingPolicyConfigError(
            "model_routing_policy_config_unreadable"
        ) from exc
    root = _mapping(
        loaded,
        {
            "artifact_kind",
            "status",
            "random_seed",
            "researcher_decision",
            "bindings",
            "selected_strategy",
            "matched_workload_comparator",
            "wave_b",
            "guards",
        },
        "model_routing_policy_config_invalid",
    )
    decision = _mapping(
        root["researcher_decision"],
        {"issue_number", "comment_url", "decision"},
        "model_routing_policy_decision_invalid",
    )
    bindings = _mapping(
        root["bindings"],
        {
            "scored_frame_id",
            "scored_manifest_sha256",
            "wave_a_id",
            "wave_a_manifest_sha256",
            "routing_selection_id",
            "routing_selection_manifest_sha256",
            "routing_selection_report_sha256",
        },
        "model_routing_policy_bindings_invalid",
    )
    wave_b = _mapping(
        root["wave_b"],
        {
            "target_sample_count",
            "sampling_unit",
            "exclusion_rule",
            "eligible_population_count",
            "eligible_component_count",
            "primary_weighting",
            "uncertain_policy",
            "strata",
        },
        "model_routing_policy_wave_b_invalid",
    )
    guards = _mapping(
        root["guards"],
        {
            "labels_entered_fit",
            "may_retrain",
            "may_open_locked_test",
            "may_generate_formal_auto_decisions",
            "threshold_status",
            "deployment_status",
            "audit_status",
            "auto_cleaning_decisions_present",
            "platform_used",
        },
        "model_routing_policy_guards_invalid",
    )
    selected = _strategy(root["selected_strategy"], expected_model="qwen")
    comparator = _strategy(
        root["matched_workload_comparator"], expected_model="sparse"
    )
    if (
        root["artifact_kind"] != "formal-cleaning-model-routing-policy"
        or root["status"] != "frozen_for_wave_b_evaluation"
        or root["random_seed"] != 20260824
        or decision
        != {
            "issue_number": 46,
            "comment_url": "https://github.com/ottercoconut/tourism-ugc-study/issues/46#issuecomment-5395692612",
            "decision": "qwen_0_14_0_86",
        }
        or selected.t_keep != 0.14
        or selected.t_exclude != 0.86
        or comparator.t_keep != 0.20
        or comparator.t_exclude != 0.80
        or selected.population_count != 10103
        or comparator.population_count != 10103
        or wave_b["target_sample_count"] != 360
        or wave_b["sampling_unit"] != "post"
        or wave_b["exclusion_rule"]
        != "exclude_all_components_touching_final_reference_or_wave_a"
        or wave_b["eligible_population_count"] != 9410
        or wave_b["eligible_component_count"] != 7273
        or wave_b["primary_weighting"]
        != "horvitz_thompson_hajek_by_cross_action_stratum"
        or wave_b["uncertain_policy"]
        != "report_separately_no_binary_coercion"
        or guards
        != {
            "labels_entered_fit": False,
            "may_retrain": False,
            "may_open_locked_test": False,
            "may_generate_formal_auto_decisions": False,
            "threshold_status": "FROZEN_FOR_WAVE_B_EVALUATION",
            "deployment_status": "NOT_AUTHORIZED",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
        }
    ):
        raise ModelRoutingPolicyConfigError("model_routing_policy_plan_invalid")
    for key, length in {
        "scored_frame_id": 32,
        "scored_manifest_sha256": 64,
        "wave_a_id": 32,
        "wave_a_manifest_sha256": 64,
        "routing_selection_id": 32,
        "routing_selection_manifest_sha256": 64,
        "routing_selection_report_sha256": 64,
    }.items():
        _hex(bindings.get(key), length, "model_routing_policy_bindings_invalid")
    strata_node = _mapping(
        wave_b["strata"], set(_EXPECTED_STRATA), "model_routing_policy_strata_invalid"
    )
    strata: list[WaveBStratumPlan] = []
    for name in _EXPECTED_STRATA:
        selected_action, comparator_action = name.split("__", maxsplit=1)
        selected_action = selected_action.removeprefix("qwen_")
        comparator_action = comparator_action.removeprefix("sparse_")
        item = _mapping(
            strata_node[name],
            {"population_count", "sample_count"},
            "model_routing_policy_strata_invalid",
        )
        if (
            not isinstance(item["population_count"], int)
            or not isinstance(item["sample_count"], int)
            or item["population_count"] <= 0
            or not 0 < item["sample_count"] <= item["population_count"]
        ):
            raise ModelRoutingPolicyConfigError("model_routing_policy_strata_invalid")
        strata.append(
            WaveBStratumPlan(
                name=name,
                selected_action=selected_action,
                comparator_action=comparator_action,
                population_count=item["population_count"],
                sample_count=item["sample_count"],
            )
        )
    if (
        sum(item.population_count for item in strata)
        != wave_b["eligible_population_count"]
        or sum(item.sample_count for item in strata) != wave_b["target_sample_count"]
    ):
        raise ModelRoutingPolicyConfigError("model_routing_policy_strata_invalid")
    policy_sha256 = _canonical_sha256(root)
    return ModelRoutingPolicyPlan(
        policy_id=policy_sha256[:32],
        policy_sha256=policy_sha256,
        random_seed=20260824,
        decision_comment_url=str(decision["comment_url"]),
        scored_frame_id=str(bindings["scored_frame_id"]),
        scored_manifest_sha256=str(bindings["scored_manifest_sha256"]),
        wave_a_id=str(bindings["wave_a_id"]),
        wave_a_manifest_sha256=str(bindings["wave_a_manifest_sha256"]),
        routing_selection_id=str(bindings["routing_selection_id"]),
        routing_selection_manifest_sha256=str(
            bindings["routing_selection_manifest_sha256"]
        ),
        routing_selection_report_sha256=str(
            bindings["routing_selection_report_sha256"]
        ),
        selected=selected,
        comparator=comparator,
        wave_b_target_sample_count=360,
        wave_b_eligible_population_count=9410,
        wave_b_eligible_component_count=7273,
        wave_b_strata=tuple(strata),
    )
