# tourism-ugc-study

面向青岛旅游目的地感知研究的多平台 UGC 数据、人工编码、文本模型与视觉模型项目。

当前仓库的 GitHub visibility 为 **Private**。仓库中由项目作者原创的代码、编码表和研究文档采用 [MIT License](LICENSE)；未进入仓库的第三方 UGC、论文 PDF、外部模型和其他第三方材料不在该授权范围内。

正式采集库由上游约束为只含青岛相关数据，当前 schema 不再保存城市字段；最终输入量随采集进度动态变化，并由每次只读快照的 manifest 重新统计。城市范围是清洗开始前的输入契约，不是清洗目标，清洗不会生成逐条城市标签。本仓库只保存可复现代码、配置、去标识化标注、数据清单和研究产物；原始采集证据库位于相邻的 `TripPostCollect` 项目，不回写，运行时一致性快照位于 Git 忽略的派生数据目录。

数据清洗 v2.4 的非人工工程实现已经完成：当前派生 schema 为 v25，覆盖只读快照、增量调度、确定性文本、文本人工工作流与线性相关性基线、图片角色/指纹/复核/审计、帖子最终决定、分析去重和不可变发布。2026-08-01 全库回归为 `332 passed, 8 warnings`。这不代表正式数据已经清洗或发布；真实文本标注、真实图片、正式质量审计和 formal `accept-release` 仍由 GitHub Issue #12 跟踪。

## 核心原则

1. **源数据不可变**：正式 SQLite 只读，所有清洗和标注均生成版本化派生结果。
2. **代码与产物分离**：可复用逻辑在 `src/`，命令入口在 `scripts/`，运行产物统一放在 `results/`。
3. **标注可追溯**：抽样清单、独立标注、仲裁结果、编码簿版本分别保存。
4. **实验可复现**：每次正式实验冻结配置、随机种子、数据清单哈希、代码版本和环境信息。
5. **隐私与版权优先**：原文、图片、作者标识、访问令牌和模型权重默认不进入 Git。

## 主要目录

| 目录 | 用途 |
| --- | --- |
| `data/` | 派生数据和人工标注；正式采集库仍在相邻项目且只读 |
| `src/tourism_ugc_study/cleaning/` | 数据清洗与质量标记逻辑 |
| `src/tourism_ugc_study/annotation/` | 抽样、标注导入、编码校验和仲裁逻辑 |
| `src/tourism_ugc_study/models/text/` | 文本基线、BERT/多头多标签模型与推断代码 |
| `src/tourism_ugc_study/models/vision/` | 图像分类、表征学习与视觉推断代码 |
| `configs/` | 平铺保存清洗、标注和模型配置，以文件名前缀区分 |
| `scripts/` | 平铺保存命令入口，以领域前缀区分 |
| `tests/` | 与 `src/` 实际模块对应的测试目录 |
| `notebooks/` | 平铺保存探索和结果检查笔记本 |
| `docs/` | 按编码与术语、方法、协议、规划和决策分类的研究文档 |
| `manuscript/` | 当前论文正文与实际形成的投稿材料 |
| `literature/` | 文献元数据、检索记录、阅读笔记和本地受限 PDF |
| `governance/` | 伦理、数据管理、许可和公开发布审查文件 |
| `results/` | 运行包、模型、指标、图表、日志、谱系和本地导出 |

完整目录说明、命名规则和现有文件迁移表见 [仓库结构规范](docs/repository-structure.md)。
许可证边界与公开策略见 [许可策略](governance/license-strategy.md)。

## 当前可用入口

- 数据清洗科研方案：[docs/methods/数据清洗科研方案.html](docs/methods/数据清洗科研方案.html)
- 数据清洗工程方案：[docs/protocols/数据清洗工程方案.md](docs/protocols/数据清洗工程方案.md)
- 文本清洗人工标注方法：[docs/protocols/文本数据清洗人工标注方法.md](docs/protocols/文本数据清洗人工标注方法.md)
- 图片正式执行与报告边界：[docs/protocols/图片数据清洗正式执行与结果报告边界.md](docs/protocols/图片数据清洗正式执行与结果报告边界.md)
- 数据清洗输入快照入口：[scripts/cleaning_snapshot_source.py](scripts/cleaning_snapshot_source.py)
- 文本处理入口：[scripts/cleaning_process_text.py](scripts/cleaning_process_text.py)
- 图片处理与复核入口：[scripts/cleaning_process_images.py](scripts/cleaning_process_images.py)、[scripts/cleaning_review_images.py](scripts/cleaning_review_images.py)
- 显式发布入口：[scripts/cleaning_release.py](scripts/cleaning_release.py)
- 既有派生构建脚本：[scripts/build_research_dataset.py](scripts/build_research_dataset.py)
- 最新人工编码簿：[docs/data-dictionary/编码簿_青岛旅游UGC编码框架.md](docs/data-dictionary/编码簿_青岛旅游UGC编码框架.md)
- 当前论文草稿：[manuscript/论文草稿_v3.4.md](manuscript/论文草稿_v3.4.md)

## Python 环境

项目固定使用 CPython 3.13.5。所有命令通过项目虚拟环境运行：

```bash
uv python install 3.13.5
uv venv --python 3.13.5 --seed .venv
.venv/bin/python -m pip install -r requirements-dev.txt

.venv/bin/python --version
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

`requirements-dev.txt` 会以 editable 模式安装当前项目及其运行依赖，并补充测试工具。数据清洗确定性文本与线性相关性基线依赖已经在项目依赖和配置摘要中冻结；其他正式内容/视觉模型新增依赖时，应先记录用途和版本，再仅安装到 `.venv`。

## License

本仓库中由项目作者原创的代码、编码表和研究文档采用 [MIT License](LICENSE)。MIT 授权不延伸至第三方平台 UGC、论文全文、预训练模型或其他由第三方持有权利的材料；这些材料即使在本地研究环境中存在，也不属于本仓库的许可内容。
