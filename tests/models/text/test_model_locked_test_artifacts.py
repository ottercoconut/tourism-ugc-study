"""唯一锁定测试的无拟合入口与不可变封存测试。"""

import ast
import inspect
from pathlib import Path

import numpy as np

import tourism_ugc_study.models.text.model_locked_test_artifacts as artifacts
from tourism_ugc_study.cleaning.config import load_cleaning_config_bundle


def _documents() -> tuple[artifacts.LockedTestDocument, ...]:
    """生成两个自动尾部零错误且支持充足的148条合成测试证据。"""

    rows: list[artifacts.LockedTestDocument] = []
    for index in range(148):
        if index < 50:
            label = "related"
        elif index < 98:
            label = "related" if index % 2 == 0 else "unrelated"
        else:
            label = "unrelated"
        rows.append(
            artifacts.LockedTestDocument(
                member_key=f"{index:064x}",
                component_id=f"component-{index}",
                normalized_model_text=f"合成测试文本 {index}",
                tourism_label=label,
            )
        )
    return tuple(rows)


def test_locked_test_entry_contains_no_fit_call() -> None:
    """正式入口的语法树不得出现任何拟合或拟合变换调用。"""

    tree = ast.parse(inspect.getsource(artifacts))
    forbidden = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"fit", "fit_transform"}
    }
    assert forbidden == set()


def test_failed_human_summary_does_not_announce_audit() -> None:
    """测试失败摘要必须明确降级人工，不能误导为继续部署审计。"""

    result = artifacts.LockedTestPackageResult(
        test_run_id="a" * 32,
        status="FAILED_MANUAL_ONLY",
        reused=True,
        package_manifest_sha256="b" * 64,
        report_sha256="c" * 64,
        probability_artifact_sha256="d" * 64,
        test_count=148,
        related_count=132,
        unrelated_count=16,
        auto_keep_count=103,
        manual_review_count=41,
        auto_exclude_count=4,
        auto_keep_unrelated_events=2,
        auto_exclude_related_events=1,
        automatic_coverage_rate=107 / 148,
        overall_metrics={
            "count": 148,
            "confusion": {
                "related_as_related": 126,
                "related_as_unrelated": 6,
                "unrelated_as_related": 7,
                "unrelated_as_unrelated": 9,
            },
            "log_loss": 0.2312,
            "pr_auc_unrelated": 0.6254,
            "brier_score": 0.0695,
        },
        test_status="opened_once",
        test_access_count=1,
        audit_status="FROZEN_NOT_RUN",
        deployment_status="MANUAL_ONLY",
        may_generate_provisional_routing=False,
        may_generate_formal_auto_decisions=False,
        fit_call_count=0,
        prediction_call_count=1,
    )

    rendered = artifacts.render_locked_test_result(result, output_format="human")

    assert "部署审计不启动" in rendered
    assert "下一门是两个自动尾部" not in rendered


def test_runs_once_then_reuses_without_reading_test_again(
    tmp_path: Path, monkeypatch
) -> None:
    """首次运行封存概率，复用时不得再次加载测试或调用预测。"""

    documents = _documents()
    calls = {"load": 0, "predict": 0}

    def fake_validate(*args, **kwargs):
        return {"status": "ACCEPTANCE_AND_AUDIT_FROZEN"}

    def fake_load(*args, **kwargs):
        calls["load"] += 1
        return documents

    def fake_predict(*args, **kwargs):
        calls["predict"] += 1
        probabilities = np.asarray(
            [0.05] * 50 + [0.5] * 48 + [0.95] * 50, dtype=float
        )
        return probabilities, {"count": 148}, {"device": "synthetic"}

    monkeypatch.setattr(artifacts, "_validate_acceptance_package", fake_validate)
    monkeypatch.setattr(artifacts, "_load_locked_test_evidence", fake_load)
    monkeypatch.setattr(artifacts, "_predict_qwen_locked_test", fake_predict)
    config, normalization = load_cleaning_config_bundle(
        Path("configs/cleaning.yaml")
    )
    first = artifacts.run_locked_test_package(
        tmp_path / "reference.csv",
        tmp_path / "reference.manifest.json",
        tmp_path / "derived.sqlite",
        tmp_path / "split",
        Path("configs/cleaning-text-challenger.yaml"),
        tmp_path / "qwen",
        Path("configs/cleaning-qwen-embedding-baseline.yaml"),
        Path("configs/cleaning-qwen-english-head-tail.yaml"),
        tmp_path / "models",
        Path("configs/cleaning-model-deployment-acceptance.yaml"),
        tmp_path / "acceptance",
        tmp_path / "root",
        config=config,
        normalization_config=normalization,
        code_version="4" * 40,
        expected_acceptance_manifest_sha256="3" * 64,
        show_progress=False,
    )
    assert first.status == "PASSED_FOR_PROVISIONAL_ROUTING"
    assert first.test_status == "opened_once"
    assert first.test_access_count == 1
    assert first.fit_call_count == 0
    assert first.prediction_call_count == 1
    assert first.may_generate_formal_auto_decisions is False
    assert calls == {"load": 1, "predict": 1}

    reused = artifacts.run_locked_test_package(
        tmp_path / "missing.csv",
        tmp_path / "missing.manifest.json",
        tmp_path / "missing.sqlite",
        tmp_path / "missing-split",
        Path("configs/cleaning-text-challenger.yaml"),
        tmp_path / "missing-qwen",
        Path("configs/cleaning-qwen-embedding-baseline.yaml"),
        Path("configs/cleaning-qwen-english-head-tail.yaml"),
        tmp_path / "missing-models",
        Path("configs/cleaning-model-deployment-acceptance.yaml"),
        tmp_path / "acceptance",
        tmp_path / "root",
        config=config,
        normalization_config=normalization,
        code_version="2" * 40,
        expected_acceptance_manifest_sha256="3" * 64,
        expected_existing_manifest_sha256=first.package_manifest_sha256,
        show_progress=False,
    )
    assert reused.reused is True
    assert calls == {"load": 1, "predict": 1}
