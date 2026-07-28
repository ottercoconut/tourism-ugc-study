# 数据目录

## 权威来源

正式采集库位于：

```text
/Users/kawauso/Documents/Projects/TripPostCollect/data/trippostcollect.sqlite
```

该库由 `TripPostCollect` 管理，本项目只读访问。当前快照约 3,044 条候选 UGC，计划逐步扩展至约 1 万条以内。每次科研派生必须记录源库 SHA-256、源表行数、抽取时间和规则版本。

## 分层

- `raw/`：源数据说明或只读指针，不复制正式库。
- `interim/`：可重建的中间结果，禁止提交 Git。
- `processed/`：分析就绪派生库和 manifest，数据文件禁止提交 Git。
- `annotations/`：人工标注、仲裁、数据切分和冻结发布。
- `manifests/`：可提交 Git 的数据谱系与哈希清单。

任何公开数据导出均须先通过 `governance/release/` 的伦理、版权和再识别审查。
