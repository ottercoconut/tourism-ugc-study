# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用
`src/tourism_ugc_study/`。正式数据清洗 v3.0 只处理帖子文本，不包含媒体文件
下载、检查、标注、筛选或发布；视觉模型由 `vision_*` 入口在后续研究阶段独立运行。

```text
cleaning_snapshot_source.py      # 冻结只读帖子快照并初始化运行
cleaning_discover_increment.py   # 登记帖子版本与待处理阶段
cleaning_create_batch.py         # 稳定冻结帖子批次
cleaning_run_batch.py            # 领取、查询和推进通用文本任务
cleaning_resume_batch.py         # 显式恢复失败或阻塞的文本任务
cleaning_process_text.py         # 确定性文本与重复候选
annotation_export_tasks.py       # 文本抽样与盲标任务导出
annotation_import_annotations.py # 文本原始标签与仲裁追加导入
annotation_adjudicate.py         # 一致性与泄漏分组
annotation_prepare_calibration.py # 共同校准主表规范化与问题队列提取
text_train_relevance.py          # 显式金标的 formal/smoke 线性基线
cleaning_release.py              # 帖子发布构建、复验、状态与显式接受
```

## 基本流程

```bash
.venv/bin/python scripts/cleaning_snapshot_source.py \
  --source-db <SOURCE_SQLITE> \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v3.0.yaml \
  --run-id <RUN_ID>

.venv/bin/python scripts/cleaning_discover_increment.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v3.0.yaml \
  --snapshot-id <SNAPSHOT_ID>

.venv/bin/python scripts/cleaning_create_batch.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v3.0.yaml \
  --run-id <RUN_ID> --max-posts 1000

.venv/bin/python scripts/cleaning_process_text.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v3.0.yaml \
  --text-config configs/cleaning-text-normalization-v1.yaml \
  process --batch-id <BATCH_ID> --drain

.venv/bin/python scripts/cleaning_process_text.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v3.0.yaml \
  --text-config configs/cleaning-text-normalization-v1.yaml \
  build-candidates --run-id <RUN_ID> --snapshot-id <SNAPSHOT_ID>

.venv/bin/python scripts/annotation_export_tasks.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v3.0.yaml \
  create-initial --candidate-build-id <BUILD_ID>

.venv/bin/python scripts/annotation_adjudicate.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v3.0.yaml \
  build-leakage --candidate-build-id <BUILD_ID> \
  --duplicate-adjudication-ids <CONFIRMED_ID_FILE>

.venv/bin/python scripts/annotation_prepare_calibration.py \
  data/annotations/round_<ID>/calibration-coding.csv \
  --labels-output data/annotations/round_<ID>/labels.csv \
  --issues-output data/annotations/round_<ID>/calibration-issues.csv \
  --codebook-version v3.6.1

.venv/bin/python scripts/text_train_relevance.py \
  --derived-db data/processed/cleaning.sqlite \
  --candidate-build-id <BUILD_ID> \
  --leakage-build-id <LEAKAGE_ID> \
  --gold-adjudication-ids <GOLD_ID_FILE> \
  --artifact-directory results/<RUN_ID> \
  --config configs/cleaning-v3.0.yaml \
  formal --execute-formal-training

.venv/bin/python scripts/cleaning_release.py \
  --derived-db data/processed/cleaning.sqlite \
  build --run-id <RUN_ID> --release-id <RELEASE_ID> --release-mode formal \
  --post-decision-build-id <FINAL_POST_DECISION_BUILD_ID> \
  --text-dedup-build-id <TEXT_DEDUP_BUILD_ID> \
  --text-keep-audit-evaluation-id <TEXT_KEEP_AUDIT_EVALUATION_ID> \
  --output-root results/<RUN_ID>
```

所有入口使用显式 ID，不提供“最新运行”回退，不回写源库，也不在日志中输出
原始正文、作者标识或源路径。发布只包含 `analysis_posts_eligible` 和
`analysis_posts_deduplicated` 两个帖子集合；`accept-release` 会复验只读快照、
必需任务、文本保留集审计、数据库成员和不可变本地包。

完整工程验收：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```
