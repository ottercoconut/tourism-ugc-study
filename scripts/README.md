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

当前已实现只读源快照、增量发现、批次冻结、任务领取/状态查询和显式恢复入口：

```bash
.venv/bin/python scripts/cleaning_snapshot_source.py \
  --source-db <SOURCE_SQLITE> \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --run-id <RUN_ID>

.venv/bin/python scripts/cleaning_discover_increment.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --snapshot-id <SNAPSHOT_ID>

.venv/bin/python scripts/cleaning_create_batch.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --run-id <RUN_ID> \
  --max-posts 1000

.venv/bin/python scripts/cleaning_run_batch.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --batch-id <BATCH_ID> \
  --stage text_deterministic

.venv/bin/python scripts/cleaning_run_batch.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --batch-id <BATCH_ID> \
  --status

.venv/bin/python scripts/cleaning_resume_batch.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --batch-id <BATCH_ID> \
  --failed-only
```

这些入口不会回写源库；运行日志只输出运行/快照/批次/任务标识、状态、计数和哈希，不输出源路径、原始正文或作者标识。去标识化 manifest 也只保存逻辑文件名和路径身份哈希，完整本地路径仅保存在 Git 忽略的派生 SQLite 中。

`cleaning_run_batch.py --stage` 只负责短事务领取和检查点登记，不会把尚未接入的文本或图片算法伪装为成功。外部处理器完成计算后，应使用 `--finish-task <TASK_ID> --result <RESULT>` 逐任务提交结果；当前 Issue 不包含实际清洗算法。
