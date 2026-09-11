# Processed data

存放分析就绪的派生 SQLite、Parquet/CSV、清洗 manifest 和视图说明。数据文件默认被 Git 忽略；不含敏感内容的行数、哈希、schema 和生成命令可直接写入同目录的版本化 manifest，文件增多后再建立子目录。

## 当前研究输入

`current-source.json` 是当前研究输入指针，绑定快照路径、快照哈希、配对 manifest
及筛选条件。2026-09-11 已切换为 `source-snapshots/topic-relevant-20260911.sqlite`：
从正式源库19,984条中仅复制 `web_posts.topic_relevant=1` 的19,255条，连同156,137条
关联图片记录和必要外键关系。正文及ID不改写，不复制媒体文件或采集调度/账号表。
状态为 `SOURCE_SNAPSHOT_READY_INFERENCE_NOT_RUN`，尚无本轮清洗决定或片段回填。

生成入口为 `scripts/cleaning_snapshot_topic_relevant.py`；源库路径通过
`--source-db` 显式传入，新输出由 `--output-db` 指定。只读单事务保证包含已提交WAL
的一致性读取；验收后才原子更新指针，拒绝覆盖已存在的快照和manifest。

## 历史依赖（保留，不冒充本轮结果）

- `source-snapshots/cleaning-20260822-schema34.sqlite`：旧13,858条原文证据，旧派生库、
  切分实验及管理端仍依赖其精确身份与哈希；不覆盖。
- `cleaning.sqlite`：旧候选人口、源版本和leakage关系，不是新快照的清洗结果库。
- `final-kept.sqlite` 及其manifest：2026-09-11经用户明确授权，原6,835条成员已全部
  清空，表、约束和索引保留。配对manifest已更新为0行，管理端空表契约校验通过。
  旧文件精确备份在 `results/final-keep-clear-20260911/backup/`；此空表不是新一轮
  最终研究集。管理端源码/配置未改，待新清洗及人工审查完成后再发布新成员。

换源、保留量估算与删除边界见
[`源快照切换与旧清洗产物保留决定`](../../docs/decisions/2026-09-11-源快照切换与旧清洗产物保留决定.md)。
