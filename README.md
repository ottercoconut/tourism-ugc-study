# tourism-ugc-study

面向青岛旅游目的地感知研究的多平台 UGC 数据、人工编码、文本模型与视觉模型项目。

当前仓库的 GitHub visibility 为 **Private**。仓库中由项目作者原创的代码、编码表和研究文档采用 [MIT License](LICENSE)；未进入仓库的第三方 UGC、论文 PDF、外部模型和其他第三方材料不在该授权范围内。

当前采集库有 3,044 条阶段性记录，其中青岛 2,274 条、济南 454 条、烟台 315 条、城市缺失 1 条。后续数据采集与数据集定稿工作将形成只含青岛、约 1 万条以内的正式研究数据；这是清洗开始前的输入准备，不属于数据清洗目标。清洗方案不会从当前混合库筛选城市或为其他城市生成清洗标签。本仓库只保存可复现代码、配置、去标识化标注、数据清单和研究产物；原始采集证据库位于相邻的 `TripPostCollect` 项目，不复制、不回写。

## 核心原则

1. **源数据不可变**：正式 SQLite 只读，所有清洗和标注均生成版本化派生结果。
2. **代码与产物分离**：可复用逻辑在 `src/`，命令入口在 `scripts/`，运行产物在 `artifacts/`。
3. **标注可追溯**：抽样清单、独立标注、仲裁结果、编码簿版本分别保存。
4. **实验可复现**：每次正式实验冻结配置、随机种子、数据清单哈希、代码版本和环境信息。
5. **隐私与版权优先**：原文、图片、作者标识、访问令牌和模型权重默认不进入 Git。

## 主要目录

| 目录 | 用途 |
| --- | --- |
| `data/` | 原始数据指针、派生数据、人工标注、数据切分和清单 |
| `src/tourism_ugc_study/cleaning/` | 数据清洗与质量标记逻辑 |
| `src/tourism_ugc_study/annotation/` | 抽样、标注导入、编码校验和仲裁逻辑 |
| `src/tourism_ugc_study/models/text/` | 文本基线、BERT/多头多标签模型与推断代码 |
| `src/tourism_ugc_study/models/vision/` | 图像分类、表征学习与视觉推断代码 |
| `configs/` | 清洗、标注和模型实验配置 |
| `notebooks/` | 仅用于探索和结果检查的笔记本 |
| `tests/` | 单元、集成、回归和数据契约测试 |
| `artifacts/` | 运行记录、模型权重、预测和嵌入，默认不提交 Git |
| `reports/` | 可审计的指标、表格、图形和质量报告 |
| `manuscript/` | 论文正文、补充材料、参考文献和投稿版本 |
| `governance/` | 伦理、数据管理、许可和公开发布审查 |
| `provenance/` | 数据、环境、实验与发布谱系 |

完整目录说明、命名规则和现有文件迁移表见 [仓库结构规范](docs/repository-structure.md)。
许可证边界与公开策略见 [许可策略](governance/licenses/license-strategy.md)。

## 当前可用入口

- 数据清洗科研方案：[docs/methods/data-cleaning-research.html](docs/methods/data-cleaning-research.html)
- 数据清洗工程方案：[docs/protocols/data-cleaning-engineering.md](docs/protocols/data-cleaning-engineering.md)
- 既有派生构建脚本：[scripts/build_research_dataset.py](scripts/build_research_dataset.py)
- 最新人工编码簿：[docs/data-dictionary/编码簿_山东旅游UGC编码框架_v3.3.md](docs/data-dictionary/编码簿_山东旅游UGC编码框架_v3.3.md)
- 当前论文草稿：[manuscript/main/论文草稿_v3.3.md](manuscript/main/论文草稿_v3.3.md)

## Python 环境

项目固定使用 CPython 3.13.5。所有命令通过项目虚拟环境运行：

```bash
.venv/bin/python --version
.venv/bin/python -m pytest -q
```

正式模型依赖尚未冻结；新增依赖时应先记录用途和版本，再仅安装到 `.venv`。

## License

本仓库中由项目作者原创的代码、编码表和研究文档采用 [MIT License](LICENSE)。MIT 授权不延伸至第三方平台 UGC、论文全文、预训练模型或其他由第三方持有权利的材料；这些材料即使在本地研究环境中存在，也不属于本仓库的许可内容。
