# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用 `src/tourism_ugc_study/`。可复用规则、持久化、训练、策略和状态机逻辑必须留在 `src/`。

> **数据清洗状态**：`FRAMEWORK_FROZEN / REFERENCE_DEDUP_FINALIZED / THRESHOLD_PENDING`。唯一最终700条及其 finalized manifest 已通过验证，正式 leakage build 已封存；baseline 训练尚未执行。

参考生成器不复用旧派生库中的模型文本，而是校验候选构建绑定的冻结源快照哈希并重新规范化。Quill Delta JSON 只提取字符串 `insert`；格式属性和非文本嵌入不进入候选或训练。最终验证器和训练入口都必须加载同一冻结规范化配置，从无 SQLite 旁文件的源快照重新投影全部候选人口，核对源快照哈希、投影成员哈希及最终700行正文后，训练才使用最终 CSV 的 `normalized_model_text`。

## 现有入口清单

```text
annotation_adjudicate.py          # 确认重复关系的泄漏分组
annotation_build_reference.py     # 生成候选、冻结候补队列并封存最终700条参考集
annotation_prepare_calibration.py # 研究内容共同校准主表转换
cleaning_validate_reference.py   # 只读校验唯一最终700条 CSV＋finalized manifest
cleaning_train_baseline.py        # 训练并封存字符 TF-IDF＋线性 SVM＋折外 Sigmoid baseline
```

仓库不提供旧协议配置、批处理、标签导入或发布入口。既有派生库仅作为700条
参考生成谱系和泄漏关系的只读/追加式来源，当前入口不会为旧协议建库、迁移或
恢复运行。

清洗不包含媒体文件下载、检查、标注、筛选或发布；视觉模型由 `vision_*` 入口在后续研究阶段独立运行。

## 已冻结的正式接口边界

后续代码阶段必须把训练、阈值策略和推理解耦：

1. **参考集入口**由 `annotation_build_reference.py` 实现。它分阶段生成完整全对重复复核 CSV、固定种子全局候补队列和唯一最终700条 CSV＋manifest；配置解析、候选计算、人工证据、候补调度和 artifact 持久化位于独立模块。
2. **训练入口**由 `cleaning_train_baseline.py` 实现。它只接收 `final-nonduplicate-model-reference` CSV、唯一配对 `finalized` manifest、只读派生库和 finalized leakage build；旧完成 CSV、任何中间 CSV、非700条、重复成员或非 finalized manifest 均被拒绝。
3. **阈值策略入口**独立保存 `T_keep`、`T_exclude`、保留集审计门、测试指标门、自动覆盖率门和恢复门；改变策略不调用训练。
4. **推理入口**显式接收冻结模型、冻结策略和目标批次；初始全量与未来新增批次使用同一入口，且不得调用 `fit`。
5. **发布入口**只读取最终帖子决定。`exclude` 只影响派生分析发布，正式采集库始终只读。

稳定配置入口为 `configs/cleaning.yaml`，所有阈值与门均为 `UNSET`；因此 baseline 训练完成后也只能形成研究性概率证据，不得产生正式自动保留或自动排除。`cleaning_validate_reference.py` 和 `cleaning_train_baseline.py` 都只读打开派生库，并拒绝已经导入 `text_post_annotations` 的参考标签。

## 参考集与 baseline 执行顺序

先对现有700条生成候选。该命令只读数据库；人工必须填写输出 CSV 的 `decision` 与 `review_status`，不得由程序猜测：

```bash
.venv/bin/python scripts/annotation_build_reference.py build-duplicate-review \
  --legacy-csv <旧完成.csv> --legacy-manifest <旧manifest.json> \
  --derived-db <derived.sqlite> \
  --output-csv <duplicate-decisions-pending.csv> \
  --output-manifest <duplicate-decisions-pending.manifest.json>
```

待处理 CSV 本身不可覆盖。复制为新的完成 CSV，填写全部决定后追加 finalized manifest：

```bash
.venv/bin/python scripts/annotation_build_reference.py seal-duplicate-decisions \
  --completed-csv <duplicate-decisions-completed.csv> \
  --pending-manifest <duplicate-decisions-pending.manifest.json> \
  --output-manifest <duplicate-decisions-finalized.manifest.json>
```

随后生成并关闭重复分量的旅游标签冲突任务。没有冲突时仍封存只有表头的空完成 CSV，以证明该门已关闭：

```bash
.venv/bin/python scripts/annotation_build_reference.py build-label-conflict-review \
  --legacy-csv <旧完成.csv> --legacy-manifest <旧manifest.json> \
  --derived-db <derived.sqlite> \
  --duplicate-decisions-csv <duplicate-decisions-completed.csv> \
  --duplicate-decisions-manifest <duplicate-decisions-finalized.manifest.json> \
  --output-csv <label-conflicts-pending.csv> \
  --output-manifest <label-conflicts-pending.manifest.json>

.venv/bin/python scripts/annotation_build_reference.py seal-label-resolutions \
  --completed-csv <label-conflicts-completed.csv> \
  --pending-manifest <label-conflicts-pending.manifest.json> \
  --output-manifest <label-conflicts-finalized.manifest.json>
```

如确认重复造成缺口，先冻结候补队列，再根据队列 manifest 记录的
`probability_gap`、`targeted_gap` 选择足以覆盖缺口和备用量的冻结前缀做联合全对检查：

```bash
.venv/bin/python scripts/annotation_build_reference.py build-replacement-queue \
  --legacy-csv <旧完成.csv> --legacy-manifest <旧manifest.json> \
  --derived-db <derived.sqlite> \
  --duplicate-decisions-csv <duplicate-decisions-completed.csv> \
  --duplicate-decisions-manifest <duplicate-decisions-finalized.manifest.json> \
  --label-resolutions-csv <label-conflicts-completed.csv> \
  --label-resolutions-manifest <label-conflicts-finalized.manifest.json> \
  --output-csv <replacement-queue.csv> \
  --output-manifest <replacement-queue.manifest.json>

.venv/bin/python scripts/annotation_build_reference.py build-replacement-duplicate-review \
  --legacy-csv <旧完成.csv> --legacy-manifest <旧manifest.json> \
  --derived-db <derived.sqlite> \
  --duplicate-decisions-csv <duplicate-decisions-completed.csv> \
  --duplicate-decisions-manifest <duplicate-decisions-finalized.manifest.json> \
  --label-resolutions-csv <label-conflicts-completed.csv> \
  --label-resolutions-manifest <label-conflicts-finalized.manifest.json> \
  --max-queue-rank <N> \
  --output-csv <replacement-duplicate-decisions-pending.csv> \
  --output-manifest <replacement-duplicate-decisions-pending.manifest.json>
```

候补重复决定也须复制待处理 CSV、完成人工判断，并用 `seal-duplicate-decisions` 追加新的 finalized manifest。即使本轮没有近重复候选，也要封存表头为空的完成副本，证明联合检查已关闭。扩展前缀可能把前轮候补标签或既有代表标签连接到同一重复分量，因此须先生成并封存候补分量标签冲突；没有冲突时同样封存表头为空的完成副本：

```bash
.venv/bin/python scripts/annotation_build_reference.py build-replacement-label-conflict-review \
  --legacy-csv <旧完成.csv> --legacy-manifest <旧manifest.json> \
  --derived-db <derived.sqlite> \
  --duplicate-decisions-csv <duplicate-decisions-completed.csv> \
  --duplicate-decisions-manifest <duplicate-decisions-finalized.manifest.json> \
  --label-resolutions-csv <label-conflicts-completed.csv> \
  --label-resolutions-manifest <label-conflicts-finalized.manifest.json> \
  --replacement-duplicate-decisions-csv <replacement-duplicate-decisions-completed.csv> \
  --replacement-duplicate-decisions-manifest <replacement-duplicate-decisions-finalized.manifest.json> \
  --max-queue-rank <N> \
  --prior-supplemental-labels-csv <上一轮补充标签.csv> \
  --prior-supplemental-labels-manifest <上一轮补充标签.manifest.json> \
  --output-csv <replacement-label-conflicts-pending.csv> \
  --output-manifest <replacement-label-conflicts-pending.manifest.json>

.venv/bin/python scripts/annotation_build_reference.py seal-label-resolutions \
  --completed-csv <replacement-label-conflicts-completed.csv> \
  --pending-manifest <replacement-label-conflicts-pending.manifest.json> \
  --output-manifest <replacement-label-conflicts-finalized.manifest.json>
```

首轮尚无补充标签时省略两个 `--prior-supplemental-labels-*` 参数；扩展前缀时才传入上一轮配对 finalized 证据。

冲突门关闭后，程序按稳定连通分量选出无标签任务；这一阶段不读取候补旅游标签：

```bash
.venv/bin/python scripts/annotation_build_reference.py build-supplemental-labels \
  --legacy-csv <旧完成.csv> --legacy-manifest <旧manifest.json> \
  --derived-db <derived.sqlite> \
  --duplicate-decisions-csv <duplicate-decisions-completed.csv> \
  --duplicate-decisions-manifest <duplicate-decisions-finalized.manifest.json> \
  --label-resolutions-csv <label-conflicts-completed.csv> \
  --label-resolutions-manifest <label-conflicts-finalized.manifest.json> \
  --replacement-duplicate-decisions-csv <replacement-duplicate-decisions-completed.csv> \
  --replacement-duplicate-decisions-manifest <replacement-duplicate-decisions-finalized.manifest.json> \
  --replacement-label-resolutions-csv <replacement-label-conflicts-completed.csv> \
  --replacement-label-resolutions-manifest <replacement-label-conflicts-finalized.manifest.json> \
  --max-queue-rank <N> --reserve-count <R> \
  --output-csv <supplemental-labels-pending.csv> \
  --output-manifest <supplemental-labels-pending.manifest.json>

.venv/bin/python scripts/annotation_build_reference.py seal-supplemental-labels \
  --completed-csv <supplemental-labels-completed.csv> \
  --pending-manifest <supplemental-labels-pending.manifest.json> \
  --output-manifest <supplemental-labels-finalized.manifest.json>
```

结构不可用、确认重复或标签为 `uncertain` 的记录不计数；候补不足时扩大
`max-queue-rank` 并创建新一轮不可变 artifact，同时用
`--prior-supplemental-labels-csv` 和 `--prior-supplemental-labels-manifest`
传入上一轮 finalized 证据。全局候补队列始终在任何候补标签前冻结；`related`
与 `unrelated` 的类别取值不参与队列顺序或样本框分配，只以任务是否已经形成可计数
的二元终止状态决定是否继续扩轮。若去重后没有
缺口，使用 `--max-queue-rank 0` 封存规范空的候补重复、候补标签冲突与补充标签链路。所有缺口关闭后运行：

```bash
.venv/bin/python scripts/annotation_build_reference.py finalize \
  --legacy-csv <旧完成.csv> --legacy-manifest <旧manifest.json> \
  --derived-db <derived.sqlite> \
  --duplicate-decisions-csv <duplicate-decisions-completed.csv> \
  --duplicate-decisions-manifest <duplicate-decisions-finalized.manifest.json> \
  --label-resolutions-csv <label-conflicts-completed.csv> \
  --label-resolutions-manifest <label-conflicts-finalized.manifest.json> \
  --replacement-duplicate-decisions-csv <replacement-duplicate-decisions-completed.csv> \
  --replacement-duplicate-decisions-manifest <replacement-duplicate-decisions-finalized.manifest.json> \
  --replacement-label-resolutions-csv <replacement-label-conflicts-completed.csv> \
  --replacement-label-resolutions-manifest <replacement-label-conflicts-finalized.manifest.json> \
  --supplemental-labels-csv <supplemental-labels-completed.csv> \
  --supplemental-labels-manifest <supplemental-labels-finalized.manifest.json> \
  --output-csv <final-reference.csv> \
  --output-manifest <final-reference.manifest.json>
```

只有 `cleaning_validate_reference.py` 验证最终 CSV 恰好700条、500/200、标签完整、身份唯一且确认重复对为零后，才能继续 leakage build。任何 pending 文件都不能当作最终决定或训练输入。

最终700条形成后，对同一完整候选构建封存 leakage build。同一作者的不同帖子仍不能跨训练、验证和测试集合；精确簇也必须同组。接口仅在显式提供数据库 finalized 仲裁 ID 时加入确认近重复边。本次最终700条内部确认重复对为0，因此实际 finalized build 使用作者边与精确簇边，没有伪造或隐式导入近重复仲裁。

```bash
.venv/bin/python scripts/annotation_adjudicate.py \
  --derived-db <derived.sqlite> \
  build-leakage \
  --candidate-build-id <candidate_build_id>
```

取得输出的 `leakage_build_id` 后，显式执行首次 baseline 训练：

```bash
.venv/bin/python scripts/cleaning_train_baseline.py \
  --config configs/cleaning.yaml \
  --csv <final-reference.csv> \
  --manifest <final-reference.manifest.json> \
  --derived-db <derived.sqlite> \
  --leakage-build-id <leakage_build_id> \
  --artifact-root results/cleaning-models \
  --execute-training
```

该命令封存模型、校准器、切分 manifest、训练折外概率、验证概率和验证指标。测试成员只锁定为 `locked_not_opened`。下一阶段先用开发证据讨论阈值和验收门；策略冻结后才允许单次开启测试集。两项阈值仍为 `UNSET`，因此本命令不生成自动决定。

## 通用运行要求

所有入口使用显式 ID，不提供“最新运行”回退，不覆盖已有运行，不回写源库，也不在日志中输出原始正文、作者标识或源路径。正式运行必须保存配置、随机种子、Git SHA、输入与输出 manifest、artifact 哈希和机器可读状态。

代码实现后的完整验收至少包括：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```
