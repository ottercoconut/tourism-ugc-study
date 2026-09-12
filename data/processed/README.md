# Processed data

存放私有原始内容归档、派生SQLite、Parquet/CSV、清洗manifest和说明。这里的数据与私有manifest默认被Git忽略，仅README提交；允许公开到代码仓库的汇总、SHA及命令写入版本化文档或`governance/research-data-freezes.json`。

## 当前研究输入

数据封存身份为`research-data-20260912`；原始内容、研究输入与清洗候选是三个不同层次，
均不等于最终人工通过的数据集。校验入口和准确冻结状态见Git中的
[`research-data-freezes.json`](../../governance/research-data-freezes.json)及[冻结协议](../../docs/protocols/研究数据冻结与校验.md)。

`current-source.json` 是当前研究输入指针，绑定快照路径、快照哈希、配对 manifest
及筛选条件。2026-09-11 已切换为 `source-snapshots/topic-relevant-20260911.sqlite`：
从正式源库19,984条中仅复制 `web_posts.topic_relevant=1` 的19,255条，连同156,137条
关联图片记录和必要外键关系。该筛选快照正文及ID不改写，未单独复制图片字节；
图片现由下述全量原始归档独立保全，不需要回到活跃采集目录取图。
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

## 原始归档与冻结清单

- `raw-crawl-20260912/raw-crawl.sqlite`：全量19,984条原始post，topic=1为19,255条、topic=0为729条；161,377条图片关系。主题子集与既有研究输入逐列逐行相同，未重做本轮输入。
- `raw-crawl-20260912/media-root/`：161,377份独立图片克隆；解析方式是该目录加`web_post_images.local_path`。`media-inventory.json`记录每份图片的SHA与大小，属私有材料。
- `research-freezes/research-data-20260912/`：`freeze-manifest.json`与`frozen-files.json`绑定所有封存根及全文件SHA；文件和目录另有只读模式及`uchg`保护。
- `current-freeze.json`：可变本地检索指针，不能替代Git登记的SHA。`current-source.json`、`current-cleaning.json`仍分别保留输入/候选阶段状态，并附`data_freeze`。

人工审核必须另建可写工作副本；不能填写冻结CSV、修改候选库或在其中回填片段。
`final-kept.sqlite`不在此次保护范围，保留空表结构供未来人工终审后发布；不能把它当作本次冻结清洗结果库。

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
