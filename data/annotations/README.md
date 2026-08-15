# 人工标注数据

> **研究内容标签模板状态**：`CURRENT_ALIGNED`
> **labels.csv契约版本**：`labels-v2.0`
> **共同校准主表契约版本**：`calibration-coding-v1.0`
> **当前对齐编码表**：`v3.6.1`
> **更新日期**：2026年8月15日

人工标注采用“编码簿—抽样轮次—独立标注—仲裁—冻结发布”流程。

## 当前组织方式

- 当前编码簿统一存放在 `docs/data-dictionary/`；标注轮次通过 `codebook_version` 和文件哈希引用，不再复制第二份。
- `templates/`：`items.csv`保存帖子级父项；`calibration-coding.csv`是编码员唯一填写的共同校准主表；`labels.csv`是转换后的逐原子字段规范长表；`calibration-issues.csv`和`revision-decisions.csv`只由研究负责人维护。`adjudication.csv`仍是清洗/通用仲裁表，不能替代研究内容编码的两张负责人表。`text-cleaning-post-annotations.csv`严格对应当前文本清洗盲标CSV接口；三个`image-*`空白表分别对应图片技术噪声盲标、第三人仲裁和保留集审计。
- 实际标注轮次直接建立为 `round_YYYYMMDD_purpose_vNN/`；原始独立标注、仲裁、切分和轮次 manifest 放在该轮目录内，只追加、不覆盖。
- 跨轮正式冻结清单以 `release_*.json` 或 `release_*.csv` 放在本目录；实际出现多个发布文件后再建立 `releases/`。
- `private/`：需要展示原文、图片或作者信息的本地工作文件；该目录被 Git 忽略。

## 数据格式

研究内容标签的规范存储采用逐原子字段长格式：每行表示“某标注者对某个帖子、文本片段或图像的一个字段作出的一次判断”。显式`0`与阳性值都必须各占一行，不能只保存命中的标签。编码员不直接手写该存储格式中的JSON和offset，而是填写更易读的共同校准主表，再由确定性工具转换。

### 共同校准最简人工作业

20—30篇共同校准阶段，编码员只使用`calibration-coding.csv`或其Excel界面，不填写独立问题表和修订决策表。系统或任务准备者预填身份、观察单位、文本、字段、值域、主观性和版本列；编码员只填写以下黄色输入列：

| 输入 | 何时填写 |
|------|----------|
| `label_value` | 每个原子字段必填 |
| `confidence` | 主观字段填写1—5；客观字段和结构性`NA`留空 |
| `reason_code`、`low_confidence_note` | 仅`confidence <= 2`或`UNRESOLVED`时填写；说明限一句 |
| `alternative_values` | 仅确有合理替代值时，用`|`分隔；没有则留空 |
| `evidence_quote` | 实质性阳性文本判断填写最短充分原文 |
| `evidence_start_if_repeated` | 同一证据原文在`raw_text`重复出现时才填起点 |
| `other_issue_type`、`other_issue_note` | 仅发现框架遗漏、反例、语境依赖或切分问题时填写 |
| `annotator_id`、`annotated_at` | 编码者及完成时间 |

转换工具负责生成`confidence_notes_json`、`evidence_spans_json`、Unicode字符offset和问题编号。推荐命令为：

```bash
.venv/bin/python scripts/annotation_prepare_calibration.py \
  round_*/calibration-coding.csv \
  --labels-output round_*/labels.csv \
  --issues-output round_*/calibration-issues.csv
```

工具只自动把`UNRESOLVED`和编码员显式填写的额外问题送入负责人问题队列；其他低置信记录保留在`labels.csv`中供负责人筛选。负责人完成分类、合并与裁决后填写`revision-decisions.csv`，编码员不承担问题编号、版本升级或重编码范围判断。

### `labels.csv`字段契约

| 字段 | 说明 |
|------|------|
| `annotation_id` | 单次原子字段判断的唯一ID |
| `item_id` | 对应`items.csv`中的帖子级父任务ID |
| `unit_type` | `POST` / `TEXT_SEGMENT` / `IMAGE` |
| `unit_id` | 在父任务内对应`post_id`、`seg_id`或`img_id`；与`item_id`、`unit_type`联合定位观察单位 |
| `annotator_id` | 项目内匿名编码员ID |
| `dimension_code` | `V0`、`V1`、`V2`、`V3`、`V5`、`V6`、`V7`、`V11`、`V12`、`V14`或`V15` |
| `field_name` | 编码表中的精确字段名，例如`cs_inf`、`at_has_eval`或`sentiment_dir` |
| `label_value` | 字段正式值，或通用状态`UNK`、`NA`；共同校准期可临时使用`UNRESOLVED`，盲试标前必须关闭 |
| `confidence` | 主观字段填写1—5；客观导入字段及结构性`NA`留空。逐字段长表中的该值对应编码表宽表`confidence_json[field_name]` |
| `confidence_notes_json` | `confidence <= 2`时必填，结构为`{"reason_code":"BOUNDARY","alternative_values":[0,1],"note":"简短说明"}`；允许的原因代码以编码表第5.4.2节为准 |
| `evidence_spans_json` | 文本证据使用编码表第5.4.3节的`fields/start/end/quote`数组；`raw_text[start:end]`必须等于`quote`。帖子级和图像级记录留空 |
| `template_schema_version` | 当前固定填写`labels-v2.0` |
| `codebook_version` | 当前固定填写`v3.6.1`，并须与轮次manifest引用的编码表版本一致 |
| `annotated_at` | ISO 8601时间戳；同一轮次统一时区 |

`labels.csv`只保存研究变量的原子判断，不保存开放代码、候选主题或主题分析结果。框架遗漏、边界案例、反例和切分问题由转换工具及研究负责人整理到`calibration-issues.csv`，不得自造`label_value`。

### 持续版本同步门

`labels.csv`是`docs/data-dictionary/编码表.md`的操作层镜像，不是独立规范源。以后凡编码表修改字段名、值域、观察单位、`1/0/UNK/NA`语义、置信度、证据跨度或版本字段，必须在同一commit/PR中同步检查并更新`labels.csv`、本说明和对应自动化测试。仅有排版或不影响记录契约的理论表述变化，也必须显式确认“模板无需修改”，不得默认跳过。

若`labels.csv`的`codebook_version`与轮次manifest不一致，或表头未通过自动化检查，该轮次不得导出。模板文件名保持为`labels.csv`，不在文件名中写版本号。

标注者使用项目内匿名 ID，不保存姓名。原文、作者 ID、主页和图片 URL 不写入可提交的标注表；标注工具通过 `source_web_post_id` 在受控环境中读取内容。

文本数据清洗是例外：现有清洗模块使用宽格式盲标 CSV，每行对应一个冻结帖子版本。仓库中的 `text-cleaning-post-annotations.csv` 只保存表头，作为字段契约和空白模板；正式标注文件必须由 `scripts/annotation_export_tasks.py export-post` 从具体抽样运行导出，才能包含有效的任务、样本和帖子版本身份。
