# 配置目录

配置文件暂时平铺存放，以文件名前缀区分用途，例如 `cleaning-*.yaml`、`annotation-*.yaml`、`text-*.yaml`、`vision-*.yaml` 和 `experiment-*.yaml`。只有某一类配置实际增多后才建立子目录。

正式配置应满足：

- 不包含密码、令牌和本机绝对路径；
- 固定随机种子、输入 manifest、输出 `run_id`、内部 artifact ID 和哈希；
- 训练配置明确数据切分、模型、损失、优化器、停止条件和评估指标；
- 每个正式运行包保存经验证的原始或解析配置副本，并由 manifest 逐文件哈希绑定；文件名按运行类型稳定定义。

文本清洗工程框架已冻结，当前研究状态为 `RESEARCHER_SELECTED_ROUTING_FROZEN / ROUTING_DELIVERABLE_READY`。旧锁定测试的 `FAILED_MANUAL_ONLY` 和新周期原双尾审计的单尾失败结论仍保持不变。通用 `cleaning.yaml` 继续保留未绑定正式策略时的安全默认值；当前授权只来自独立交付配置和内容寻址artifact。唯一最终700条及其 leakage build 已封存，论文内容编码和视觉模型仍不得创建带有猜测参数的默认配置或空子目录。

`cleaning-model-retraining.yaml` 冻结 Issue #49 的前瞻性新模型周期，计划ID为
`19893303fa975523448c12a06aa72b57`。它把final 700、Wave A 240和Wave B 360
绑定为1,300条训练证据，固定三个候选、Qwen完整连续分块、5折leakage-group
OOF、交叉拟合Sigmoid、2%/5%点风险门、完整阈值网格和150+150双尾盲审。
该配置不改写旧Qwen `FAILED_MANUAL_ONLY`，也不授权在新盲审前自动清洗。

`cleaning-model-retraining-delivery.yaml` 冻结研究者在查看完整2,401点事后网格后
明确选择的融合模型和 `T_keep=0.31 / T_exclude=0.96`，配置决策ID为
`f08dea56b16f1769006ae97d2704518d`。它绑定原冻结模型、既有12,558条概率、
阈值网格、旧审计和1,300条训练快照的精确ID/SHA，并固定6,255/2,292/4,011的
三段计数、2,286条人工表及13,858条最终决定计数。接受依据明确为
`researcher_accepted_post_hoc_risk`；旧 `ONE_TAIL_RELEASED` 不得改写，最后
300条只作选择后诊断且 `independent_release_evidence=false`。该配置要求fit、
predict、encoder、源库写入和源记录删除计数全部为0。

上述零predict/encoder计数只描述“冻结交付策略”这一步没有重新运行模型，不是
禁止后续推理。未来新增批次由 `score-new-batch` 读取同一配置与策略包，在程序
内规范化后执行纯预测；它必须记录动态批次数量、逐条checkpoint和
`fit_call_count=0`，并拒绝平台、标签、旧概率或抽样字段进入模型输入。

当前稳定入口为 `configs/cleaning.yaml`。`reference` 节冻结 `final-nonduplicate-model-reference`、700/500/200 计数、字符 3–5 gram TF-IDF、`0.80` 候选阈值、全局候补队列、固定种子和“无法证明概率有效性时标记不可用”的失败关闭行为。`0.80` 不是自动删除阈值；只有精确规范哈希或人工最终确认边能形成重复分量。平台配额和平台排序被显式禁止。

同一文件还冻结 baseline 的全局字符 TF-IDF、固定 `C=1.0` 线性 SVM、分组折外 Sigmoid 校准、全局时间留出和三段式策略接口。`T_keep`、`T_exclude`、审计门、测试指标门、自动覆盖率门和恢复门全部为 `UNSET`。源库、派生库、最终参考 CSV/manifest、leakage build、artifact 根目录和运行身份由命令行显式传入；训练不再接收独立样本框迁移文件。

`cleaning-text-challenger.yaml` 预登记首轮54个稀疏候选：字符 TF-IDF＋LinearSVC、binary count＋类别 L1 NB log-count ratio＋LinearSVC、字符 TF-IDF＋`liblinear` LogisticRegression 各18个；计划完整 SHA-256 为 `ad515917c735ce77ed5231f0fa25bd536e8395115f22b9bf4e5035f4df199301`。标题/正文分通道、平台特征和未登记网格都被拒绝。`cleaning-model-acceptance.yaml` 独立冻结 UGC 安全优先门；两份配置都不设置生产路由阈值，也不授权打开锁定测试。

`cleaning-qwen-embedding-baseline.yaml` 预登记唯一 Qwen3-Embedding-4B 本地语义 baseline：固定上游 revision、14文件快照及两片权重聚合 SHA-256、MPS/bfloat16/batch=1 执行身份、单通道全文、统一中文任务说明、2048 token、2560维 L2 归一化向量和唯一 `C=1` 逻辑回归概率头；计划完整 SHA-256 为 `56a5900909834c0877725bf3367d295ef5c94bc39527e7737bb5fe802b2b461a`。`cleaning-qwen-model-acceptance.yaml` 把已验证 sparse candidate 绑定为 comparator，并自包含固定0.90高置信度 UGC 尾部安全门，完整 SHA-256 为 `3d201246b06fa58f48f88083c81f0de16588da9520e30944c3bf5354ab06ff7a`。风险—覆盖率网格、固定0.1/0.9和0.90尾部门都只是开发评价，不是路由阈值；正式训练、验证、测试、阈值和审计状态彼此分离。

`cleaning-model-reliability-study.yaml` 冻结 Issue #46 已完成人口评分与 Wave A 抽样计划，完整 SHA-256 为 `f8e322ea0c53eb2fedbb9509ba2004502d68107e44a98ffe88618cde8f3b964b`。它绑定 sparse 与 Qwen head-tail comparator、10,103条/7,500分量人口和 Wave A 240条六层分配。文件中的旧 Wave B 四层、延迟复标和稳定性字段为已完成运行的历史绑定字节，已由 `2026-08-24-Wave-A双阈值选择与Wave-B独立评价` 决策替代，不再代表后续执行协议；不得原地修改该文件。双阈值选择、Wave B 三段交叉抽样和最终测试/审计均使用新的独立配置绑定已封存产物。新标签未进入 `fit`。

`cleaning-model-routing-selection.yaml` 绑定已封存人口框、Wave A、完成表与基础评价，沿用标签打开前提交 `71610b9` 已写明的16×16双阈值网格。它只生成三段式风险—人工量选择证据，不能调用 `fit`、打开锁定测试、自动冻结阈值或产生正式清洗决定。

`cleaning-model-routing-policy.yaml` 记录研究者在 Issue #46 确认的 Qwen `T_keep=0.14 / T_exclude=0.86`，并把 sparse `0.20/0.80` 固定为相同人工量描述性 comparator；配置 ID 为 `30a0806c341546c749034a07597b8d9b`，完整 SHA-256 为 `30a0806c341546c749034a07597b8d9b28cbf9ae008f0a90517f7cc54d6a053a`。它同时冻结排除 Wave A 分量后的9,410条/7,273分量 Wave B 人口、九个动作交叉层容量和360条分配。该配置只授权 Wave B 独立评价；`deployment_status=NOT_AUTHORIZED`、审计为 `UNSET`、锁定测试未开启，不得生成正式自动清洗决定。

`cleaning-model-deployment-acceptance.yaml` 在锁定测试开启前绑定 Wave B 完成表与评价 manifest、唯一 Qwen 模型、700条固定切分和148条测试成员摘要。计划 ID 为 `0244dc9359679616a20bede755ff1791`，完整 SHA-256 为 `0244dc9359679616a20bede755ff17913dcdda6ccc98c024fc946ffb306884f9`。锁定测试只允许一次 Qwen `0.14/0.86` 预测：两个自动尾部均要求0个方向性错误，任一尾部支持少于20条则判证据不足。测试通过只允许生成临时路由；正式自动决定还要求 `auto_keep` 与 `auto_exclude` 各150条盲法简单随机审计均为0个不利事件。0事件对应单侧95% Clopper–Pearson上界约1.98%，不等于真实误差为0。任一审计事件暂停对应自动动作；不能靠换 seed、替换样本或追加样本稀释失败。

该计划的锁定测试已由运行 `3ae5ccddabf10548f28e472bd4d46695` 单次消费，结果为 `FAILED_MANUAL_ONLY`：103/41/4三段路由中，保留端2个方向性错误、排除端1个方向性错误，排除端支持仅4条。配置文件仍保持测试前冻结字节，不回写结果；结果由不可变测试 artifact 绑定。部署审计不启动，正式自动清洗未获授权。

`cleaning-text-normalization-v1.yaml` 单独保存结构化正文投影、确定性文本规范化、结构检查、精确重复和近似候选参数。`structured_text` 冻结 Quill Delta 的 `ops`/`insert` 键、可忽略的图片与截断嵌入及结构损坏时的失败关闭策略；识别只看内容结构，不看平台。主配置用“人工版本＋文件 SHA-256”锁定规则文件；任一字节变化都会使加载失败，必须显式更新主配置和受影响 artifact。`near_duplicate.candidate_threshold_ppm` 仅是候选召回线，`final_threshold` 在完成人工文本对验证前必须保持 `null`。

主配置同时用 `text_runtime` 锁定 CPython、Unicode、regex、NumPy、SciPy 和 scikit-learn 的规范哈希；项目依赖对参与确定性文本输出的包使用精确版本。运行时不一致时拒绝处理，而不是复用旧阶段任务或旧候选构建。
