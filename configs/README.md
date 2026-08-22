# 配置目录

配置文件暂时平铺存放，以文件名前缀区分用途，例如 `cleaning-*.yaml`、`annotation-*.yaml`、`text-*.yaml`、`vision-*.yaml` 和 `experiment-*.yaml`。只有某一类配置实际增多后才建立子目录。

正式配置应满足：

- 不包含密码、令牌和本机绝对路径；
- 固定随机种子、输入 manifest、输出 `run_id`、内部 artifact ID 和哈希；
- 训练配置明确数据切分、模型、损失、优化器、停止条件和评估指标；
- 运行时解析后的完整配置复制到对应的 `results/<run_id>/config.yaml`。

文本清洗工程框架已冻结，状态为 `THRESHOLD_PENDING / IMPLEMENTATION_PENDING`；论文内容编码和视觉模型仍不得创建带有猜测参数的默认配置或空子目录。

当前稳定入口为 `configs/cleaning.yaml`。它冻结全局字符 TF-IDF、固定 `C=1.0` 的线性 SVM、五折上限的分组折外 Sigmoid 校准、全局时间留出和三段式策略接口；这些参数对应已实现的首个正式 baseline，不是阈值或最终效果声明。平台最低名额、平台负例门和平台审计配额均不进入配置。`T_keep`、`T_exclude`、保留集审计门、测试指标门、自动覆盖率门和恢复门当前全部为 `UNSET`。源库、派生库、参考 CSV、参考 manifest、样本迁移 manifest、泄漏分组身份、artifact 根目录、目标批次与运行身份仍由命令行显式传入，不写入共享配置。

`cleaning-text-normalization-v1.yaml` 单独保存确定性文本规范化、结构检查、精确重复和近似候选参数。主配置用“人工版本＋文件 SHA-256”锁定它；规则文件任一字节变化都会使加载失败，必须显式更新主配置和受影响阶段版本。`near_duplicate.candidate_threshold_ppm` 仅是待人工校准的候选召回线，`final_threshold` 在完成人工文本对验证前必须保持 `null`。

主配置同时用 `text_runtime` 锁定 CPython、Unicode、regex、NumPy、SciPy 和 scikit-learn 的规范哈希；项目依赖对参与确定性文本输出的包使用精确版本。运行时不一致时拒绝处理，而不是复用旧阶段任务或旧候选构建。
