# 数据目录

## 权威来源

正式采集库位于：

```text
/Users/kawauso/Documents/Projects/TripPostCollect/data/trippostcollect.sqlite
```

该库由 `TripPostCollect` 管理，本项目只读访问。当前数据库仍处于采集推进阶段；后续正式研究数据只包含青岛并逐步扩展至约 1 万条以内。每次科研派生必须记录源库 SHA-256、源表行数、抽取时间和规则版本。

## 当前目录

- `processed/`：实际存在的派生 SQLite、manifest 和分析就绪结果；数据文件禁止提交 Git。
- `annotations/`：标注模板和实际发生的标注轮次。轮次尚未开始时不预建 `rounds/`、`splits/`、`releases/` 等空目录。

正式源库不复制到本仓库，因此不再保留空的 `raw/`。中间文件可在确有需要时放入 `processed/work/`，完成后应能由源快照、配置和代码重建。任何公开数据导出均须经过 `governance/release-checklist.md` 的伦理、版权和再识别审查。
