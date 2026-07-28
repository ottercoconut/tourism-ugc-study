# 配置目录

配置文件按研究阶段分组：`cleaning/`、`annotation/`、`models/text/`、`models/vision/` 和 `experiments/`。

正式配置应满足：

- 不包含密码、令牌和本机绝对路径；
- 固定随机种子、输入 manifest、输出 `run_id` 和版本号；
- 训练配置明确数据切分、模型、损失、优化器、停止条件和评估指标；
- 运行时解析后的完整配置复制到对应的 `artifacts/runs/<run_id>/config.yaml`。

模型和标注方案未冻结前，本目录只保留结构，不创建带有猜测参数的“默认配置”。
