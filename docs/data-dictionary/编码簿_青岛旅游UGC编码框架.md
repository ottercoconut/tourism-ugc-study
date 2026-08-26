# 编码簿

> **KOL和KOC视角下旅游目的地形象的UGC内容特征与建构策略研究**
> ——青岛旅游UGC文本主线+视觉辅助编码框架
>
> 文档状态：`CURRENT_ALIGNED`
> 上位标准：`编码表.md` v3.13.0；发生冲突时以编码表为准
> 执行成熟度：V0与V1—V6已进入同轮`PILOT_ONLY`共同校准；V0须在规则v0.2、50名新作者盲试标、信度和组别支持门通过后才能进入正式比较；视觉轨本轮不启动
> 内部文档版本：`v3.13.0-alignment.2`（本文件不独立定义或修改编码规则）
> Excel执行模板：固定文件`data/annotations/templates/all-label-manual-coding.xlsx`，内部模板版本`all-label-manual-coding-v2.5`
> V0执行边界：现有Excel继续只承载帖子/文本/图像内容编码；作者角色使用独立作者表，且在数据、信度与组别支持门通过前仅为试点字段
> 校订日期：2026年8月26日
> 案例地：山东省青岛市
> 数据来源（当前研究快照）：B站、抖音、微博、小红书、知乎（五平台）；实得平台与样本量以冻结manifest为事实源
> 数据类型：作者主页快照与固定历史证据 + 帖文主体文本 + 配图
> 目标数据规模：帖文约10,000条，配图约100,000张
> 轨道关系：文本为主研究，视觉为辅助研究；视觉轨不阻塞文本主线
> 唯一方法：理论导向的结构化内容分析（量化取向）；不采用主题分析法

---

## 一、编码总览

本编码簿以**文本主线+视觉辅助**组织编码任务：

| 轨道 | 编码对象 | 编码单元 | 维度编号 | 核心问题 |
|------|---------|---------|---------|---------|
| **作者层** | 作者主页、固定历史证据与角色裁决 | 平台账号 × 作者证据窗口 | V0 | 创作者角色、触达规模与内容垂直度分别是什么？ |
| **帖子层** | 帖子元数据与披露信息 | 每帖一行 | V1–V2 | 是否有明确商业披露？帖子来自何平台、采用何种媒体形式？ |
| **文本轨** | 帖文主体文字 | 语义片段（句子/短段落） | V3–V6 | 用什么策略？调用什么资源？以何种语言功能和情感表达哪些目的地属性？ |
| **视觉轨** | 帖子配图 | 单张图片 | V7–V11 | 画面呈现了什么？如何取景？人物如何出现？对象呈现何种可见状态？ |

文本片段须在内容编码前由确定性程序按编码表规则固定：句末标点（。！？；）形成基本边界；逗号通常不切分，但评价或建议表达中出现“目的地属性指向改变”“同一属性的情感方向改变”或“不同属性分别承载不同情感方向”任一情形时，即使只以逗号、顿号或转折连接词衔接，也必须拆成不同评价原子；相同属性、相同方向的并列修饰或递进描述不切分。列表项、纯表情和纯标签独立成段；对话/引用在内部未发生上述切换时整体成段。程序预先生成固定`seg_id`，两位编码员不得自行改变边界；边界争议只作审计记录。

### 1.1 核心设计理念

v3.0将编码对象从“形象建构效果”调整为“目的地资源调用策略”；v3.1按国标重构旧编号V2（现V4）；v3.7将旧编号V1（现V3）改为父类—子类层级多标签结构并把旧编号V5并入`CS-INT`子项层；v3.8将目的地属性指向独立为旧编号V4（现V6）；v3.9新增旧编号V16（现V11）可见对象状态；v3.10将旧编号V2（现V4）扩为三级结构；v3.11只连续重排顶层编号。v3.12把角色与粉丝规模分成两轴；v3.13进一步把研究总体固定为个人旅游UGC创作者，CI在现库中统一为`UNAVAILABLE`并退出角色矩阵，采用`KOL_TYPE/KOC_TYPE`试点标签。V1—V11字段和值域不变。

固定Excel内部模板2.5承接V1—V11的帖子、文本和图像内容任务，不显示作者角色、粉丝规模或原始主页资料。V0角色试点使用独立作者表；两轨冻结后只通过`author_snapshot_id`关联。内容模板中的`rs_r_act`仍只在分析数据中由`rs_r_rec OR rs_r_evt`汇总，不在模板中填写。

### 1.2 维度编号方案

| 编号段 | 轨道 | 维度 |
|--------|------|------|
| V0 | 作者层 | 创作者画像：角色、触达规模与内容垂直度 |
| V1–V6 | 帖子级元数据 + 文本轨 | V1/V2/V3/V4/V5/V6（旧编号V5并入V3-CS-INT子项层） |
| V7–V11 | 视觉轨 | V7/V8/V9/V10/V11（共5个，旧编号V13已删除） |

### 1.3 互斥、共现与人工编码关系

| 维度 | 编码员直接判断 | 关系 | 复核关系（不自动计算） |
|------|----------------|------|----------------------|
| V0 创作者画像 | `actor_scope`、`content_vertical`、EA/CE封闭criterion codes与诊断性`raw_role_response`；SC、覆盖状态和正式角色由程序派生 | CI固定UNAVAILABLE；角色与触达规模独立；同一作者快照只编码一次 | 整体原始响应只作规则诊断，不覆盖裁决组件或正式角色 |
| V3 内容策略 | 5个父类 + 22个子项 | 父类及子项均逐项0/1；前四个父类和具名子类可共现 | 子类—父类、`cs_oth`残余关系及`is_non`排除关系仅供复核 |
| V4 目的地资源 | 2个Layer 1父类、10个Layer 2资源亚类及4个Layer 3事件子类 | 16个0/1项，可共现 | 两级父子关系仅供复核；`rs_r_act`仅作分析汇总 |
| V5 语言功能与情感 | `at_has_info`、`at_has_eval`、`at_has_sug`、`at_non`；条件性情感方向与双向强度 | 语言功能可共现；情感字段按适用性判断 | `at_non`、方向和强度关系仅供复核 |
| V6 目的地属性指向 | `asp_applicable`、4个父类、12个方向中立子类、`asp_oth` | 全部逐字段人工判断；适用时多标签 | 适用性、父子覆盖和`asp_oth`残余关系仅供复核 |
| V7 视觉主体 | `visual_subject` | 5类互斥单选 | — |
| V8 视觉呈现 | `scene_context`、`shot_scale`、`viewpoint` | 三个相互独立的单选子字段 | — |
| V9 人物呈现 | `people_count`、`human_scene_interaction` | 人数单选；互动0/1 | — |
| V10 消费符号 | `cs_brand`、`cs_accom`、`cs_rest`、`cs_trans` | 4个0/1项，可共现 | 总项与子类关系仅供复核 |
| V11 可见对象状态 | 4组正负原子项 + `vis_hazard_present` + `vis_protection_present` | 10个条件性0/1项；同轴正负可同时为1 | 无相关对象或状态轴未触发时记`NA`；不生成整图总体正负 |

跨维度边界如下：`CS-INF`只指面向旅行决策的实用信息，`AT-INF`指任何可核验命题，因此`CS-EDU`可与`AT-INF`共现；`CS-EMO`指感官或氛围渲染，`AT-EVL`指明确评价，二者可分别出现或共现。V6只记录被评价或建议的目的地属性，不携带正负方向；方向与强度由同一评价原子片段的V5记录。V11只记录单图可见对象状态，不从文本情感或V6属性评价派生。

### 1.4 方法定位与边界

本研究只采用**理论导向的结构化内容分析（量化取向）**：初始类目由理论、GB/T 18972-2017、既有研究和研究问题预先定义；共同校准与盲试标用于发现框架遗漏、边界案例、反例、语境依赖和切分问题；冻结后按固定`seg_id`、`img_id`和值域进行可复核测量，并以频数、比例、共现和组间比较回答研究问题。校准期的语境阅读和分析备忘只用于开发测量工具，不构成并列的质性分析方法。

V0—V11是分析类目/变量，不是“主题”。本研究不开展开放编码、候选主题发展、主题地图或数据饱和判断。校准时登记的框架外内容只用于修订编码簿，不能直接成为新标签或独立研究结果；任何类目变更须经团队书面决策、编码表升版和相应重编码后才生效。

---

## 二、作者层与帖子级元数据

### V0 创作者画像（作者快照级）

V0使用两个独立轴：`creator_role`是基于作者证据包的KOL/KOC等角色推断，`reach_tier`是粉丝数快照的触达规模档。粉丝量、认证、单篇表达或单篇互动均不能单独推出KOL/KOC。

**单位与关联**：`author_snapshot_id = platform + author_id + evidence_window_id`。同一作者同一窗口内的多篇目标帖共享一次角色裁决；跨平台账号不得因同名、相似头像或简介自动合并。原始账号ID、显示名、简介和主页URL属于受限证据，不进入公开数据。

**证据隔离**：T0作者包可含主页资料、领域身份资料、固定历史窗口内的非目标帖及去数值化的关系性社群证据；T1目标帖进入V3—V11编码。角色编码员看不到`follower_count`、`reach_tier`、点赞/收藏/分享/浏览原始数、一般平台等级、T1内容标签、T1互动结果、假设方向或角色模型建议；内容编码员看不到任何作者链接、资料、角色或规模。编码回收后才由受限程序附加`author_snapshot_id`。

#### V0.1 作者快照字段（客观导入）

| 字段 | 取值/说明 |
|------|-----------|
| `platform` | 平台名 |
| `author_id` | 随机映射或密钥HMAC生成的项目内稳定化名ID，不直接拼接原始ID |
| `platform_author_id_raw` | 受限平台原始账号ID；仅用于私有追溯与化名映射 |
| `creator_entity_id` | 可选受限跨平台实体键；无强证据时NA |
| `evidence_window_id` | 版本化T0证据窗口ID；窗口或角色有效期改变时生成新值 |
| `author_snapshot_id` | V0观察单位唯一ID |
| `profile_captured_at` | 主页实际抓取时间 |
| `evidence_window_start` / `evidence_window_end` | 统一预注册的T0历史证据窗口 |
| `t1_window_start` / `t1_window_end` / `t1_reference_at` | 目标观察期及时间差参照点 |
| `profile_time_relation` | PRE_T1 / WITHIN_T1 / POST_T1 / MISSING |
| `profile_post_gap_days` | `profile_captured_at - t1_reference_at`的有符号天数差 |
| `time_gate_status` | MAIN_ELIGIBLE / SENSITIVITY_ONLY / INSUFFICIENT |
| `time_gate_reason` / `time_rule_version` | 时间门原因和规则版本 |
| `display_name_raw` / `bio_raw` / `profile_url_raw` | 受限主页原始资料；不得公开 |
| `verification_raw` | 平台原始认证文字；未认证与未提取分开 |
| `follower_count` / `following_count` / `post_count_raw` | 快照计数；缺失、未提取或解析失败不得写0 |
| `field_parse_status_json` | 逐字段OBSERVED/MISSING/NOT_EXTRACTED/PARSE_ERROR及来源定位 |

后置主页资料不能伪装为发帖当时状态，也不能单独使作者进入主分析；只可作敏感性材料。历史帖按原始`source_published_at`审查。同一作者的目标帖跨出角色有效期时必须建立新`author_snapshot_id`。

#### V0.2 触达规模 `reach_tier`

| 代码 | 标签 | 粉丝量 |
|------|------|--------|
| `R4_MEGA` | 超大触达 | ≥ 100万 |
| `R3_LARGE` | 大触达 | 10万–不足100万 |
| `R2_MEDIUM` | 中触达 | 1万–不足10万 |
| `R1_SMALL` | 小触达 | 0–不足1万 |
| `R0_UNK` | 触达未知 | 缺失、未提取或解析失败 |

`reach_tier`只按同次快照的绝对粉丝数派生；平台分位数仅作敏感性分析。旧值只作规模迁移：`H-KOL→R4_MEGA`、`W-KOL→R3_LARGE`、`T-KOL→R2_MEDIUM`、`KOC→R1_SMALL`，不保留角色含义；旧记录缺少原始粉丝数时不得反推。

#### V0.3 内容垂直度

| 代码 | 操作定义 |
|------|----------|
| `TRAVEL` | T0内以旅行、目的地或旅游决策内容为持续主线 |
| `FOOD` | 以餐饮、美食探店或烹饪为主线，旅游不是主导 |
| `LIFESTYLE` | 以生活方式为主线，旅行只是其中一类 |
| `GENERAL` | 多领域并列，无稳定主导 |
| `OTHER` | 有稳定主线但不属于以上类型 |
| `UNK` | T0资料不足 |

单篇T1目标帖不得代替作者垂直度，正式分析使用`content_vertical_adjudicated`。

#### V0.4 原子证据代码与证据字段

编码员只判断`actor_scope`、`content_vertical`、EA代码、CE代码和诊断性整体角色，并为每一项填写1—5级置信度与证据来源ID。SC、覆盖状态和最终角色由程序派生；CI固定`UNAVAILABLE`，不由编码员填写。

| 人工字段 | 封闭值域与规则 |
|----------|----------------|
| `actor_scope` | PERSONAL_CREATOR / ORGANIZATION / MULTI_AUTHOR / UNCLEAR；只有单一自然人进入角色派生 |
| `expert_authority_criterion_codes` | EA_CREDENTIAL / EA_DOMAIN_OCCUPATION / EA_DOMAIN_VERIFICATION / EA_INSTITUTION_AFFILIATION / EA_SPECIALIST_HISTORY / EA_NONE / EA_UNK；前五项任一有证据即支持EA=1，NONE/UNK与阳性互斥 |
| `consumer_experience_criterion_codes` | CE_FIRSTHAND_REPEAT / CE_PEER_ORIENTATION / CE_NONE / CE_UNK；CE=1须两个不同日期的第一手来源和持续同伴导向同时成立 |
| `content_vertical` | TRAVEL / FOOD / LIFESTYLE / GENERAL / OTHER / UNK |
| `raw_role_response` | KOL_TYPE / KOC_TYPE / HYBRID / ORDINARY / UNK / NA；只作诊断 |

SC=1要求至少3个不同日期的旅游相关来源且跨度不少于30天。只有同时满足这一门槛并有可用主页资料或等价身份材料时，才可使用`EA_NONE/CE_NONE`；否则必须使用UNK。显示名本身不是等价主页材料。

`community_relation_status`当前统一为`UNAVAILABLE`。现库无评论、回复或关系材料，本轮不补抓；CI不参与充分性或角色派生，互动量、粉丝量、认证和商业披露也不能替代CI。

#### V0.5 组件裁决与角色派生

| `creator_role_derived` | 唯一组合 |
|------------------------|----------|
| `KOL_TYPE` | PERSONAL_CREATOR + SUFFICIENT；EA=1、CE=0、SC=1 |
| `KOC_TYPE` | PERSONAL_CREATOR + SUFFICIENT；EA=0、CE=1、SC=1 |
| `HYBRID` | PERSONAL_CREATOR + SUFFICIENT；EA=1、CE=1、SC=1 |
| `ORDINARY` | PERSONAL_CREATOR + SUFFICIENT；未满足前三种组合；不得解释为普通游客 |
| `UNK` | 身份或EA/CE/SC关键证据不足 |
| `NA` | ORGANIZATION或MULTI_AUTHOR，不适用个人角色分类 |

`raw_role_response`不覆盖派生结果。当前KOL/KOC须写作`KOL_TYPE/KOC_TYPE`，明确其为本研究试点操作类型。

#### V0.6 试点门

同轮建立25名V0作者共同校准与25篇作者互斥的V1—V6文本共同校准。V0按15名历史丰富、5名边界、5名随机抽取，每名最多展示5篇；共同校准不计算正式信度。完成15—25篇聚焦文献矩阵后修订并冻结`role-pilot-v0.2`，随即另抽50名新作者双人独立盲试标。对`actor_scope`、`content_vertical`和每个EA/CE代码报告alpha、作者级bootstrap 95%CI、支持数和混淆矩阵；任一核心字段`alpha < 0.80`、变异不足或存在系统边界分歧时返回校准。

正式比较至少需要10名证据充分的KOL型和10名KOC型作者；不足时只能完成规则试点。两份原始编码不可覆盖，裁决只追加；V0与内容任务只在两边锁定后由受限映射关联。视觉V7—V11另行启动，失败或延期不阻塞文本主论文。

### V1 明确商业披露（帖子级）

V1只记录帖子中是否存在可直接观察的商业披露，不根据正面程度、品牌出现次数或写作风格推断真实合作关系。

| 代码 | 定义 | 识别方式 |
|------|------|----------|
| 0 | 未观察到明确商业披露 | 帖文及其可用披露区域中未出现广告、赞助、合作、受邀体验或推广声明；该值不证明不存在商业关系 |
| 1 | 观察到明确商业披露 | 出现`#广告`、`#合作`、`品牌合作`、`赞助`、`受邀体验`、`推广`或语义等价的明确声明 |

反复称赞单一品牌、使用营销化语言、提供购买建议或整体评价高度正面，均不得单独据此编码为1。共同校准期间如发现疑似但未披露的推广性呈现，可写入问题登记或备注供方法审计，但不得形成正式标签，也不得据此声称存在未披露的商业合作。

### V2 内容特征元数据

| 字段 | 类型 | 取值 | 说明 |
|------|------|------|------|
| platform | 定类 | B站/抖音/微博/小红书/知乎 | 来源平台 |
| media_type | 定类 | TEXT_ONLY / IMAGE_TEXT / VIDEO_TEXT / LIVE | 帖子媒体形态 |
| text_length | 整数 | 字符数 | 帖文总长度 |
| image_count | 整数 | 张数 | 配图数量 |
| has_emoji | 0/1 | — | 帖文是否含表情包/emoji |
| has_hashtag | 0/1 | — | 帖文是否含#话题标签 |
| destination_type | 定类 | NATURE / CULTURE / CITY / RESORT / MIXED | 帖子主要涉及的目的地类型 |

---

## 三、文本轨：各维度操作定义

### V3 内容策略（片段级，5个父类 + 22个子项）

V3回答“创作者采用了哪些内容呈现策略，以及这些策略如何实现”。父类、子类和排除项全部由编码员逐字段判断并填写置信度；层级关系只用于冲突复核，不得自动补写。

| 父类 | 父类字段 | 子项字段 | 子项中文标签 |
|------|----------|----------|--------------|
| CS-INF 信息提供型 | `cs_inf` | `cs_inf_itn` / `cs_inf_par` / `cs_inf_pro` / `cs_inf_oth` | 行程与路线组织 / 实用参数提供 / 操作指导与选择辅助 / 其他信息提供方式 |
| CS-EDU 知识阐释型 | `cs_edu` | `cs_edu_bkg` / `cs_edu_cau` / `cs_edu_mea` / `cs_edu_oth` | 背景知识介绍 / 成因与机制解释 / 意义与价值阐释 / 其他知识阐释方式 |
| CS-EMO 情感渲染型 | `cs_emo` | `cs_emo_sen` / `cs_emo_atm` / `cs_emo_aff` / `cs_emo_oth` | 感官意象 / 氛围营造 / 主体情绪与身体感受 / 其他情感渲染方式 |
| CS-INT 互动激发型 | `cs_int` | `is_que` / `is_dir` / `is_soc` / `is_oth` / `is_non` | 面向受众提问 / 互动行动号召 / 社群与身份参与 / 其他互动方式 / 无互动信号 |
| CS-OTH 其他/未命中策略 | `cs_oth` | `cs_oth_gre` / `cs_oth_str` / `cs_oth_met` / `cs_oth_sym` / `cs_oth_res` | 寒暄与开场收束 / 结构过渡与篇章导航 / 创作与平台元话语 / 纯符号与纯标签 / 其他残余表达 |

前四个父类及其具名子项可以共现；同组`*_oth`只在具名子项均不适用时使用。`is_dir`只记录邀请评论、点赞、收藏、转发、关注、投票或标记他人的互动号召，普通旅行建议归`AT-SUG`。`is_non=1`要求`cs_int=0`且四个正向互动子项均为0；`cs_oth=1`原则上要求前四个父类均为0。上述关系只作复核。

| 原句 | 父类 | 子项 | 编码逻辑 |
|------|------|------|----------|
| “青岛三日游路线：Day1栈桥→天主教堂→信号山” | CS-INF | `cs_inf_itn` | 组织行程和站点顺序 |
| “栈桥始建于1892年，是青岛最早的军事码头” | CS-EDU | `cs_edu_bkg` | 提供历史背景 |
| “海风带着咸味，落日把海面染成金色” | CS-EMO | `cs_emo_sen`、`cs_emo_atm` | 感官意象与氛围共现 |
| “你们觉得哪里的烧烤最好吃？评论区告诉我” | CS-INT | `is_que`、`is_dir` | 提问与互动号召共现 |
| “嘿嘿，下一部分说交通” | CS-OTH | `cs_oth_gre`、`cs_oth_str` | 寒暄与篇章导航共现 |

CS-COM不作为片段标签；明确商业披露由V1记录。旧编号V5已并入`CS-INT`子项层，不再作为独立顶层维度。

---

### V4 目的地资源（片段级，三级编码）

该片段调用了目的地的哪类资源来建构形象？按《旅游资源分类、调查与评价》（GB/T 18972-2017）设计。

**编码员同时标注Layer 1、Layer 2、相关Layer 3事件子类及对应证据。** 层级关系只作复核；各标签可共现。

#### Layer 1：基本大类（编码员标注）

| 代码 | 大类 | 复核关系 |
|------|------|----------|
| RS-N | 自然旅游资源 | 与RS-N-GEO/RS-N-WAT/RS-N-BIO/RS-N-CLI的OR关系仅供复核 |
| RS-R | 人文旅游资源 | 与RS-R-HIS/RS-R-ARC/RS-R-GAS/RS-R-FOL/RS-R-REC/RS-R-EVT的OR关系仅供复核 |

#### Layer 2：资源亚类（编码员标注）

**RS-N 自然旅游资源（4个亚类）**

| 代码 | 亚类 | 定义 | 语义锚点 |
|------|------|------|----------|
| RS-N-GEO | 地文景观 | 山岳、岩石、地貌、洞穴、沙滩 | 崂山、信号山、浮山、岩石、沙滩 |
| RS-N-WAT | 水域风光 | 海洋、河流、湖泊、瀑布、泉水 | 大海、海滨、胶州湾、青岛湾、汇泉湾 |
| RS-N-BIO | 生物景观 | 动物、植物、森林、花海 | 海鸥、荷花、古树、花海、森林 |
| RS-N-CLI | 天象气候 | 日出、日落、星空、云海、雪景 | 崂山日出、石老人日出、海滨日落、海上日出、星空 |

**RS-R 人文旅游资源（6个亚类）**

| 代码 | 亚类 | 定义 | 语义锚点 |
|------|------|------|----------|
| RS-R-HIS | 遗址遗迹 | 以历史价值或遗产身份被提及的遗址、古建筑、文物和历史街区 | 栈桥、八大关、天主教堂、德国总督楼旧址 |
| RS-R-ARC | 建筑设施 | 以当代使用功能被提及的建筑、景区设施、住宿、交通和公共服务设施 | 酒店、民宿、高铁、缆车、景区大门 |
| RS-R-GAS | 饮食文化 | 地方美食、特色饮品、餐饮体验 | 海鲜、青岛啤酒、鲅鱼水饺、烧烤、野馄饨 |
| RS-R-FOL | 民俗风情 | 非遗技艺、民俗传统、地方礼俗、宗教文化和方言；事件本身不自动触发 | 非遗、道教（崂山）、海洋民俗、地方仪式 |
| RS-R-REC | 休闲游憩活动 | 不依赖特定场次、赛期、节期或届次即可反复获得的游览、休闲、运动、观光或互动体验 | 赶海、骑行、露营、潜水、游船观光、喂海鸥 |
| RS-R-EVT | 节事与文体事件 | 依赖特定场次、赛期、节期、展期或届次而发生的时限性事件 | 马拉松、足球赛、演唱会、音乐节、青岛啤酒节 |

#### Layer 3：节事与文体事件子类（编码员标注）

| 代码 | 子类 | 定义 | 语义锚点 |
|------|------|------|----------|
| EVT-SPT | 体育赛事 | 具有竞赛、赛会或职业/大众体育赛事属性 | 马拉松、足球赛、帆船赛、自行车赛 |
| EVT-PER | 演艺活动 | 以音乐、戏剧、舞蹈、曲艺或其他现场表演为核心 | 演唱会、音乐演出、舞台剧、现场演艺 |
| EVT-FES | 节庆会展 | 以节庆、庆典、主题节、展览或会议会展为核心 | 青岛啤酒节、灯会、文化节、博览会 |
| EVT-OTH | 其他节事与文体事件 | 已满足`RS-R-EVT`但不属于三个具名子类 | 颁奖活动、主题公共活动等边界事件 |

**编码示例**：

| 原句 | 主要V4标签 | 事件子类 | 编码逻辑 |
|------|------------|----------|----------|
| “坐船环游胶州湾” | `RS-N-WAT + RS-R-REC` | — | 常态观光体验 |
| “来青岛体验帆船” | `RS-N-WAT + RS-R-REC` | — | 不依赖特定赛事 |
| “来看青岛国际帆船赛” | `RS-N-WAT + RS-R-EVT` | `EVT-SPT` | 体育赛事 |
| “去凤凰之声看演唱会” | `RS-R-ARC + RS-R-EVT` | `EVT-PER` | 场馆与演艺事件共现 |
| “青岛啤酒节太热闹了” | `RS-R-EVT` | `EVT-FES` | 节庆名称不自动触发饮食或民俗 |
| “在啤酒节喝原浆、看乐队演出” | `RS-R-GAS + RS-R-EVT` | `EVT-FES + EVT-PER` | 饮食、节庆和演艺共现 |

`REC`与`EVT`不按收费或组织主体区分，而按是否依赖特定时限事件区分。事件不自动触发`RS-R-FOL`；只有直接调用民俗传统、地方仪式、非遗或宗教文化时才并标。`RS-R-ACT/rs_r_act = rs_r_rec OR rs_r_evt`仅在分析阶段汇总，不进入人工模板、置信度、证据跨度或模型输出；旧版人工`rs_r_act`不能反推新标签。完整边界以编码表v3.13.0为准。

---

### V5 语言功能与情感（片段级，可共现）

V5回答“片段承担什么语言功能，以及评价或建议表达什么情感方向和强度”。信息、评价和建议可共现；目的地属性指向已独立为V6。

#### Layer 1：语言功能（3个实质项 + 1个排除项）

| 代码 | 类型 | 定义 | 操作化标准 |
|------|------|------|------------|
| AT-INF | 信息性陈述 | 包含至少一个可核验的事实命题 | 可判断真假的时间、地点、属性、数据或知识陈述 |
| AT-EVL | 评价性表达 | 对目的地、设施或体验作出明确价值判断 | “美”“值”“贵”“拥挤”“失望”等评价词或等价表达 |
| AT-SUG | 建议/推荐 | 向受众提出行动建议、推荐或回避 | “推荐”“必去”“避雷”“千万别”等 |
| AT-NON | 其他/未命中 | 未命中信息、评价和建议三种V5功能；可能仍含V3-CS-INT子项记录的互动 | 编码员0/1判断 |

例如”门票80元，很值得去”同时编码`AT-INF=1`和`AT-EVL=1`；”人很多，建议早上去”同时编码`AT-INF=1`和`AT-SUG=1`。这类共现正是后续分析对象，不再用优先级抹去。

**AT-EVL 亚类**（基于Russell, 1980情感环状模型）：

| 亚类代码 | 情感质量 | 唤醒度 | 愉悦度 | 典型表达 |
|----------|----------|--------|--------|----------|
| AT-EVL-ADM | 赞赏/惊叹 | 高 | 正面 | “太美了””震撼””绝了” |
| AT-EVL-SAT | 满意/舒适 | 低 | 正面 | “很舒服””很满意””超值” |
| AT-EVL-EXC | 兴奋/激动 | 高 | 正面 | “太刺激了””超爽””尖叫” |
| AT-EVL-DIS | 失望/不满 | 低 | 负面 | “有点失望””不如预期””一般般” |
| AT-EVL-FRU | 沮丧/愤怒 | 高 | 负面 | “太坑了””巨坑””气死了” |
| AT-EVL-NOS | 怀旧/感慨 | 混合 | 混合 | “怀念””物是人非””故地重游” |

#### Layer 2：情感方向与双向强度

| 代码 | 含义 | 判定标准 |
|------|------|----------|
| POS | 正面情感 | 评价/建议中的情感倾向明确为正面 |
| NEG | 负面情感 | 评价/建议中的情感倾向明确为负面 |
| MIX | 混合/矛盾情感 | 完成评价原子切分后，同一属性仍存在不可合理拆开的正负并存或矛盾立场 |
| NEU | 中性 | 片段含评价或建议，且表达真实中性、无褒贬方向的立场 |

`sentiment_judgeable`由编码员判断。为0时`sentiment_dir`及两个强度字段记`NA`；为1时方向须为POS、NEG、MIX或NEU之一。`sentiment_pos_val`与`sentiment_neg_val`分别取0/1/2；净方向只能在分析阶段计算，不得覆盖人工原值。

**编码示例**：

| 原句 | INF | EVL | SUG | 情感方向 | 理由 |
|------|-----|-----|-----|----------|------|
| “栈桥始建于1892年” | 1 | 0 | 0 | NA | 无评价或建议，情感不可判定 |
| “门票80元，很值得去” | 1 | 1 | 0 | POS | 事实与评价共现 |
| “人很多，建议早上去” | 1 | 0 | 1 | NEU | 在固定评价原子片段内记录真实中性建议 |
| “避雷！千万别去这家海鲜店” | 0 | 0 | 1 | NEG | 负面回避建议 |
| “风景美但管理差” | — | — | — | 应按评价对象切成两个评价原子片段；只有无法合理切分时才保留MIX |
| “你们最喜欢哪里？” | 0 | 0 | 0 | NA | 提问由V3-CS-INT子项记录，情感不适用 |

### V6 目的地属性指向（片段级，4个父类 + 12个子类 + 1个其他项 + 1个适用性状态）

V6回答“评价或建议指向目的地的什么属性”。V6只记录方向中立的属性对象，正负方向与强度仍由同一评价原子片段的V5记录。若评价对象或情感方向发生切换，须在切分阶段形成新的`seg_id`，避免把不同对象与不同方向错误配对。

| 层级 | 代码/字段 | 中文标签 | 直接子类 |
|------|-----------|----------|----------|
| 适用性 | ASP-APP / `asp_applicable` | 目的地属性指向适用 | 为0时其余V6字段均记`NA`；为1时逐项0/1 |
| 父类 | ASP-EXP / `asp_exp` | 吸引力与体验 | `asp_res`资源/吸引物品质；`asp_act`活动与游览体验；`asp_atm`地方氛围与社会环境 |
| 父类 | ASP-SUP / `asp_sup` | 消费、服务与设施 | `asp_pri`价格与性价比；`asp_ser`服务态度与专业性；`asp_fac`设施完备与使用体验 |
| 父类 | ASP-OPS / `asp_ops` | 运营与可达 | `asp_cro`客流、排队与拥挤；`asp_mgt`秩序与运营管理；`asp_acc`交通与可达性 |
| 父类 | ASP-WEL / `asp_wel` | 卫生、安全与保障 | `asp_hyg`环境与食品卫生；`asp_saf`人身、财产与活动风险；`asp_sec`治安、警示与应急保障 |
| 其他 | ASP-OTH / `asp_oth` | 其他目的地属性 | 仅在12个具名子类均不适用时使用 |

`asp_applicable=1`时，至少一个具名子类或`asp_oth`为1；父类、子类和其他项仍分别人工判断。纯事实提及价格、交通或设施而没有评价/建议时，`asp_applicable=0`，其余V6字段记`NA`。V4记录被调用的资源，V6记录被评价或建议的属性，两者不可互相替代。

---

## 四、视觉轨：各维度操作定义

### V7 视觉主体（图片级，单标签）

| 代码 | 类别 | 定义 | 典型内容 |
|------|------|------|----------|
| VS-NAT | 自然景观 | 以自然地貌、水体、天空、植被为主要视觉元素 | 海景、山峦、日出日落、森林、花海 |
| VS-ARC | 建筑遗产 | 以人造建筑、古迹、城市地标为主要视觉元素 | 古桥、城墙、寺庙、现代地标建筑 |
| VS-PEO | 人物活动 | 以人物及其活动为主要视觉元素 | 游客打卡照、当地人生活场景 |
| VS-FOD | 美食/物产 | 以食物、饮品、地方特产为主要视觉元素 | 海鲜大餐、啤酒、烧烤 |
| VS-FAC | 服务设施 | 以旅游服务设施、交通工具为主要元素 | 酒店、民宿、游船、缆车 |

### V8 视觉呈现方式（图片级，三个独立子字段）

旧编号V12把场景性质、景别和拍摄视角混在一个互斥变量中，例如“标志性景点的航拍全景”会同时满足三个类别。本版拆为三个相互独立的单选子字段。

| 子字段 | 代码 | 类别 | 定义 |
|--------|------|------|------|
| 场景语境 `scene_context` | SC-ICO | 标志性场景 | 画面呈现可识别的目的地地标或代表性景点 |
|  | SC-STR | 日常场景 | 普通街道、市场、社区或市井生活 |
|  | SC-OTH | 其他/不适用 | 美食特写、室内设施或无法归入前两类的画面 |
| 景别 `shot_scale` | SS-CLO | 特写 | 聚焦人物、食物或物体局部细节 |
|  | SS-MID | 中景 | 主体及其周边环境均清晰可见 |
|  | SS-PAN | 全景 | 开阔场景或大范围环境占画面主体 |
| 视角 `viewpoint` | VP-GRD | 平视/常规视角 | 接近普通观看高度 |
|  | VP-HIG | 高位俯视 | 从高处俯看，但无明确航拍特征 |
|  | VP-AER | 航拍 | 具有无人机或显著高空俯瞰特征 |

> **注**：旧编号V13（视觉氛围，5类）已从编码体系中删除。视觉轨现保留V7、V8、V9、V10、V11五个维度。

### V9 人物呈现（图片级，人数 + 互动）

原`HP-INT`与单人/群体并不互斥，因此拆为两个字段。

| 子字段 | 代码/取值 | 类别 | 定义 |
|--------|-----------|------|------|
| 人数 `people_count` | PC-NON | 无人 | 画面中不出现可识别人体 |
|  | PC-SIN | 单人 | 画面中仅出现1人 |
|  | PC-GRP | 群体 | 画面中出现2人及以上 |
| 人景互动 `human_scene_interaction` | 0/1 | 无/有 | 人物是否与目的地元素发生明确接触、操作或面向性交互；无人时通常记0，但仍由编码员人工填写 |

### V10 消费符号（图片级，存在性 + 类型）

| 代码 | 类别 | 定义 |
|------|------|------|
| CS-BRA | 品牌标识存在 | 画面中可见可识别的品牌Logo或名称；只记录一次存在性 |
| CS-ACC | 住宿设施 | 画面中可见具体的住宿场所名称 |
| CS-RES | 餐饮场所 | 画面中可见具体的餐厅/店铺名称 |
| CS-TRA | 交通工具 | 画面中可见具体交通工具品牌 |

`CS-BRA`、`CS-ACC`、`CS-RES`和`CS-TRA`均由编码员人工判断，且可共现。若可识别品牌不属于三类，则只标`CS-BRA=1`；总项与子类关系仅供复核。

### V11 可见对象状态（图片级，10个条件性原子项）

V11不判断整张图片“正面/负面”，只记录画面中对象的直接可见状态。设施维护、卫生整洁、现场秩序和环境维护各拆为正向与负向字段，另设可见危险源和防护措施；同轴正负可以同时为1。

| 状态轴 | 正向字段 | 负向字段 |
|--------|----------|----------|
| 设施维护 | `vis_fac_condition_pos`：设施完好/维护良好 | `vis_fac_condition_neg`：设施破损/失修 |
| 卫生整洁 | `vis_hyg_condition_pos`：干净整洁 | `vis_hyg_condition_neg`：脏乱/不卫生 |
| 现场秩序 | `vis_order_condition_pos`：秩序清晰 | `vis_order_condition_neg`：杂乱/秩序混乱 |
| 环境维护 | `vis_environment_condition_pos`：环境维护良好 | `vis_environment_condition_neg`：污染/环境退化 |
| 可见安全线索 | `vis_protection_present`：可见防护措施 | `vis_hazard_present`：可见危险源 |

无相关对象、状态轴未触发或可见范围不足时记`NA`；状态轴适用且某标签未成立时记`0`；共同校准期无法唯一裁决时记`UNRESOLVED`并备注。完整纳入/排除规则、`UNK`边界及冻结门仅以`编码表.md` v3.13.0为准。

---

## 五、编码记录格式

### 5.0 V0作者表（独立于内容编码Excel）

V0使用五张逻辑表，不能把同一作者的多篇帖子重复计算为多个角色样本。内容编码界面不显示作者链接；回收后才附加`author_snapshot_id`。

| 逻辑表 | 每行单位 | 必要字段 |
|--------|----------|----------|
| `author_linkage_private`（受限） | 每个作者快照一行 | `author_snapshot_id`、`platform`、`platform_author_id_raw`、`author_id`、可选`creator_entity_id`、`display_name_raw`、`bio_raw`、`profile_url_raw`、`verification_raw`、`linkage_created_at`、`linkage_rule_version` |
| `author_snapshots` | 每个作者快照一行 | `author_snapshot_id`、`author_id`、`platform`、`evidence_window_id`、`evidence_manifest_id`、`evidence_manifest_hash`、`profile_captured_at`、`evidence_window_start`、`evidence_window_end`、`t1_window_start`、`t1_window_end`、`t1_reference_at`、`profile_time_relation`、`profile_post_gap_days`、`time_gate_status`、`time_gate_reason`、`time_rule_version`、`follower_count`、`following_count`、`post_count_raw`、`reach_tier`、`field_parse_status_json`、`codebook_version` |
| `author_evidence_sources`（受限） | 每个manifest来源一行 | `evidence_manifest_id`、`source_id`、`source_type`、`source_published_at`、`captured_at`、`source_inclusion_status`、`exclusion_reason_code`、`domain_relevance`、`dedup_cluster_id`、`visibility_parse_status`、`private_locator`、`source_checksum`、`aggregate_definition_json` |
| `author_role_annotations` | 每个作者快照×编码员一行 | `author_snapshot_id`、`evidence_manifest_id`、`coder`、`coded_at`、`actor_scope`、`content_vertical`、`expert_authority_criterion_codes`、`consumer_experience_criterion_codes`、`community_relation_status=UNAVAILABLE`、`raw_role_response`、EA/CE/SC派生值、`evidence_status`、逐字段证据ID与置信度、`role_rule_version`、`codebook_version` |
| `author_role_adjudications` | 每个作者快照一行 | `author_snapshot_id`、`evidence_manifest_id`、`adjudication_status`、`actor_scope_adjudicated`、`content_vertical_adjudicated`、EA/CE裁决代码、`community_relation_status=UNAVAILABLE`、EA/CE/SC派生值、`evidence_status_adjudicated`、`role_rule_version`、`creator_role_derived`、`creator_role_adjudicated`、裁决责任与时间、`codebook_version` |

`adjudication_status`取`AGREEMENT_ACCEPTED / RESOLVED / UNRESOLVED`：全部实质输入一致才可用AGREEMENT_ACCEPTED；任一组件不同须RESOLVED并保留轨迹；关键组件未决时为UNRESOLVED、最终证据状态为INSUFFICIENT且正式角色为UNK。v3.13.0不允许个案override，原始双人响应永不被覆盖；机构与多人账号为NA。

### 5.1 帖子级元数据（每帖一行）

| 字段 | 类型 | 维度 | 说明 |
|------|------|------|------|
| post_id | str | — | 帖子唯一ID |
| platform | str | V2 | 平台名 |
| author_snapshot_id | str | V0 | 分析就绪表关联V0作者快照；内容编码员界面隐藏，回收后由受限程序附加 |
| has_commercial_disclosure | int | V1 | 是否观察到明确商业披露（0/1） |
| media_type | str | V2 | 帖子媒体形态 |
| text_length | int | V2 | 帖文字数 |
| image_count | int | V2 | 配图数量 |
| has_emoji | int | V2 | 0/1 |
| has_hashtag | int | V2 | 0/1 |
| destination_type | str | V2 | NATURE/CULTURE/CITY/RESORT/MIXED |
| confidence_json | json | — | 本行人工主观字段的逐字段置信度映射 |
| confidence_notes_json | json | — | 置信度1—2字段的结构化备注 |

### 5.2 文本片段级编码（每片段一行）

| 字段 | 类型 | 维度 | 说明 |
|------|------|------|------|
| post_id | str | — | 帖子唯一ID |
| seg_id | int | — | 片段序号 |
| raw_text | str | — | 原始文本 |
| cs_inf | int | V3-L1 | 信息提供型父类0/1，编码员标注 |
| cs_inf_itn / cs_inf_par / cs_inf_pro / cs_inf_oth | int | V3-L2 | 信息提供型4个子类，逐字段0/1 |
| cs_edu | int | V3-L1 | 知识阐释型父类0/1，编码员标注 |
| cs_edu_bkg / cs_edu_cau / cs_edu_mea / cs_edu_oth | int | V3-L2 | 知识阐释型4个子类，逐字段0/1 |
| cs_emo | int | V3-L1 | 情感渲染型父类0/1，编码员标注 |
| cs_emo_sen / cs_emo_atm / cs_emo_aff / cs_emo_oth | int | V3-L2 | 情感渲染型4个子类，逐字段0/1 |
| cs_int | int | V3-L1 | 互动激发型父类0/1，编码员标注 |
| is_que / is_dir / is_soc / is_oth / is_non | int | V3-L2-INT | 互动4个正向子项及1个排除项，逐字段0/1 |
| cs_oth | int | V3-L1 | 其他/未命中策略父类0/1，编码员标注 |
| cs_oth_gre / cs_oth_str / cs_oth_met / cs_oth_sym / cs_oth_res | int | V3-L2 | 其他策略5个子类，逐字段0/1 |
| rs_n | int | V4-L1 | 自然旅游资源0/1，编码员标注 |
| rs_r | int | V4-L1 | 人文旅游资源0/1，编码员标注 |
| rs_n_geo | int | V4-L2 | 地文景观 0/1 |
| rs_n_wat | int | V4-L2 | 水域风光 0/1 |
| rs_n_bio | int | V4-L2 | 生物景观 0/1 |
| rs_n_cli | int | V4-L2 | 天象气候 0/1 |
| rs_r_his | int | V4-L2 | 遗址遗迹 0/1 |
| rs_r_arc | int | V4-L2 | 建筑设施 0/1 |
| rs_r_gas | int | V4-L2 | 饮食文化 0/1 |
| rs_r_fol | int | V4-L2 | 民俗风情 0/1 |
| rs_r_rec | int | V4-L2 | 休闲游憩活动0/1 |
| rs_r_evt | int | V4-L2 | 节事与文体事件0/1 |
| evt_spt / evt_per / evt_fes / evt_oth | int | V4-L3-EVT | 体育赛事/演艺活动/节庆会展/其他事件，逐字段0/1 |
| evidence_spans_json | json | — | V3/V4/V5/V6统一文本证据跨度；结构见第5.4节 |
| at_has_info | int | V5-L1 | 信息性陈述0/1，编码员标注 |
| at_has_eval | int | V5-L1 | 评价性表达0/1，编码员标注 |
| at_eval_subtype | str | V5-L1 | AT-EVL亚类：ADM/SAT/EXC/DIS/FRU/NOS；at_has_eval=0时为NA |
| at_has_sug | int | V5-L1 | 建议/推荐0/1，编码员标注 |
| at_non | int | V5-L1 | 其他/未命中0/1，编码员标注 |
| sentiment_judgeable | int | V5-L2 | 情感方向可判定0/1，编码员标注 |
| sentiment_dir | str | V5-L2 | POS/NEG/MIX/NEU；不可判定时NA |
| sentiment_pos_val | int | V5-L2 | 正面情感强度0/1/2；情感不可判定时NA，编码员标注 |
| sentiment_neg_val | int | V5-L2 | 负面情感强度0/1/2；情感不可判定时NA，编码员标注 |
| asp_applicable | int | V6-STATE | 目的地属性指向适用0/1；为0时其余V6字段记NA |
| asp_exp / asp_sup / asp_ops / asp_wel | int/NA | V6-L1 | 4个属性父类，逐字段0/1/NA |
| asp_res / asp_act / asp_atm | int/NA | V6-L2 | 吸引力与体验3个子类，逐字段0/1/NA |
| asp_pri / asp_ser / asp_fac | int/NA | V6-L2 | 消费、服务与设施3个子类，逐字段0/1/NA |
| asp_cro / asp_mgt / asp_acc | int/NA | V6-L2 | 运营与可达3个子类，逐字段0/1/NA |
| asp_hyg / asp_saf / asp_sec | int/NA | V6-L2 | 卫生、安全与保障3个子类，逐字段0/1/NA |
| asp_oth | int/NA | V6-OTHER | 其他目的地属性0/1/NA |
| seg_length | int | — | 字符数 |
| coder | str | — | 编码者标识 |
| confidence_json | json | — | 本片段全部人工主观字段的逐字段置信度映射 |
| confidence_notes_json | json | — | 置信度1—2字段的结构化备注 |

### 5.3 配图级编码（每图一行）

| 字段 | 类型 | 维度 | 说明 |
|------|------|------|------|
| post_id | str | — | 帖子唯一ID |
| img_id | int | — | 图片序号 |
| img_url | str | — | 图片原始URL |
| visual_subject | str | V7 | VS-NAT/VS-ARC/VS-PEO/VS-FOD/VS-FAC |
| scene_context | str | V8 | SC-ICO/SC-STR/SC-OTH |
| shot_scale | str | V8 | SS-CLO/SS-MID/SS-PAN |
| viewpoint | str | V8 | VP-GRD/VP-HIG/VP-AER |
| people_count | str | V9 | PC-NON/PC-SIN/PC-GRP |
| human_scene_interaction | int | V9 | 人景互动0/1，编码员标注；无人时通常记0 |
| cs_brand | int | V10 | 品牌标识 0/1 |
| cs_accom | int | V10 | 住宿设施 0/1 |
| cs_rest | int | V10 | 餐饮场所 0/1 |
| cs_trans | int | V10 | 交通工具 0/1 |
| vis_fac_condition_pos | int/NA | V11 | 设施完好/维护良好，条件性0/1 |
| vis_fac_condition_neg | int/NA | V11 | 设施破损/失修，条件性0/1 |
| vis_hyg_condition_pos | int/NA | V11 | 干净整洁，条件性0/1 |
| vis_hyg_condition_neg | int/NA | V11 | 脏乱/不卫生，条件性0/1 |
| vis_order_condition_pos | int/NA | V11 | 秩序清晰，条件性0/1 |
| vis_order_condition_neg | int/NA | V11 | 杂乱/秩序混乱，条件性0/1 |
| vis_environment_condition_pos | int/NA | V11 | 环境维护良好，条件性0/1 |
| vis_environment_condition_neg | int/NA | V11 | 污染/环境退化，条件性0/1 |
| vis_hazard_present | int/NA | V11 | 可见危险源，条件性0/1 |
| vis_protection_present | int/NA | V11 | 可见防护措施，条件性0/1 |
| coder | str | — | 编码者标识 |
| confidence_json | json | — | 本图全部人工主观字段的逐字段置信度映射 |
| confidence_notes_json | json | — | 置信度1—2字段的结构化备注 |

### 5.4 状态、置信度与文本证据记录契约

#### 一般状态语义

| 状态 | 操作定义 | 使用边界 |
|------|----------|----------|
| `1` | 字段适用，且观察到支持该判断的材料证据 | 正式值 |
| `0` | 字段适用，完整判断后未观察到相应内容 | 不得表示未检查、无法判断或不适用 |
| `UNK` | 字段适用，但材料缺失、过度模糊或证据冲突，无法形成有证据的唯一判断 | 是否进入某字段正式值域由共同校准后的书面决定确定 |
| `NA` | 字段因条件未触发而结构性不适用 | 不参加该字段信度分母和模型损失 |
| `UNRESOLVED` | 共同校准期的临时工作状态 | 不属于正式研究值；必须登记并在盲试标前关闭 |

共同校准用于决定哪些字段需要专门`UNK/NA`或边界规则，不计算正式信度。规则冻结后另取新样本盲试标；字段达到`alpha >= 0.80`、类别支持足够且没有系统性边界分歧时，不再追加字段专门规则。实质修订后须换新样本重新确认。

#### 逐字段置信度

置信度是每个主观判断均须执行的程序性审计，不是准确概率、研究结果或模型监督目标。客观导入字段不评分；结构性`NA`记为`null`。

| 等级 | 操作定义 | 处理 |
|------|----------|------|
| 5 | 直接明确证据，与锚点高度一致，无合理替代判断 | 正常保存 |
| 4 | 证据清楚，仅需少量语境理解，替代判断明显不合理 | 正常保存 |
| 3 | 判断有依据，但接近边界、依赖语境，或存在一个合理替代判断 | 正常保存，不强制备注 |
| 2 | 证据较弱、冲突或高度依赖上下文，存在两个以上合理判断 | 必须备注并重点复核 |
| 1 | 没有足够证据支持唯一判断 | 共同校准期记`UNRESOLVED`；正式允许时记`UNK`；必须备注和复核 |

`confidence_json`以字段名为键保存1—5。`confidence_notes_json`覆盖所有1—2级字段，并记录`reason_code`、`alternative_values`和`note`；没有合理替代值时`alternative_values`为空数组。编码员只从`BOUNDARY`、`CONTEXT`、`CONFLICT`、`EVIDENCE_MISSING`或`OTHER`中选择原因并写一句说明，不手写JSON。

#### 统一文本证据跨度

文本轨只使用`evidence_spans_json`，每个对象包含`fields`、`start`、`end`和`quote`：

```json
[
  {"fields": ["rs_n_wat"], "start": 3, "end": 5, "quote": "海风"},
  {"fields": ["at_has_eval", "sentiment_dir"], "start": 8, "end": 11, "quote": "很舒服"}
]
```

- offset定位不可变`raw_text`，按Unicode字符从0开始，采用左闭右开`[start, end)`。
- 编码员只复制最短充分原文，工具自动计算offset；同一原文重复出现时才填写起始位置。
- 必须满足`raw_text[start:end] == quote`；多个不连续跨度分开保存，跨度可重叠，同一跨度可支持多个字段。
- 选择最短充分证据；否定、程度和转折改变含义时须一并纳入。
- 实质性阳性标签原则上至少一个跨度；`0`、`UNK`、`NA`和整体排除判断不伪造局部跨度。

### 5.5 校准问题与编码簿修订记录（不属于研究变量）

以下记录只服务于共同校准、盲试标与版本评审，不进入H1—H8检验，不作为模型监督目标，也不构成主题分析材料。

共同校准编码员只填写一张主表：编码值、逐字段置信度、置信度1—2或`UNRESOLVED`时的一句说明、阳性判断证据原文，以及极少数额外问题。系统生成JSON、offset和问题编号；以下问题登记表与修订决策表只由研究负责人维护。

**问题登记表**

| 字段 | 类型 | 说明 |
|------|------|------|
| issue_id | str | 问题唯一ID |
| post_id | str | 对应帖子ID |
| seg_id | int/NA | 涉及文本片段时填写 |
| img_id | int/NA | 涉及图片时填写 |
| issue_type | str | `FRAMEWORK_GAP` / `BOUNDARY_CASE` / `COUNTEREXAMPLE` / `CONTEXT_DEPENDENCE` / `UNITIZATION_PROBLEM` |
| affected_fields_json | json | 可能受影响的现有字段 |
| evidence_spans_json | json | 文本证据遵循第5.4节；视觉图像区域契约待视觉T0关闭 |
| issue_note | str | 描述材料及现行规则为何不足，不写最终裁决 |
| coder | str | 登记者标识 |
| codebook_version | str | 问题发生时使用的编码表版本 |
| status | str | `OPEN` / `DECIDED` / `DEFERRED` |

**修订决策表**

| 字段 | 类型 | 说明 |
|------|------|------|
| decision_id | str | 决策唯一ID |
| issue_ids_json | json | 作为证据的问题ID列表 |
| decision_type | str | `CLARIFY_DEFINITION` / `ADD_ANCHOR_EXAMPLE` / `REVISE_RULE` / `ADD_CATEGORY` / `MERGE_CATEGORY` / `NO_CHANGE` / `DEFER` |
| affected_fields_json | json | 被澄清或修改的字段 |
| decision_rationale | str | 依据研究问题、理论/标准、支持证据与反例说明理由 |
| recoding_scope | str | `NONE` / `PILOT_ONLY` / `AFFECTED_FIELD_ALL` / `FULL_RECODE` |
| effective_version | str | 生效版本；`DEFER`时为NA |
| decided_by_json | json | 参与决定的研究者标识 |
| decided_at | datetime | 决策时间 |

`FRAMEWORK_GAP`不是开放代码，`COUNTEREXAMPLE`不得因不符合理论预期而删除。编码员只能在主表中用一句话提示额外问题，不能创建新值；问题分类、合并与关闭由研究负责人在`calibration-issues.csv`和`revision-decisions.csv`中完成。

---

## 六、编码规则清单

### 文本轨编码规则

| # | 规则 | 说明 |
|---|------|------|
| 1 | **逐字段人工编码** | 编码员对所有进入记录表的字段逐项判断；跨字段逻辑仅用于复核，不得由程序自动生成或覆写 |
| 2 | **逐字段置信度** | 每个主观判断均评分；1—2级必须写结构化备注并进入重点复核 |
| 3 | **贴着文本** | 编码来自句子自身传达的信息，不过度推断 |
| 4 | **证据先行** | V3/V4/V5/V6实质性阳性判断统一记录证据跨度；层级和跨字段逻辑仅用于复核 |
| 5 | **资源≠语言功能≠属性指向** | V4判断调用了什么旅游资源；V5判断语言功能与情感；V6判断评价/建议指向什么目的地属性 |
| 6 | **渲染≠评价** | 感官/氛围呈现归`CS-EMO`；明确好坏、价值或满意度判断归`AT-EVL`；二者可共现 |
| 7 | **情感方向的条件标注** | 先人工标注`sentiment_judgeable`；为1时再标POS/NEG/MIX/NEU，为0时`sentiment_dir=NA`。NEU只表示真实中性评价 |
| 8 | **平台语境** | 以V3定义为主、平台校准为辅 |
| 9 | **共现不是折中** | 每个原子项分别作0/1判断，不设置“主要标签”或人为优先级 |
| 10 | **评价原子切分** | 评价对象或情感方向切换时强制切分；V5情感与V6属性只在同一固定`seg_id`内配对 |

### 视觉轨编码规则

| # | 规则 | 说明 |
|---|------|------|
| 1 | **画面优先** | 编码依据是画面本身，而非配文描述 |
| 2 | **主体面积优先** | 仅V7视觉主体在多个元素共存时按视觉突出度和面积判定 |
| 3 | **单图独立** | 每张图片独立编码 |
| 4 | **宁缺毋滥** | 品牌/标识不清晰时不编码为1 |
| 5 | **分轴判断** | V8分别判断场景语境、景别和视角；V9分别判断人数和互动，不互相替代 |
| 6 | **可见证据限定** | V11只记录画面直接可见的对象状态，不从配文、滤镜、品牌或场景类别推断 |
| 7 | **适用性先行** | V11状态轴未触发或可见范围不足时记NA；适用但某原子标签不成立才记0 |
| 8 | **正负独立** | V11同轴正负可同时为1；不得互斥、抵消或汇总为整图情感 |
| 9 | **安全线索不对称** | 可见危险源与防护措施均须有直接证据；未见防护不等于不安全，见到防护也不等于无风险 |

### 编码簿开发与修订规则

| # | 规则 | 执行要求 |
|---|------|----------|
| 1 | **问题登记不等于新增编码** | 发现框架遗漏、边界案例或反例时登记问题；团队决定生效前不得自造标签 |
| 2 | **相关性先行** | 只有与既定研究问题直接相关且现行字段无法有效表达的内容可记为`FRAMEWORK_GAP` |
| 3 | **反例必须保留** | 实质修订须同时检查支持证据、分歧案例和反例，不能只凭高频词、典型例或理论预期改规则 |
| 4 | **语境与观察单位分开** | 可阅读完整帖子与必要图文语境消歧，但标签仍落在固定`seg_id`或`img_id`；切分问题单独登记 |
| 5 | **备忘不具规范效力** | `issue_note`只记录问题；只有写入新版编码表的定义、值域、规则和锚点例句才生效 |
| 6 | **冻结后不得静默改码** | 正式编码后若边界需要改变，暂停受影响字段、升版并确定重编码范围 |
| 7 | **AI仅提供线索** | AI可检索相似案例或提出候选问题，但研究者须核验并记录模型版本；AI无权增删类目或作最终裁决 |

仅修正错别字、链接或完全不改变判定结果的表述时可记`recoding_scope=NONE`；增加锚点但不改变边界时至少复核试标样本；定义、值域、分析单位、纳入/排除规则或类目集合改变时，旧信度失效，至少重编码全部受影响字段，并用未参与规则修订的新样本重新确认信度。

---

## 七、候选帖子级聚合指标体系（`DEFERRED`）

本节只保留未来可能使用的聚合候选，不构成编码表冻结、文本试标或模型训练的前置条件。原子标签通过正式信度、模型效度和全语料发布后，才重新决定采用何种聚合方法。

### 7.1 文本轨聚合指标

| 指标 | 公式 | 取值范围 | 含义 |
|------|------|----------|------|
| segment_count | COUNT(seg_id) | 1-N | 有效文本片段总数 |
| **V4 Layer 1** | | | |
| rsd_n | SUM(rs_n) / segment_count | [0, 1] | 自然旅游资源提及密度 |
| rsd_r | SUM(rs_r) / segment_count | [0, 1] | 人文旅游资源提及密度 |
| **V4 Layer 2** | | | |
| ICB | 10个Layer 2自然+人文亚类出现数之和 | 0-10 | 总资源覆盖广度；事件Layer 3不重复计入 |
| IMP_N | [rsd_n_geo, rsd_n_wat, rsd_n_bio, rsd_n_cli] | 四维向量 | 自然资源亚类轮廓 |
| IMP_R | [rsd_r_his, rsd_r_arc, rsd_r_gas, rsd_r_fol, rsd_r_rec, rsd_r_evt] | 六维向量 | 人文资源亚类轮廓 |
| ETP | [evtd_spt, evtd_per, evtd_fes, evtd_oth] | 四维向量 | 节事与文体事件内部类型轮廓；分母为`rs_r_evt=1`片段数 |
| rsd_r_act | SUM(`rs_r_rec OR rs_r_evt`) / segment_count | [0, 1] | 全部活动资源汇总密度，仅分析阶段派生 |
| **V3** | | | |
| CSP-L1 | [csd_inf, csd_edu, csd_emo, csd_int, csd_oth] | 五维向量 | 内容策略父类出现率；各维可共现，和不必为1 |
| CSP-L2 | 22个V3子项各自出现率 | 22维向量 | 子类实现方式轮廓；父子关系仅作一致性复核 |
| **V5 Layer 1** | | | |
| ATP | [atd_inf, atd_evl, atd_sug] | 三维向量 | 语言功能出现率；各维可共现，和不必为1 |
| **V5 Layer 2** | | | |
| ATD | [atd_pos, atd_neg, atd_mix] | 三维向量 | 情感方向轮廓（仅AT-EVL+AT-SUG） |
| **V6** | | | |
| AIP-L1 | [aspd_exp, aspd_sup, aspd_ops, aspd_wel] | 四维向量 | 目的地属性父类轮廓；仅以`asp_applicable=1`片段为分母 |
| AIP-L2 | 12个V6具名子类各自出现率 | 12维向量 | 方向中立的属性指向轮廓；排除NA |
| **V3-CS-INT子项** | | | |
| ISP | [isd_que, isd_dir, isd_soc, isd_oth] | 四维向量 | 互动实现方式轮廓 |
| CED | SUM((sentiment_pos_val - sentiment_neg_val) × rs_depth) / segment_count | [-20, +20] | 资源加权净情感方向（分析阶段计算），不解释为因果“效能” |
| CEI_pos / CEI_neg | SUM(`sentiment_pos_val` × `rs_depth`) / SUM(`sentiment_neg_val` × `rs_depth`) | [0, +20] | 正面/负面方向的资源加权值（分析阶段计算） |
| CER | CEI_pos / (CEI_pos + CEI_neg) | [0, 1] | 有方向表达中的正向占比 |

当`CEI_pos + CEI_neg = 0`时，`CER`记为缺失值（NA），不把“没有方向表达”编码成0.5。

### 7.2 视觉轨聚合指标

| 指标 | 公式 | 含义 |
|------|------|------|
| image_count | COUNT(img_id) | 配图数量 |
| VIC | 视觉主体覆盖广度（0-5） | 几类视觉主体被使用 |
| dominant_visual_subject | V7频次取最大值 | 主导视觉主体 |
| hpd_sin | COUNT(PC-SIN) / image_count | 单人呈现占比 |
| hpd_grp | COUNT(PC-GRP) / image_count | 群体呈现占比 |
| hpd_int | SUM(human_scene_interaction) / image_count | 人景互动占比 |
| v16_fac_pos / v16_fac_neg | SUM对应字段=1 / COUNT对应字段∈{0,1} | 设施维护状态正向/负向出现率；各自排除NA |
| v16_hyg_pos / v16_hyg_neg | SUM对应字段=1 / COUNT对应字段∈{0,1} | 卫生整洁状态正向/负向出现率；各自排除NA |
| v16_order_pos / v16_order_neg | SUM对应字段=1 / COUNT对应字段∈{0,1} | 现场秩序状态正向/负向出现率；各自排除NA |
| v16_environment_pos / v16_environment_neg | SUM对应字段=1 / COUNT对应字段∈{0,1} | 环境维护状态正向/负向出现率；各自排除NA |
| v16_hazard / v16_protection | SUM对应字段=1 / COUNT对应字段∈{0,1} | 可见危险源/防护措施出现率；各自排除NA |

V11只报告原子项或成对轮廓，不计算单一“视觉正负得分”；上述聚合在独立信度冻结与分析预注册前均为`DEFERRED`。

### 7.3 跨维度关联指标

箭头表示条件化方向，不表示因果传导。V3与V5均为多标签，因此矩阵同一行的比例之和不要求等于1。

**SRM_L1：策略→资源大类关联（4×2）**

```
SRM_L1[CS-X][RS-Z] = COUNT(segments with CS-X AND RS-Z=1) / COUNT(segments with CS-X)
```

**SRM_L2：策略→资源亚类关联（4×10）**

**SRM_EVT：策略→事件子类关联（4×4；限`rs_r_evt=1`片段）**

**SAM_L1：资源大类→语言功能共现（2×3）**

**SAM_L2：资源亚类→语言功能共现（10×3）**

**SAM_EVT：事件子类→语言功能共现（4×3；限`rs_r_evt=1`片段）**

**AAM：目的地属性→情感方向关联（12×3）**

```
AAM[ASP-X][DIR-Z] = COUNT(segments with ASP-X=1 AND sentiment_dir=Z) / COUNT(segments with ASP-X=1)
```

其中`Z ∈ {POS, NEG, MIX}`。AAM仅分析`asp_applicable=1`且V6具名子类为1的评价原子片段；属性与方向必须来自同一`seg_id`，不得跨片段配对，也不将条件共现解释为因果传导。

**COD：共现密度**

| 指标 | 公式 | 含义 |
|------|------|------|
| COD_n_r | SUM(rs_n × rs_r) / segment_count | 自然+人文大类同片段共现密度 |
| COD_ngeo_rhis | SUM(rs_n_geo × rs_r_his) / segment_count | 地文景观+历史遗存共现密度 |
| COD_inf_evl | SUM(cs_inf × at_has_eval) / segment_count | 实用信息策略+评价功能共现密度 |
| IFD | SUM(rs_depth) / segment_count | 平均资源融合深度（0-10）；`rs_depth`只累加10个Layer 2亚类，不重复累加Layer 3事件子类或`rs_r_act`汇总项 |

---

## 八、标注方案与模型验证

本研究依次执行“框架建立—数据校准—盲试标冻结—正式人工测量—模型扩展—统计分析”。神经网络只用于扩大已冻结字段的测量范围。人工信度与模型效度分开报告：前者检验编码规则能否稳定执行，后者检验模型能否复现经裁决的人工金标。

V0作者角色沿同一“共同校准—新作者盲试标—信度—裁决”逻辑运行，但单位是作者快照，且使用独立作者表。角色试点不得混入下表的内容编码Excel或用同作者的重复帖子抬高样本量；若以后训练角色模型，须另行通过锁定测试、逐类/平台/时间子群和概率校准门。

| 阶段 | 文本轨 | 视觉轨 | 目的 |
|------|--------|--------|------|
| 阶段0：框架与范围预定义 | 定义变量、一般状态语义、单位、逐字段置信度和证据规则；达到`CALIBRATION_READY`即可启动共同校准 | 启动前另行冻结视觉人口、单位和边界T0 | 建立研究问题—构念—字段—输出的可追溯关系 |
| 阶段1：共同校准与规则修订 | 约20—30篇共同编码；编码员只填写单一主表，1—2级或`UNRESOLVED`写一句备注；工具提取问题，负责人分类和裁决；不计算正式信度 | 仅在视觉人口明确后，用真实单图共同校准V7/V8边界和V11状态轴 | 决定字段专门`UNK/NA`与边界规则，不生成主题或正式结果 |
| 阶段2：独立盲试标与冻结 | 另取约70—80篇新样本双人独立编码，首轮完成前不交换判断；随后计算信度并裁决 | 视觉轨启动后采用同样流程 | 冻结规则；实质修订后旧信度失效并用新样本确认 |
| 阶段3：评估金标与训练标注 | 锁定测试集全部双人独立编码并共识；训练池由主编码员标注，第二人复核随机比例及全部风险样本 | 文本证据通过后另行确定，不预设600张 | 建立可追溯金标与训练标注；隔离训练、开发和测试 |
| 阶段4：主动学习 | 与同人工分钟数的随机扩样作等预算对照，并记录人工成本 | 仅在视觉质量门和信度通过后启动 | 检验主动学习是否真正节省人工；不触碰锁定测试集 |
| 阶段5：模型与统计验证 | 在锁定测试集报告逐标签Precision/Recall/F1、PR-AUC、校准、子群表现和置信区间；达到发布门后才执行预设统计 | 同样执行锁定测试与选择性复核 | 决定逐字段自动化级别并回答预设研究问题 |

共同校准材料可用于改规则，不能进入正式信度估计；盲试标不得边标边改。只增加不改变边界的锚点例句时，记录决定并复核试标样本；定义、值域、单位或纳入/排除规则实质变化时，须升版、重编码受影响字段并用新样本重新验证。正式编码后不得临时增加类目。

“500篇文本”仅为初始人工预算锚点，不等于全部双人金标；充分性由逐标签阳性数、学习曲线、置信区间和人工分钟数决定。

文本多标签信度对每个原子二值标签分别计算Krippendorff's alpha（名义尺度）；两位编码员时可同时报告Cohen's kappa作为敏感性结果。集合级一致性可补充报告MASI距离版本的alpha。目标为`alpha >= 0.80`；同时检查类别样本数、阳性/阴性一致率、`UNK/NA`比例和分歧类型。达到目标且无系统性边界分歧时不追加字段专门规则；`0.667 <= alpha < 0.80`只作暂定使用并须复查，低于0.667的标签不得进入模型训练。

AI可以预测已冻结字段、检索相似错例或提出候选问题，但不能新增类目、替代人工裁决、把聚类命名为研究发现或输出正式主题。V3父类和子类、V6适用性/父类/子类/其他项都须逐字段输出；V11在视觉人口、专门规则和独立信度冻结前不得训练或发布，冻结后也不得用整图情感分数替代10个原子字段。

---

## 九、候选研究问题与假设

研究问题只由冻结的结构化字段及其预设聚合回答，不另设主题生成问题。所有角色比较还须通过V0数据、信度与KOL/KOC组别支持门。H1、H2和H5可在文本主轨与V0角色门均通过后评估；H3、H4、H6、H7和H8当前为`DEFERRED`。

```
H1: 经V0派生并接受的纯型KOL与纯型KOC在资源调用轮廓上存在显著差异（Layer 1: RS_profile; Layer 2: IMP）
H2: 经V0派生并接受的纯型KOL与纯型KOC在内容策略轮廓上存在显著差异
H3: 经V0派生并接受的纯型KOL与纯型KOC在策略→资源关联矩阵（SRM_L1/L2）上存在结构性差异
H4: 经V0派生并接受的纯型KOL与纯型KOC在资源→语言功能共现矩阵（SAM_L1/L2）及目的地属性→情感方向关联矩阵（AAM）上存在结构性差异
H5: 经V0派生并接受的纯型KOL与纯型KOC在互动信号轮廓（ISP）上存在显著差异
H6: 文字资源与视觉资源的匹配程度在经V0派生并接受的纯型KOL与纯型KOC间存在差异
H7: 明确商业披露状态（V1）调节策略→资源关联关系
H8: 平台情境与reach_tier下的纯型KOL/KOC比较结果存在异质性
```

---

## 参考文献

1. Akram, S., & Majeed, R. (2026). Influencer marketing in the tourism industry. *Journal of Rural Tourism*.
2. Baloglu, S., & McCleary, K. W. (1999). A model of destination image formation. *Annals of Tourism Research*, 26(4), 868-897.
3. GB/T 18972-2017. 旅游资源分类、调查与评价.
4. Guerreiro, M., et al. (2024). The online destination image as portrayed by UGC on social media. *Tourism & Management Studies*, 20(4), 1-15.
5. 潘莉, 高雅雯, 李辉. (2026). 旅游图片大数据研究进展. *旅游学刊*, 41(6), 141-155.
6. 郑羽蘅, 赵磊. (2023). 跨文化视角下生态旅游地视觉表征与社会构建. *旅游学刊*, 38(1), 96-108.
7. 黄颖华, 黄福才. (2007). 旅游者感知价值模型、测度与实证研究. *旅游学刊*, 22(8).
8. Hsieh, H.-F., & Shannon, S. E. (2005). Three approaches to qualitative content analysis. *Qualitative Health Research*, 15(9), 1277-1288. https://doi.org/10.1177/1049732305276687
9. Krippendorff, K. (2018). *Content analysis: An introduction to its methodology* (4th ed.). SAGE.
10. Mayring, P. (2000). Qualitative content analysis. *Forum Qualitative Sozialforschung / Forum: Qualitative Social Research*, 1(2), Art. 20.
11. Neuendorf, K. A. (2017). *The content analysis guidebook* (2nd ed.). SAGE. https://doi.org/10.4135/9781071873045

---

## 文档迭代记录

| 内部版本 | 日期 | 变更 |
|----------|------|------|
| v3.5.0-alignment.1 | 2026-08-13 | 明确理论导向结构化内容分析的唯一方法定位，并加入问题登记—修订闭环 |
| v3.5.1-alignment.1 | 2026-08-14 | 将V6同步为“明确商业披露”二元字段 |
| v3.6.0-alignment.1 | 2026-08-14 | 同步一般状态、逐主观字段置信度、低置信备注门和统一文本证据跨度契约；采用稳定文件名 |
| v3.6.1-alignment.1 | 2026-08-15 | 同步编码员单表、条件备注、自动offset和负责人问题/决策模板 |
| v3.6.1-alignment.2 | 2026-08-21 | 同步v3.9.0的V16可见对象状态摘要与Excel字段；暂标NEEDS_UPDATE |
| v3.9.0-alignment.1 | 2026-08-21 | 全面承接V1父子结构、原V5并入CS-INT、V4独立属性体系、评价原子切分、V16及Excel内部模板2.2；恢复CURRENT_ALIGNED |
| v3.9.0-alignment.2 | 2026-08-21 | Excel内部模板升至2.3，补齐帖子级8个与图像级6个上级分类字段及9个相邻置信度列；编码表仍为v3.9.0 |
| v3.10.0-alignment.1 | 2026-08-21 | 同步V2三级结构：休闲游憩与节事文体事件并列，事件细分为体育/演艺/节庆会展/其他；Excel模板升至2.4，`rs_r_act`转为分析汇总 |
| v3.10.0-alignment.2 | 2026-08-21 | 补齐V2新增结构的分析汇总口径：Layer 2扩至10项、事件子类单列报告，`rs_depth`与相关矩阵不重复计入Layer 3或`rs_r_act`汇总 |
| v3.11.0-alignment.1 | 2026-08-22 | 按作者/帖子、文本、图像顺序将现行大类连续编号为V0—V11；字段、值域和观察单位不变；Excel模板升至2.5 |
| v3.12.0-alignment.1 | 2026-08-24 | 同步V0双轴作者设计：中性触达规模、作者快照、受限linkage、证据manifest、T0/T1隔离、封闭criterion codes、组件裁决、版本化角色派生与作者级信度；V1—V11及内容Excel模板2.5不变 |
| v3.13.0-alignment.1 | 2026-08-24 | 启动同轮双任务共同校准；研究总体固定为个人旅游UGC创作者；`actor_scope`替代`account_type`；CI固定UNAVAILABLE并退出角色矩阵；角色改为KOL_TYPE/KOC_TYPE等试点值；冻结25+25共同校准和50名新作者盲试标路径 |
| v3.13.0-alignment.2 | 2026-08-26 | 完成docs全量对齐；修正文本切分摘要，使逗号一般不切与属性指向/情感方向变化时强制拆分、同属性同方向不拆分同时成立 |

---

*编码簿v3.13.0-alignment.2 | 2026年8月26日 | 对齐固定路径`docs/data-dictionary/编码表.md` v3.13.0；Excel固定文件内部模板版本为2.5且仅承载内容任务。本文件不具备反向覆盖权。*
