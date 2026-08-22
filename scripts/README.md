# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用 `src/tourism_ugc_study/`。可复用规则、持久化、训练、策略和状态机逻辑必须留在 `src/`。

> **数据清洗状态**：`FRAMEWORK_FROZEN / THRESHOLD_PENDING / IMPLEMENTATION_PENDING`。当前脚本和旧配置只保留准备、诊断或历史复算能力，不构成正式自动清洗入口；本阶段不修改代码，也不提供可执行的正式训练、阈值或发布命令。

## 现有入口清单

```text
cleaning_snapshot_source.py       # 冻结只读帖子快照并初始化运行
cleaning_discover_increment.py    # 登记帖子版本与待处理阶段
cleaning_create_batch.py          # 稳定冻结帖子批次
cleaning_run_batch.py             # 领取、查询和推进通用文本任务
cleaning_resume_batch.py          # 显式恢复失败或阻塞的文本任务
cleaning_process_text.py          # 确定性文本与重复候选
annotation_export_tasks.py        # 文本抽样与旅游相关性任务导出/封存
annotation_import_annotations.py  # 旧人工记录接口；不得用于复制现有 700 条权威标签
annotation_adjudicate.py          # 确认重复关系的泄漏分组
annotation_prepare_calibration.py # 研究内容共同校准主表转换
text_train_relevance.py           # 现有线性相关性基线；尚未满足正式训练契约
cleaning_release.py               # 现有派生发布构建与复验
```

清洗不包含媒体文件下载、检查、标注、筛选或发布；视觉模型由 `vision_*` 入口在后续研究阶段独立运行。

## 已冻结、待实现的正式接口

后续代码阶段必须把训练、阈值策略和推理解耦：

1. **训练入口**显式接收冻结参考 CSV、参考 manifest、只读派生数据库和泄漏分组身份；校验 700 条身份、成员、哈希与规范化文本后，输出不可变的字符 TF-IDF、线性 SVM、分组折外 Sigmoid 校准器和测试运行包。
2. **阈值策略入口**独立保存 `T_keep`、`T_exclude`、保留集审计门、测试指标门、自动覆盖率门和恢复门；改变策略不调用训练。
3. **推理入口**显式接收冻结模型、冻结策略和目标批次；保存 `p_unrelated`、三段式路由、人工覆盖和输出 manifest。初始全量与未来新增批次使用同一入口，且入口不得调用 `fit`。
4. **人工证据入口**校验并封存人工灰区、未来新增人工结果和保留集审计的完成 CSV 与 manifest。现有 700 条标签由完成 CSV 直接读取，不导入 `text_post_annotations`。
5. **发布入口**只读取最终帖子决定。`exclude` 只影响派生分析发布，正式采集库始终只读。

稳定配置入口将为 `configs/cleaning.yaml`，当前尚不存在。所有阈值与门均为 `UNSET`；因此即使旧代码可以执行某些步骤，也不得据此产生正式自动保留或自动排除。

## 通用运行要求

所有入口使用显式 ID，不提供“最新运行”回退，不覆盖已有运行，不回写源库，也不在日志中输出原始正文、作者标识或源路径。正式运行必须保存配置、随机种子、Git SHA、输入与输出 manifest、artifact 哈希和机器可读状态。

代码实现后的完整验收至少包括：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```
