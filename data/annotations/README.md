# 人工标注数据

> **研究内容标签模板状态**：`CURRENT_ALIGNED`
> **labels.csv契约版本**：`labels-v2.0`
> **共同校准主表契约版本**：`calibration-coding-v1.0`
> **当前对齐编码表**：`v3.15.0`
> **更新日期**：2026年9月3日

人工标注采用“编码簿—抽样轮次—独立标注—仲裁—冻结发布”流程。

## 当前组织方式

- 当前编码簿统一存放在 `docs/data-dictionary/`；标注轮次通过 `codebook_version` 和文件哈希引用，不再复制第二份。
- `templates/`：`items.csv`保存帖子级父项；`calibration-coding.csv`是V1—V6共同校准主表；`labels.csv`是转换后的逐原子字段规范长表；`author-role-calibration.csv`、`author-role-evidence.csv`和`author-role-adjudication.csv`是独立V0作者试点契约。`calibration-issues.csv`和`revision-decisions.csv`只由研究负责人维护。`adjudication.csv`是后续研究内容编码的通用裁决表，不能用于文本清洗。
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
  --issues-output round_*/calibration-issues.csv \
  --codebook-version v3.15.0
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
| `dimension_code` | `V0`、`V1`、`V2`、`V3`、`V4`、`V5`、`V6`、`V7`、`V8`、`V9`、`V10`或`V11` |
| `field_name` | 编码表中的精确字段名，例如`cs_inf`、`at_has_eval`或`sentiment_dir` |
| `label_value` | 字段正式值，或通用状态`UNK`、`NA`；共同校准期可临时使用`UNRESOLVED`，盲试标前必须关闭 |
| `confidence` | 主观字段填写1—5；客观导入字段及结构性`NA`留空。逐字段长表中的该值对应编码表宽表`confidence_json[field_name]` |
| `confidence_notes_json` | `confidence <= 2`时必填，结构为`{"reason_code":"BOUNDARY","alternative_values":[0,1],"note":"简短说明"}`；允许的原因代码以编码表第5.4.2节为准 |
| `evidence_spans_json` | 文本证据使用编码表第5.4.3节的`fields/start/end/quote`数组；`raw_text[start:end]`必须等于`quote`。帖子级和图像级记录留空 |
| `template_schema_version` | 当前固定填写`labels-v2.0` |
| `codebook_version` | 当前固定填写`v3.15.0`，并须与轮次manifest引用的编码表版本一致 |
| `annotated_at` | ISO 8601时间戳；同一轮次统一时区 |

`labels.csv`只保存研究变量的原子判断，不保存开放代码、候选主题或主题分析结果。框架遗漏、边界案例、反例和切分问题由转换工具及研究负责人整理到`calibration-issues.csv`，不得自造`label_value`。

现行V4在旧编号V2的v3.10.0修订中新增`rs_r_rec`、`rs_r_evt`、`evt_spt`、`evt_per`、`evt_fes`和`evt_oth`六个人工字段，并移除人工`rs_r_act`字段；`rs_r_act`仅在分析阶段由`rs_r_rec OR rs_r_evt`汇总。固定Excel文件`data/annotations/templates/all-label-manual-coding.xlsx`已升至内部模板`all-label-manual-coding-v2.5`，文本面现有79个标签列与2个人工强度字段。旧模板或旧`rs_r_act`记录不得自动迁移，须回到原文重编码。

V11“可见对象状态”继续使用相同的通用长表结构，不需要为`labels.csv`增加新列。其10个`field_name`、条件性`0/1/NA/UNRESOLVED`填写规则和逐字段置信度以编码表v3.15.0及固定Excel模板2.5为准；视觉人口与专门状态边界冻结前，V11不能作为正式研究数据发布。

### V0与V1—V6同轮双任务试点

私有轮次`round_20260824_role_text_calibration_v01`最初按编码表v3.13.0生成并统一标记为`PILOT_ONLY`，包含两个隔离任务：V0身份共同校准25名作者，以及作者完全互斥的V1—V6文本共同校准25篇帖子。v3.15.0没有改变V1—V6文本字段和值域，因此原文本样本与固定`seg_id`可在明确记录来源后复用；但旧V0既包含范围外平台，又缺少当前人工总体字段，所以该双任务包不得整体静默改签为v3.15.0。

当前文本共同校准使用独立生成的v3.15.0人工工作簿；旧双任务包只作为样本与切分快照来源保留。文本工作簿不显示作者资料、粉丝量或角色信息。V0须从小红书和知乎的最低材料可用作者中重新生成，并使用符合v3.15.0纯人工字段契约的作者工作簿；最低材料门不预判证据充分性或角色，CI固定为`UNAVAILABLE`且不进入角色判断。

研究切片及任务包通过以下命令生成；命令拒绝覆盖既有切片或轮次：

```bash
.venv/bin/python scripts/annotation_prepare_role_pilot.py \
  --source-database trippostcollect-research-snapshot-*/trippostcollect-*.sqlite \
  --source-manifest trippostcollect-research-snapshot-*/manifest.json
```

两位编码员分别使用`role/coder_a-role-coding.csv`和`role/coder_b-role-coding.csv`，不得交换或覆盖。共同校准回收后使用`annotation_summarize_role_pilot.py --mode CALIBRATION`只生成分歧、UNK与角色支持摘要，不计算正式信度；规则冻结后的50名新作者任务才允许使用`--mode BLIND_PILOT`生成alpha、95%CI、支持数和混淆矩阵。

V0空模板生成器现已对齐v3.15.0和`role-pilot-v0.2-draft`。作者工作簿让编码员直接填写EA/CE原子证据及总体值、SC、证据充分性和最终角色；程序不生成或覆盖这些人工字段。实际任务成员继续使用两份彼此独立的个人文件，不能把两个编码员的原始判断写入同一工作簿。

文本人工工作簿由`annotation_build_text_calibration_workbooks.mjs`从旧轮次的两份空白响应CSV只读生成。生成器只更新文本任务的版本记录、加入直白说明和标签速查，不改变帖子、片段、字段、允许值或任何人工答案。默认输出位于当前任务的`outputs/<任务目录>/文本共同校准/`；`outputs/`和私有轮次均被Git忽略。两名编码员的工作簿、填写结果和文件哈希必须继续保持独立。

### 持续版本同步门

`labels.csv`是`docs/data-dictionary/编码表.md`的操作层镜像，不是独立规范源。以后凡编码表修改字段名、值域、观察单位、`1/0/UNK/NA`语义、置信度、证据跨度或版本字段，必须在同一commit/PR中同步检查并更新`labels.csv`、本说明和对应自动化测试。仅有排版或不影响记录契约的理论表述变化，也必须显式确认“模板无需修改”，不得默认跳过。

若`labels.csv`的`codebook_version`与轮次manifest不一致，或表头未通过自动化检查，该轮次不得导出。模板文件名保持为`labels.csv`，不在文件名中写版本号。

标注者使用项目内匿名 ID，不保存姓名。原文、作者 ID、主页和图片 URL 不写入可提交的标注表；标注工具通过 `source_web_post_id` 在受控环境中读取内容。

### 文本数据清洗例外契约

文本清洗使用宽格式审核 CSV，每行对应一个冻结帖子版本，只判断青岛旅游相关性 `tourism_label`，不保存原因码、自由文本备注、逐行人员标识或人工操作时间。空白只表示任务尚未完成；`uncertain` 是阅读后作出的显式判断，包括文本不足或抓取拼接污染导致无法稳定二分的记录，二者不得混用。轮次完成时间、任务身份、成员清单和文件哈希由 manifest 保存。

现有 `data/annotations/private/round_20260821_text_cleaning_v01/tourism-relevance-completed.csv` 与同目录 manifest 是 `final-nonduplicate-model-reference` 的迁移输入，不是训练权威源。全对重复候选、人工决定、候补队列和补充标注也只作生成谱系。只有恰好700条、标签二元、身份唯一、无最终确认重复成员的最终 CSV 与其唯一配对 `finalized` manifest 可进入训练；标签不导入 `text_post_annotations`。最终仍保持500条概率框与200条定向框，概率框补样的总体估计有效性必须由 manifest 明确声明。

三个 `text-cleaning-reference-*` 文件只保存新契约表头；实际任务位于 Git 忽略的私有目录。最终参考集之外的未来人工灰区和保留集审计另建独立任务，不复用本契约。参考集生成器只读派生库且不写入标签；后续系统即使保存决定引用，数据库也不能成为标签副本。标签定义继续使用 `text-cleaning-v1.5`。
