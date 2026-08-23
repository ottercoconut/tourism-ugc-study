"""正式清洗 baseline 训练结果的去敏展示层。

本模块只把已经封存的训练摘要转换为人类可读文本或机器可读 JSON；不读取
训练语料、不加载测试成员，也不据开发指标判断模型是否通过正式验收。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Literal, Mapping

from .formal_training import FormalTrainingPackageResult


BaselineOutputFormat = Literal["human", "json"]


class BaselineReportingError(RuntimeError):
    """训练摘要不满足展示契约时抛出的去敏异常。

    Attributes:
        reason_code: 不含正文、作者或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化展示失败。

        Args:
            reason_code: 供 CLI 和测试使用的稳定失败码。
        """

        super().__init__("formal baseline report rendering failed")
        self.reason_code = reason_code


def _nonnegative_count(value: Any) -> int:
    """解析用于报告的非负整数计数。

    Args:
        value: 待验证值。

    Returns:
        通过验证的非负整数。

    Raises:
        BaselineReportingError: 值不是规范非负整数。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BaselineReportingError("baseline_report_count_invalid")
    return value


def _finite_metric(metrics: Mapping[str, Any], field: str) -> float:
    """读取处于有限范围内的验证指标。

    Args:
        metrics: 已封存验证指标。
        field: 指标字段名。

    Returns:
        转换后的有限浮点数。

    Raises:
        BaselineReportingError: 字段缺失、非数值或超出指标合法范围。
    """

    value = metrics.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BaselineReportingError("baseline_report_metric_invalid")
    parsed = float(value)
    # 当前展示的六项指标都应为非负值；其中比例指标不能超过 1。
    if parsed < 0.0 or parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise BaselineReportingError("baseline_report_metric_invalid")
    if field in {
        "diagnostic_cutoff",
        "precision_unrelated",
        "recall_unrelated",
        "f1_unrelated",
        "pr_auc_unrelated",
        "brier_score",
    } and parsed > 1.0:
        raise BaselineReportingError("baseline_report_metric_invalid")
    return parsed


def _confusion_counts(metrics: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    """校验并返回验证集混淆矩阵及总数。

    Args:
        metrics: 已封存验证指标。

    Returns:
        ``(related→related, related→unrelated, unrelated→related,
        unrelated→unrelated, count)``。

    Raises:
        BaselineReportingError: 混淆矩阵缺失、计数非法或与总数不一致。
    """

    confusion = metrics.get("confusion")
    if not isinstance(confusion, Mapping):
        raise BaselineReportingError("baseline_report_confusion_invalid")
    fields = (
        "related_as_related",
        "related_as_unrelated",
        "unrelated_as_related",
        "unrelated_as_unrelated",
    )
    if set(confusion) != set(fields):
        raise BaselineReportingError("baseline_report_confusion_invalid")
    values = tuple(_nonnegative_count(confusion[field]) for field in fields)
    count = _nonnegative_count(metrics.get("count"))
    if count == 0 or sum(values) != count:
        raise BaselineReportingError("baseline_report_confusion_count_mismatch")
    return (*values, count)


def _percentage(value: float) -> str:
    """把零到一的比例格式化为一位小数百分比。"""

    return f"{value * 100:.1f}%"


def render_baseline_training_result(
    result: FormalTrainingPackageResult,
    *,
    output_format: BaselineOutputFormat = "human",
) -> str:
    """渲染正式 baseline 训练摘要。

    Args:
        result: 训练或完整复用返回的去敏运行包摘要。
        output_format: ``human`` 输出中文多行报告；``json`` 保留稳定机器接口。

    Returns:
        不含正文、作者或本机路径的报告字符串。

    Raises:
        BaselineReportingError: 输出格式未知，或验证指标不能形成一致报告。
    """

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise BaselineReportingError("baseline_report_output_format_invalid")

    metrics = result.validation_metrics
    (
        related_as_related,
        related_as_unrelated,
        unrelated_as_related,
        unrelated_as_unrelated,
        validation_count,
    ) = _confusion_counts(metrics)
    if _nonnegative_count(result.split_counts.get("validation")) != validation_count:
        raise BaselineReportingError("baseline_report_validation_count_mismatch")

    correct = related_as_related + unrelated_as_unrelated
    accuracy = correct / validation_count
    cutoff = _finite_metric(metrics, "diagnostic_cutoff")
    precision = _finite_metric(metrics, "precision_unrelated")
    recall = _finite_metric(metrics, "recall_unrelated")
    f1 = _finite_metric(metrics, "f1_unrelated")
    pr_auc = _finite_metric(metrics, "pr_auc_unrelated")
    brier = _finite_metric(metrics, "brier_score")
    log_loss = _finite_metric(metrics, "log_loss")
    if metrics.get("diagnostic_cutoff_is_routing_threshold") is not False:
        raise BaselineReportingError("baseline_report_cutoff_role_invalid")

    determinate_count = _nonnegative_count(result.determinate_count)
    if determinate_count == 0:
        raise BaselineReportingError("baseline_report_determinate_count_invalid")
    train_count = _nonnegative_count(result.split_counts.get("train"))
    test_count = _nonnegative_count(result.split_counts.get("test"))
    if train_count + validation_count + test_count != determinate_count:
        raise BaselineReportingError("baseline_report_split_count_mismatch")

    run_kind = "复用既有不可变训练包" if result.reused else "首次训练并封存"
    lines = [
        "正式清洗 baseline 结果",
        "=====================",
        "",
        "运行状态",
        f"  训练包：{result.status}（{run_kind}）",
        f"  模型 ID：{result.model_id}",
        f"  参考证据：{result.reference_count} 条",
        f"  确定标签：{result.determinate_count} 条；不确定标签：{result.uncertain_count} 条",
        f"  校准：训练集分组折外 Sigmoid，{result.calibration_fold_count} 折",
        "",
        "数据切分",
        f"  训练：{train_count} 条（{_percentage(train_count / determinate_count)}）",
        f"  验证：{validation_count} 条（{_percentage(validation_count / determinate_count)}）",
        f"  锁定测试：{test_count} 条（{_percentage(test_count / determinate_count)}；未开启）",
        "",
        f"验证集诊断（n={validation_count}，p_unrelated={cutoff:.3f}；不是路由阈值）",
        f"  准确率：{_percentage(accuracy)}（{correct}/{validation_count}）",
        f"  unrelated Precision：{_percentage(precision)}",
        f"  unrelated Recall：{_percentage(recall)}",
        f"  unrelated F1：{_percentage(f1)}",
        f"  unrelated PR-AUC：{_percentage(pr_auc)}",
        f"  Brier score：{brier:.4f}（越低越好）",
        f"  Log loss：{log_loss:.4f}（越低越好）",
        "",
        "混淆矩阵（行=实际，列=预测）",
        "                     related  unrelated",
        f"  实际 related       {related_as_related:>7}  {related_as_unrelated:>9}",
        f"  实际 unrelated     {unrelated_as_related:>7}  {unrelated_as_unrelated:>9}",
        "",
        "实验边界",
        f"  锁定测试集：{result.test_status}",
        f"  路由阈值：{result.threshold_status}",
        "  验收结论：未设置；当前开发结果不能自动判定通过或失败",
        "  平台字段：不进入模型、切分、阈值或性能门",
        "",
        "Artifact 完整性身份",
        f"  模型 SHA-256：{result.artifact_sha256}",
        f"  校准器 SHA-256：{result.calibration_artifact_sha256}",
        f"  训练包 manifest SHA-256：{result.package_manifest_sha256}",
    ]
    return "\n".join(lines)


def render_baseline_training_failure(
    reason_code: str,
    *,
    output_format: BaselineOutputFormat = "human",
) -> str:
    """渲染不泄露私有输入的训练失败摘要。

    Args:
        reason_code: 核心训练流程提供的稳定失败码。
        output_format: ``human`` 输出中文提示；``json`` 输出机器对象。

    Returns:
        包含稳定 ``reason_code`` 的去敏错误文本。

    Raises:
        BaselineReportingError: 输出格式未知或失败码为空。
    """

    if not reason_code.strip():
        raise BaselineReportingError("baseline_report_reason_code_missing")
    if output_format == "json":
        return json.dumps(
            {"status": "failed", "reason_code": reason_code},
            ensure_ascii=False,
            sort_keys=True,
        )
    if output_format != "human":
        raise BaselineReportingError("baseline_report_output_format_invalid")
    return "\n".join(
        (
            "正式清洗 baseline 失败",
            "=====================",
            f"reason_code：{reason_code}",
            "未生成或覆盖正式训练包。",
        )
    )
