# tourism-ugc-study

面向青岛旅游目的地感知研究的多平台 UGC 数据、人工编码、文本模型与视觉模型项目。

> 2026-09-12状态：原始抓取内容与本次清洗候选按`research-data-20260912`冻结。
> 原始集19,984条post、161,377张图片；研究输入19,255条，清洗候选为keep 10,914、
> exclude 5,305、manual_review 3,036。全部本轮人工终审仍为pending，正式keep表为0。
> **冻结候选不等于最终研究数据集**；当前清洗状态仍为`CANDIDATES_READY_AWAITING_HUMAN_REVIEW`。

当前仓库的 GitHub visibility 为 **Private**。仓库中由项目作者原创的代码、编码表和研究文档采用 [MIT License](LICENSE)；未进入仓库的第三方 UGC、论文 PDF、外部模型和其他第三方材料不在该授权范围内。

正式采集工作库位于相邻`TripPostCollect`项目，本仓库只读访问。上游可继续采集，但本次冻结版本不再扩充；`topic_relevant=1`是关键词主题门，不等于清洗keep。原始内容及图片已有独立本地归档，SQLite保留原文、ID及必要关系，不复制账号/调度控制面。Git仅保存代码、配置、允许提交的标注模板、汇总与摘要；原文、图片、数据库和私有清单均被忽略。

## 核心原则

1. **源数据不可变**：正式 SQLite 只读，所有清洗和标注均生成版本化派生结果。
2. **代码与产物分离**：可复用逻辑在 `src/`，命令入口在 `scripts/`，运行产物统一放在 `results/`。
3. **人工证据可追溯**：概率/定向抽样清单、旅游相关性判断、最终确认和手册版本分别保存。
4. **实验可复现**：每次正式实验冻结配置、随机种子、数据清单哈希、代码版本和环境信息。
5. **隐私与版权优先**：原文、图片、作者标识、访问令牌和模型权重默认不进入 Git。

## 主要目录

| 目录 | 用途 |
| --- | --- |
| `data/` | 私有原始内容/图片归档、派生数据和人工标注；上游工作库只读 |
| `src/tourism_ugc_study/cleaning/` | 数据清洗与质量标记逻辑 |
| `src/tourism_ugc_study/annotation/` | 最终参考集候选、人工证据、候补调度、artifact 和泄漏分组逻辑 |
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

## 数据清洗状态

| 层次 | 当前本地资产 | 数量与边界 |
| --- | --- | --- |
| 原始抓取集 | `data/processed/raw-crawl-20260912/raw-crawl.sqlite`及同目录`media-root/` | 19,984条post；161,377张图，保留729条topic=0 |
| 固定研究输入 | `data/processed/source-snapshots/topic-relevant-20260911.sqlite` | 19,255条topic=1；156,137条图片关系 |
| 清洗候选 | `data/processed/research-cleaning-20260911-autodl-r2/cleaning-candidates.sqlite`的`cleaning_candidates`表 | 一post一行；10,914 keep / 5,305 exclude / 3,036 manual_review；全部pending |
| 最终人工keep | `data/processed/final-kept.sqlite` | 0行，保留结构，尚未发布新研究集；不属于本次冻结资产 |

AutoDL-r2已完成17,853条纯预测，另承接1,402条同规范正文人工证据。全部36批输入、预测、17,696份缓存向量、日志及回执已在本机保全；远端批次数据已释放，旧MPS/Runpod产物不恢复。冻结模型、0.31/0.96阈值及选择后风险披露不变，本次不是新的独立准确率验证。

冻结保护覆盖215,044份资产文件及另2份元数据文件，逐文件SHA校验，文件/目录只读并设置macOS `uchg`。图片为独立APFS写时复制克隆，不是硬链接；同盘归档防误写但不是异地备份或硬件WORM。审核应另建可写工作副本，冻结库/模板不原位修改；未进行片段回填、内容编码或人工最终发布。

可信身份见[冻结登记](governance/research-data-freezes.json)、[冻结决定](docs/decisions/2026-09-12-原始抓取集与清洗候选冻结.md)和[校验协议](docs/protocols/研究数据冻结与校验.md)。本机复核：

```bash
.venv/bin/python scripts/cleaning_freeze_research_data.py verify \
  --manifest data/processed/research-freezes/research-data-20260912/freeze-manifest.json \
  --expected-sha256 526d76d15374e08de7f6d9a802353f620bc30f0520268841af6d39fc469d11f4
```

只克隆Git仓库不会获得私有数据，须在授权本地资产环境执行该命令。历史13,858条结果、旧6,835条keep及旧2,286条人工任务只保留历史适用范围。详细状态见[当前状态与执行索引](docs/protocols/数据清洗当前状态与执行索引.md)；管理端仍应先与用户讨论设计，尚未实施。

## 当前可用入口

- 数据清洗当前状态与交付索引：[docs/protocols/数据清洗当前状态与执行索引.md](docs/protocols/数据清洗当前状态与执行索引.md)
- 数据清洗科研方案：[docs/methods/数据清洗科研方案.html](docs/methods/数据清洗科研方案.html)
- 数据清洗工程方案：[docs/protocols/数据清洗工程方案.md](docs/protocols/数据清洗工程方案.md)
- 文本清洗人工审核方法：[docs/protocols/文本数据清洗人工审核方法.md](docs/protocols/文本数据清洗人工审核方法.md)
- 数据清洗人工作业简明指南（非正式）：[docs/protocols/数据清洗人工作业简明指南.html](docs/protocols/数据清洗人工作业简明指南.html)
- 数据清洗最终交付决策：[docs/decisions/2026-08-25-1300条标签重训与前瞻性非零风险验收.md](docs/decisions/2026-08-25-1300条标签重训与前瞻性非零风险验收.md)
- 三段式路由历史基础：[docs/decisions/2026-08-22-数据清洗三段式自动路由与冻结模型增量推理.md](docs/decisions/2026-08-22-数据清洗三段式自动路由与冻结模型增量推理.md)
- 最终参考集生成入口：[scripts/annotation_build_reference.py](scripts/annotation_build_reference.py)
- 参考证据校验入口：[scripts/cleaning_validate_reference.py](scripts/cleaning_validate_reference.py)
- 泄漏分组入口：[scripts/annotation_adjudicate.py](scripts/annotation_adjudicate.py)
- 正式 baseline 训练入口：[scripts/cleaning_train_baseline.py](scripts/cleaning_train_baseline.py)
- sparse challenger 训练与安全验收入口：[scripts/cleaning_train_sparse_challenger.py](scripts/cleaning_train_sparse_challenger.py)
- sparse challenger 一次性验证入口：[scripts/cleaning_validate_sparse_challenger.py](scripts/cleaning_validate_sparse_challenger.py)
- Qwen3-Embedding 本地权重准备入口：[scripts/cleaning_prepare_qwen_embedding.py](scripts/cleaning_prepare_qwen_embedding.py)
- Qwen3-Embedding 语义 baseline 训练入口：[scripts/cleaning_train_qwen_embedding_baseline.py](scripts/cleaning_train_qwen_embedding_baseline.py)
- Qwen3-Embedding 缓存向量分类头 challenger：[scripts/cleaning_train_qwen_head_challenger.py](scripts/cleaning_train_qwen_head_challenger.py)
- sparse＋Qwen3-Embedding 无泄漏融合：[scripts/cleaning_train_qwen_sparse_fusion.py](scripts/cleaning_train_qwen_sparse_fusion.py)
- Qwen3-Embedding 英文 head-tail 第三层：[scripts/cleaning_train_qwen_head_tail.py](scripts/cleaning_train_qwen_head_tail.py)
- 唯一冻结融合模型CSV推理入口：[scripts/cleaning_predict_tourism_relevance.py](scripts/cleaning_predict_tourism_relevance.py)
- 最新人工编码簿：[docs/data-dictionary/编码簿_青岛旅游UGC编码框架.md](docs/data-dictionary/编码簿_青岛旅游UGC编码框架.md)
- 当前论文草稿：[manuscript/论文草稿.md](manuscript/论文草稿.md)

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

`requirements-dev.txt` 会以 editable 模式安装当前项目及其运行依赖，并补充测试工具。数据清洗确定性文本与线性相关性基线依赖已经在项目依赖和配置摘要中冻结；Qwen 本地语义 baseline 使用 `.venv/bin/python -m pip install -e '.[semantic]'` 安装独立固定版本依赖。其他正式内容/视觉模型新增依赖时，应先记录用途和版本，再仅安装到 `.venv`。

## License

本仓库中由项目作者原创的代码、编码表和研究文档采用 [MIT License](LICENSE)。MIT 授权不延伸至第三方平台 UGC、论文全文、预训练模型或其他由第三方持有权利的材料；这些材料即使在本地研究环境中存在，也不属于本仓库的许可内容。
