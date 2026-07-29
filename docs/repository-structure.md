# 科研仓库结构规范

版本：1.0  
日期：2026-07-28  
适用仓库：`tourism-ugc-study`

## 1. 设计目标

本结构服务于多平台旅游 UGC 的非破坏式清洗、人工编码、文本模型、视觉模型和论文分析。文本与视觉分别建模，不设置多模态模型分支。它解决四个问题：

1. 数据来源、清洗、标注和训练之间具有可追溯关系；
2. 原始数据与代码、实验产物和论文结果严格分离；
3. 当前约 3,044 条数据可以直接使用，并能平稳扩展到约 1 万条以内；
4. 研究者可以从同一数据清单和配置重建论文中的表格、图形和模型指标。

## 2. 目标目录树

```text
tourism-ugc-study/
├── .github/workflows/              # 后续 CI；未冻结依赖前不启用训练任务
├── configs/
│   ├── cleaning/                   # 清洗规则配置
│   ├── annotation/                 # 抽样、编码簿和仲裁配置
│   ├── models/
│   │   ├── text/                   # 文本模型配置
│   │   └── vision/                 # 视觉模型配置
│   └── experiments/                # 正式实验组合配置
├── data/
│   ├── raw/                        # 只放外部源数据说明或只读指针，不提交数据
│   ├── interim/                    # 清洗中间结果，不提交 Git
│   ├── processed/                  # 分析就绪派生库，不提交 Git
│   ├── annotations/
│   │   ├── templates/              # 标注轮次模板
│   │   ├── rounds/                 # 各轮独立人工标注
│   │   ├── adjudicated/            # 仲裁后的金标
│   │   ├── splits/                 # 训练/验证/测试清单
│   │   └── releases/               # 可复现实验使用的冻结标注版本
│   └── manifests/                  # 数据行数、哈希、字段和来源清单
├── src/tourism_ugc_study/
│   ├── data/                       # SQLite I/O、schema 和数据契约
│   ├── cleaning/                   # 规范化、质量标记、去重
│   ├── annotation/                 # 抽样、导入、校验、一致性和仲裁
│   ├── models/
│   │   ├── text/                   # TF-IDF/线性基线、BERT、多标签模型
│   │   └── vision/                 # 图像基线、视觉编码器、分类头
│   ├── evaluation/                 # 分平台指标、置信区间和误差分析
│   ├── analysis/                   # 论文统计分析和敏感性分析
│   └── utils/                      # 日志、随机种子、哈希和通用工具
├── scripts/
│   ├── cleaning/                   # 清洗命令入口
│   ├── annotation/                 # 抽样/导入/仲裁命令入口
│   ├── text/                       # 文本训练和推断入口
│   ├── vision/                     # 视觉训练和推断入口
│   ├── evaluation/                 # 评估与误差分析入口
│   └── release/                    # 冻结数据/模型/论文结果入口
├── notebooks/
│   ├── 00_sandbox/                 # 临时探索，不可作为论文唯一证据
│   ├── 01_data_audit/              # 数据审计与描述统计
│   ├── 02_annotation/              # 标注分布和一致性检查
│   ├── 03_text/                    # 文本模型探索
│   ├── 04_vision/                  # 视觉模型探索
│   └── 05_statistical_analysis/    # 论文统计分析检查
├── tests/
│   ├── unit/                       # 纯函数和单模块测试
│   ├── integration/                # SQLite—清洗—标注—模型链路测试
│   ├── regression/                 # 样本数、哈希和指标回归测试
│   └── fixtures/                   # 小型合成或脱敏测试样本
├── artifacts/
│   ├── runs/                       # 每次实验运行包
│   ├── checkpoints/                # 模型权重
│   ├── predictions/                # 逐条预测
│   └── embeddings/                 # 文本/图像表征
├── reports/
│   ├── figures/                    # 论文图
│   ├── tables/                     # 论文表
│   ├── metrics/                    # 指标与置信区间
│   └── logs/                       # 本地运行日志
├── docs/
│   ├── data-dictionary/            # 当前编码簿与维度复核报告
│   ├── methods/                    # 文本/视觉方法设计说明
│   ├── protocols/                  # 清洗和正式实验协议
│   └── decisions/                  # 研究决策记录
├── manuscript/
│   ├── main/                       # 论文主文稿
│   ├── supplements/                # 编码簿、附表和补充方法
│   ├── bibliography/               # BibTeX/RIS 等引用库
│   └── submission/                 # 目标期刊格式与投稿包
├── literature/
│   ├── bibliography/               # 可提交 Git 的文献元数据
│   ├── notes/                      # 结构化阅读笔记
│   └── searches/                   # 检索式、日期和筛选记录
├── governance/
│   ├── ethics/                     # 伦理、隐私与再识别风险
│   ├── data-management/            # 数据管理与保存策略
│   ├── licenses/                   # 平台条款、软件和数据许可记录
│   └── release/                    # 对外发布审查清单
└── provenance/
    ├── data/                       # 源库与派生数据谱系
    ├── environment/                # Python、系统和依赖快照
    ├── experiments/                # 正式实验索引
    └── releases/                   # 论文/数据/代码发布记录
```

## 3. 数据分层规则

| 层级 | 是否可改 | 是否提交 Git | 内容 |
| --- | --- | --- | --- |
| `data/raw/` | 否 | 否 | 外部正式 SQLite 或其只读指针；本项目源库仍位于 `TripPostCollect` |
| `data/interim/` | 可重建 | 否 | 清洗中间表、下载缓存、图像变换中间结果 |
| `data/processed/` | 可重建 | 否 | 分析就绪 SQLite、Parquet/CSV 和清洗清单 |
| `data/annotations/rounds/` | 追加，不覆盖 | 条件允许时提交去标识化标签 | 每位标注者的独立判断；引用 `docs/data-dictionary/` 中的编码簿版本 |
| `data/annotations/adjudicated/` | 版本化 | 是，须去标识化 | 仲裁金标及分歧说明 |
| `data/annotations/releases/` | 冻结 | 是，须通过发布审查 | 训练和论文实际使用的标注版本 |
| `data/manifests/` | 版本化 | 是 | 哈希、行数、字段、时间和生成命令 |

原始文本、图片 URL、作者平台 ID、主页、访问令牌或能直接再识别用户的字段，不应进入公开 Git 历史。标注表只保存稳定的项目内记录键和标签；需要阅读原文的工作文件放在 `data/annotations/private/`，该目录已被忽略。

## 4. 人工标注轮次规范

每轮目录命名为 `round_YYYYMMDD_purpose_vNN/`，至少包含：

```text
round_20260728_relevance_v01/
├── README.md              # 目的、抽样方法、编码簿、负责人和状态
├── items.csv              # 待标记录清单，不含原文和作者标识
├── labels_annotator-a.csv # 标注者 A 的独立结果
├── labels_annotator-b.csv # 标注者 B 的独立结果
└── manifest.json          # 输入哈希、随机种子、行数和生成命令
```

禁止让两位标注者写入同一结果文件。仲裁结果进入 `adjudicated/`，不得覆盖原始分歧。编码簿只在 `docs/data-dictionary/` 保留当前版本；标注行必须保存 `codebook_version` 和相应文件哈希。

## 5. 代码分层规则

- `src/` 放可导入、可测试的业务逻辑，不读取硬编码绝对路径。
- `scripts/` 只解析命令行参数并调用 `src/`；不在脚本里复制模型或清洗逻辑。
- `notebooks/` 用于探索和可视化；论文最终数字必须由脚本或可测试模块生成。
- 文本模型与视觉模型共享 `data/`、`evaluation/` 和 `utils/`，但各自的数据变换、训练和推断保持独立；图文结果如需在统计层面比较，应在 `analysis/` 完成，而不是新增多模态模型。
- 数据切分读取 `data/annotations/splits/` 的冻结清单，不在训练时重新随机切分。

## 6. 正式实验运行包

每次正式实验使用 `YYYYMMDD-HHMMSS_task_git7` 作为 `run_id`，在 `artifacts/runs/<run_id>/` 至少保存：

```text
config.yaml             # 实际解析后的完整配置
command.txt             # 启动命令
data_manifest.json      # 输入数据哈希与样本量
split_manifest.json     # 训练/验证/测试记录键哈希
environment.txt         # Python、关键依赖、CPU/GPU 信息
metrics.json            # 机器可读指标
summary.md              # 人可读结论、异常和限制
```

随机实验必须记录所有随机种子。按平台报告指标；同一作者和重复簇不能跨训练/测试分区。只有被论文实际引用的运行，才将其摘要复制或链接到 `provenance/experiments/`。

## 7. 版本命名

- 清洗规则：`cleaning-v1.0.0`
- 编码簿：`codebook-v3.4`
- 标注发布：`annotations-v0.1.0`
- 数据发布：`dataset-v0.1.0`
- 模型：`text-bert-multilabel-v0.1.0`、`vision-baseline-v0.1.0`
- 论文结果冻结：`paper-results-v0.1.0`

版本号表示研究工件变化，不等同于 Git commit。每个版本清单仍须记录 Git SHA 和上游数据哈希。

## 8. 当前文件布局

文档已经按用途归档，仓库工作树只保留当前有效版本。旧编码簿、历史研究路径和过期报告由 Git 历史保存，不在当前目录重复保留。

| 当前文件 | 用途 | 状态 |
| --- | --- | --- |
| `docs/data-dictionary/编码簿_山东旅游UGC编码框架_v3.4.md` | 唯一有效编码簿 | 当前版本 |
| `docs/methods/BERT多头多标签编码框架设计说明.md` | 文本模型方法设计 | 当前版本 |
| `docs/protocols/` | 清洗与实验协议 | 当前版本 |
| `manuscript/main/论文草稿_v3.4.md` | 当前主稿 | 当前版本 |
| `literature/notes/` | 文献综述和阅读记录 | 按日期维护 |
| `scripts/build_research_dataset.py` | 既有派生构建脚本 | 待后续重构到 `src/` |
| `data/processed/` | 分析就绪派生数据 | 按当前源库重新生成 |

## 9. 首次公开前的门槛

1. 补充 `CITATION.cff`、作者和机构信息；这些信息不能由工具代填。仓库已采用 MIT License。
2. 审查平台条款、著作权、个人信息、再识别风险和图片公开范围。
3. 确认 Git 历史没有原始数据库、访问令牌、作者直接标识或受限 PDF。
4. 用全新环境复跑最小清洗测试和一项基线实验。
5. 冻结代码、数据清单、标注、实验运行和论文结果之间的版本映射。
