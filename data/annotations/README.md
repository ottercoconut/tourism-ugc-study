# 人工标注数据

人工标注采用“编码簿—抽样轮次—独立标注—仲裁—冻结发布”流程。

## 当前组织方式

- 当前编码簿统一存放在 `docs/data-dictionary/`；标注轮次通过 `codebook_version` 和文件哈希引用，不再复制第二份。
- `templates/`：`items.csv`、长格式 `labels.csv` 和 `adjudication.csv` 模板。
- 实际标注轮次直接建立为 `round_YYYYMMDD_purpose_vNN/`；原始独立标注、仲裁、切分和轮次 manifest 放在该轮目录内，只追加、不覆盖。
- 跨轮正式冻结清单以 `release_*.json` 或 `release_*.csv` 放在本目录；实际出现多个发布文件后再建立 `releases/`。
- `private/`：需要展示原文、图片或作者信息的本地工作文件；该目录被 Git 忽略。

## 数据格式

标签采用长格式：每行表示“某标注者对某条记录的某个编码维度给出的一个标签”。这样既支持旅游相关性等单标签任务，也支持目的地形象的多维、多标签编码。

标注者使用项目内匿名 ID，不保存姓名。原文、作者 ID、主页和图片 URL 不写入可提交的标注表；标注工具通过 `source_web_post_id` 在受控环境中读取内容。
