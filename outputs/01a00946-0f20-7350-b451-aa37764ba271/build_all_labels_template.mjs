import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const workspace = "/Users/a123/Desktop/david/tourism-ugc-study";
const outputDir = path.join(workspace, "outputs/01a00946-0f20-7350-b451-aa37764ba271");
const outputPath = path.join(outputDir, "全标签人工编码模板.xlsx");
const repositoryPath = path.join(workspace, "data/annotations/templates/all-label-manual-coding.xlsx");
const previewDir = path.join(outputDir, "previews-all-labels-v1");
const DATA_ROWS = 300;
const CODEBOOK_VERSION = "v3.6.1";
const TEMPLATE_VERSION = "all-label-manual-coding-v1.0";

await fs.mkdir(previewDir, { recursive: true });
await fs.mkdir(path.dirname(repositoryPath), { recursive: true });

const workbook = await Workbook.create();
const colors = {
  navy: "#183B56", navy2: "#244E67", teal: "#0F766E", tealLight: "#E7F4F1",
  amber: "#B7791F", amberLight: "#FFF5D6", blue: "#2F6B8A", blueLight: "#EAF3F8",
  purple: "#6B4E8A", purpleLight: "#F1ECF7", gray: "#5C6770", grayLight: "#F2F4F5",
  grid: "#D8DEE3", red: "#B42318", redLight: "#FDECEA", green: "#287D3C",
  greenLight: "#E7F5E9", white: "#FFFFFF", ink: "#1F2933",
};

function styleTitle(range) {
  range.format = { fill: colors.navy, font: { bold: true, color: colors.white, size: 18 }, verticalAlignment: "center" };
  range.format.rowHeightPx = 44;
}
function styleSection(range, fill = colors.teal) {
  range.format = { fill, font: { bold: true, color: colors.white, size: 11 }, verticalAlignment: "center", wrapText: true };
  range.format.rowHeightPx = 28;
}
function styleHeader(range, fill) {
  range.format = { fill, font: { bold: true, color: colors.white, size: 10 }, horizontalAlignment: "center", verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
  range.format.rowHeightPx = 60;
}
function styleBody(range) {
  range.format = { font: { color: colors.ink, size: 10 }, verticalAlignment: "top", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
}
function applyList(range, values) {
  range.dataValidation = { rule: { type: "list", values } };
}
function applyIndicatorFormatting(range) {
  range.conditionalFormats.add("cellIs", { operator: "equal", formula: 1, format: { fill: colors.greenLight, font: { color: colors.green, bold: true } } });
  range.conditionalFormats.add("containsText", { text: "UNRESOLVED", format: { fill: colors.redLight, font: { color: colors.red, bold: true } } });
}
function applyConfidenceFormatting(range) {
  range.conditionalFormats.add("cellIs", { operator: "lessThanOrEqual", formula: 2, format: { fill: colors.redLight, font: { color: colors.red, bold: true } } });
  range.conditionalFormats.add("cellIs", { operator: "equal", formula: 3, format: { fill: colors.amberLight, font: { color: "#7A4B00", bold: true } } });
  range.conditionalFormats.add("cellIs", { operator: "greaterThanOrEqual", formula: 4, format: { fill: colors.greenLight, font: { color: colors.green, bold: true } } });
}
function tag(track, unit, dimension, groupZh, parentField, code, labelZh, field, definition, condition = "", confidenceField = "", confidenceZh = "") {
  return { track, unit, dimension, groupZh, parentField, code, labelZh, field, definition, condition, confidenceField, confidenceZh };
}

const postLabels = [
  tag("帖子", "每帖一行", "V0.1", "创作者层级", "author_tier", "H-KOL", "头部KOL", "author_tier_h_kol", "粉丝量不少于100万。", "按绝对粉丝阈值判断。", "author_tier_confidence", "创作者层级置信度"),
  tag("帖子", "每帖一行", "V0.1", "创作者层级", "author_tier", "W-KOL", "腰部KOL", "author_tier_w_kol", "粉丝量10万至不足100万。", "按绝对粉丝阈值判断。", "author_tier_confidence", "创作者层级置信度"),
  tag("帖子", "每帖一行", "V0.1", "创作者层级", "author_tier", "T-KOL", "长尾KOL", "author_tier_t_kol", "粉丝量1万至不足10万。", "按绝对粉丝阈值判断。", "author_tier_confidence", "创作者层级置信度"),
  tag("帖子", "每帖一行", "V0.1", "创作者层级", "author_tier", "KOC", "消费者型创作者", "author_tier_koc", "粉丝量不足1万。", "按绝对粉丝阈值判断。", "author_tier_confidence", "创作者层级置信度"),
  ...[
    ["TRAVEL", "旅游垂直", "content_vertical_travel"], ["LIFESTYLE", "生活方式", "content_vertical_lifestyle"],
    ["FOOD", "美食垂直", "content_vertical_food"], ["GENERAL", "综合内容", "content_vertical_general"],
    ["OTHER", "其他垂直度", "content_vertical_other"],
  ].map(([code, labelZh, field]) => tag("帖子", "每帖一行", "V0.2", "内容垂直度", "content_vertical", code, labelZh, field, "创作者账号内容垂直度类别。", "依据账号长期内容结构判断。", "content_vertical_confidence", "内容垂直度置信度")),
  tag("帖子", "每帖一行", "V6", "明确商业披露", "has_commercial_disclosure", "0", "未观察到明确商业披露", "has_commercial_disclosure_0", "未出现广告、赞助、合作、受邀体验或推广声明。", "不证明不存在商业关系。", "has_commercial_disclosure_confidence", "商业披露置信度"),
  tag("帖子", "每帖一行", "V6", "明确商业披露", "has_commercial_disclosure", "1", "观察到明确商业披露", "has_commercial_disclosure_1", "出现广告、合作、赞助、受邀体验、推广或语义等价声明。", "不得仅凭正面语气推断。", "has_commercial_disclosure_confidence", "商业披露置信度"),
  ...[
    ["B站", "B站", "platform_bilibili"], ["携程", "携程", "platform_ctrip"], ["抖音", "抖音", "platform_douyin"],
    ["微博", "微博", "platform_weibo"], ["小红书", "小红书", "platform_xiaohongshu"],
  ].map(([code, labelZh, field]) => tag("帖子", "每帖一行", "V7", "来源平台", "platform", code, labelZh, field, "帖子来源平台。")),
  ...[
    ["TEXT_ONLY", "纯文本", "media_type_text_only"], ["IMAGE_TEXT", "图文", "media_type_image_text"],
    ["VIDEO_TEXT", "视频文本", "media_type_video_text"], ["LIVE", "直播", "media_type_live"],
  ].map(([code, labelZh, field]) => tag("帖子", "每帖一行", "V7", "媒体形态", "media_type", code, labelZh, field, "帖子媒体形态。")),
  tag("帖子", "每帖一行", "V7", "表情符号", "has_emoji", "0", "不含emoji", "has_emoji_0", "帖文不含表情包或emoji。"),
  tag("帖子", "每帖一行", "V7", "表情符号", "has_emoji", "1", "含emoji", "has_emoji_1", "帖文含表情包或emoji。"),
  tag("帖子", "每帖一行", "V7", "话题标签", "has_hashtag", "0", "不含话题标签", "has_hashtag_0", "帖文不含#话题标签。"),
  tag("帖子", "每帖一行", "V7", "话题标签", "has_hashtag", "1", "含话题标签", "has_hashtag_1", "帖文含#话题标签。"),
  ...[
    ["NATURE", "自然型目的地", "destination_type_nature"], ["CULTURE", "文化型目的地", "destination_type_culture"],
    ["CITY", "城市型目的地", "destination_type_city"], ["RESORT", "度假型目的地", "destination_type_resort"],
    ["MIXED", "混合型目的地", "destination_type_mixed"],
  ].map(([code, labelZh, field]) => tag("帖子", "每帖一行", "V7", "目的地类型", "destination_type", code, labelZh, field, "帖子主要涉及的目的地类型。", "校准阶段不强制互斥。", "destination_type_confidence", "目的地类型置信度")),
];

const textLabels = [];
const addBinaryText = (dimension, groupZh, parentField, code, labelZh, field, definition, condition = "") => {
  textLabels.push(tag("文本", "每个seg_id一行", dimension, groupZh, parentField, code, labelZh, field, definition, condition, `${field}_confidence`, `${labelZh}置信度`));
};
[
  ["CS-INF", "信息提供型", "cs_inf", "提供事实、数据、攻略、路线或步骤。"],
  ["CS-EDU", "知识阐释型", "cs_edu", "解释文化、历史、地理、非遗或地方知识。"],
  ["CS-EMO", "情感渲染型", "cs_emo", "以感官描写或氛围语言营造体验。"],
  ["CS-INT", "互动激发型", "cs_int", "以提问、号召或社交触发推动参与。"],
  ["CS-OTH", "其他/未命中策略", "cs_oth", "未命中上述四种内容策略。"],
].forEach(([code, labelZh, field, definition]) => addBinaryText("V1", "内容策略", field, code, labelZh, field, definition));
[
  ["RS-N", "自然旅游资源", "rs_n", "片段调用自然旅游资源大类。"],
  ["RS-R", "人文旅游资源", "rs_r", "片段调用人文旅游资源大类。"],
  ["RS-N-GEO", "地文景观", "rs_n_geo", "山岳、岩石、地貌、洞穴或沙滩。"],
  ["RS-N-WAT", "水域风光", "rs_n_wat", "海洋、河流、湖泊、瀑布或泉水。"],
  ["RS-N-BIO", "生物景观", "rs_n_bio", "动物、植物、森林或花海。"],
  ["RS-N-CLI", "天象气候", "rs_n_cli", "日出、日落、星空、云海或雪景。"],
  ["RS-R-HIS", "遗址遗迹", "rs_r_his", "以历史或遗产身份被提及的遗址、古建筑或文物。"],
  ["RS-R-ARC", "建筑设施", "rs_r_arc", "以当代使用功能被提及的建筑、住宿、交通或设施。"],
  ["RS-R-GAS", "饮食文化", "rs_r_gas", "地方美食、特色饮品或餐饮体验。"],
  ["RS-R-FOL", "民俗风情", "rs_r_fol", "非遗、民俗、节庆或宗教文化。"],
  ["RS-R-ACT", "旅游活动", "rs_r_act", "户外运动、休闲体验或互动参与。"],
].forEach(([code, labelZh, field, definition]) => addBinaryText(code.startsWith("RS-N-") || code.startsWith("RS-R-") ? "V2-L2" : "V2-L1", "目的地资源", field, code, labelZh, field, definition));
[
  ["AT-INF", "信息性陈述", "at_has_info", "包含至少一个可核验事实命题。"],
  ["AT-EVL", "评价性表达", "at_has_eval", "对目的地、设施或体验作明确价值判断。"],
  ["AT-SUG", "建议/推荐", "at_has_sug", "向受众提出行动建议、推荐或回避。"],
  ["AT-NON", "其他/未命中语言功能", "at_non", "未命中信息、评价和建议。"],
].forEach(([code, labelZh, field, definition]) => addBinaryText("V3-L1", "语言功能", field, code, labelZh, field, definition));
[
  ["AT-EVL-ADM", "赞赏/惊叹", "at_eval_subtype_adm", "高唤醒正面评价。"],
  ["AT-EVL-SAT", "满意/舒适", "at_eval_subtype_sat", "低唤醒正面评价。"],
  ["AT-EVL-EXC", "兴奋/激动", "at_eval_subtype_exc", "高唤醒正面兴奋。"],
  ["AT-EVL-DIS", "失望/不满", "at_eval_subtype_dis", "低唤醒负面评价。"],
  ["AT-EVL-FRU", "沮丧/愤怒", "at_eval_subtype_fru", "高唤醒负面评价。"],
  ["AT-EVL-NOS", "怀旧/感慨", "at_eval_subtype_nos", "混合愉悦度的怀旧或感慨。"],
  ["NA", "评价亚类不适用", "at_eval_subtype_na", "片段不含评价时使用。"],
].forEach(([code, labelZh, field, definition]) => textLabels.push(tag("文本", "每个seg_id一行", "V3-L1", "评价亚类", "at_eval_subtype", code, labelZh, field, definition, "at_has_eval=0时NA；亚类基数待T0冻结。", "at_eval_subtype_confidence", "评价亚类置信度")));
[
  ["0", "情感方向不可判定", "sentiment_judgeable_0", "不含评价/建议，或方向无法从文本判断。"],
  ["1", "情感方向可判定", "sentiment_judgeable_1", "评价/建议的情感方向可从文本判断。"],
].forEach(([code, labelZh, field, definition]) => textLabels.push(tag("文本", "每个seg_id一行", "V3-L2", "情感可判定", "sentiment_judgeable", code, labelZh, field, definition, "校准阶段逐列记录。", "sentiment_judgeable_confidence", "情感可判定置信度")));
[
  ["POS", "正面情感", "sentiment_dir_pos", "评价或建议倾向明确为正面。"],
  ["NEG", "负面情感", "sentiment_dir_neg", "评价或建议倾向明确为负面。"],
  ["MIX", "混合/矛盾情感", "sentiment_dir_mix", "同一片段兼有正面与负面评价。"],
  ["NEU", "中性情感", "sentiment_dir_neu", "含评价或建议且立场真实中性。"],
  ["NA", "情感方向不适用", "sentiment_dir_na", "情感方向不可判定时使用。"],
].forEach(([code, labelZh, field, definition]) => textLabels.push(tag("文本", "每个seg_id一行", "V3-L2", "情感方向", "sentiment_dir", code, labelZh, field, definition, "与sentiment_judgeable和强度字段联合复核。", "sentiment_dir_confidence", "情感方向置信度")));
[
  ["ASP-RES", "资源/吸引物品质", "asp_res", "对旅游资源品质或吸引力作评价/建议。"],
  ["ASP-PRI", "价格与性价比", "asp_pri", "对价格或性价比作评价/建议。"],
  ["ASP-SER", "服务与设施", "asp_ser", "对服务、接待或设施质量作评价/建议。"],
  ["ASP-CRO", "拥挤与管理秩序", "asp_cro", "对客流、排队、秩序或管理作评价/建议。"],
  ["ASP-ACC", "交通与可达性", "asp_acc", "对到达、换乘、停车或步行距离作评价/建议。"],
  ["ASP-HYG", "卫生与安全", "asp_hyg", "对卫生、食品安全或风险保障作评价/建议。"],
].forEach(([code, labelZh, field, definition]) => addBinaryText("V3-L3", "目的地属性指向", field, code, labelZh, field, definition, "无评价或建议时记0。"));
[
  ["IS-QUE", "提问", "is_que", "以问句形式出现。"], ["IS-DIR", "直接号召", "is_dir", "直接号召受众采取行动。"],
  ["IS-SOC", "社交触发", "is_soc", "触发分享、认同或归属。"], ["IS-NON", "无互动信号", "is_non", "未命中三类互动信号。"],
].forEach(([code, labelZh, field, definition]) => addBinaryText("V5", "互动信号", field, code, labelZh, field, definition));

const visualLabels = [
  ...[
    ["VS-NAT", "自然景观", "visual_subject_vs_nat", "自然地貌、水体、天空或植被是主要视觉内容。"],
    ["VS-ARC", "建筑遗产", "visual_subject_vs_arc", "建筑、古迹、街区或地标是主要视觉内容。"],
    ["VS-PEO", "人物活动", "visual_subject_vs_peo", "人物及其活动是主要视觉内容。"],
    ["VS-FOD", "美食/物产", "visual_subject_vs_fod", "食物、饮品或地方物产是主要视觉内容。"],
    ["VS-FAC", "服务设施", "visual_subject_vs_fac", "旅游服务设施或交通设施是主要视觉内容。"],
  ].map(([code, labelZh, field, definition]) => tag("图像", "每个img_id一行", "V11", "视觉主体", "visual_subject", code, labelZh, field, definition, "校准阶段不强制互斥。", "visual_subject_confidence", "视觉主体置信度")),
  ...[
    ["SC-ICO", "标志性场景", "scene_context_sc_ico", "目的地地标或代表性景点。", "scene_context", "场景语境置信度"],
    ["SC-STR", "日常场景", "scene_context_sc_str", "普通街道、市场、社区或市井生活。", "scene_context", "场景语境置信度"],
    ["SC-OTH", "其他/不适用场景", "scene_context_sc_oth", "无法归入标志性或日常场景。", "scene_context", "场景语境置信度"],
    ["SS-CLO", "特写", "shot_scale_ss_clo", "聚焦人物、食物或物体局部细节。", "shot_scale", "景别置信度"],
    ["SS-MID", "中景", "shot_scale_ss_mid", "主体及周边环境均清晰可见。", "shot_scale", "景别置信度"],
    ["SS-PAN", "全景", "shot_scale_ss_pan", "开阔场景或大范围环境占主体。", "shot_scale", "景别置信度"],
    ["VP-GRD", "平视/常规视角", "viewpoint_vp_grd", "接近普通观看高度。", "viewpoint", "视角置信度"],
    ["VP-HIG", "高位俯视", "viewpoint_vp_hig", "从高处俯看但无明确航拍特征。", "viewpoint", "视角置信度"],
    ["VP-AER", "航拍", "viewpoint_vp_aer", "具有无人机或显著高空俯瞰特征。", "viewpoint", "视角置信度"],
  ].map(([code, labelZh, field, definition, parentField, confidenceZh]) => tag("图像", "每个img_id一行", "V12", parentField === "scene_context" ? "场景语境" : parentField === "shot_scale" ? "景别" : "视角", parentField, code, labelZh, field, definition, "校准阶段逐标签记录。", `${parentField}_confidence`, confidenceZh)),
  ...[
    ["PC-NON", "无人", "people_count_pc_non", "画面中没有可识别人物。"],
    ["PC-SIN", "单人", "people_count_pc_sin", "画面中仅出现一名可识别人物。"],
    ["PC-GRP", "群体", "people_count_pc_grp", "画面中出现两人及以上。"],
  ].map(([code, labelZh, field, definition]) => tag("图像", "每个img_id一行", "V14", "人物数量", "people_count", code, labelZh, field, definition, "校准阶段逐标签记录。", "people_count_confidence", "人物数量置信度")),
  tag("图像", "每个img_id一行", "V14", "人景互动", "human_scene_interaction", "0", "无互动", "human_scene_interaction_0", "人物未与目的地元素发生明确互动。", "无人时通常成立。", "human_scene_interaction_confidence", "人景互动置信度"),
  tag("图像", "每个img_id一行", "V14", "人景互动", "human_scene_interaction", "1", "有互动", "human_scene_interaction_1", "人物与目的地元素发生明确接触、操作或面向性交互。", "边界行为需校准。", "human_scene_interaction_confidence", "人景互动置信度"),
  ...[
    ["CS-BRA", "品牌标识存在", "cs_brand", "画面中可见可识别的品牌Logo或名称。"],
    ["CS-ACC", "住宿设施", "cs_accom", "画面中可见具体住宿场所名称或设施。"],
    ["CS-RES", "餐饮场所", "cs_rest", "画面中可见具体餐厅或店铺。"],
    ["CS-TRA", "交通工具", "cs_trans", "画面中可见具体交通工具品牌或工具。"],
  ].map(([code, labelZh, field, definition]) => tag("图像", "每个img_id一行", "V15", "消费符号", field, code, labelZh, field, definition, "四项可共现。", `${field}_confidence`, `${labelZh}置信度`)),
];

const textScales = [
  { track: "文本", unit: "每个seg_id一行", dimension: "V3-L2", groupZh: "情感强度", parentField: "sentiment_pos_val", code: "0/1/2/NA", labelZh: "正向情感强度", field: "sentiment_pos_val", values: [0, 1, 2, "NA", "UNRESOLVED"], definition: "0无正面、1一般正面、2强正面；不可判定时NA。", condition: "人工量表字段，不拆成标签列。", confidenceField: "sentiment_pos_val_confidence", confidenceZh: "正向情感强度置信度" },
  { track: "文本", unit: "每个seg_id一行", dimension: "V3-L2", groupZh: "情感强度", parentField: "sentiment_neg_val", code: "0/1/2/NA", labelZh: "负向情感强度", field: "sentiment_neg_val", values: [0, 1, 2, "NA", "UNRESOLVED"], definition: "0无负面、1一般负面、2强负面；不可判定时NA。", condition: "人工量表字段，不拆成标签列。", confidenceField: "sentiment_neg_val_confidence", confidenceZh: "负向情感强度置信度" },
];

const allLabels = [...postLabels, ...textLabels, ...visualLabels];
const allScales = [...textScales];
function uniqueConfidences(labels, scales = []) {
  const result = [];
  const seen = new Set();
  for (const item of [...labels, ...scales]) {
    if (item.confidenceField && !seen.has(item.confidenceField)) {
      seen.add(item.confidenceField);
      result.push({ field: item.confidenceField, zh: item.confidenceZh });
    }
  }
  return result;
}

const guide = workbook.worksheets.add("填写说明");
guide.showGridLines = false;
guide.mergeCells("A1:F1");
guide.getRange("A1").values = [["全标签逐列人工编码模板"]];
styleTitle(guide.getRange("A1:F1"));
guide.mergeCells("A2:F2");
guide.getRange("A2").values = [[`编码表 ${CODEBOOK_VERSION}｜模板 ${TEMPLATE_VERSION}｜帖子、文本片段、单图分表`]];
guide.getRange("A2:F2").format = { fill: colors.blueLight, font: { bold: true, color: colors.navy2 }, verticalAlignment: "center" };
guide.mergeCells("A4:F4");
guide.getRange("A4").values = [["模板覆盖范围"]];
styleSection(guide.getRange("A4:F4"), colors.amber);
guide.getRange("A5:B8").values = [
  ["帖子标注", `${postLabels.length}个标签列；每帖一行，同时记录粉丝数、文本长度和图片数。`],
  ["文本标注", `${textLabels.length}个标签列 + 2个人工情感强度字段；每个固定seg_id一行。`],
  ["图像标注", `${visualLabels.length}个标签列；每张图片独立一行。`],
  ["总计", `${allLabels.length}个候选标签列全部进入人工编码；不在模板中自动派生或覆盖。`],
];
for (let row = 5; row <= 8; row += 1) guide.mergeCells(`B${row}:F${row}`);
styleBody(guide.getRange("A5:F8"));
guide.getRange("A5:A8").format = { fill: colors.amberLight, font: { bold: true, color: "#7A4B00" }, verticalAlignment: "top", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
guide.mergeCells("A10:F10");
guide.getRange("A10").values = [["统一填写规则"]];
styleSection(guide.getRange("A10:F10"));
guide.getRange("A11:B17").values = [
  ["观察单位分开", "帖子、文本片段和单图不可混为同一行；annotation_id在各表内唯一。"],
  ["标签逐列填写", "每个标签列填1、0或UNRESOLVED；空白表示漏填，不等于0。"],
  ["校准期不强制互斥", "同一原字段的多个候选标签可以同时为1，待基数规则冻结后再派生正式字段。"],
  ["全部人工判断", "即使字段间存在逻辑关系，也不得由程序替编码员生成或覆盖标签值。"],
  ["逐字段置信度", "主观字段填1—5；置信度1—2或UNRESOLVED时必须登记低置信备注。"],
  ["文本证据", "V1/V2/V3/V5实质性阳性判断在“文本证据”中记录最短充分原文。"],
  ["CSV导出", "各主表、低置信备注和文本证据分别导出UTF-8 CSV；不得改列头或手写JSON。"],
];
for (let row = 11; row <= 17; row += 1) guide.mergeCells(`B${row}:F${row}`);
styleBody(guide.getRange("A11:F17"));
guide.getRange("A11:A17").format = { fill: colors.tealLight, font: { bold: true, color: colors.teal }, verticalAlignment: "top", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
guide.mergeCells("A19:F19");
guide.getRange("A19").values = [["状态与版本"]];
styleSection(guide.getRange("A19:F19"), colors.gray);
guide.mergeCells("A20:F23");
guide.getRange("A20").values = [["标签列：1=标签成立，0=标签不成立，UNRESOLVED=当前证据不足。情感强度字段允许0/1/2/NA/UNRESOLVED。编码时间使用ISO 8601。编码表版本固定为v3.6.1，模板版本固定为all-label-manual-coding-v1.0。当前模板适合共同校准；正式盲标前仍须关闭编码表T0。"]];
styleBody(guide.getRange("A20:F23"));
guide.getRange("A20:F23").format.fill = colors.grayLight;
guide.getRange("A1:A23").format.columnWidthPx = 130;
guide.getRange("B1:F23").format.columnWidthPx = 152;
guide.freezePanes.freezeRows(2);

const trailingHeaders = [["编码员标识", "coder"], ["编码时间", "annotated_at"], ["编码表版本", "codebook_version"], ["模板版本", "template_schema_version"]];

function createCodingSheet(name, metadata, labels, scales, trackFill, trackLight) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const confidences = uniqueConfidences(labels, scales);
  const headers = metadata.map((item) => `${item.zh}（${item.field}）`);
  const labelStart = headers.length;
  headers.push(...labels.map((item) => `${item.labelZh}（${item.field}）`));
  const scaleStart = headers.length;
  headers.push(...scales.map((item) => `${item.labelZh}（${item.field}）`));
  const confidenceStart = headers.length;
  headers.push(...confidences.map((item) => `${item.zh}（${item.field}）`));
  const trailingStart = headers.length;
  headers.push(...trailingHeaders.map(([zh, field]) => `${zh}（${field}）`));
  sheet.getRangeByIndexes(0, 0, 1, headers.length).values = [headers];
  styleHeader(sheet.getRangeByIndexes(0, 0, 1, metadata.length), colors.gray);
  if (labels.length) styleHeader(sheet.getRangeByIndexes(0, labelStart, 1, labels.length), trackFill);
  if (scales.length) styleHeader(sheet.getRangeByIndexes(0, scaleStart, 1, scales.length), colors.purple);
  if (confidences.length) styleHeader(sheet.getRangeByIndexes(0, confidenceStart, 1, confidences.length), colors.amber);
  styleHeader(sheet.getRangeByIndexes(0, trailingStart, 1, trailingHeaders.length), colors.gray);
  sheet.getRangeByIndexes(1, 0, DATA_ROWS, headers.length).format = { font: { color: colors.ink, size: 10 }, verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: "#E6EAED" } };
  sheet.getRangeByIndexes(1, 0, DATA_ROWS, metadata.length).format.fill = colors.grayLight;
  for (let index = 0; index < labels.length; index += 1) {
    const range = sheet.getRangeByIndexes(1, labelStart + index, DATA_ROWS, 1);
    range.format.fill = trackLight;
    applyList(range, [0, 1, "UNRESOLVED"]);
    applyIndicatorFormatting(range);
  }
  for (let index = 0; index < scales.length; index += 1) {
    const range = sheet.getRangeByIndexes(1, scaleStart + index, DATA_ROWS, 1);
    range.format.fill = colors.purpleLight;
    applyList(range, scales[index].values);
    range.conditionalFormats.add("containsText", { text: "UNRESOLVED", format: { fill: colors.redLight, font: { color: colors.red, bold: true } } });
  }
  for (let index = 0; index < confidences.length; index += 1) {
    const range = sheet.getRangeByIndexes(1, confidenceStart + index, DATA_ROWS, 1);
    range.format.fill = colors.amberLight;
    range.dataValidation = { rule: { type: "whole", operator: "between", formula1: 1, formula2: 5 } };
    applyConfidenceFormatting(range);
  }
  sheet.getRangeByIndexes(1, trailingStart, DATA_ROWS, trailingHeaders.length).format.fill = colors.grayLight;
  const fieldIndex = new Map(headers.map((header, index) => [header.slice(header.lastIndexOf("（") + 1, -1), index]));
  if (fieldIndex.has("annotation_stage")) applyList(sheet.getRangeByIndexes(1, fieldIndex.get("annotation_stage"), DATA_ROWS, 1), ["CALIBRATION", "BLIND_PILOT", "FORMAL"]);
  if (fieldIndex.has("annotator_slot")) applyList(sheet.getRangeByIndexes(1, fieldIndex.get("annotator_slot"), DATA_ROWS, 1), [1, 2]);
  applyList(sheet.getRangeByIndexes(1, fieldIndex.get("codebook_version"), DATA_ROWS, 1), [CODEBOOK_VERSION]);
  applyList(sheet.getRangeByIndexes(1, fieldIndex.get("template_schema_version"), DATA_ROWS, 1), [TEMPLATE_VERSION]);
  for (const item of metadata) {
    if (item.type !== "number") sheet.getRangeByIndexes(1, fieldIndex.get(item.field), DATA_ROWS, 1).format.numberFormat = "@";
  }
  sheet.getRangeByIndexes(1, trailingStart, DATA_ROWS, trailingHeaders.length).format.numberFormat = "@";
  for (let index = 0; index < headers.length; index += 1) {
    let width = 138;
    if (index < metadata.length) width = metadata[index].width ?? 125;
    else if (index >= labelStart && index < scaleStart) width = 150;
    else if (index >= scaleStart && index < confidenceStart) width = 145;
    else if (index >= confidenceStart && index < trailingStart) width = 142;
    else if (index === trailingStart + 1) width = 170;
    else if (index === trailingStart + 3) width = 180;
    sheet.getRangeByIndexes(0, index, DATA_ROWS + 1, 1).format.columnWidthPx = width;
  }
  sheet.getRangeByIndexes(1, 0, DATA_ROWS, headers.length).format.rowHeightPx = name === "文本标注" ? 42 : 24;
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(Math.min(metadata.length, 9));
  return { sheet, headers, labelCount: labels.length, confidenceCount: confidences.length };
}

const postSheetInfo = createCodingSheet("帖子标注", [
  { zh: "标注记录编号", field: "annotation_id", width: 150 }, { zh: "标注轮次", field: "round_id", width: 115 },
  { zh: "标注阶段", field: "annotation_stage", width: 125 }, { zh: "帖子编号", field: "post_id", width: 125 },
  { zh: "帖子地址", field: "post_url", width: 220 }, { zh: "创作者编号", field: "author_id", width: 125 },
  { zh: "粉丝数", field: "follower_count", width: 105, type: "number" }, { zh: "帖子字符数", field: "text_length", width: 110, type: "number" },
  { zh: "图片数量", field: "image_count", width: 100, type: "number" }, { zh: "盲标槽位", field: "annotator_slot", width: 105 },
], postLabels, [], colors.blue, colors.blueLight);

const textSheetInfo = createCodingSheet("文本标注", [
  { zh: "标注记录编号", field: "annotation_id", width: 150 }, { zh: "标注轮次", field: "round_id", width: 115 },
  { zh: "标注阶段", field: "annotation_stage", width: 125 }, { zh: "帖子编号", field: "post_id", width: 125 },
  { zh: "片段序号", field: "seg_id", width: 95 }, { zh: "原始文本", field: "raw_text", width: 340 },
  { zh: "片段字符数", field: "seg_length", width: 110, type: "number" }, { zh: "盲标槽位", field: "annotator_slot", width: 105 },
], textLabels, textScales, colors.teal, colors.tealLight);

const visualSheetInfo = createCodingSheet("图像标注", [
  { zh: "标注记录编号", field: "annotation_id", width: 150 }, { zh: "标注轮次", field: "round_id", width: 115 },
  { zh: "标注阶段", field: "annotation_stage", width: 125 }, { zh: "帖子编号", field: "post_id", width: 125 },
  { zh: "图片序号", field: "img_id", width: 95 }, { zh: "平台", field: "platform", width: 100 },
  { zh: "图片本地路径", field: "image_path", width: 230 }, { zh: "图片原始地址", field: "img_url", width: 230 },
  { zh: "盲标槽位", field: "annotator_slot", width: 105 },
], visualLabels, [], colors.purple, colors.purpleLight);

const lowConfidence = workbook.worksheets.add("低置信备注");
lowConfidence.showGridLines = false;
const noteHeaders = ["低置信记录编号（note_id）", "观察单位（unit_type）", "标注记录编号（annotation_id）", "标注轮次（round_id）", "帖子编号（post_id）", "片段序号（seg_id）", "图片序号（img_id）", "低置信字段（field_name）", "涉及标签列（label_field_names）", "置信度（confidence）", "原因代码（reason_code）", "备选判断（alternative_values）", "简短说明（low_confidence_note）", "编码员标识（coder）", "编码时间（annotated_at）", "编码表版本（codebook_version）", "模板版本（template_schema_version）"];
lowConfidence.getRange("A1:Q1").values = [noteHeaders];
styleHeader(lowConfidence.getRange("A1:G1"), colors.gray);
styleHeader(lowConfidence.getRange("H1:M1"), colors.amber);
styleHeader(lowConfidence.getRange("N1:Q1"), colors.gray);
lowConfidence.getRange(`A2:Q${DATA_ROWS + 1}`).format = { font: { color: colors.ink, size: 10 }, verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: "#E6EAED" } };
lowConfidence.getRange(`A2:G${DATA_ROWS + 1}`).format.fill = colors.grayLight;
lowConfidence.getRange(`H2:M${DATA_ROWS + 1}`).format.fill = colors.amberLight;
lowConfidence.getRange(`N2:Q${DATA_ROWS + 1}`).format.fill = colors.grayLight;
applyList(lowConfidence.getRange(`B2:B${DATA_ROWS + 1}`), ["POST", "SEGMENT", "IMAGE"]);
applyList(lowConfidence.getRange(`J2:J${DATA_ROWS + 1}`), [1, 2]);
applyList(lowConfidence.getRange(`K2:K${DATA_ROWS + 1}`), ["BOUNDARY", "CONTEXT", "CONFLICT", "EVIDENCE_MISSING", "OTHER"]);
applyList(lowConfidence.getRange(`P2:P${DATA_ROWS + 1}`), [CODEBOOK_VERSION]);
applyList(lowConfidence.getRange(`Q2:Q${DATA_ROWS + 1}`), [TEMPLATE_VERSION]);
applyConfidenceFormatting(lowConfidence.getRange(`J2:J${DATA_ROWS + 1}`));
lowConfidence.getRange(`A2:I${DATA_ROWS + 1}`).format.numberFormat = "@";
lowConfidence.getRange(`K2:Q${DATA_ROWS + 1}`).format.numberFormat = "@";
const noteWidths = [140, 100, 150, 115, 120, 90, 90, 180, 260, 90, 145, 220, 300, 120, 170, 115, 180];
noteWidths.forEach((width, index) => lowConfidence.getRangeByIndexes(0, index, DATA_ROWS + 1, 1).format.columnWidthPx = width);
lowConfidence.freezePanes.freezeRows(1);
lowConfidence.freezePanes.freezeColumns(7);

const evidence = workbook.worksheets.add("文本证据");
evidence.showGridLines = false;
const evidenceHeaders = ["证据记录编号（evidence_id）", "标注记录编号（annotation_id）", "标注轮次（round_id）", "帖子编号（post_id）", "片段序号（seg_id）", "支持标签列（field_names）", "最短充分原文（evidence_quote）", "重复位置提示（occurrence_hint）", "编码员标识（coder）", "编码时间（annotated_at）", "编码表版本（codebook_version）", "模板版本（template_schema_version）"];
evidence.getRange("A1:L1").values = [evidenceHeaders];
styleHeader(evidence.getRange("A1:E1"), colors.gray);
styleHeader(evidence.getRange("F1:H1"), colors.teal);
styleHeader(evidence.getRange("I1:L1"), colors.gray);
evidence.getRange(`A2:L${DATA_ROWS + 1}`).format = { font: { color: colors.ink, size: 10 }, verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: "#E6EAED" } };
evidence.getRange(`A2:E${DATA_ROWS + 1}`).format.fill = colors.grayLight;
evidence.getRange(`F2:H${DATA_ROWS + 1}`).format.fill = colors.tealLight;
evidence.getRange(`I2:L${DATA_ROWS + 1}`).format.fill = colors.grayLight;
applyList(evidence.getRange(`K2:K${DATA_ROWS + 1}`), [CODEBOOK_VERSION]);
applyList(evidence.getRange(`L2:L${DATA_ROWS + 1}`), [TEMPLATE_VERSION]);
evidence.getRange(`A2:L${DATA_ROWS + 1}`).format.numberFormat = "@";
const evidenceWidths = [145, 150, 115, 120, 90, 280, 360, 170, 120, 170, 115, 180];
evidenceWidths.forEach((width, index) => evidence.getRangeByIndexes(0, index, DATA_ROWS + 1, 1).format.columnWidthPx = width);
evidence.freezePanes.freezeRows(1);
evidence.freezePanes.freezeColumns(5);

const catalog = workbook.worksheets.add("标签总表");
catalog.showGridLines = false;
catalog.mergeCells("A1:K1");
catalog.getRange("A1").values = [["编码表全部人工标签与字段映射"]];
styleTitle(catalog.getRange("A1:K1"));
catalog.mergeCells("A2:K2");
catalog.getRange("A2").values = [[`共${allLabels.length}个标签列和${allScales.length}个人工量表字段；中文标签与机器字段一一对应。`]];
catalog.getRange("A2:K2").format = { fill: colors.blueLight, font: { bold: true, color: colors.navy2 }, verticalAlignment: "center" };
catalog.getRange("A4:K4").values = [["轨道", "观察单位", "维度", "判断组", "原字段", "原代码/值", "中文标签", "人工列字段", "单元格值域", "置信度字段", "定义与条件"]];
styleHeader(catalog.getRange("A4:K4"), colors.navy2);
const labelCatalogRow = (item) => [item.track, item.unit, item.dimension, item.groupZh, item.parentField, item.code, item.labelZh, item.field, "0 / 1 / UNRESOLVED", item.confidenceField || "—", `${item.definition}${item.condition ? ` ${item.condition}` : ""}`];
const scaleCatalogRow = (item) => [item.track, item.unit, item.dimension, item.groupZh, item.parentField, item.code, item.labelZh, item.field, item.values.join(" / "), item.confidenceField, `${item.definition} ${item.condition}`];
const catalogRows = [
  ...postLabels.map(labelCatalogRow),
  ...textLabels.map(labelCatalogRow),
  ...textScales.map(scaleCatalogRow),
  ...visualLabels.map(labelCatalogRow),
];
catalog.getRange(`A5:K${4 + catalogRows.length}`).values = catalogRows;
styleBody(catalog.getRange(`A5:K${4 + catalogRows.length}`));
let start = 5;
for (const [track, count, fill] of [["帖子", postLabels.length, colors.blueLight], ["文本", textLabels.length + textScales.length, colors.tealLight], ["图像", visualLabels.length, colors.purpleLight]]) {
  const end = start + count - 1;
  catalog.getRange(`A${start}:K${end}`).format.fill = fill;
  start = end + 1;
}
const catalogWidths = [75, 115, 85, 145, 190, 110, 170, 230, 165, 210, 420];
catalogWidths.forEach((width, index) => catalog.getRangeByIndexes(0, index, catalogRows.length + 4, 1).format.columnWidthPx = width);
catalog.freezePanes.freezeRows(4);

const options = workbook.worksheets.add("下拉选项");
options.showGridLines = false;
const confidenceFields = uniqueConfidences(allLabels, allScales).map((item) => item.field);
const optionHeaders = ["indicator_value", "intensity_value", "confidence", "reason_code", "unit_type", "confidence_field", "annotation_stage", "annotator_slot"];
options.getRange("A1:H1").values = [optionHeaders];
styleHeader(options.getRange("A1:H1"), colors.navy2);
const optionColumns = [[0, 1, "UNRESOLVED"], [0, 1, 2, "NA", "UNRESOLVED"], [1, 2, 3, 4, 5], ["BOUNDARY", "CONTEXT", "CONFLICT", "EVIDENCE_MISSING", "OTHER"], ["POST", "SEGMENT", "IMAGE"], confidenceFields, ["CALIBRATION", "BLIND_PILOT", "FORMAL"], [1, 2]];
const maxOptionRows = Math.max(...optionColumns.map((column) => column.length));
const optionRows = Array.from({ length: maxOptionRows }, (_, rowIndex) => optionColumns.map((column) => column[rowIndex] ?? ""));
options.getRange(`A2:H${maxOptionRows + 1}`).values = optionRows;
styleBody(options.getRange(`A2:H${maxOptionRows + 1}`));
options.getRange(`A2:H${maxOptionRows + 1}`).format.fill = colors.grayLight;
options.getRange("A1:H1").format.columnWidthPx = 165;
options.getRange(`F1:F${maxOptionRows + 1}`).format.columnWidthPx = 235;
options.freezePanes.freezeRows(1);

const workbookInspection = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 5000 });
const postInspection = await workbook.inspect({ kind: "table", range: "帖子标注!A1:AU3", include: "values,formulas", tableMaxRows: 3, tableMaxCols: postSheetInfo.headers.length });
const textInspection = await workbook.inspect({ kind: "table", range: "文本标注!A1:CO3", include: "values,formulas", tableMaxRows: 3, tableMaxCols: textSheetInfo.headers.length });
const visualInspection = await workbook.inspect({ kind: "table", range: "图像标注!A1:AT3", include: "values,formulas", tableMaxRows: 3, tableMaxCols: visualSheetInfo.headers.length });
const errorScan = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "最终公式错误扫描" });

const renderSpecs = [
  ["填写说明", "A1:F23", "guide.png"], ["帖子标注", "A1:AT10", "post-coding.png"],
  ["文本标注", "A1:CO8", "text-coding.png"], ["图像标注", "A1:AT10", "image-coding.png"],
  ["低置信备注", "A1:Q10", "low-confidence.png"], ["文本证据", "A1:L10", "text-evidence.png"],
  ["标签总表", `A1:K${catalogRows.length + 4}`, "catalog.png"], ["下拉选项", `A1:H${maxOptionRows + 1}`, "options.png"],
];
for (const [sheetName, range, fileName] of renderSpecs) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, fileName), new Uint8Array(await preview.arrayBuffer()));
}

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
await fs.copyFile(outputPath, repositoryPath);
console.log(JSON.stringify({
  outputPath, repositoryPath, previewDir,
  counts: { postLabels: postLabels.length, textLabels: textLabels.length, textScales: textScales.length, visualLabels: visualLabels.length, totalLabels: allLabels.length, totalConfidenceFields: confidenceFields.length },
  sheetColumns: { post: postSheetInfo.headers.length, text: textSheetInfo.headers.length, visual: visualSheetInfo.headers.length },
  workbookInspection: workbookInspection.ndjson,
  postInspection: postInspection.ndjson,
  textInspection: textInspection.ndjson,
  visualInspection: visualInspection.ndjson,
  errorScan: errorScan.ndjson,
}, null, 2));
