# AGENTS.md

本仓库用于旅游 UGC 的数据清洗、人工编码、文本模型、视觉模型和论文分析。

## 目录用途

- `src/tourism_ugc_study/`：可复用、可测试的核心 Python 代码。
  - `cleaning/`：文本规范化、质量检查和重复检测。
  - `annotation/`：抽样、标注导入、一致性检验和仲裁。
  - `models/text/`：文本基线、BERT 和多标签文本模型。
  - `models/vision/`：视觉编码、图像分类和视觉模型。
- `scripts/`：平铺保存清洗、标注、训练、评估和发布入口，以 `cleaning_`、`annotation_`、`text_`、`vision_` 等前缀区分；核心逻辑放在 `src/`。
- `configs/`：平铺保存配置，以领域前缀区分；同类配置实际增多后才建立子目录。
- `data/processed/`：派生 SQLite、manifest 和分析就绪结果，默认不提交 Git。
- `data/annotations/`：标注模板及实际发生的 `round_*` 轮次；编码簿仍在 `docs/data-dictionary/`。
- `tests/`：目录与 `src/tourism_ugc_study/` 的实际模块对应；当前测试位于 `tests/cleaning/`，不要预建空的测试层级。
- `notebooks/`：平铺保存探索、审计和可视化检查；论文最终结果必须由脚本重建。
- `docs/`：保持分类目录；`data-dictionary/`、`methods/`、`protocols/`、`planning/`、`decisions/` 分别保存编码与术语、方法、协议、研究规划和研究决策。
- `manuscript/`：当前论文正文放在根部，已经形成的格式化或投稿文档放在 `submission/`。
- `literature/`：文献元数据、检索记录和阅读笔记；本地受限 PDF 位于 `literature/papers/` 且默认不提交 Git。
- `governance/`：伦理、隐私、许可和公开发布审查；文件较少时保持平铺。
- `results/`：统一保存过去分散的运行包、模型、预测、指标、图表、日志、谱系和本地导出；默认不提交可重建或大型文件。

## 基本约定

- 使用 CPython 3.13.5，所有 Python 命令通过 `.venv/bin/python` 运行。
- 不为未来可能出现的内容预建目录；只有出现实际文件，或存在明确的隐私、Git 忽略、生命周期边界时才增加子目录。
- `docs/` 始终按文档类型分类；`tests/` 始终跟随 `src/` 模块结构，二者不适用“全部平铺”。
- 正式采集数据库只读，清洗和标注不得回写源库。
- 不提交原始 UGC、作者标识、访问令牌、数据库、模型权重或受限 PDF。
- 可复用逻辑写入 `src/`，`scripts/` 只作为入口，notebook 只用于探索。
- 正式实验应记录配置、随机种子、数据 manifest、代码版本和评估结果。
- 数据清洗的科研方案与工程方案共享版本号；标签、抽样、算法、阈值原则或决策状态变化时，必须在同一 commit/PR 中同步更新两份文档。

## Git 与 GitHub 约定

- Commit 标题和正文以简体中文为主，简洁说明“做了什么”和“为什么”。
- Pull Request 的标题、说明、检查结果和评审回复以简体中文为主。
- 分支名使用描述实际任务的简短英文小写与连字符，不得包含 `codex`、`agent` 等代理或工具身份字样。
- 不把无关变更混入同一提交；涉及数据、标注或模型结果时说明对应版本。
