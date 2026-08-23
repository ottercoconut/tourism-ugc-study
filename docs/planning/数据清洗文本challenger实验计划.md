# 数据清洗文本 challenger 实验计划

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: plan + implementation validation
- Origin Date: 2026-08-23
- Verification Status: IMPLEMENTED_VALIDATED
- Version Label: cleaning_challenger_plan_stage_1

## 1. 状态与目标

状态：`TRAIN_OOF_ACCEPTED / VALIDATION_DIRECTIONALLY_CONSISTENT / SPARSE_SEARCH_CLOSED / TEST_LOCKED`。

本计划已完成 baseline `9cd30922aabf7fb2e2ba42e5a0396cfd` 的首轮54候选训练侧比较；唯一候选通过预登记 UGC 安全验收并完成一次性验证方向复核。本阶段没有读取锁定测试概率，也没有生成自动清洗决定。目标不是单独追求 Accuracy，而是在当前最终700条、同一 leakage component 和冻结开发切分上寻找排序、概率质量与 UGC 安全更稳健的稀疏文本模型。

## 2. 先完成标签一致性门

### 2.1 复核对象

从训练 OOF 与验证开发证据中选择模型与人工标签强烈矛盾的记录，例如：

- 人工为 `related`，但 `p(unrelated) >= 0.90`；
- 人工为 `unrelated`，但 `p(unrelated) <= 0.10`；
- 短文本中的两类误差。

再为这些记录按原人工标签、所属切分和文本长度匹配模型预测正确记录，采用1:1且对照不重复。匹配优先在同一长度层随机抽取；长度层内不足时，仍固定原标签和切分，改取字符长度最近的正确记录并单独记录放宽匹配数量。模型选择记录与随机对照使用同一随机种子抽取并混合排序。

### 2.2 “隐藏模型答案”的具体含义

复核者只能看到任务中的可判读规范化文本和冻结标签规则；任务不展示原人工标签、模型类别、模型概率、是否预测正确、为何被抽中或属于哪一组。复核完成后才解除分组身份：

- 强矛盾组回答“模型发现的标签冲突中有多少经独立规则复核成立”；
- 随机正确对照回答“同样的复核流程会不会把本来稳定的标签误改”。

两组必须分别报告。强矛盾组由模型定向选择，不能用它估计700条或候选人口的总体标签错误率；随机对照也只是复核过程的有限稳定性检查，不替代双人独立标注的一致性统计。

实现已绑定 Issue #41。私有任务的复核 ID 为 `c5e4c7139c703e04309f80a78506e7ee`，绑定模型 `b55caa7fb29d3503f0b2fc5e465891c8` 与随机种子 `20260728`；共68条，其中模型矛盾目标34条、1:1随机正确对照34条，32条对照为同长度层精确匹配，2条为同标签/切分内最近长度匹配。初始任务、私有映射和包 manifest SHA-256 分别为 `32e199b240efecac3788eb35a4ba00b3d4caa42871e2c55ec1d54d00dc50b15b`、`46c1b88aa3b43d82f6f616974fad00dc1ed9e22cfba85cda62374c02165d80ea`、`6deab43d9bbfdc5284013d02a927ba72e8730ad7a2f550858062d0204fb4c135`。

复核已完成：模型矛盾组修改12/34（35.3%），匹配正确对照修改1/34（2.9%），均无 `uncertain`。用户逐条仲裁后批准10条（9条 `related→unrelated`、1条 `unrelated→related`），否决3条并维持原标签。汇总、完成任务和应用回执 SHA-256 分别为 `7356f23347322b5fcdf0f94f60faced5afbb242b58cc851ffeae49bfef522f9c`、`954a271b220f3c6d93b61e55282d6eb3d4856577395076861f2ef2795f954d16`、`f0435f308426865a141095f42f89f55e19393b2306c1e28c039ad93a9c82340b`。标签一致性门已完成；两个修改率都不是700条或候选人口的总体错标率估计。

## 3. 首轮候选矩阵

所有候选继续使用同一规范化标题＋正文拼接文本。标题/正文分通道、中文分词、平台、作者、样本框和文本长度都不进入特征。

| 候选族 | 表示与分类器 | 预登记搜索空间 | 进入首轮的理由 |
|---|---|---|---|
| B0 | 当前字符 TF-IDF＋LinearSVC | `(2,5)`、`min_df=2`、`C=1` | 原样保留基准锚点 |
| C1 | 字符 TF-IDF＋LinearSVC | `ngram_range={(2,5),(3,5),(2,6)}`；`min_df={1,2}`；`C={0.3,1,3}` | 检验局部字符上下文、稀有词项与正则强度 |
| C2 | 字符 binary count＋NB log-count ratio＋LinearSVC | 与 C1 相同的字符范围、`min_df` 和 `C`；`alpha=1`；类别内 L1 归一化 | 检验类别条件词项权重是否改善广告/游客身份边界 |
| C3 | 字符 TF-IDF＋LogisticRegression | 与 C1 相同；`C={0.3,1,3}` | 检验分类损失及原生概率模型，而不引入新文本特征 |

共同冻结项：`max_df=0.995`、`sublinear_tf=true`（适用时）、`class_weight=balanced`、规范化规则、随机种子 `20260728`、标签编码、leakage build 与成员切分。C2 按 Wang–Manning 构造使用二值文档出现、`alpha=1` 加性平滑、类别内 L1 归一化后的 `log(P(feature|unrelated)/P(feature|related))`，再输入 `LinearSVC`；该比率必须在每个拟合折内部重新估计。C1/C2 的 SVM 概率都只由相应训练端的分组 OOF margin 拟合 Sigmoid，C3 使用 `liblinear` 逻辑回归的原生 `predict_proba`。

完整矩阵冻结于 `configs/cleaning-text-challenger.yaml`：计划 ID 为 `ad515917c735ce77ed5231f0fa25bd53`，完整 SHA-256 为 `ad515917c735ce77ed5231f0fa25bd536e8395115f22b9bf4e5035f4df199301`。C1、C2、C3 各18个候选，共54个；配置解析器拒绝未知字段、标题/正文分通道、平台特征、折数或任一未登记参数漂移。

首轮不做标题/正文分通道，因为当前误差证据没有显示字段权重是主要瓶颈，现有拼接也保留了标题和正文的字符证据。移除这条搜索轴能降低小样本多重尝试风险。fastText 和 MacBERT 仅在首轮稀疏候选未通过冻结验收门时另行预登记，不与首轮混跑。

## 4. 无测试泄漏的选择流程

1. 只使用训练442条；外层采用至多5折 `StratifiedGroupKFold`，内层采用至多4折，分组键均为 finalized leakage component；不可行时只能按稳定规则逐级减少折数并记录 reason code。
2. 每个外层训练部分的内层证据选择候选族和超参数；外层留出部分不得参与选择。
3. 为得到诚实的外层概率，在每个外层训练部分内部重新生成分组 OOF margin、拟合 Sigmoid 校准器，再把校准器应用于该外层留出部分；不得用外层留出标签拟合校准器。
4. 汇总每条训练记录恰好一次的外层 OOF 概率，报告 Accuracy、两类 Precision/Recall/F1、混淆矩阵、unrelated PR-AUC、Brier、log loss、风险—覆盖率和文本长度误差层。
5. 完成训练侧比较后只能选出一个胜出者；再在全部训练442条上按同一嵌套规则确定配置、生成训练 OOF 校准证据并拟合冻结模型。
6. 唯一胜出者允许读取现有验证110条一次，作为已暴露开发集上的方向性复核。不得根据验证结果扩搜索空间、改特征或试第二个胜出者。
7. 锁定测试148条继续保持 `locked_not_opened`，直至模型、模型验收门、`T_keep`、`T_exclude` 和审计门全部冻结。

## 5. 选择与停止规则

用户已确认“UGC 安全优先”，数值验收门已独立冻结在 `configs/cleaning-model-acceptance.yaml`；策略 ID 为 `3640f3c06ba3b658a4ba15076bd0e199`，完整 SHA-256 为 `3640f3c06ba3b658a4ba15076bd0e1993ab8ab715af7ddcfe054852ee1673d63`。该配置不改变 baseline 训练参数，也不是 `T_keep` 或 `T_exclude`。比较只接受当前442条训练成员在同一 nested group 外层折中的 baseline/challenger 配对 OOF 概率，并以 leakage component 为重采样单位运行5,000次固定种子 bootstrap。硬门按以下顺序执行：

1. **UGC 安全点估计：**在0.5诊断分界下，challenger 的 `related→unrelated` 错误率不得高于同折 baseline；
2. **UGC 安全不确定性：**上述误排率差的单侧90% bootstrap 上界不得超过+2个百分点；这是有限样本的不劣界，不是允许点估计变差；
3. **主要改善：**log loss 点估计至少改善0.01，且 challenger−baseline 差的单侧90% bootstrap 上界小于0；
4. **排序不劣：**unrelated PR-AUC 点估计最多退化0.005，其单侧90% bootstrap 下界不得低于−0.01；
5. **校准不劣：**Brier score 点估计不得变差。

任一门失败都固定保留 baseline，Accuracy 不能覆盖安全失败。通过全部训练侧门只表示有资格成为唯一验证候选；验证只作一次方向性复核，不得据此扩展搜索。候选比较同时遵守：

- 以训练侧嵌套 OOF 的 log loss 和 unrelated PR-AUC 为主要选择证据；Accuracy 及0.5诊断混淆矩阵是次要解释指标，0.5不得变成路由阈值；
- 用 leakage component 为重采样单位做配对不确定性分析，不能把帖子当作完全独立样本；
- 胜出者不能以降低 `related→unrelated` 安全性为代价换取总体 Accuracy；
- 若训练侧没有一致、可解释的增益，保留 B0，不因“已经做过实验”而强行换模型；
- 首轮结束即停止稀疏搜索，不按验证误差临时增加 n-gram、字段权重、分词器或模型族。

## 6. 训练侧结果与实现状态

- 标签一致性复核、用户仲裁、10条显式批准修改及复核后 baseline 已完成；
- 训练运行 ID 为 `ce19406cd132e55b2eb00531f5cc4cd3`，候选模型 ID 为 `1b68baa8bef99d6b9d75b7bf3226cfb4`；五个外层训练端及全训练内层选择均选中同一字符 TF-IDF＋LinearSVC：`ngram_range=(2,5)`、`min_df=2`、`C=3`、分组 OOF Sigmoid；
- 442条 paired outer OOF 上 Accuracy 均为393/442；候选把 `related→unrelated` 从31条降至30条，同时把 `unrelated→related` 从18条增至19条，符合已确认的 UGC 安全优先而非 Accuracy 优先；log loss 改善0.0112、PR-AUC 提高0.0014、Brier 改善0.0023。component bootstrap 中安全差上界为0，log loss 差上界为−0.0070，七项训练门全部通过；
- 训练 manifest、验收报告、paired OOF 与候选模型 SHA-256 分别为 `9769da2175d217833d5183a0d261ea3fae0b809f1483c4a0dd266beb648681d9`、`dfa886b348b3eef5419e1284080692cc919713c034c52f0abde801165f23c3ed`、`95d5624336d2336c26ea3d044f879b32485a0e2dcd98e6b11dd48ae592aa03c6`、`40f870c2eb08f4cbf0d733493b6b9d4f410f06362e9584361863bf05f543a4c3`；
- 无拟合验证运行 `6d105b334df0234350a4b5c32358f7a9` 已封存为 `directionally_consistent`：验证 Accuracy 86.36%→88.18%，UGC误排10→10、无关误留5→3，log loss 0.2804→0.2645、PR-AUC 0.9676→0.9696、Brier 0.0902→0.0835；
- sparse 搜索已经关闭，不再依据该验证增加 n-gram、字段或稀疏模型；Qwen3-Embedding 是另行预登记的独立语义 baseline，详见《数据清洗Qwen语义baseline实验计划》；阈值研究、锁定测试与正式推理均未开始。

## 7. 一次性验证解释规则

验证不新增显著性门或最低改善幅度，只检查四个预声明方向：`related→unrelated` 不升、log loss 不升、unrelated PR-AUC 不降、Brier 不升。四项均满足记为 `directionally_consistent`，否则记为 `mixed_or_reversed`；两者都只是已暴露验证集上的描述，不是锁定测试结论。验证后不得切换候选、修改 `C`、扩充 n-gram 或启动第二个模型；代码只允许调用冻结候选的 `predict_p_unrelated`，语法树测试禁止任何 `fit`/`fit_transform` 调用。

实际四项方向均满足。该结论关闭本计划的 sparse 搜索，不把验证集转化为新的调参集。后续 Qwen 计划使用新的固定模型族、配置身份和验收策略，并以本计划候选作为 comparator；它不会修改或覆盖本计划 artifact。
