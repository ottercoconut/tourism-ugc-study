"""正式清洗 baseline 去敏输出的可读性与机器接口测试。"""

from __future__ import annotations

import json

import pytest

from tourism_ugc_study.models.text.baseline_reporting import (
    BaselineReportingError,
    render_baseline_training_failure,
    render_baseline_training_result,
)
from tourism_ugc_study.models.text.formal_training import FormalTrainingPackageResult


def _result() -> FormalTrainingPackageResult:
    """构造与首次正式 baseline 形状一致的去敏结果。"""

    return FormalTrainingPackageResult(
        model_id="a2de389942e91027733665fe47f9dfd2",
        status="frozen",
        reused=False,
        reference_count=700,
        determinate_count=700,
        uncertain_count=0,
        split_counts={"train": 441, "validation": 111, "test": 148},
        calibration_fold_count=5,
        validation_metrics={
            "count": 111,
            "diagnostic_cutoff": 0.5,
            "diagnostic_cutoff_is_routing_threshold": False,
            "precision_unrelated": 0.8448275862068966,
            "recall_unrelated": 0.8448275862068966,
            "f1_unrelated": 0.8448275862068966,
            "pr_auc_unrelated": 0.9377690802383652,
            "brier_score": 0.11144285298119151,
            "log_loss": 0.37116003447456103,
            "related_count": 53,
            "unrelated_count": 58,
            "confusion": {
                "related_as_related": 44,
                "related_as_unrelated": 9,
                "unrelated_as_related": 9,
                "unrelated_as_unrelated": 49,
            },
        },
        artifact_sha256="a" * 64,
        calibration_artifact_sha256="b" * 64,
        package_manifest_sha256="c" * 64,
        test_status="locked_not_opened",
        threshold_status="UNSET",
    )


def test_human_report_explains_accuracy_errors_and_experiment_boundaries() -> None:
    report = render_baseline_training_result(_result())

    assert "准确率：83.8%（93/111）" in report
    assert "实际 related            44          9" in report
    assert "实际 unrelated           9         49" in report
    assert "不是路由阈值" in report
    assert "锁定测试集：locked_not_opened" in report
    assert "路由阈值：UNSET" in report
    assert "验收结论：未设置" in report
    assert "平台字段：不进入模型、切分、阈值或性能门" in report


def test_json_report_preserves_machine_readable_result() -> None:
    payload = json.loads(
        render_baseline_training_result(_result(), output_format="json")
    )

    assert payload["model_id"] == "a2de389942e91027733665fe47f9dfd2"
    assert payload["validation_metrics"]["confusion"]["related_as_unrelated"] == 9
    assert payload["test_status"] == "locked_not_opened"
    assert payload["threshold_status"] == "UNSET"


def test_human_failure_keeps_stable_reason_code() -> None:
    report = render_baseline_training_failure("baseline_example_failure")

    assert "正式清洗 baseline 失败" in report
    assert "reason_code：baseline_example_failure" in report
    assert "未生成或覆盖正式训练包" in report


def test_report_rejects_inconsistent_confusion_count() -> None:
    result = _result()
    metrics = dict(result.validation_metrics)
    metrics["count"] = 110
    inconsistent = FormalTrainingPackageResult(
        **{**result.__dict__, "validation_metrics": metrics}
    )

    with pytest.raises(BaselineReportingError) as error:
        render_baseline_training_result(inconsistent)

    assert error.value.reason_code == "baseline_report_confusion_count_mismatch"
