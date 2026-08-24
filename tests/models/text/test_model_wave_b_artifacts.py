"""Wave B 策略与四列人工任务 artifact 测试。"""

import codecs
import csv
import json
from pathlib import Path

import tourism_ugc_study.models.text.model_wave_b_artifacts as artifacts
from tourism_ugc_study.models.text.model_reliability_study import (
    EligibleEvaluationMember,
    EligiblePopulation,
    ScoredEvaluationMember,
)
from tourism_ugc_study.models.text.model_routing_policy_config import (
    load_model_routing_policy_plan,
)


POLICY = load_model_routing_policy_plan(
    Path("configs/cleaning-model-routing-policy.yaml")
)


def _synthetic_inputs():
    """按正式九层容量生成无正文概率人口和对应文本人口。"""

    wave_components = [f"wave-a-{index}" for index in range(227)]
    scored: list[ScoredEvaluationMember] = []
    index = 0
    for offset in range(693):
        scored.append(
            ScoredEvaluationMember(
                source_post_id=index + 1,
                source_version=1,
                component_id=wave_components[offset % len(wave_components)],
                normalized_sha256="",
                sparse_p_unrelated=0.5,
                qwen_p_unrelated=0.5,
            )
        )
        index += 1
    action_probability = {
        "auto_keep": 0.1,
        "manual_review": 0.5,
        "auto_exclude": 0.9,
    }
    eligible_index = 0
    for stratum in POLICY.wave_b_strata:
        for _repeat in range(stratum.population_count):
            scored.append(
                ScoredEvaluationMember(
                    source_post_id=index + 1,
                    source_version=1,
                    component_id=f"eligible-{eligible_index % 7273}",
                    normalized_sha256="",
                    sparse_p_unrelated=action_probability[
                        stratum.comparator_action
                    ],
                    qwen_p_unrelated=action_probability[stratum.selected_action],
                )
            )
            index += 1
            eligible_index += 1
    members: list[EligibleEvaluationMember] = []
    fixed_scored: list[ScoredEvaluationMember] = []
    import hashlib

    for item in scored:
        text = f"合成任务文本 {item.source_post_id}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fixed_scored.append(
            ScoredEvaluationMember(
                source_post_id=item.source_post_id,
                source_version=item.source_version,
                component_id=item.component_id,
                normalized_sha256=digest,
                sparse_p_unrelated=item.sparse_p_unrelated,
                qwen_p_unrelated=item.qwen_p_unrelated,
            )
        )
        members.append(
            EligibleEvaluationMember(
                source_post_id=item.source_post_id,
                source_version=item.source_version,
                component_id=item.component_id,
                platform_key="synthetic",
                normalized_model_text=text,
                normalized_sha256=digest,
            )
        )
    private_records = [
        {"component_id": wave_components[index % len(wave_components)]}
        for index in range(240)
    ]
    population = EligiblePopulation(
        members=tuple(members),
        candidate_count=10103,
        candidate_component_count=7500,
        reference_count=700,
        reference_component_count=587,
        excluded_count=3755,
        eligible_component_count=7500,
        projection_member_sha256="a" * 64,
        leakage_output_sha256="b" * 64,
    )
    return tuple(fixed_scored), private_records, population


def test_freeze_policy_then_prepare_four_column_wave_b(
    tmp_path: Path, monkeypatch
) -> None:
    """任务必须隐藏模型答案、保留设计权重并维持部署锁。"""

    scored, private_records, population = _synthetic_inputs()
    for name in ("scored", "wave-a", "selection"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(
        artifacts, "_load_scored_members", lambda *args, **kwargs: scored
    )
    monkeypatch.setattr(
        artifacts,
        "_validate_wave_a_package",
        lambda *args, **kwargs: ({"wave_id": POLICY.wave_a_id}, {"records": private_records}),
    )
    monkeypatch.setattr(artifacts, "_validate_selection_package", lambda *args: None)

    policy_result = artifacts.freeze_routing_policy_package(
        tmp_path / "scored",
        tmp_path / "wave-a",
        tmp_path / "selection",
        Path("configs/cleaning-model-reliability-study.yaml"),
        Path("configs/cleaning-model-routing-policy.yaml"),
        tmp_path / "policy-root",
        code_version="a" * 40,
    )
    assert policy_result.status == "ROUTING_POLICY_FROZEN"
    assert policy_result.deployment_status == "NOT_AUTHORIZED"
    policy_package = tmp_path / "policy-root" / policy_result.policy_id
    monkeypatch.setattr(
        artifacts,
        "load_eligible_evaluation_population",
        lambda *args, **kwargs: population,
    )
    result = artifacts.prepare_wave_b_package(
        tmp_path / "reference.csv",
        tmp_path / "derived.sqlite",
        tmp_path / "scored",
        tmp_path / "wave-a",
        policy_package,
        Path("configs/cleaning-model-reliability-study.yaml"),
        Path("configs/cleaning-model-routing-policy.yaml"),
        tmp_path / "wave-b-root",
        normalization_config=object(),
        code_version="b" * 40,
        expected_policy_manifest_sha256=policy_result.package_manifest_sha256,
    )

    package = tmp_path / "wave-b-root" / result.wave_id
    task = package / "wave-b-tourism-relevance-annotation.csv"
    with task.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    private = json.loads((package / "private-map.json").read_text(encoding="utf-8"))

    assert task.read_bytes().startswith(codecs.BOM_UTF8)
    assert reader.fieldnames == [
        "task_id",
        "sample_run_id",
        "normalized_model_text",
        "tourism_label",
    ]
    assert len(rows) == 360
    assert all(row["tourism_label"] == "" for row in rows)
    assert {row["sample_run_id"] for row in rows} == {result.wave_id}
    assert len(private["records"]) == 360
    assert all(record["analysis_weight"] > 0 for record in private["records"])
    assert all(record["platform_key"] == "synthetic" for record in private["records"])
    assert result.labels_entered_fit is False
    assert result.test_status == "locked_not_opened"
    assert result.deployment_status == "NOT_AUTHORIZED"
