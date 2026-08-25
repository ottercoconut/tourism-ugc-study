# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用 `src/tourism_ugc_study/`。可复用规则、持久化、训练、策略和状态机逻辑必须留在 `src/`。

> **数据清洗状态**：`RESEARCHER_SELECTED_ROUTING_FROZEN / ROUTING_DELIVERABLE_READY`。旧Qwen `0.14/0.86` 的锁定测试失败和新模型0.44/0.96的单尾审计结论保持不变。研究者另以公开配置明确接受选择后风险，冻结融合模型与0.31/0.96；现有概率已完成零预测重分流，中间层四列表和完整派生决定均已封存。

参考生成器不复用旧派生库中的模型文本，而是校验候选构建绑定的冻结源快照哈希并重新规范化。Quill Delta JSON 只提取字符串 `insert`；格式属性和非文本嵌入不进入候选或训练。最终验证器和训练入口都必须加载同一冻结规范化配置，从无 SQLite 旁文件的源快照重新投影全部候选人口，核对源快照哈希、投影成员哈希及最终700行正文后，训练才使用最终 CSV 的 `normalized_model_text`。

## 现有入口清单

```text
annotation_adjudicate.py          # 确认重复关系的泄漏分组
annotation_build_reference.py     # 生成候选、冻结候补队列并封存最终700条参考集
annotation_prepare_calibration.py # 研究内容共同校准主表转换
cleaning_validate_reference.py   # 只读校验唯一最终700条 CSV＋finalized manifest
cleaning_train_baseline.py        # 训练并封存字符 TF-IDF＋线性 SVM＋折外 Sigmoid baseline
cleaning_analyze_baseline_errors.py # 只分析训练 OOF 与验证误差，不读取锁定测试
cleaning_blind_label_review.py   # 生成、汇总并显式应用隐藏模型答案的标签一致性复核
cleaning_train_sparse_challenger.py # 训练54候选、封存 paired OOF 并执行 UGC 安全验收
cleaning_validate_sparse_challenger.py # 唯一候选一次性无拟合验证方向复核
cleaning_evaluate_model_acceptance.py # 按 UGC 安全优先硬门评估配对 nested OOF
cleaning_prepare_qwen_embedding.py # 下载/校验固定公开权重并运行合成文本烟雾测试
cleaning_train_qwen_embedding_baseline.py # 训练固定语义 baseline 并与 sparse OOF 配对验收
cleaning_model_reliability.py # 两个旧模型的纯预测人口框、Wave A盲标与加权评价
cleaning_retrain_routing_model.py # 1,300条重训、路由选择、双尾审计及全量/新增批次纯预测
```

Issue #49 的前两步使用同一薄CLI。快照命令只读联结私有标签和派生库；编码命令
只读取已封存快照与仓库外公开权重，逐记录写checkpoint，恢复时验证成员顺序、
正文哈希、向量哈希和计划身份：

```bash
.venv/bin/python scripts/cleaning_retrain_routing_model.py freeze-snapshot \
  --final-reference-csv <final-reference.csv> \
  --wave-a-completed-csv <wave-a-completed.csv> \
  --wave-a-private-map <wave-a-private-map.json> \
  --wave-b-completed-csv <wave-b-completed.csv> \
  --wave-b-private-map <wave-b-private-map.json> \
  --derived-db <cleaning.sqlite> --split-manifest <old-split-manifest.json> \
  --artifact-root results/cleaning-model-retraining-snapshot \
  --execute-snapshot-freeze

.venv/bin/python scripts/cleaning_retrain_routing_model.py encode-qwen \
  --snapshot-package <frozen-snapshot-package> \
  --expected-snapshot-manifest-sha256 <sha256> \
  --model-dir ../models/Qwen3-Embedding-4B \
  --artifact-root results/cleaning-model-retraining-embeddings \
  --execute-qwen-encoding
```

编码中断后原命令即可续跑；完整结束后的再次调用必须额外提供
`--expected-existing-manifest-sha256`，否则拒绝把目录存在误当成成功。编码入口
没有标签或fit参数，最终要求全体记录的省略token合计严格为0。

编码封存后依次执行固定候选重训和策略选择。两步都要求外部manifest哈希；路由
命令只读取Wave B成员的OOF概率与设计权重，不重新编码、不重新训练，也不打开
旧148条测试：

```bash
.venv/bin/python scripts/cleaning_retrain_routing_model.py train-candidates \
  --snapshot-package <frozen-snapshot-package> \
  --expected-snapshot-manifest-sha256 <sha256> \
  --embedding-package <qwen-embedding-package> \
  --expected-embedding-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining \
  --execute-candidate-training

.venv/bin/python scripts/cleaning_retrain_routing_model.py freeze-routing \
  --training-package <fixed-candidate-package> \
  --expected-training-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining-routing \
  --execute-routing-freeze
```

唯一模型和阈值冻结后，纯预测、盲审和最终决定严格分开。`score-population`对
12,558条精确待处理人口提供逐条checkpoint；若唯一候选是sparse，不加载Qwen
权重。审计任务仍是UTF-8 BOM四列，人工只填最后一列：

```bash
.venv/bin/python scripts/cleaning_retrain_routing_model.py score-population \
  --derived-db data/processed/cleaning.sqlite \
  --snapshot-package <frozen-snapshot-package> \
  --expected-snapshot-manifest-sha256 <sha256> \
  --policy-package <routing-policy-package> \
  --expected-policy-manifest-sha256 <sha256> \
  --model-dir ../models/Qwen3-Embedding-4B \
  --artifact-root results/cleaning-model-retraining-inference \
  --execute-prediction

# 新增批次CSV严格六列：source_post_id,source_version,component_id,
# title,body,source_status；程序内规范化，自动生成中间层盲四列表。
.venv/bin/python scripts/cleaning_retrain_routing_model.py score-new-batch \
  --input-csv <new-records.csv> \
  --snapshot-package <frozen-snapshot-package> \
  --expected-snapshot-manifest-sha256 <sha256> \
  --policy-package <researcher-policy-package> \
  --expected-policy-manifest-sha256 <sha256> \
  --model-dir ../models/Qwen3-Embedding-4B \
  --artifact-root results/cleaning-model-retraining-incremental \
  --execute-prediction

.venv/bin/python scripts/cleaning_retrain_routing_model.py prepare-audit \
  --inference-package <inference-package> \
  --expected-inference-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining-audit \
  --execute-audit-sampling

.venv/bin/python scripts/cleaning_retrain_routing_model.py assess-audit \
  --completed-csv <tourism-relevance-routing-audit-completed.csv> \
  --task-package <audit-task-package> \
  --expected-task-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining-audit-assessment \
  --execute-audit-assessment

.venv/bin/python scripts/cleaning_retrain_routing_model.py build-decisions \
  --snapshot-package <frozen-snapshot-package> \
  --expected-snapshot-manifest-sha256 <sha256> \
  --inference-package <inference-package> \
  --expected-inference-manifest-sha256 <sha256> \
  --audit-assessment-package <audit-assessment-package> \
  --expected-audit-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining-decisions \
  --execute-final-decisions

.venv/bin/python scripts/cleaning_retrain_routing_model.py explore-threshold-grid \
  --inference-package <inference-package> \
  --expected-inference-manifest-sha256 <sha256> \
  --training-package <fixed-candidate-package> \
  --expected-training-manifest-sha256 <sha256> \
  --audit-assessment-package <audit-assessment-package> \
  --expected-audit-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining-threshold-grid \
  --execute-threshold-exploration
```

研究者确认网格点后，以下四步形成新的不可变交付谱系。第一步复制原冻结模型的
精确字节；第二步只重算既有概率对应动作；第三步产出尚无人工作答的人工中间层
四列表；第四步合并1,300条训练标签、300条审计标签和两个自动尾部。每次严格
复用都必须追加对应的 `--expected-existing-manifest-sha256`：

```bash
.venv/bin/python scripts/cleaning_retrain_routing_model.py freeze-researcher-policy \
  --base-policy-package <base-policy-package> \
  --threshold-grid-package <threshold-grid-package> \
  --audit-assessment-package <audit-assessment-package> \
  --artifact-root results/cleaning-model-retraining-delivery-policy \
  --execute-researcher-policy-freeze

.venv/bin/python scripts/cleaning_retrain_routing_model.py reroute-existing-probabilities \
  --inference-package <existing-inference-package> \
  --policy-package <researcher-policy-package> \
  --expected-policy-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining-delivery-rerouting \
  --execute-probability-rerouting

.venv/bin/python scripts/cleaning_retrain_routing_model.py prepare-manual-review \
  --rerouting-package <rerouting-package> \
  --expected-rerouting-manifest-sha256 <sha256> \
  --audit-assessment-package <audit-assessment-package> \
  --artifact-root results/cleaning-model-retraining-manual-review \
  --execute-manual-review-task

.venv/bin/python scripts/cleaning_retrain_routing_model.py build-delivery-decisions \
  --snapshot-package <frozen-snapshot-package> \
  --rerouting-package <rerouting-package> \
  --expected-rerouting-manifest-sha256 <sha256> \
  --manual-review-package <manual-review-package> \
  --expected-manual-manifest-sha256 <sha256> \
  --audit-assessment-package <audit-assessment-package> \
  --policy-package <researcher-policy-package> \
  --expected-policy-manifest-sha256 <sha256> \
  --artifact-root results/cleaning-model-retraining-delivery-decisions \
  --execute-delivery-decisions
```

任一命令缺少显式执行开关都会失败关闭。审计失败后没有重抽入口；`build-decisions`
按尾部独立降级，300条审计成员始终由人工标签覆盖。正式采集数据库全程只读，
最终决定只写入Git忽略的派生artifact。

历史审计判读 `b618ae76cc6328f776a87d4f5db89092` 为排除端0/150通过、
保留端11/150失败；历史决定 `910b544d4dc6310751a262406ccbe3fe` 只启用
`auto_exclude`，因8,397条人工而被研究者拒绝采用。这些结论不改写。

`explore-threshold-grid` 固定输出49×49共2,401组聚合结果，不含正文、身份或逐条
概率。每行包含12,558条人口三段数量、Wave B原始/设计加权风险、300条审计诊断
和抽样支持标记；推荐只作非冻结建议，`fit_call_count=predict_call_count=0`。
正式探索 `8df3dcac9a2985ac53314a86629f88fc` 的非冻结推荐为0.15/0.96；研究者
最终选择0.31/0.96。策略 `8c87b85cd44803b9451c825bacaa3e90` 与重分流
`06bdab80b623f51352582eb911d473f3` 对12,558条给出6,255条自动保留、2,292条
中间层和4,011条自动排除。人工任务 `ab6fae6ceab29bcc0254f84d49278fde`
排除已有审计标签后稳定包含2,286条；文件名为
`manual-review-tourism-relevance-annotation.csv`，UTF-8 BOM固定四列，人工只填
`tourism_label`。交付决定 `e79dc72751e0c7a1af75ee2f1dfbb1d7` 的完整动作是
keep 6,835、exclude 4,737、manual_review 2,286；源库写入和删除均为0。

仓库不提供旧协议配置、批处理、标签导入或发布入口。既有派生库仅作为700条
参考生成谱系和泄漏关系的只读/追加式来源，当前入口不会为旧协议建库、迁移或
恢复运行。

清洗不包含媒体文件下载、检查、标注、筛选或发布；视觉模型由 `vision_*` 入口在后续研究阶段独立运行。

## 已冻结的正式接口边界

后续代码阶段必须把训练、阈值策略和推理解耦：

1. **参考集入口**由 `annotation_build_reference.py` 实现。它分阶段生成完整全对重复复核 CSV、固定种子全局候补队列和唯一最终700条 CSV＋manifest；配置解析、候选计算、人工证据、候补调度和 artifact 持久化位于独立模块。
2. **baseline 训练入口**由 `cleaning_train_baseline.py` 实现。它只接收 `final-nonduplicate-model-reference` CSV、唯一配对 `finalized` manifest、只读派生库和 finalized leakage build；旧完成 CSV、任何中间 CSV、非700条、重复成员或非 finalized manifest 均被拒绝。
3. **sparse challenger 训练入口**由 `cleaning_train_sparse_challenger.py` 实现。它严格绑定当前 baseline 包、54候选计划与 UGC 安全策略，只物化冻结训练成员，输出 paired nested OOF、唯一候选和训练侧验收；不接收验证、测试、平台或阈值参数。
4. **sparse challenger 验证入口**由 `cleaning_validate_sparse_challenger.py` 实现。它只接受训练侧验收通过的唯一候选，只调用一次概率预测；相同输入以后只复用不可变结果，不再预测。入口不含候选、超参数、测试或阈值参数。
5. **Qwen 公开权重入口**由 `cleaning_prepare_qwen_embedding.py` 实现。它不接收研究数据，只下载/校验固定 revision 和权重哈希，并可编码两条内置合成文本。
6. **Qwen 语义 baseline 训练入口**由 `cleaning_train_qwen_embedding_baseline.py` 实现。它只编码冻结训练442条，以唯一逻辑回归头生成 leakage-group OOF，并与已封存 sparse candidate OOF 配对验收；不接收验证、测试、平台、阈值或审计参数。
7. **阈值策略入口**独立保存 `T_keep`、`T_exclude`、保留集审计门、测试指标门、自动覆盖率门和恢复门；改变策略不调用训练。
8. **推理入口**显式接收冻结模型、冻结策略和目标批次；初始全量与未来新增批次使用同一入口，且不得调用 `fit`。
9. **发布入口**只读取最终帖子决定。`exclude` 只影响派生分析发布，正式采集库始终只读。

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

默认输出中文多行报告，直接显示切分数量、验证准确率的分子/分母、两类错误、校准指标、锁定测试状态和 artifact 哈希。自动化调用可追加 `--output-format json` 保留机器可读 JSON。

该命令封存模型、校准器、切分 manifest、训练折外概率、验证概率和验证指标。测试成员只锁定为 `locked_not_opened`。报告中的 `0.5` 仅为验证诊断分界，不是路由阈值，也不自动给出模型通过或失败结论。开发 challenger 验收由独立的 UGC 安全优先策略执行；路由阈值、最终测试门和审计门冻结后才允许单次开启测试集。两项阈值仍为 `UNSET`，因此本命令不生成自动决定。

训练完成后，只读生成类别、固定文本长度层和概率区间的开发误差分析：

```bash
.venv/bin/python scripts/cleaning_analyze_baseline_errors.py \
  --package-dir <baseline-package-dir> \
  --reference-csv <final-reference.csv> \
  --error-coding <finalized-error-type-coding.json> \
  --output <development-error-analysis.json>
```

输出中的逐条误判只含绑定模型的不可逆 `review_key`、集合、真实/预测标签、概率、文本长度和人工类型码，不复制正文、作者、平台或源身份。人工编码必须覆盖该模型在训练 OOF 与验证集合的全部误判，且只能引用分析产生的完整 `review_key` 集合。该入口拒绝包含测试概率、测试已开启或阈值已设置的训练包。

进入 challenger 前，生成一次隐藏原标签与模型答案的一致性任务。输出目录必须尚不存在；任务只使用训练 OOF 与验证开发概率：

```bash
.venv/bin/python scripts/cleaning_blind_label_review.py prepare \
  --package-dir <baseline-package-dir> \
  --reference-csv <final-reference.csv> \
  --output-dir <private-blind-review-dir>
```

人工只编辑 `<private-blind-review-dir>/review-task.csv` 的 `review_label`，可选填写 `review_note`；不得修改 `review_key`、`review_text`，不得查看同目录的 `review-map.json`。完成全部68条后运行：

```bash
.venv/bin/python scripts/cleaning_blind_label_review.py summarize \
  --review-dir <private-blind-review-dir> \
  --output <blind-review-summary.json>
```

汇总入口先校验任务固定内容和私有映射，再分别报告模型矛盾目标与随机正确对照的维持、修改和不确定计数。输出状态固定为 `completed_not_applied`；程序不会自动改写最终参考 CSV。定向矛盾组不得用于估计700条或候选人口总体标签错误率。

只有在逐条人工仲裁并获得用户明确批准后，才可把获批的 `review_key` 原位应用到唯一最终 CSV＋manifest。必须同时绑定操作前 CSV 哈希并传入执行开关；未获批的改标会作为“维持原标签”写入 manifest 谱系：

```bash
.venv/bin/python scripts/cleaning_blind_label_review.py apply \
  --review-dir <private-blind-review-dir> \
  --summary <blind-review-summary.json> \
  --reference-csv <final-reference.csv> \
  --reference-manifest <final-reference.manifest.json> \
  --output-receipt <blind-review-application.json> \
  --expected-reference-csv-sha256 <sha256-before-update> \
  --approved-review-key <approved-key-1> \
  --approved-review-key <approved-key-2> \
  --execute-reference-update
```

`apply` 会重新校验已完成任务、私有映射、汇总、当前700条标签和操作前哈希，随后更新标签计数与所有输出摘要并生成去敏回执；它不写数据库、不读取测试成员或概率，也不修改模型、阈值或自动清洗决定。更新后必须重新运行最终参考验证器，并以新证据身份重新训练 baseline。

标签证据和 baseline 重新封存后，正式 challenger 使用以下单一入口。该命令不会接收或预测验证/测试集合；它在训练侧5×4嵌套 leakage-group 结构中比较预登记54候选，封存唯一候选和 paired outer OOF，并立即执行冻结的 UGC 安全验收：

```bash
.venv/bin/python scripts/cleaning_train_sparse_challenger.py \
  --config configs/cleaning.yaml \
  --plan configs/cleaning-text-challenger.yaml \
  --acceptance-policy configs/cleaning-model-acceptance.yaml \
  --csv <final-reference.csv> \
  --manifest <final-reference.manifest.json> \
  --derived-db <derived.sqlite> \
  --baseline-package <baseline-package-dir> \
  --artifact-root results/cleaning-challenger \
  --execute-training
```

默认中文报告优先显示唯一候选、`related→unrelated` 安全诊断、log loss、PR-AUC、验收结论及验证/测试/阈值边界；自动化可追加 `--output-format json`。输出状态为 `passed` 时只授权该唯一候选进入一次验证方向性复核；`failed_retain_baseline` 时不得读取验证来挽救候选。

运行包中的 `paired-outer-oof.json` 也符合独立验收入口契约，可在需要重建聚合报告时另行调用；输出路径必须尚不存在：

```bash
.venv/bin/python scripts/cleaning_evaluate_model_acceptance.py \
  --policy configs/cleaning-model-acceptance.yaml \
  --evidence <paired-nested-oof-comparison.json> \
  --output <model-acceptance-report.json>
```

证据只允许训练侧成员，必须标明 `paired_outer_folds=true`、`test_members_read=false`、`test_probabilities_present=false`、`platform_used=false`。验收顺序固定为 `related→unrelated` 安全硬门 → log loss 明确改善 → PR-AUC 不劣 → Brier 不劣；重采样单位为 leakage component。任一门失败都输出 `failed_retain_baseline`。通过仅授权唯一候选进入一次验证方向性复核，不打开锁定测试，不设置路由阈值。

当前运行 `ce19406cd132e55b2eb00531f5cc4cd3` 已通过训练侧门，唯一候选模型 ID 为 `1b68baa8bef99d6b9d75b7bf3226cfb4`。验证前先提交并保持代码身份不变，再由用户显式执行：

```bash
.venv/bin/python scripts/cleaning_validate_sparse_challenger.py \
  --config configs/cleaning.yaml \
  --plan configs/cleaning-text-challenger.yaml \
  --acceptance-policy configs/cleaning-model-acceptance.yaml \
  --csv data/annotations/private/final-reference.csv \
  --manifest data/annotations/private/final-reference.manifest.json \
  --derived-db data/processed/cleaning.sqlite \
  --baseline-package results/cleaning-baseline/9cd30922aabf7fb2e2ba42e5a0396cfd \
  --challenger-package results/cleaning-challenger/ce19406cd132e55b2eb00531f5cc4cd3 \
  --artifact-root results/cleaning-challenger-validation \
  --execute-validation
```

该入口只比较四个点估计方向：UGC 误排率与 log loss、Brier 不升，unrelated PR-AUC 不降；输出 `directionally_consistent` 或 `mixed_or_reversed`，不另设显著性门，也不把验证方向描述写成锁定测试通过。验证 artifact 记录 `validation_access_count=1`、`candidate_prediction_calls=1`、`may_expand_search=false`、`test_status=locked_not_opened` 和 `threshold_status=UNSET`。

Qwen 语义 baseline 的公开权重先放在仓库相邻目录。该命令不读取任何研究数据；目录存在时只做身份校验，追加 `--download` 才允许缺失时下载固定 revision，追加 `--smoke-test` 只编码两条内置合成文本：

```bash
.venv/bin/python -m pip install -e '.[semantic]'
.venv/bin/python scripts/cleaning_prepare_qwen_embedding.py \
  --model-dir ../Qwen3-Embedding-4B \
  --download \
  --smoke-test
```

权重就绪不表示已训练。只有用户显式确认后，才执行唯一语义 baseline 的训练 OOF：

```bash
.venv/bin/python scripts/cleaning_train_qwen_embedding_baseline.py \
  --config configs/cleaning.yaml \
  --plan configs/cleaning-qwen-embedding-baseline.yaml \
  --acceptance-policy configs/cleaning-qwen-model-acceptance.yaml \
  --sparse-plan configs/cleaning-text-challenger.yaml \
  --csv data/annotations/private/final-reference.csv \
  --manifest data/annotations/private/final-reference.manifest.json \
  --derived-db data/processed/cleaning.sqlite \
  --split-anchor-package results/cleaning-baseline/9cd30922aabf7fb2e2ba42e5a0396cfd \
  --comparator-package results/cleaning-challenger/ce19406cd132e55b2eb00531f5cc4cd3 \
  --model-dir ../Qwen3-Embedding-4B \
  --artifact-root results/cleaning-qwen-embedding \
  --output-format human \
  --execute-training
```

该入口要求干净 Git 工作树，设备、batch 和 dtype 只能来自冻结计划；它只编码训练442条并拟合固定逻辑回归头。训练嵌入、含折号的 Qwen OOF、与 sparse candidate 的成员级 paired OOF、截断统计、两模型风险—覆盖率/中间带对照、线性头、原始配置和聚合报告被原子封存。报告中的0.5、0.1/0.9、0.90尾部门和置信度网格都是开发诊断，不是路由阈值。既有 run 只有追加 `--expected-existing-manifest-sha256 <已冻结摘要>` 才可复用；缺少外部摘要时失败关闭。训练通过只允许后续实现一次 Qwen 验证方向复核；当前没有 Qwen 验证入口，不得用 sparse 验证脚本绕过模型身份。

## 新标签先评价两个旧模型

Issue #46 的第一步不是训练，而是排除与最终700条共享 leakage component 的全部成员后，对10,103条独立评价人口执行两个冻结模型各一次纯预测。该排除只用于评价泄漏隔离；正式全量推理的当前范围是13,858减最终700，即13,158条。Qwen 第三层仍是未通过开发门的研究 comparator；本入口不会改变其历史状态：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py score-frame \
  --config configs/cleaning.yaml \
  --study-plan configs/cleaning-model-reliability-study.yaml \
  --csv data/annotations/private/final-reference.csv \
  --derived-db data/processed/cleaning.sqlite \
  --sparse-package results/cleaning-challenger/ce19406cd132e55b2eb00531f5cc4cd3 \
  --qwen-package results/cleaning-qwen-head-tail/bdf73219d584edfcbeea02772716a90a \
  --model-dir ../models/Qwen3-Embedding-4B \
  --artifact-root results/cleaning-model-reliability-frame \
  --execute-prediction
```

输出会给出 `frame_id` 和 `package_manifest_sha256`。Qwen 对完整人口编码耗时较长；最终包只保存两个概率、身份绑定和去敏诊断，不保存模型权重或正文。首次成功后，重复使用同一人口框必须显式提供既有 manifest SHA-256，不能静默重算或选择“最新”运行。

第二步用该人口框生成240条 Wave A 盲标任务：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py prepare-wave-a \
  --config configs/cleaning.yaml \
  --study-plan configs/cleaning-model-reliability-study.yaml \
  --csv data/annotations/private/final-reference.csv \
  --derived-db data/processed/cleaning.sqlite \
  --scored-package results/cleaning-model-reliability-frame/<frame_id> \
  --expected-scored-manifest-sha256 <package_manifest_sha256> \
  --artifact-root results/cleaning-model-reliability-wave-a
```

生成器在 `results/cleaning-model-reliability-wave-a/` 根目录平铺 `wave-a-tourism-relevance-annotation.csv`，同时在 `<wave_id>/` 中保留同字节的不可变原件、manifest 与私有映射。该表使用 UTF-8 BOM，固定列序为 `task_id / sample_run_id / normalized_model_text / tourism_label`，`sample_run_id` 为本轮 `wave_id`。人工只填写 `tourism_label`（`related`、`unrelated` 或 `uncertain`），不需要原因码或文字说明。不得查看哈希子目录中的 `private-map.json`；源身份、版本、平台、模型名称、概率、分层、入选原因、纳入概率和分析权重均只在该私有映射中。评价导入会拒绝列序变化、`sample_run_id` 混批、正文变化、缺行、额外行或非法标签。

完成全部240条后执行当前已实现的基础设计加权评价：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py evaluate-wave-a \
  --study-plan configs/cleaning-model-reliability-study.yaml \
  --completed-csv <wave-a-completed.csv> \
  --wave-a-package results/cleaning-model-reliability-wave-a/<wave_id> \
  --scored-package results/cleaning-model-reliability-frame/<frame_id> \
  --expected-wave-manifest-sha256 <wave_package_manifest_sha256> \
  --expected-scored-manifest-sha256 <frame_package_manifest_sha256> \
  --artifact-root results/cleaning-model-reliability-evaluation
```

基础入口报告设计加权总体指标、固定概率/覆盖率风险和 component-bootstrap 区间，但不能单独冻结策略。Wave A不设置延迟复标任务，完成表封存后直接进入独立双阈值选择分析；新标签始终是 evaluation-only。

使用标签打开前已记录的固定16×16网格执行完整三段式分析：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py analyze-routing-grid \
  --selection-plan configs/cleaning-model-routing-selection.yaml \
  --scored-package results/cleaning-model-reliability-frame/96e0c758576a95b85641fe4d956fe819 \
  --base-evaluation-package results/cleaning-model-reliability-evaluation/0ea7d3cdbfa096350c7f0c515f5654d2 \
  --artifact-root results/cleaning-model-routing-selection \
  --execute-selection-analysis
```

配置已绑定人口评分、Wave A、完成表和基础评价 manifest，不接受命令行覆盖阈值。输出包括保留端误留、排除端UGC误删、人工率、自动覆盖、原始计数、设计加权区间、相同目标人工率比较、跨模型 Pareto 前沿以及人类可读 Markdown；程序不产生唯一模型或阈值。重复使用同一分析包必须显式提供 `--expected-existing-manifest-sha256`。

正式运行 `91e05fc4a20317a69d31d32150bd472f` 已完成：每模型256组，跨模型42个 Pareto 点全部属于Qwen。manifest SHA-256 为 `f2415c456dd06d90ac1d15f038bb5c74a2c004c107d6aed4da09a3b17467a3c5`；该结果仍为 `WAVE_A_SELECTION_READY`，没有冻结模型或阈值。

研究者已确认 Qwen `T_keep=0.14 / T_exclude=0.86`，并冻结 sparse `0.20/0.80` 为等人工量描述性 comparator。`configs/cleaning-model-routing-policy.yaml` 固定排除 Wave A 分量后的9,410条人口、九层容量和360条分配；后续 Wave B 入口只复用已封存概率，不训练、不打开锁定测试、不读取平台，也不得看 Wave B 结果后改策略。

先校验选择证据、Wave A 排除分量与九层容量，并封存策略 manifest：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py freeze-routing-policy \
  --policy-plan configs/cleaning-model-routing-policy.yaml \
  --scored-package results/cleaning-model-reliability-frame/96e0c758576a95b85641fe4d956fe819 \
  --wave-a-package results/cleaning-model-reliability-wave-a/0e130622e0369e371812c1570900391a \
  --selection-package results/cleaning-model-routing-selection/91e05fc4a20317a69d31d32150bd472f \
  --artifact-root results/cleaning-model-routing-policy \
  --execute-policy-freeze
```

策略包封存后，以其 manifest SHA-256 生成 Wave B：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py prepare-wave-b \
  --csv data/annotations/private/final-reference.csv \
  --derived-db data/processed/cleaning.sqlite \
  --policy-plan configs/cleaning-model-routing-policy.yaml \
  --scored-package results/cleaning-model-reliability-frame/96e0c758576a95b85641fe4d956fe819 \
  --wave-a-package results/cleaning-model-reliability-wave-a/0e130622e0369e371812c1570900391a \
  --policy-package results/cleaning-model-routing-policy/<policy_id> \
  --expected-policy-manifest-sha256 <policy_manifest_sha256> \
  --artifact-root results/cleaning-model-reliability-wave-b \
  --execute-wave-b-sampling
```

哈希子目录继续封存 `wave-b-tourism-relevance-annotation.csv` 原件、manifest 与私有映射；供人工直接填写的工作副本平铺为 `results/cleaning-model-reliability-wave-b/wave-b-tourism-relevance-completed.csv`。工作副本使用 UTF-8 BOM、360行，列序为 `task_id / sample_run_id / normalized_model_text / tourism_label`，人工只填写最后一列；复用同一运行时生成器不会覆盖已存在的完成表。身份、平台、概率、动作、交叉层、纳入概率和权重只在私有映射。两个入口均要求干净 Git 工作树并记录代码 SHA。

正式策略包 ID 为 `30a0806c341546c749034a07597b8d9b`，manifest SHA-256 为 `7205638147fe696c32ee577af5be23e4a13602c2ed4a640f241efe976aa7b404`。正式 Wave B ID 为 `aecfd402eace2041aa0276d27420dafd`，manifest SHA-256 为 `4acc02997e59337eec4919f4a03fc51d75362739b39baa216da6fe92f178c243`，任务 SHA-256 为 `d65a81a0ae20085d6470f3399d2de231f7c4c4c979456bba04be4264a7998c32`。完成文件统一命名为 `wave-b-tourism-relevance-completed.csv`。

若人工直接在任务包内原位填写，使用下列入口保全完成表、逐字节恢复空白任务模板并独立评价冻结策略：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py evaluate-wave-b \
  --completed-source results/cleaning-model-reliability-wave-b/aecfd402eace2041aa0276d27420dafd/wave-b-tourism-relevance-annotation.csv \
  --completed-output data/annotations/private/wave-b-tourism-relevance-completed.csv \
  --wave-b-package results/cleaning-model-reliability-wave-b/aecfd402eace2041aa0276d27420dafd \
  --policy-package results/cleaning-model-routing-policy/30a0806c341546c749034a07597b8d9b \
  --scored-package results/cleaning-model-reliability-frame/96e0c758576a95b85641fe4d956fe819 \
  --wave-a-package results/cleaning-model-reliability-wave-a/0e130622e0369e371812c1570900391a \
  --expected-wave-id aecfd402eace2041aa0276d27420dafd \
  --expected-wave-manifest-sha256 4acc02997e59337eec4919f4a03fc51d75362739b39baa216da6fe92f178c243 \
  --expected-policy-manifest-sha256 7205638147fe696c32ee577af5be23e4a13602c2ed4a640f241efe976aa7b404 \
  --artifact-root results/cleaning-model-reliability-wave-b-evaluation \
  --execute-wave-b-evaluation
```

入口只在“清空标签后与原任务 SHA-256 完全一致”时允许恢复模板；完成表先原子写入私有路径，再恢复任务包。评价只计算冻结 Qwen `0.14/0.86` 与 sparse `0.20/0.80` comparator，不扫描其他阈值、不形成机械通过门、不调用 `fit` 或预测，也不打开锁定测试。

正式完成表 SHA-256 为 `6c0f24e5510497641f69bb970c56c02f4235c81009e491421674029ce8c8b891`，任务模板已恢复原摘要。Wave B 独立评价 ID 为 `fc5c2721c4e15e8addf7c55ab115950e`，manifest SHA-256 为 `691aab5c923f4fa3f111ffa7141372a5f4ced04803f56ca1f1720399aef1ce20`，报告 SHA-256 为 `49655e99d95c96a0e494e445f4d7b85c340fe2d391898cebdbb2c42a1678cf9f`。证据状态为 `sufficient_for_descriptive_independent_evaluation`，不自动授权部署。

在锁定测试前，先用干净工作树封存最终判读和双尾审计计划：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py freeze-deployment-acceptance \
  --acceptance-plan configs/cleaning-model-deployment-acceptance.yaml \
  --policy-package results/cleaning-model-routing-policy/30a0806c341546c749034a07597b8d9b \
  --wave-b-evaluation-package results/cleaning-model-reliability-wave-b-evaluation/fc5c2721c4e15e8addf7c55ab115950e \
  --wave-b-completed-csv data/annotations/private/wave-b-tourism-relevance-completed.csv \
  --qwen-package results/cleaning-qwen-head-tail/bdf73219d584edfcbeea02772716a90a \
  --split-anchor-package results/cleaning-baseline/9cd30922aabf7fb2e2ba42e5a0396cfd \
  --artifact-root results/cleaning-model-deployment-acceptance \
  --execute-acceptance-freeze
```

该入口只校验并封存规则，保持 `fit_call_count=0`、`prediction_call_count=0`、`test_status=locked_not_opened` 和 `deployment_status=NOT_AUTHORIZED`。正式冻结 ID 为 `723a8c59a3f6e2fa7d581dde1fe1c5fa`，manifest SHA-256 为 `3400335ad5084dcf202d58fbdbb04f309d45b834814d095e4defb50efa77572d`；严格复用已验证。成功后才允许单次锁定测试入口读取148条测试成员；部署审计任务生成入口仍需在测试通过后实现。

唯一锁定测试入口已经实现。它只接受上述正式计划包、固定参考证据、固定切分、唯一 Qwen 模型和本地公开权重；首次执行会永久消耗测试访问：

```bash
.venv/bin/python scripts/cleaning_model_reliability.py run-locked-test \
  --csv data/annotations/private/final-reference.csv \
  --manifest data/annotations/private/final-reference.manifest.json \
  --derived-db data/processed/cleaning.sqlite \
  --split-anchor-package results/cleaning-baseline/9cd30922aabf7fb2e2ba42e5a0396cfd \
  --qwen-package results/cleaning-qwen-head-tail/bdf73219d584edfcbeea02772716a90a \
  --model-dir ../models/Qwen3-Embedding-4B \
  --acceptance-package results/cleaning-model-deployment-acceptance/723a8c59a3f6e2fa7d581dde1fe1c5fa \
  --expected-acceptance-manifest-sha256 3400335ad5084dcf202d58fbdbb04f309d45b834814d095e4defb50efa77572d \
  --artifact-root results/cleaning-model-locked-test \
  --execute-locked-test-once
```

入口按冻结计划身份扫描整个 artifact 根目录，代码版本变化也不能创建第二次测试；只有显式提供既有测试 manifest SHA-256 才严格复用，并且复用路径不再读取测试记录或调用预测。正式运行 `3ae5ccddabf10548f28e472bd4d46695` 的 manifest SHA-256 为 `eb61b5cdb44c85f49694c5998f5638a1e13ffd675c455fa12851cc96956b8b8d`，已保存去标识概率、聚合报告和 `test_access_count=1`。

该运行在148条上产生103/41/4三段动作，保留端2个 `unrelated`、排除端1个 `related`，且排除端支持仅4条，故判为 `FAILED_MANUAL_ONLY / MANUAL_ONLY`。固定0.5 Accuracy 91.22%、log loss 0.2312、Brier 0.0695和unrelated PR-AUC 0.6254仅作诊断。部署审计不启动；再次执行此命令只能严格复用既有结果，不能重开测试。

## 通用运行要求

所有入口使用显式 ID，不提供“最新运行”回退，不覆盖已有运行，不回写源库，也不在日志中输出原始正文、作者标识或源路径。正式运行必须保存配置、随机种子、Git SHA、输入与输出 manifest、artifact 哈希和机器可读状态。

代码实现后的完整验收至少包括：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```
