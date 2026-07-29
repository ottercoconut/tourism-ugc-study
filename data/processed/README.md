# Processed data

存放分析就绪的派生 SQLite、Parquet/CSV、清洗 manifest 和视图说明。数据文件默认被 Git 忽略；不含敏感内容的行数、哈希、schema 和生成命令可直接写入同目录的版本化 manifest，文件增多后再建立子目录。

现有 `trippost_research.sqlite` 与 `cleaning_manifest.json` 生成于旧数据快照，正式分析前必须按当前源库重新构建并使用新版本号。
