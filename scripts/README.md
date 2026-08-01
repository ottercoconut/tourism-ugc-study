# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用 `src/tourism_ugc_study/`。数据清洗、采样、模型和评估逻辑不得只存在于脚本或 notebook 中。

脚本保持平铺，并使用领域前缀；现有 `build_research_dataset.py` 暂时保留以维持兼容，但不属于 v2.4 正式清洗链。当前正式入口按职责分为：

```text
cleaning_snapshot_source.py     # 冻结只读源快照并初始化运行
cleaning_discover_increment.py  # 登记对象版本与待处理阶段
cleaning_create_batch.py        # 稳定冻结批次成员
cleaning_run_batch.py           # 领取、查询和推进通用任务
cleaning_resume_batch.py        # 显式恢复失败或阻塞任务
cleaning_process_text.py        # 确定性文本与重复候选
annotation_export_tasks.py      # 文本抽样与盲标任务导出
annotation_import_annotations.py # 文本原始标签与仲裁追加导入
annotation_adjudicate.py        # 一致性与泄漏分组
text_train_relevance.py         # 显式金标的 formal/smoke 线性基线
cleaning_process_images.py      # 图片 manifest、角色、指纹与候选
cleaning_review_images.py       # 图片双标、仲裁、决定、传播与审计
cleaning_release.py             # 发布构建、复验、状态与显式接受
```

入口只接收其职责所需的显式参数，不提供隐式“最新运行”。配置驱动的步骤接收 `--config`；运行、快照、批次、构建、评估和发布均使用相应显式 ID。随机种子从冻结配置与运行谱系读取，不允许在审计阶段临时换 seed；本地产物入口拒绝覆盖已有正式目录。

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

.venv/bin/python scripts/cleaning_review_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  create-review --candidate-build-id <IMAGE_CANDIDATE_BUILD_ID> \
  --kind candidate_review

.venv/bin/python scripts/cleaning_review_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  build-decisions --candidate-build-id <IMAGE_CANDIDATE_BUILD_ID>

.venv/bin/python scripts/cleaning_review_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  propagate-sha --decision-build-id <IMAGE_DECISION_BUILD_ID>

.venv/bin/python scripts/cleaning_review_images.py \
  --derived-db data/processed/cleaning.sqlite \
  --config configs/cleaning-v2.4.yaml \
  create-audit --decision-build-id <IMAGE_DECISION_BUILD_ID> --round-number 1

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

.venv/bin/python scripts/cleaning_release.py \
  --derived-db data/processed/cleaning.sqlite \
  build --run-id <RUN_ID> --release-id <RELEASE_ID> --release-mode formal \
  --post-decision-build-id <FINAL_POST_DECISION_BUILD_ID> \
  --text-dedup-build-id <TEXT_DEDUP_BUILD_ID> \
  --text-keep-audit-evaluation-id <TEXT_KEEP_AUDIT_EVALUATION_ID> \
  --image-decision-build-id <IMAGE_DECISION_BUILD_ID> \
  --image-keep-audit-evaluation-id <IMAGE_KEEP_AUDIT_EVALUATION_ID> \
  --output-root results/<RUN_ID>

.venv/bin/python scripts/cleaning_release.py \
  --derived-db data/processed/cleaning.sqlite \
  verify --run-id <RUN_ID> --release-id <RELEASE_ID> \
  --output-root results/<RUN_ID>

.venv/bin/python scripts/cleaning_release.py \
  --derived-db data/processed/cleaning.sqlite \
  accept-release --run-id <RUN_ID> --release-id <RELEASE_ID> \
  --output-root results/<RUN_ID>
```

这些入口不会回写源库；运行日志只输出运行/快照/批次/任务标识、状态、计数和哈希，不输出源路径、原始正文或作者标识。图片派生 SQLite 只保存 manifest 相对路径和根目录身份摘要，不保存完整本地路径；冻结源快照路径属于清洗运行基础设施字段，不会出现在图片 CLI 回执中。

`cleaning_run_batch.py --stage` 仍是通用的短事务领取和检查点入口，不会把领取伪装为成功。`cleaning_process_text.py process` 专门领取并执行 `text_deterministic`，从运行绑定的冻结快照读取正文，先幂等写入派生结果再完成任务；异常回执不输出原文、本地路径或作者。候选构建默认要求显式快照中的帖子全部已有规范化结果；只在需要观察批间进展时使用 `--allow-partial`，中间构建不会覆盖后续完整构建。

`text_train_relevance.py` 不提供隐式全量输入。formal 必须同时给出显式金标/泄漏构建和 `--execute-formal-training`；smoke 限制金标上限并永久标为 smoke。模型只写复核候选，不能覆盖追加式人工标签或形成最终排除。

`cleaning_process_images.py` 不联网下载或补图。没有 manifest 时省略 `--manifest-id`，对应图片任务会以 `blocked_by_manifest` 结束，而文本任务仍可继续；图片下载和 manifest 导入完成后，先使用 `cleaning_resume_batch.py` 显式恢复，再重新执行图片子命令。头像和整页证据只保留角色/跳过记录，只有 `content` 打开文件；所有输出均为技术候选，不是最终图片排除标签。

图片入口在每次写入或复用前重验冻结快照与配置。快照文件消失或读取失败时退出码为 1，stderr 只输出 `snapshot_unreadable` 的单行 JSON，不打印 traceback、绝对路径或底层 `OSError` 文本；参数解析错误仍使用 argparse 的退出码 2。

`cleaning_review_images.py` 只消费已封存的图片候选和人工 CSV：先创建
`candidate_review`，导出/import slot 1，再为拟排除或 `uncertain` 创建动态
slot 2；分歧经 `adjudicate` 追加仲裁。决定封存后只有 `propagate-sha` 能传播
`technical_noise_label`，pHash 仍只组织复核。保留集审计使用 `create-audit`、
`export-audit`、`import-audit`、`evaluate-audit`。三个仓库模板只定义列契约，
正式任务必须由具体运行导出；包含人工任务的填充文件不得提交 Git。

`cleaning_release.py build` 不自行挑选上游证据。调用者必须先通过核心仓储接口封存 candidate/final 帖子决定、文本保留集评估和分析去重构建，再显式提供五个上游构建/评估 ID。`build` 只生成不可变 `finalized` 包；`verify` 重算数据库成员、报告和磁盘 manifest；`accept-release` 再以 formal 证据、只读快照、零未决项、通过的文本/图片审计和一次性 connection guard 原子接受运行与发布。smoke 发布、缺少真实人工证据或任一 `review/blocked` 状态都不能 accepted。

完整工程验收使用：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```

2026-08-01 基线为 `332 passed, 8 warnings`；警告来自 joblib/NumPy 的既有弃用提示。该结果只证明工程契约，不代表正式人工标注、真实图片审计或 formal 发布已经完成；剩余正式工作见 Issue #12。
