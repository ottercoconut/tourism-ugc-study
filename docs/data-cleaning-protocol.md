# TripPostCollect 科研数据清洗协议

## 目标与边界

本项目把 `/Users/kawauso/Documents/Projects/TripPostCollect/data/trippostcollect.sqlite` 视为不可变的采集证据库。科研数据通过 `scripts/build_research_dataset.py` 派生，任何清洗都不能回写源库。

默认核心样本限定为 B站、微博、知乎、小红书、抖音五个平台中，`source_type=mediacrawler_search` 且城市、检索关键词有效的记录。其他记录仍保留在 `research_posts`，但 `scope_core_sample=0`。

## 清洗规则

1. 文本执行 Unicode NFKC、不可见格式字符和段内连续空白标准化，同时保留段落换行；不做分词、停用词删除或语义改写。
2. 发布时间仅解析并统一为 Asia/Shanghai ISO 8601；缺失或无效值保持空，不使用采集时间插补。
3. 负值或非整数计数字段置空；真实 0 保留。
4. 粉丝数只有在数值非负、`followers_observed=true` 且证据来源符合平台契约时进入 `author_followers_count_clean`，否则置空并留标记。
5. 图片数量从 `web_post_images` 中 `image_role=content` 的关系重算；作者头像与页面截图不计入内容图片。
6. 平台内标准化文本相同的记录建立重复簇，但不物理删除。去重视图优先选择字段更完整、发布时间更早的记录。
7. 极端互动量只按平台和指标使用正值 `log1p` 三倍 IQR 标记，不删除、不缩尾。
8. 硬排除只适用于视频记录、缺少内容身份、标题和正文均为空三类情况。
9. 作者直接标识不复制到分析主表，改用稳定 SHA-256 联结键。正文和图片 URL 仍可能再识别，因此结果是假名化而非匿名化。

## 可复现执行

```bash
.venv/bin/python scripts/build_research_dataset.py
.venv/bin/python -m pytest -q
```

每次构建记录源库路径、源库 SHA-256、Python 版本、清洗规则版本和输出 SHA-256。脚本使用 SQLite URI `mode=ro` 打开源库，并核对构建前后源库哈希。

## 分析原则

- 主分析与敏感性分析应分别声明所用视图和样本量。
- 跨平台互动量不得直接合并；至少按平台分层或加入平台固定效应。
- 城市覆盖只包含实际采集到的城市，不能声称代表山东 16 市。
- 在公开数据或论文附件前，需另行完成伦理、平台条款、著作权和再识别风险审查。
