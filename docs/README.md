# 理论研究文档索引

## 唯一规范源

[`data-dictionary/编码表.md`](data-dictionary/编码表.md)是研究范围、构念、标签、编码单位、值域、派生关系和标注方案的唯一最高标准。文件名永久固定为`编码表.md`，只在文档内部迭代版本。编码簿、方法报告、研究规划、决策记录和论文都是下游理论文档，不得反向覆盖编码表。

实得平台、样本量、数据哈希和模型结果以冻结manifest/artifact为事实源。如果实得事实与编码表的设计范围不同，必须显式披露并先决定是否修订编码表，不得为达成文字一致而改写事实。

## 状态词表

- `CURRENT_CANONICAL`：仅编码表使用。
- `CURRENT_ALIGNED`：已对齐当前编码表，可用于理论解释。
- `NEEDS_UPDATE`：尚有实质冲突，修复前不得作为当前依据。
- `PROPOSAL`：尚未写入编码表的建议，不生效。
- `HISTORICAL`：仅用于追溯，不参与当前执行。

## 当前理论文档

下列文件已完成对编码表v3.4.2的语义对齐，但统一处于`NOT_FROZEN`：编码表第零节T0关闭前，它们不能作为正式标注任务已经可执行的证明。

- [`data-dictionary/编码表.md`](data-dictionary/编码表.md)：`CURRENT_CANONICAL`。
- [`data-dictionary/编码簿_青岛旅游UGC编码框架.md`](data-dictionary/编码簿_青岛旅游UGC编码框架.md)：编码员执行说明。
- [`data-dictionary/编码维度主观性评定报告_v3.4.md`](data-dictionary/编码维度主观性评定报告_v3.4.md)：主观性与重叠风险复核。
- [`data-dictionary/编码表与BERT学习的对应关系.md`](data-dictionary/编码表与BERT学习的对应关系.md)：人工编码与模型学习目标对应。
- [`methods/BERT多头多标签编码框架设计说明.md`](methods/BERT多头多标签编码框架设计说明.md)：文本神经网络方法设计。
- [`methods/文本片段切分标准设计报告.md`](methods/文本片段切分标准设计报告.md)：固定片段与边界审计方法。
- [`planning/编码表与AI协同后续执行清单.md`](planning/编码表与AI协同后续执行清单.md)：两人团队的研究阶段门。
- [`decisions/2026-08-11-编码表最高标准与版本对齐规则.md`](decisions/2026-08-11-编码表最高标准与版本对齐规则.md)：理论文档的权威与对齐决策。
- [`../manuscript/论文草稿_v3.4.md`](../manuscript/论文草稿_v3.4.md)：当前论文Markdown草稿。

## 建议文档（不生效）

- [`data-dictionary/AT-EVL亚类方案可行性报告.md`](data-dictionary/AT-EVL亚类方案可行性报告.md)
- [`decisions/2026-08-10-生产端定位、KOL-KOC识别与受众端研究设计报告.md`](decisions/2026-08-10-生产端定位、KOL-KOC识别与受众端研究设计报告.md)

## 历史文档（仅追溯）

- [`data-dictionary/名词解释清单.md`](data-dictionary/名词解释清单.md)
- [`data-dictionary/创作者层级跨平台判定标准.md`](data-dictionary/创作者层级跨平台判定标准.md)
- [`data-dictionary/编码表问题清单与优先级.md`](data-dictionary/编码表问题清单与优先级.md)
- [`methods/机理传导指标计算公式推导说明.md`](methods/机理传导指标计算公式推导说明.md)
- [`planning/研究路径全景梳理.md`](planning/研究路径全景梳理.md)
- [`decisions/2026-08-10-研究可行性与逻辑链评估.md`](decisions/2026-08-10-研究可行性与逻辑链评估.md)

历史与建议文档保留当时正文，只增加状态、适用版本与替代指向，避免抹去审计轨迹。
