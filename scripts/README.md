# 命令入口

`scripts/` 只放薄命令入口：参数解析、配置读取和调用 `src/tourism_ugc_study/`。可复用规则、持久化、训练、策略和状态机逻辑必须留在 `src/`。

> **数据清洗状态**：`FRAMEWORK_FROZEN / THRESHOLD_PENDING / IMPLEMENTATION_PENDING`。当前已提供稳定配置、700 条参考证据只读校验、500/200 样本迁移 manifest，以及不打开锁定测试集的正式 baseline 训练入口；尚未执行首次正式训练，阈值、推理、审计和发布仍未启用。

## 现有入口清单

```text
annotation_adjudicate.py          # 确认重复关系的泄漏分组
annotation_prepare_calibration.py # 研究内容共同校准主表转换
cleaning_validate_reference.py   # 校验 700 条权威完成 CSV 并生成 500/200 迁移 manifest
cleaning_train_baseline.py        # 训练并封存字符 TF-IDF＋线性 SVM＋折外 Sigmoid baseline
```

仓库不提供旧协议配置、批处理、标签导入或发布入口。既有派生库仅作为700条
参考证据和泄漏关系的只读/追加式谱系来源，当前入口不会为旧协议建库、迁移或
恢复运行。

清洗不包含媒体文件下载、检查、标注、筛选或发布；视觉模型由 `vision_*` 入口在后续研究阶段独立运行。

## 已冻结、待实现的正式接口

后续代码阶段必须把训练、阈值策略和推理解耦：

1. **训练入口**已由 `cleaning_train_baseline.py` 实现。它显式接收冻结参考 CSV、参考 manifest、500/200样本迁移 manifest、只读派生数据库和泄漏分组身份；校验700条身份、成员、哈希与规范化文本后，输出不可变的字符 TF-IDF、线性 SVM、分组折外 Sigmoid 校准器、开发概率和锁定测试成员。训练阶段不预测测试集。
2. **阈值策略入口**独立保存 `T_keep`、`T_exclude`、保留集审计门、测试指标门、自动覆盖率门和恢复门；改变策略不调用训练。
3. **推理入口**显式接收冻结模型、冻结策略和目标批次；保存 `p_unrelated`、三段式路由、人工覆盖和输出 manifest。初始全量与未来新增批次使用同一入口，且入口不得调用 `fit`。
4. **人工证据入口**校验并封存人工灰区、未来新增人工结果和保留集审计的完成 CSV 与 manifest。现有 700 条标签由完成 CSV 直接读取，不导入 `text_post_annotations`。
5. **发布入口**只读取最终帖子决定。`exclude` 只影响派生分析发布，正式采集库始终只读。

稳定配置入口为 `configs/cleaning.yaml`，所有阈值与门均为 `UNSET`；因此 baseline 训练完成后也只能形成研究性概率证据，不得产生正式自动保留或自动排除。`cleaning_validate_reference.py` 和 `cleaning_train_baseline.py` 都只读打开派生库，并拒绝已经导入 `text_post_annotations` 的参考标签。

## baseline 后续执行顺序

先对与700条样本相同的候选构建封存泄漏分组。当前没有最终确认近重复关系时，不传 `--duplicate-final-review-ids`；以后如有确认记录，只能传显式 ID 清单。

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
  --csv <tourism-relevance-completed.csv> \
  --manifest <round-manifest.json> \
  --sample-migration-manifest <sample-migration.json> \
  --derived-db <derived.sqlite> \
  --leakage-build-id <leakage_build_id> \
  --artifact-root results/cleaning-models \
  --execute-training
```

该命令封存模型、校准器、切分 manifest、训练折外概率、验证概率和验证指标。测试成员只锁定为 `locked_not_opened`。下一阶段先用开发证据讨论阈值和验收门；策略冻结后才允许单次开启测试集。两项阈值仍为 `UNSET`，因此本命令不生成自动决定。

## 通用运行要求

所有入口使用显式 ID，不提供“最新运行”回退，不覆盖已有运行，不回写源库，也不在日志中输出原始正文、作者标识或源路径。正式运行必须保存配置、随机种子、Git SHA、输入与输出 manifest、artifact 哈希和机器可读状态。

代码实现后的完整验收至少包括：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```
