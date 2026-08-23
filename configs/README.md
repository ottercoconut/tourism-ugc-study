# 配置目录

配置文件暂时平铺存放，以文件名前缀区分用途，例如 `cleaning-*.yaml`、`annotation-*.yaml`、`text-*.yaml`、`vision-*.yaml` 和 `experiment-*.yaml`。只有某一类配置实际增多后才建立子目录。

正式配置应满足：

- 不包含密码、令牌和本机绝对路径；
- 固定随机种子、输入 manifest、输出 `run_id`、内部 artifact ID 和哈希；
- 训练配置明确数据切分、模型、损失、优化器、停止条件和评估指标；
- 运行时解析后的完整配置复制到对应的 `results/<run_id>/config.yaml`。

文本清洗工程框架已冻结，状态为 `FRAMEWORK_FROZEN / REFERENCE_DEDUP_FINALIZED / THRESHOLD_PENDING`；唯一最终700条及其 leakage build 已封存，论文内容编码和视觉模型仍不得创建带有猜测参数的默认配置或空子目录。

当前稳定入口为 `configs/cleaning.yaml`。`reference` 节冻结 `final-nonduplicate-model-reference`、700/500/200 计数、字符 3–5 gram TF-IDF、`0.80` 候选阈值、全局候补队列、固定种子和“无法证明概率有效性时标记不可用”的失败关闭行为。`0.80` 不是自动删除阈值；只有精确规范哈希或人工最终确认边能形成重复分量。平台配额和平台排序被显式禁止。

同一文件还冻结 baseline 的全局字符 TF-IDF、固定 `C=1.0` 线性 SVM、分组折外 Sigmoid 校准、全局时间留出和三段式策略接口。`T_keep`、`T_exclude`、审计门、测试指标门、自动覆盖率门和恢复门全部为 `UNSET`。源库、派生库、最终参考 CSV/manifest、leakage build、artifact 根目录和运行身份由命令行显式传入；训练不再接收独立样本框迁移文件。

`cleaning-text-challenger.yaml` 预登记首轮54个稀疏候选：字符 TF-IDF＋LinearSVC、binary count＋类别 L1 NB log-count ratio＋LinearSVC、字符 TF-IDF＋`liblinear` LogisticRegression 各18个；计划完整 SHA-256 为 `ad515917c735ce77ed5231f0fa25bd536e8395115f22b9bf4e5035f4df199301`。标题/正文分通道、平台特征和未登记网格都被拒绝。`cleaning-model-acceptance.yaml` 独立冻结 UGC 安全优先门；两份配置都不设置生产路由阈值，也不授权打开锁定测试。

`cleaning-qwen-embedding-baseline.yaml` 预登记唯一 Qwen3-Embedding-0.6B 本地语义 baseline：固定上游 revision 与主权重 SHA-256、单通道全文、统一中文任务说明、2048 token、1024维 L2 归一化向量和唯一 `C=1` 逻辑回归概率头；计划完整 SHA-256 为 `edba25172f2b68ba875f29d3742e302902059456534455b96e4201a6b75bf756`。`cleaning-qwen-model-acceptance.yaml` 把已验证 sparse candidate 绑定为 comparator，完整 SHA-256 为 `4ccee62e1c9bb6822fc4cd0f603a65b01cb8d09f65de73b95dc00ac67fdae407`。固定0.1/0.9只是开发工作量代理，不是路由阈值；正式训练、验证、测试、阈值和审计状态彼此分离。

`cleaning-text-normalization-v1.yaml` 单独保存结构化正文投影、确定性文本规范化、结构检查、精确重复和近似候选参数。`structured_text` 冻结 Quill Delta 的 `ops`/`insert` 键、可忽略的图片与截断嵌入及结构损坏时的失败关闭策略；识别只看内容结构，不看平台。主配置用“人工版本＋文件 SHA-256”锁定规则文件；任一字节变化都会使加载失败，必须显式更新主配置和受影响 artifact。`near_duplicate.candidate_threshold_ppm` 仅是候选召回线，`final_threshold` 在完成人工文本对验证前必须保持 `null`。

主配置同时用 `text_runtime` 锁定 CPython、Unicode、regex、NumPy、SciPy 和 scikit-learn 的规范哈希；项目依赖对参与确定性文本输出的包使用精确版本。运行时不一致时拒绝处理，而不是复用旧阶段任务或旧候选构建。
