# 数据清洗 Qwen3-Embedding 语义 baseline 实验计划

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: plan + implementation validation
- Origin Date: 2026-08-23
- Verification Status: BASELINE_FAILED / LAYER1_FAILED / LAYER2_FAILED / LAYER3_IMPLEMENTED_READY_NOT_RUN
- Version Label: cleaning_qwen_embedding_challenger

## 1. 研究问题与当前状态

研究问题是：在保持最终700条标签、442/110/148成员切分、leakage component、UGC 安全偏好和测试锁定不变时，冻结的 `Qwen3-Embedding-4B` 语义表示加唯一线性概率头，能否比已通过开发验收的 sparse comparator 提供更好的训练侧概率质量，并减少固定开发概率中间带，而不增加真实游客 UGC 的误排风险。

4B 首次 baseline 已完成，运行 ID 为 `14feebc04a7a61b8b97f959a998d14dc`，模型 ID 为 `cb5bad8cd27c2c5df9edea3a2ca20834`。442条训练成员的 leakage-group OOF 上，真实 UGC 误排率由 sparse 的15.71%降至13.61%，但 log loss 由0.2621恶化到0.3430、unrelated PR-AUC 由0.9733降至0.9509、Brier 由0.0774恶化到0.0949，因此未通过冻结验收门，验证状态为 `not_allowed`。训练文本中57/442条超过2048-token单视图上限，最大值为70,077；这只说明当前首部截断机制可能丢失尾部信息，不说明长文本必然是错误原因。

当前状态为 `BASELINE_FAILED_RETAIN_SPARSE / LAYER1_FAILED / LAYER2_FAILED / LAYER3_IMPLEMENTED_READY_NOT_RUN / TEST_LOCKED / THRESHOLD_UNSET / AUDIT_UNSET`。根据 [Issue #45](https://github.com/ottercoconut/tourism-ugc-study/issues/45)，改进按缓存分类头、无泄漏融合、英文 instruction＋head-tail 三层顺序执行；任一层通过既有训练门后停止扩展。第一、二层运行均已不可变封存且未通过全部验收门；第三层代码、冻结配置、合成 MPS 烟雾测试和单元测试已完成，尚未对442条正式训练成员重新编码。验证和锁定测试均未读取。

## 2. 为什么建立新的语义 baseline

首轮 sparse challenger 最终仍是字符 TF-IDF＋LinearSVC，训练 paired OOF 与验证方向复核都只显示小幅改善。其主要盲区是游客身份、体验叙述、广告意图和城市资讯之间需要跨短语语义判断，继续扩字符 n-gram 网格可能增加分析路径而不能解决表示瓶颈。因此本阶段不继续稀疏调参，而建立成本更高但仍能在本机运行的冻结语义表示基线。

Qwen3-Embedding 论文和官方模型卡报告该系列面向文本分类、聚类与检索，4B版本提供2560维向量、最长32K上下文并支持100多种语言；这些公开结果只说明候选具有技术合理性，不能替代本项目的分组 OOF 证据（[Zhang et al., 2025](https://arxiv.org/abs/2506.05176)；[官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-4B)）。论文已保存到本地 Zotero，item key 为 `KJWIZ7GU`。

## 3. 首次预登记模型与实际结果

唯一候选冻结在 `configs/cleaning-qwen-embedding-baseline.yaml`，计划 ID 为 `56a5900909834c0877725bf3367d295e`，完整 SHA-256 为 `56a5900909834c0877725bf3367d295ef5c94bc39527e7737bb5fe802b2b461a`。

- 编码器：`Qwen/Qwen3-Embedding-4B`，revision `5cf2132abc99cad020ac570b19d031efec650f2b`；
- 模型快照：revision 下14个非缓存文件逐文件校验，规范快照 SHA-256 为 `cce6e0f7cd81e6c7cf31a67708362e6e9762b6c343d9805506c08ee283d0bac9`；两片权重逐片校验后形成规范聚合 SHA-256 `e49e59781ff5f117a16cbf9e37655202ce529729ace5aaf3b68fa88fe57906b9`，并要求权重索引只引用这两片文件；
- 文本：仍使用冻结的标题＋正文单通道 `normalized_model_text`，不拆标题/正文；
- 任务说明：所有记录使用同一条中文游客 UGC/纯广告身份边界说明，不含标签答案、示例或平台；
- 最大长度：2048 tokens；末 token pooling；输出转 `float32` 后再次 L2 归一化；训练报告保存聚合截断条数、比例、最大值和P95，不保存正文或成员身份；
- 编码器：完全冻结，禁止 fine-tuning、LoRA 和远程 API；
- 分类器：唯一 `LogisticRegression(C=1.0, class_weight=balanced, solver=liblinear, max_iter=2000)`，使用原生 `predict_proba`；
- 本地执行：固定 Apple MPS、batch size 1、参数 `bfloat16`、输出 `float32`；设备、硬件、macOS/Darwin版本、Python，以及包含 `tokenizers`、`safetensors` 的依赖版本进入运行身份，CLI 不允许覆盖；batch size 1 是16GB统一内存下的资源约束，不改变成员或统计设计；
- 训练评价：Qwen 使用固定候选 leakage-group 最多5折 OOF；sparse comparator 是已封存的 nested OOF。两者绑定同一442条成员、标签和 leakage component，但不宣称外层折号完全相同；没有超参数搜索和模型族选择。

本地公开模型位于仓库相邻目录 `../Qwen3-Embedding-4B`，不进入 Git。运行时固定 `torch==2.13.0`、`transformers==5.15.1`、`sentence-transformers==6.0.0` 和 `huggingface-hub==1.28.0`。权重准备入口禁止远程代码和运行时联网回退；既有目录非法时失败关闭，不覆盖用户文件。

## 4. 比较与验收

比较锚点是 sparse 运行 `ce19406cd132e55b2eb00531f5cc4cd3` 的 candidate 模型 `1b68baa8bef99d6b9d75b7bf3226cfb4`，而不是更早的 `C=1` TF-IDF 模型。Qwen OOF 与 sparse candidate OOF 按成员、标签和 leakage component 逐条配对，以 component 为单位做5,000次固定种子 bootstrap。

独立验收策略位于 `configs/cleaning-qwen-model-acceptance.yaml`，策略 ID 为 `3d201246b06fa58f48f88083c81f0de1`，完整 SHA-256 为 `3d201246b06fa58f48f88083c81f0de16588da9520e30944c3bf5354ab06ff7a`。0.90尾部门与其非路由声明直接包含在该策略中，硬门顺序保持 UGC 安全优先：

1. 固定0.5诊断分界下，`related→unrelated` 点估计不得高于 sparse，且差值单侧90% bootstrap 上界不得超过+2个百分点；
2. log loss 至少改善0.01，且差值单侧90%上界小于0；
3. unrelated PR-AUC 点退化不超过0.005，且差值单侧90%下界不低于−0.01；
4. Brier 点估计不得恶化；
5. 在固定0.90高置信度诊断下，Qwen 将真实 `related` 判成高置信度 `unrelated` 的条数不得高于 sparse。

Accuracy 只作解释，不能覆盖安全门。固定 `[0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.975, 0.99]` 置信度网格对 sparse 与 Qwen 同时报告训练 OOF 的覆盖、错误、选择性风险、两类错误和差值；固定 `0.1/0.9` 同时比较两模型的中间带工作量代理。所有这些值均为开发诊断，不是 `T_keep/T_exclude`，不生成自动决定。`class_weight=balanced` 下的逻辑回归输出是开发样本条件概率分数，不可直接解释为约14,000条候选人口的后验概率或实际工作量。

首次运行已按本规则失败并保留 sparse comparator，没有读取 Qwen 验证集。锁定测试148条继续保持 `locked_not_opened`。后续三层搜索是在首次结果和57条截断聚合证据出现后另行冻结的新计划，不追溯改写首次 baseline。

### 4.1 Issue #45 三层改进决策树

第一层配置计划 ID 为 `f9a1cccab81b2ea9ba8c26b5d539781c`，完整 SHA-256 为 `f9a1cccab81b2ea9ba8c26b5d539781cbb54ba991e185d9a16f19323ae492fb9`。它只读取首次运行包内已封存的442×2560训练 embedding 与去标识 OOF 成员，不再次编码正文。MRL 前缀维度固定为256、512、1024、2560，截取后逐行重新 L2 归一化；逻辑回归比较 `C ∈ {0.1,1,10,100}`，LinearSVC 比较 `C ∈ {0.1,1,10}`，两者都比较无类别权重与 `balanced`，共56个候选。所有分类头统一从当前拟合端的分组 OOF margin 拟合 Sigmoid，因此 log loss 与 Brier 的比较不再混合“原生概率”和“校准概率”。外层5折、内层4折均按 leakage component 分组；每个外层训练端先要求候选的 UGC 误排率不高于原4B头规范形成的内部安全锚，再按最低 log loss、最高 PR-AUC 和稳定候选 ID 选择。全局 sparse OOF 不进入内层选择，避免其训练谱系把外层留出标签间接带回选择过程；第一层外层 OOF 完成后才与 sparse 逐成员配对执行既有验收门。

第一层运行 ID 为 `3d23bc1b9b8c2957856851fa1454eccd`，模型 ID 为 `a2f919b09d1a78e7b8a8b749524013a0`，manifest SHA-256 为 `bf5936977f4712b556e2b0cb41cdb092330ebbb1c46e543bc58516970ae135ee`。全训练端最终选择2560维、`C=1`、`class_weight=balanced` 的 LinearSVC＋OOF Sigmoid；五个外层折实际选择三种头，说明 nested 程序没有把全训练端结果倒灌到外层。相对 sparse，UGC误排率改善2.09个百分点，log loss改善0.0333，Brier改善0.0129，但 PR-AUC退化0.0142；此外固定0.90诊断下高置信 UGC 误排为9条，而 sparse 为4条。component bootstrap 的 log loss 差90%上界为+0.0013，PR-AUC差90%下界为−0.0296。故安全点门、Brier门和log loss点改善门通过，但PR-AUC两门、高置信UGC尾部门及log loss区间门失败，状态为 `failed_retain_baseline / validation_not_allowed`。

若第一层失败，第二层固定 sparse comparator，不重新搜索字符网格；Qwen 端在每个外层训练端重做第一层内层选择，再仅以内层分组 OOF 在 logit 空间比较固定融合权重0、0.25、0.5、0.75、1，其中0和1为审计锚点。第二层计划 ID 为 `7198161453f248a70197a8a960506501`，完整 SHA-256 为 `7198161453f248a70197a8a9605065010a035787fe984551d11946524b33c94d`；同外层配对验收策略 ID 为 `42b1770fe0241fb7aa69303878141330`，完整 SHA-256 为 `42b1770fe0241fb7aa69303878141330cedcd7cb54053ed82908d8fad64d7836`。与第一层不同，第二层在每个外层训练端重建固定 sparse 的向量器、SVM及分组 OOF Sigmoid，同时重做 Qwen 头选择与校准；两个基模型和融合候选对外层留出共同只预测一次，所以验收契约为 `paired_outer_folds=true`。

第二层运行 ID 为 `eba8568816309688f1f85e6092a57b1d`，模型 ID 为 `4eb42812a7d9d68388418a6d58442c2b`，manifest SHA-256 为 `37fcdc206ba4c7c43afe2820ad2c47292925a91a8bf191084dc13b9c8ed261d3`。全训练端选择 Qwen logit 权重0.5；外层五折选择0.5三次、0.75两次。相对同外层 sparse，UGC误排率改善3.14个百分点，log loss改善0.0440且90% component bootstrap上界为−0.0217，Brier改善0.0144；但 PR-AUC退化0.005350，略超过0.005点门，90%下界为−0.01543，也超过−0.01区间门。固定0.90诊断下高置信UGC误排8条，sparse为4条。重建的外层 sparse 概率与既有正式 sparse OOF 逐成员最大绝对差为0，证明配对复现成立。故第二层状态为 `failed_retain_baseline / validation_not_allowed`。

第二层失败后，第三层才重新编码。第三层计划 ID 为 `f7bb536e7f71596bd18bf2255b57df99`，完整 SHA-256 为 `f7bb536e7f71596bd18bf2255b57df99451cbcb2c2d5a1e1f1a57edc1b833eda`。instruction 固定为英文游客亲历青岛UGC与广告/本地非游客/城市资讯边界；使用固定模板 `Instruct: {instruction}\nQuery: `。对英文 prompt 加特殊 token 后仍在2048上限内的文本编码一次；超限文本分别编码最大可容纳的头部与尾部 token 窗口，平均两个归一化向量后再次 L2 归一化。实际 tokenizer 在当前计划下导出每视图内容预算2001 tokens，合成 MPS 烟雾测试的编码视图超限为0。若原内容超过两个窗口总预算，中间 token 仍会省略，运行报告保存省略成员数与省略 token 总数；因此该方法只缓解“只保留开头”的偏差，不声称覆盖极长文本中间全部内容。第三层用新表示重新执行第一层已冻结的56候选分类头 nested OOF，不追加临时参数，并另行报告单视图内、head-tail溢出和中段省略三个训练侧聚合分组。

三层都只使用训练成员；平台、验证、测试、路由阈值、配额和审计均不进入模型选择。模型卡说明 Qwen3-Embedding 支持 MRL 自定义维度，并建议多语言任务优先使用英文 instruction；这些是候选设计依据，不是效果保证（[官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-4B)）。

## 5. 实现与不可变 artifact

- `cleaning_prepare_qwen_embedding.py`：下载/校验公开权重，并可用两条内置合成文本烟雾测试；
- `cleaning_train_qwen_embedding_baseline.py`：只物化训练442条，先编码一次，再生成分组 OOF、最终线性头、配对验收和不可变运行包；
- 运行包保存训练嵌入缓存、含折号的逐成员 Qwen OOF、与 sparse 的逐成员 paired OOF、聚合报告、解析前计划/验收 YAML、线性头和完整 manifest；不复制公开 Qwen 权重；
- 加载或复用既有运行包前必须由调用方给出外部 manifest SHA-256；加载线性头还必须给出期望模型 ID。系统先校验固定文件名、路径、哈希、计划、快照与执行谱系，再允许复用或 `joblib.load`；
- manifest 固定 `test_status=locked_not_opened`、`threshold_status=UNSET`、`audit_status=UNSET`、`auto_cleaning_decisions_present=false` 和 `platform_used=false`；
- 所有私有嵌入、成员概率、分类器和运行包继续由 Git 忽略，不得提交。

## 6. 第一层正式运行命令

工作目录：`/Users/kawauso/Documents/Projects/TripPostResearch`

```bash
.venv/bin/python scripts/cleaning_train_qwen_head_challenger.py \
  --source-package results/cleaning-qwen-embedding/14feebc04a7a61b8b97f959a998d14dc \
  --comparator-package results/cleaning-challenger/ce19406cd132e55b2eb00531f5cc4cd3 \
  --base-plan configs/cleaning-qwen-embedding-baseline.yaml \
  --plan configs/cleaning-qwen-head-challenger.yaml \
  --acceptance-policy configs/cleaning-qwen-model-acceptance.yaml \
  --artifact-root results/cleaning-qwen-head-challenger \
  --output-format human \
  --execute-training
```

命令要求工作树干净，并把执行时当前 Git `HEAD` 记录为代码身份；仓库内 artifact 根必须已被 Git 忽略。入口不接收 CSV、数据库、正文、验证、测试或模型目录，只复用由 SHA-256 绑定的首次训练 embedding。预期输出目录为 `results/cleaning-qwen-head-challenger/<run_id>/`。

第一层已运行失败后，第二层正式命令为：

```bash
.venv/bin/python scripts/cleaning_train_qwen_sparse_fusion.py \
  --config configs/cleaning.yaml \
  --csv data/annotations/private/final-reference.csv \
  --manifest data/annotations/private/final-reference.manifest.json \
  --derived-db data/processed/cleaning.sqlite \
  --split-anchor-package results/cleaning-baseline/9cd30922aabf7fb2e2ba42e5a0396cfd \
  --sparse-plan configs/cleaning-text-challenger.yaml \
  --sparse-package results/cleaning-challenger/ce19406cd132e55b2eb00531f5cc4cd3 \
  --qwen-source-package results/cleaning-qwen-embedding/14feebc04a7a61b8b97f959a998d14dc \
  --head-package results/cleaning-qwen-head-challenger/3d23bc1b9b8c2957856851fa1454eccd \
  --qwen-base-plan configs/cleaning-qwen-embedding-baseline.yaml \
  --head-plan configs/cleaning-qwen-head-challenger.yaml \
  --fusion-plan configs/cleaning-qwen-sparse-fusion.yaml \
  --acceptance-policy configs/cleaning-qwen-fusion-model-acceptance.yaml \
  --artifact-root results/cleaning-qwen-sparse-fusion \
  --output-format human \
  --execute-training
```

该入口只从最终证据物化训练442条文本，并用缓存4B embedding 按成员键对齐；没有验证或测试参数。运行包不复制 embedding 或公开权重，只保存最终融合模型、同外层 paired OOF、折内选择、五权重评分、计划、验收策略、报告和 manifest。

第二层已运行失败后，第三层正式命令为：

```bash
.venv/bin/python scripts/cleaning_train_qwen_head_tail.py \
  --config configs/cleaning.yaml \
  --csv data/annotations/private/final-reference.csv \
  --manifest data/annotations/private/final-reference.manifest.json \
  --derived-db data/processed/cleaning.sqlite \
  --split-anchor-package results/cleaning-baseline/9cd30922aabf7fb2e2ba42e5a0396cfd \
  --sparse-plan configs/cleaning-text-challenger.yaml \
  --sparse-package results/cleaning-challenger/ce19406cd132e55b2eb00531f5cc4cd3 \
  --layer2-package results/cleaning-qwen-sparse-fusion/eba8568816309688f1f85e6092a57b1d \
  --qwen-base-plan configs/cleaning-qwen-embedding-baseline.yaml \
  --head-plan configs/cleaning-qwen-head-challenger.yaml \
  --projection-plan configs/cleaning-qwen-english-head-tail.yaml \
  --acceptance-policy configs/cleaning-qwen-model-acceptance.yaml \
  --model-dir ../models/Qwen3-Embedding-4B \
  --artifact-root results/cleaning-qwen-head-tail \
  --output-format human \
  --execute-training
```

运行包保存新训练 embedding、nested OOF、与 sparse 的逐成员配对、56候选评分、长度分组聚合诊断、模型、计划、策略和 manifest；不保存正文或逐成员 token 长度，不复制公开权重。

## 7. 完成判据与仍未完成项

第一层实现判据是：严格解析56候选、MRL重归一化、外5/内4分组选择、所有头的训练端 OOF Sigmoid、不可变 artifact、去敏可读输出和测试均完成；正式运行后再根据原验收策略决定停止或进入第二层。每进入下一层前都必须先提交该层代码、配置和两份方案文档，且前一层运行包不可覆盖。

三层结束后仍需：若唯一候选通过则另行实现并执行一次无拟合验证；冻结最终模型验收结论、`T_keep/T_exclude`、审计事件/样本量/置信上限/恢复门和锁定测试门；最后才允许一次测试开启、无 `fit` 全量/增量推理与正式自动清洗。当前任何一项都不得误报为完成。
