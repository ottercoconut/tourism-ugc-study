# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用 `src/tourism_ugc_study/`。数据清洗、采样、模型和评估逻辑不得只存在于脚本或 notebook 中。

脚本保持平铺，并使用领域前缀；现有 `build_research_dataset.py` 暂时保留以维持兼容。后续入口示例：

```text
scripts/cleaning_build_dataset.py
scripts/annotation_sample_round.py
scripts/annotation_validate_labels.py
scripts/text_train.py
scripts/text_predict.py
scripts/vision_train.py
scripts/vision_predict.py
scripts/evaluate.py
scripts/freeze_results.py
```

所有正式入口应支持 `--config`、`--run-id`、`--seed` 和 `--output-dir`，并拒绝覆盖已有正式运行目录。
