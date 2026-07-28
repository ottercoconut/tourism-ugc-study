# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用 `src/tourism_ugc_study/`。数据清洗、采样、模型和评估逻辑不得只存在于脚本或 notebook 中。

现有 `build_research_dataset.py` 暂时保留以维持兼容。重构后建议入口如下：

```text
scripts/cleaning/build_dataset.py
scripts/annotation/sample_round.py
scripts/annotation/validate_labels.py
scripts/text/train.py
scripts/text/predict.py
scripts/vision/train.py
scripts/vision/predict.py
scripts/evaluation/evaluate.py
scripts/release/freeze_results.py
```

所有正式入口应支持 `--config`、`--run-id`、`--seed` 和 `--output-dir`，并拒绝覆盖已有正式运行目录。
