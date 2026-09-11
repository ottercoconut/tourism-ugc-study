# Processed data

存放分析就绪的派生 SQLite、Parquet/CSV、清洗 manifest 和视图说明。数据文件默认被 Git 忽略；不含敏感内容的行数、哈希、schema 和生成命令可直接写入同目录的版本化 manifest，文件增多后再建立子目录。

## 当前研究输入

`current-source.json` 是当前研究输入指针，绑定快照路径、快照哈希、配对 manifest
及筛选条件。2026-09-11 已切换为 `source-snapshots/topic-relevant-20260911.sqlite`：
从正式源库19,984条中仅复制 `web_posts.topic_relevant=1` 的19,255条，连同156,137条
关联图片记录和必要外键关系。正文及ID不改写，不复制媒体文件或采集调度/账号表。
输入状态为 `SOURCE_SNAPSHOT_READY`；清洗状态由单独的`current-cleaning.json`
记录实际执行状态。旧尝试已按要求销毁且不能恢复；用户随后重新授权在AutoDL
以相同研究快照和冻结模型从头推理，全量轮次为`research-cleaning-20260911-autodl-r2`。
当前已完成全部36批、17,853条模型输入的推理、回传和验收，另承接1,402条
同规范正文人工证据。每批最多500条，预测、向量和回执在本机归档后才清理
远端输入/输出/缓存；不向服务器复制整份数据库。

`research-cleaning-20260911-autodl-r2/cleaning-candidates.sqlite`的
`cleaning_candidates`表保留全部19,255条：keep 10,914条、exclude 5,305条、
manual_review 3,036条。配对`candidate-manifest.json`及当前指针绑定实际摘要，
状态为`CANDIDATES_READY_AWAITING_HUMAN_REVIEW`。所有候选均为pending，不原位
修改此候选快照；人工终审前不发布正式keep。本轮没有片段回填或内容标注。

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
