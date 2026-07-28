# 科研仓库结构规范

版本：2.0
日期：2026-07-29
适用仓库：`tourism-ugc-study`

## 1. 设计原则

本仓库既要支持数据清洗、人工标注、文本模型、视觉模型和论文复现，也要让人类成员能快速判断文件应放在哪里。采用以下规则：

1. 不为未来可能出现的内容预建目录；有第一份实际文件时再创建。
2. 文件较少时优先使用清晰前缀平铺，避免只有一层占位文件的目录树。
3. `docs/` 保持按文档类型分类，因为研究文档会持续增长。
4. `tests/` 与 `src/tourism_ugc_study/` 的实际模块对应，不增加统一的 `unit/integration/regression` 中间层。
5. 不使用 `research/` 等语义宽泛的总括目录；论文、文献、治理和结果各自使用明确名称。
6. 原始数据、代码、运行结果和论文材料仍保持边界清楚；简化目录不降低可追溯性。

## 2. 当前目录树

```text
tourism-ugc-study/
├── .github/
│   └── pull_request_template.md
├── configs/                         # 配置平铺，以领域前缀区分
├── data/
│   ├── annotations/
│   │   └── templates/               # 当前实际存在的标注模板
│   └── processed/                   # 派生 SQLite 与 manifest，数据文件忽略
├── docs/
│   ├── data-dictionary/             # 编码簿与编码维度说明
│   ├── methods/                     # 科研方法与模型设计
│   ├── protocols/                   # 工程、清洗和实验协议
│   └── decisions/                   # 研究决策记录
├── governance/                      # 伦理、许可与公开审查文件，当前平铺
├── literature/
│   ├── notes/                       # 结构化阅读笔记
│   └── papers/                      # 本地受限 PDF，默认忽略
├── manuscript/                      # 当前论文及实际形成的投稿材料，当前平铺
├── notebooks/                       # 探索性 notebook，当前平铺
├── results/                         # 运行包、模型、报告、谱系与本地导出
│   └── exports/                     # 当前存在的本地导出，默认忽略
├── scripts/                         # 命令入口平铺，以领域前缀区分
├── src/tourism_ugc_study/
│   ├── cleaning/                    # 数据清洗与增量调度
│   ├── annotation/                  # 抽样、标注与仲裁
│   └── models/
│       ├── text/                    # 文本模型
│       └── vision/                  # 视觉模型
└── tests/
    └── cleaning/                    # 当前已有清洗测试；其余随源码出现
```

README、LICENSE、`pyproject.toml` 等仓库级文件保留在根目录。

## 3. 何时允许新增子目录

满足任一条件才建立新目录：

- 已经出现第一份需要放入其中的实际文件；
- 与相邻文件具有不同的 Git 忽略、隐私或访问控制要求；
- 生命周期明显不同，例如本地论文 PDF 与可提交的阅读笔记；
- 同类文件增多，平铺后已经难以浏览。

禁止为了展示“完整科研流程”而提前创建空目录、空 README 或 `.gitkeep`。结构规范描述创建规则，不要求磁盘上提前存在全部路径。

## 4. 代码、脚本、配置和测试

- `src/tourism_ugc_study/` 只放可导入、可测试的核心逻辑。目前保留用户明确需要的 `cleaning/`、`annotation/`、`models/text/` 和 `models/vision/`。
- 新的源码模块首次出现时，再建立对应包；不要预建 `analysis/`、`evaluation/` 或 `utils/`。
- `scripts/` 保持平铺，使用 `cleaning_*.py`、`annotation_*.py`、`text_*.py`、`vision_*.py` 等名称。脚本只解析参数并调用 `src/`。
- `configs/` 保持平铺，使用相同领域前缀。只有同类配置明显增多后才分类。
- `tests/` 跟随源码模块，例如 `src/.../cleaning/` 对应 `tests/cleaning/`，`src/.../models/text/` 对应 `tests/models/text/`。测试性质通过文件名表达，如 `test_*_integration.py` 或 `test_*_regression.py`。

## 5. 数据与人工标注

正式采集库仍由相邻的 `TripPostCollect` 项目管理，本仓库只读访问，不建立 `data/raw/` 副本。

- `data/processed/` 保存实际存在的派生 SQLite、清洗 manifest 和分析就绪文件，默认不提交数据文件。
- `data/annotations/templates/` 保存现有 CSV 模板。
- 每次真实标注直接创建 `data/annotations/round_YYYYMMDD_purpose_vNN/`，将独立标注、仲裁、切分和轮次 manifest 放在同一轮目录。
- 跨轮冻结清单文件较少时直接放在 `data/annotations/`；增多后再建立 `releases/`。
- 需要原文、图片或作者信息的本地标注材料放在被忽略的 `data/annotations/private/`。

原始文本、图片、作者直接标识和访问令牌不得进入 Git 历史。

## 6. 运行结果与研究谱系

过去的 `artifacts/`、`reports/`、`output/` 和 `provenance/` 合并为 `results/`。每次正式运行直接建立：

```text
results/<run_id>/
├── config.yaml
├── command.txt
├── data_manifest.json
├── split_manifest.json
├── environment.txt
├── metrics.json
├── summary.md
└── ...                         # 该运行实际产生的模型、预测、图表或日志
```

配置、环境、数据哈希和指标与运行结果放在一起，即构成该运行的谱系，不再复制到独立目录。论文只引用状态为 `accepted` 的 `run_id`。

`results/exports/` 保存当前实际存在的本地 DOCX、PDF 或 HTML 导出；论文图表实际形成时再创建 `results/paper/`。

## 7. 文档、论文、文献与治理

- `docs/` 不平铺：编码簿、方法、协议和决策分别进入现有四个分类目录。
- `manuscript/` 在文件较少时平铺；真正进入投稿阶段后，可按一次投稿建立 `submission_YYYYMMDD/`。
- `literature/notes/` 保存阅读笔记，`literature/papers/` 保存默认不提交的本地全文；题录和检索文件较少时直接放在 `literature/`。
- `governance/` 使用明确文件名平铺；只有伦理、许可或数据管理文件实际增多后再分类。
- `notebooks/` 使用 `NN_topic_initials_YYYYMMDD.ipynb` 命名并平铺，正式论文数字必须由脚本重建。

## 8. 本次简化映射

| 原位置 | 新位置或处理 |
| --- | --- |
| `artifacts/`、`reports/`、`output/`、`provenance/` | 合并为 `results/`；本地导出进入 `results/exports/` |
| `papers/` | 移入 `literature/papers/` |
| `manuscript/main/` | 当前稿件移到 `manuscript/` 根部 |
| `governance/licenses/`、`governance/release/` | 当前文件移到 `governance/` 根部 |
| `configs/*/`、`scripts/*/`、`notebooks/NN_*/` | 删除纯占位层级，未来以文件名前缀平铺 |
| `tests/unit|integration|regression/` | 改为直接对应源码模块，当前测试进入 `tests/cleaning/` |
| `data/raw/`、`data/interim/`、空的 annotation/manifests 目录 | 删除；在真实文件出现时按本规范创建 |
| `src/.../analysis|data|evaluation|utils/` | 删除空包；出现实际代码时再建立 |

## 9. 版本与公开门槛

清洗规则、编码簿、标注发布、数据发布、模型和论文结果继续使用各自版本号；每个版本 manifest 必须记录 Git SHA 和上游数据哈希。

首次公开前仍需：补充 `CITATION.cff` 与真实作者信息；完成伦理、版权和平台条款审查；确认 Git 历史没有受限数据；在全新环境复跑最小清洗测试；冻结代码、数据、标注、运行与论文结果的映射。
