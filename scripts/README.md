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

当前已实现只读源快照、增量发现、批次冻结、任务领取/状态查询、显式恢复、确定性文本、文本人工标注、相关性基线和本地图片框架入口：

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

.venv/bin/python scripts/cleaning_process_text.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --text-config configs/cleaning-text-normalization-v1.yaml \
  process --batch-id <BATCH_ID> --drain

.venv/bin/python scripts/cleaning_process_text.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --text-config configs/cleaning-text-normalization-v1.yaml \
  build-candidates --run-id <RUN_ID> --snapshot-id <SNAPSHOT_ID>

.venv/bin/python scripts/cleaning_resume_batch.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  --batch-id <BATCH_ID> \
  --failed-only

.venv/bin/python scripts/cleaning_process_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  import-manifest --run-id <RUN_ID> --snapshot-id <SNAPSHOT_ID> \
  --manifest <IMAGE_MANIFEST.csv> --image-root <LOCAL_IMAGE_ROOT>

.venv/bin/python scripts/cleaning_process_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  roles --batch-id <BATCH_ID> --manifest-id <IMAGE_MANIFEST_ID>

.venv/bin/python scripts/cleaning_process_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  fingerprints --batch-id <BATCH_ID> --manifest-id <IMAGE_MANIFEST_ID> \
  --image-root <LOCAL_IMAGE_ROOT>

.venv/bin/python scripts/cleaning_process_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  candidates --batch-id <BATCH_ID> --manifest-id <IMAGE_MANIFEST_ID>

.venv/bin/python scripts/annotation_export_tasks.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  create-initial --candidate-build-id <BUILD_ID>

.venv/bin/python scripts/annotation_adjudicate.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  build-leakage --candidate-build-id <BUILD_ID> \
  --duplicate-adjudication-ids <CONFIRMED_ID_FILE>

.venv/bin/python scripts/text_train_relevance.py \
  --derived-db data/processed/cleaning.sqlite \
  --candidate-build-id <BUILD_ID> \
  --leakage-build-id <LEAKAGE_ID> \
  --gold-adjudication-ids <GOLD_ID_FILE> \
  --artifact-directory results/<RUN_ID> \
  --config configs/cleaning-v2.4.yaml \
  formal --execute-formal-training
```

这些入口不会回写源库；运行日志只输出运行/快照/批次/任务标识、状态、计数和哈希，不输出源路径、原始正文或作者标识。图片派生 SQLite 只保存 manifest 相对路径和根目录身份摘要，不保存完整本地路径；冻结源快照路径属于清洗运行基础设施字段，不会出现在图片 CLI 回执中。

`cleaning_run_batch.py --stage` 仍是通用的短事务领取和检查点入口，不会把领取伪装为成功。`cleaning_process_text.py process` 专门领取并执行 `text_deterministic`，从运行绑定的冻结快照读取正文，先幂等写入派生结果再完成任务；异常回执不输出原文、本地路径或作者。候选构建默认要求显式快照中的帖子全部已有规范化结果；只在需要观察批间进展时使用 `--allow-partial`，中间构建不会覆盖后续完整构建。

`text_train_relevance.py` 不提供隐式全量输入。formal 必须同时给出显式金标/泄漏构建和 `--execute-formal-training`；smoke 限制金标上限并永久标为 smoke。模型只写复核候选，不能覆盖追加式人工标签或形成最终排除。

`cleaning_process_images.py` 不联网下载或补图。没有 manifest 时省略 `--manifest-id`，对应图片任务会以 `blocked_by_manifest` 结束，而文本任务仍可继续；图片下载和 manifest 导入完成后，先使用 `cleaning_resume_batch.py` 显式恢复，再重新执行图片子命令。头像和整页证据只保留角色/跳过记录，只有 `content` 打开文件；所有输出均为技术候选，不是最终图片排除标签。
