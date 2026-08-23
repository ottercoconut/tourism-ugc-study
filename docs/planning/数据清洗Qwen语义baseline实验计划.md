# 数据清洗 Qwen3-Embedding 语义 baseline 实验计划

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: plan + implementation validation
- Origin Date: 2026-08-23
- Verification Status: IMPLEMENTED_READY_NOT_TRAINED
- Version Label: cleaning_qwen_embedding_baseline

## 1. 研究问题与当前状态

研究问题是：在保持最终700条标签、442/110/148成员切分、leakage component、UGC 安全偏好和测试锁定不变时，冻结的 `Qwen3-Embedding-0.6B` 语义表示加唯一线性概率头，能否比已通过开发验收的 sparse comparator 提供更好的训练侧概率质量，并减少固定开发概率中间带，而不增加真实游客 UGC 的误排风险。

当前状态为 `IMPLEMENTED_READY_NOT_TRAINED / PUBLIC_WEIGHTS_READY / TEST_LOCKED / THRESHOLD_UNSET / AUDIT_UNSET`。代码、配置、依赖、本地公开权重和合成文本烟雾测试已经就绪；700条参考集没有被 Qwen 编码，线性头没有拟合，验证和锁定测试均没有执行。

## 2. 为什么建立新的语义 baseline

首轮 sparse challenger 最终仍是字符 TF-IDF＋LinearSVC，训练 paired OOF 与验证方向复核都只显示小幅改善。其主要盲区是游客身份、体验叙述、广告意图和城市资讯之间需要跨短语语义判断，继续扩字符 n-gram 网格可能增加分析路径而不能解决表示瓶颈。因此本阶段不继续稀疏调参，而建立成本更高但仍能在本机运行的冻结语义表示基线。

Qwen3-Embedding 论文和官方模型卡报告该系列面向文本分类、聚类与检索，0.6B 版本提供1024维向量、最长32K上下文并支持100多种语言；这些公开结果只说明候选具有技术合理性，不能替代本项目的分组 OOF 证据（[Zhang et al., 2025](https://arxiv.org/abs/2506.05176)；[官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)）。论文已保存到本地 Zotero，item key 为 `KJWIZ7GU`。

## 3. 预登记模型

唯一候选冻结在 `configs/cleaning-qwen-embedding-baseline.yaml`，计划 ID 为 `1bb4cdfcb59c27c213624931d3d2d369`，完整 SHA-256 为 `1bb4cdfcb59c27c213624931d3d2d3691f88741e3f750c0fe6f902a45b9311a6`。

- 编码器：`Qwen/Qwen3-Embedding-0.6B`，revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`；
- 模型快照：revision 下12个非缓存文件逐文件校验，规范快照 SHA-256 为 `302e3ceebabd93cebf4f9b0a4bb42765c4504ff9aa3087720ec23497c3afc8bb`；其中主权重 `model.safetensors` 的 SHA-256 为 `0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`；
- 文本：仍使用冻结的标题＋正文单通道 `normalized_model_text`，不拆标题/正文；
- 任务说明：所有记录使用同一条中文游客 UGC/纯广告身份边界说明，不含标签答案、示例或平台；
- 最大长度：2048 tokens；末 token pooling；输出转 `float32` 后再次 L2 归一化；训练报告保存聚合截断条数、比例、最大值和P95，不保存正文或成员身份；
- 编码器：完全冻结，禁止 fine-tuning、LoRA 和远程 API；
- 分类器：唯一 `LogisticRegression(C=1.0, class_weight=balanced, solver=liblinear, max_iter=2000)`，使用原生 `predict_proba`；
- 本地执行：固定 Apple MPS、batch size 4、参数 `bfloat16`、输出 `float32`；设备、硬件、macOS/Darwin版本、Python，以及包含 `tokenizers`、`safetensors` 的依赖版本进入运行身份，CLI 不允许覆盖；
- 训练评价：Qwen 使用固定候选 leakage-group 最多5折 OOF；sparse comparator 是已封存的 nested OOF。两者绑定同一442条成员、标签和 leakage component，但不宣称外层折号完全相同；没有超参数搜索和模型族选择。

本地公开模型位于仓库相邻目录 `../Qwen3-Embedding-0.6B`，不进入 Git。运行时固定 `torch==2.13.0`、`transformers==5.15.1`、`sentence-transformers==6.0.0` 和 `huggingface-hub==1.28.0`。权重准备入口禁止远程代码和运行时联网回退；既有目录非法时失败关闭，不覆盖用户文件。

## 4. 比较与验收

比较锚点是 sparse 运行 `ce19406cd132e55b2eb00531f5cc4cd3` 的 candidate 模型 `1b68baa8bef99d6b9d75b7bf3226cfb4`，而不是更早的 `C=1` TF-IDF 模型。Qwen OOF 与 sparse candidate OOF 按成员、标签和 leakage component 逐条配对，以 component 为单位做5,000次固定种子 bootstrap。

独立验收策略位于 `configs/cleaning-qwen-model-acceptance.yaml`，策略 ID 为 `3d201246b06fa58f48f88083c81f0de1`，完整 SHA-256 为 `3d201246b06fa58f48f88083c81f0de16588da9520e30944c3bf5354ab06ff7a`。0.90尾部门与其非路由声明直接包含在该策略中，硬门顺序保持 UGC 安全优先：

1. 固定0.5诊断分界下，`related→unrelated` 点估计不得高于 sparse，且差值单侧90% bootstrap 上界不得超过+2个百分点；
2. log loss 至少改善0.01，且差值单侧90%上界小于0；
3. unrelated PR-AUC 点退化不超过0.005，且差值单侧90%下界不低于−0.01；
4. Brier 点估计不得恶化；
5. 在固定0.90高置信度诊断下，Qwen 将真实 `related` 判成高置信度 `unrelated` 的条数不得高于 sparse。

Accuracy 只作解释，不能覆盖安全门。固定 `[0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.975, 0.99]` 置信度网格对 sparse 与 Qwen 同时报告训练 OOF 的覆盖、错误、选择性风险、两类错误和差值；固定 `0.1/0.9` 同时比较两模型的中间带工作量代理。所有这些值均为开发诊断，不是 `T_keep/T_exclude`，不生成自动决定。`class_weight=balanced` 下的逻辑回归输出是开发样本条件概率分数，不可直接解释为约14,000条候选人口的后验概率或实际工作量。

若训练侧未通过，保留 sparse comparator，不读取 Qwen 验证集。若通过，只允许新模型对现有110条验证集做一次无拟合方向复核；不得根据验证结果改任务说明、长度、线性头、权重版本或再试第二个 Qwen 配置。锁定测试148条继续保持 `locked_not_opened`。

## 5. 实现与不可变 artifact

- `cleaning_prepare_qwen_embedding.py`：下载/校验公开权重，并可用两条内置合成文本烟雾测试；
- `cleaning_train_qwen_embedding_baseline.py`：只物化训练442条，先编码一次，再生成分组 OOF、最终线性头、配对验收和不可变运行包；
- 运行包保存训练嵌入缓存、含折号的逐成员 Qwen OOF、与 sparse 的逐成员 paired OOF、聚合报告、解析前计划/验收 YAML、线性头和完整 manifest；不复制公开 Qwen 权重；
- 加载或复用既有运行包前必须由调用方给出外部 manifest SHA-256；加载线性头还必须给出期望模型 ID。系统先校验固定文件名、路径、哈希、计划、快照与执行谱系，再允许复用或 `joblib.load`；
- manifest 固定 `test_status=locked_not_opened`、`threshold_status=UNSET`、`audit_status=UNSET`、`auto_cleaning_decisions_present=false` 和 `platform_used=false`；
- 所有私有嵌入、成员概率、分类器和运行包继续由 Git 忽略，不得提交。

## 6. 正式训练命令（当前禁止执行）

工作目录：`/Users/kawauso/Documents/Projects/TripPostResearch`

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
  --model-dir ../Qwen3-Embedding-0.6B \
  --artifact-root results/cleaning-qwen-embedding \
  --output-format human \
  --execute-training
```

命令要求工作树干净，并把执行时当前 Git `HEAD` 记录为代码身份；模型目录必须在仓库外，仓库内 artifact 根必须已被 Git 忽略。预期输出目录为 `results/cleaning-qwen-embedding/<run_id>/`；建议硬超时120分钟，监控进程存活、MPS 内存和该目录是否原子出现。按照当前阶段约束，在用户回来并明确确认正式训练前不得执行此命令。

## 7. 完成判据与仍未完成项

本实现阶段的完成判据是：配置可严格解析、权重身份可验证、合成文本本地编码成功、固定候选 OOF 与 artifact 测试通过、科研/工程文档同步、独立审查无阻塞问题。它不包含真实训练结果。

正式训练之后仍需：解释训练 OOF；若通过则实现并执行一次无拟合验证；冻结最终模型验收门、`T_keep/T_exclude`、审计事件/样本量/置信上限/恢复门和锁定测试门；最后才允许一次测试开启、无 `fit` 全量/增量推理与正式自动清洗。当前任何一项都不得误报为完成。
