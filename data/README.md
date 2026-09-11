# 数据目录

## 权威来源

正式采集库由 `TripPostCollect` 项目管理，不属于本仓库。运行时通过命令行参数传入当前机器上可读的绝对路径：

```text
<SOURCE_SQLITE>
```

例如，生成研究输入快照时使用 `--source-db <SOURCE_SQLITE>` 显式指定该库。本项目只读访问源库，版本化配置不保存机器或用户特定的绝对路径；私有本地manifest记录实际来源。源库仍可扩充，规模以每次一致性快照为准，不再沿用历史“约1万条以内”的预期。每次科研派生必须记录数据哈希、源表行数、抽取时间和规则版本。

2026-09-11起，当前研究输入只包含源库 `web_posts.topic_relevant=1` 的记录。本地
`processed/current-source.json` 绑定当前筛选快照；原始值、ID和必要图片关系保留。
该字段是采集端关键词主题门，不是旅游相关性清洗的 `keep`，不替代正式模型或人工判断。

## 当前目录

- `processed/`：实际存在的派生 SQLite、manifest 和分析就绪结果；数据文件禁止提交 Git。
- `annotations/`：标注模板和实际发生的标注轮次。轮次尚未开始时不预建 `rounds/`、`splits/`、`releases/` 等空目录。

正式源库不整体复制到本仓库，因此不保留空的 `raw/`；筛选后的研究输入快照保存在
Git忽略的 `processed/source-snapshots/`，不复制采集控制面、账号表或媒体文件。
历史快照如仍绑定训练、标注或审计证据，不得覆盖为当前人口。中间文件可在确有需要时
放入 `processed/work/`，完成后应能由源快照、配置和代码重建。任何公开数据导出均须经过
`governance/release-checklist.md` 的伦理、版权和再识别审查。
