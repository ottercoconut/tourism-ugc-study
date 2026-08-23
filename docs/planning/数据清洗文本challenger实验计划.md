# 数据清洗文本 challenger 实验计划

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: plan + implementation validation
- Origin Date: 2026-08-23
- Verification Status: IMPLEMENTED_VALIDATED
- Version Label: cleaning_challenger_plan_stage_1

## 1. 状态与目标

状态：`PREPARED / NOT_FIT / TEST_LOCKED / ACCEPTANCE_GATE_FROZEN`。

本计划只准备标签一致性门完成后 baseline `9cd30922aabf7fb2e2ba42e5a0396cfd` 的首轮提升实验，不启动拟合，不读取锁定测试概率，也不生成自动清洗决定。目标不是单独追求 Accuracy，而是在当前最终700条、同一 leakage component 和新 baseline 冻结的开发切分上，寻找排序、概率质量与两类错误均更稳健的稀疏文本模型。

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
| C2 | 字符 NB log-count ratio＋线性分类器 | 与 C1 相同的字符范围和 `min_df`；`C={0.3,1,3}` | 检验类别条件词项权重是否改善广告/游客身份边界 |
| C3 | 字符 TF-IDF＋LogisticRegression | 与 C1 相同；`C={0.3,1,3}` | 检验分类损失及原生概率模型，而不引入新文本特征 |

共同冻结项：`max_df=0.995`、`sublinear_tf=true`（适用时）、`class_weight=balanced`、规范化规则、随机种子 `20260728`、标签编码、leakage build 与成员切分。NB log-count ratio 的平滑常数、是否使用 binary count 以及最终线性分类器实现必须在写代码前补入配置并封存，不能在看到结果后改变。

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

## 6. 尚未完成

- 标签一致性复核、用户仲裁、10条显式批准修改及复核后 baseline 已完成；
- challenger 配置、计算模块、artifact 契约、CLI 和测试尚未实现；
- NB-SVM 的平滑常数、binary count 选择和最终线性分类器实现尚未冻结；数值模型验收门已冻结；
- fastText、MacBERT、阈值研究、锁定测试与正式推理均未开始。
