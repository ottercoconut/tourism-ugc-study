import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const workspace = "/Users/a123/Desktop/david/tourism-ugc-study";
const outputDir = path.join(workspace, "outputs/01a00946-0f20-7350-b451-aa37764ba271");
const outputPath = path.join(outputDir, "视觉轨人工编码模板.xlsx");
const repositoryPath = path.join(workspace, "data/annotations/templates/visual-coding.xlsx");
const previewDir = path.join(outputDir, "previews-wide-v2");
const DATA_ROWS = 500;

await fs.mkdir(previewDir, { recursive: true });
await fs.mkdir(path.dirname(repositoryPath), { recursive: true });

const workbook = await Workbook.create();
const colors = {
  navy: "#183B56", navy2: "#244E67", teal: "#0F766E", tealLight: "#E7F4F1",
  amber: "#B7791F", amberLight: "#FFF5D6", sky: "#EAF3F8", gray: "#5C6770",
  grayLight: "#F2F4F5", grid: "#D8DEE3", red: "#B42318", redLight: "#FDECEA",
  green: "#287D3C", greenLight: "#E7F5E9", white: "#FFFFFF", ink: "#1F2933",
};

function styleTitle(range) {
  range.format = { fill: colors.navy, font: { bold: true, color: colors.white, size: 18 }, verticalAlignment: "center" };
  range.format.rowHeightPx = 44;
}
function styleSection(range, fill = colors.teal) {
  range.format = { fill, font: { bold: true, color: colors.white, size: 11 }, verticalAlignment: "center", wrapText: true };
  range.format.rowHeightPx = 28;
}
function styleBody(range) {
  range.format = { font: { color: colors.ink, size: 10 }, verticalAlignment: "top", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
}
function styleHeader(range, fill) {
  range.format = { fill, font: { bold: true, color: colors.white, size: 10 }, horizontalAlignment: "center", verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
  range.format.rowHeightPx = 58;
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

const labelGroups = [
  { dimension: "V11", groupZh: "视觉主体", parentField: "visual_subject", confidenceZh: "视觉主体维度置信度", confidenceField: "visual_subject_confidence", labels: [
    ["VS-NAT", "自然景观", "visual_subject_vs_nat", "以山水、海岸、森林、天气等自然要素为主要视觉内容。", "按当前图片的视觉显著性与画面占比判断。"],
    ["VS-ARC", "建筑遗产", "visual_subject_vs_arc", "以建筑、街区、纪念物、历史或文化遗产为主要视觉内容。", "普通室内服务空间以设施功能为主时，另判服务设施。"],
    ["VS-PEO", "人物活动", "visual_subject_vs_peo", "人物及其行为构成主要视觉内容。", "画面中有人不等于人物活动标签成立。"],
    ["VS-FOD", "美食/物产", "visual_subject_vs_fod", "食品、饮品、餐食或地方物产构成主要视觉内容。", "餐厅环境与食物主体可在校准中同时记录为1。"],
    ["VS-FAC", "服务设施", "visual_subject_vs_fac", "住宿、餐饮、交通、商业或旅游服务设施构成主要视觉内容。", "校准阶段不以其他主体标签为由强制排除。"],
  ] },
  { dimension: "V12", groupZh: "场景语境", parentField: "scene_context", confidenceZh: "场景语境维度置信度", confidenceField: "scene_context_confidence", labels: [
    ["SC-ICO", "标志性场景", "scene_context_sc_ico", "呈现具有目的地识别度的地标、典型景观或代表性场所。", "不要仅因画面好看就判为标志性。"],
    ["SC-STR", "日常场景", "scene_context_sc_str", "以街道、生活空间、常规活动或非地标体验为主。", "与视觉主体轴独立判断。"],
    ["SC-OTH", "其他/不适用", "scene_context_sc_oth", "不属于标志性或日常语境，或该轴对当前图片不适用。", "证据不足时使用UNRESOLVED，不用本标签兜底。"],
  ] },
  { dimension: "V12", groupZh: "景别", parentField: "shot_scale", confidenceZh: "景别维度置信度", confidenceField: "shot_scale_confidence", labels: [
    ["SS-CLO", "特写", "shot_scale_ss_clo", "主体局部或细节占据画面，环境信息很少。", "按画面构图判断，不按拍摄距离猜测。"],
    ["SS-MID", "中景", "shot_scale_ss_mid", "主体与部分环境共同可见，二者相对平衡。", "边界样例需在共同校准中固定。"],
    ["SS-PAN", "全景", "shot_scale_ss_pan", "大范围环境或整体空间关系构成画面重点。", "不等同于航拍；视角另判。"],
  ] },
  { dimension: "V12", groupZh: "视角", parentField: "viewpoint", confidenceZh: "视角维度置信度", confidenceField: "viewpoint_confidence", labels: [
    ["VP-GRD", "平视/常规视角", "viewpoint_vp_grd", "接近常规人眼高度或无明显俯视特征。", "默认值不能代替证据判断。"],
    ["VP-HIG", "高位俯视", "viewpoint_vp_hig", "从明显高于主体的位置向下拍摄，但不属于航拍。", "与航拍边界须在校准中统一。"],
    ["VP-AER", "航拍", "viewpoint_vp_aer", "具有无人机或航空平台形成的典型高空俯瞰视角。", "仅有高角度不自动等于航拍。"],
  ] },
  { dimension: "V14", groupZh: "人物数量", parentField: "people_count", confidenceZh: "人物数量维度置信度", confidenceField: "people_count_confidence", labels: [
    ["PC-NON", "无人", "people_count_pc_non", "画面中没有可识别的真实人物。", "海报、雕像和屏幕人物不按真实人物计。"],
    ["PC-SIN", "单人", "people_count_pc_sin", "画面中有且仅有一名可识别人物。", "人物很小但可识别时仍计数。"],
    ["PC-GRP", "群体", "people_count_pc_grp", "画面中有两名或以上可识别人物。", "不要求人物是视觉主体。"],
  ] },
  { dimension: "V14", groupZh: "人景互动", parentField: "human_scene_interaction", confidenceZh: "人景互动维度置信度", confidenceField: "human_scene_interaction_confidence", labels: [
    ["0", "无互动", "human_scene_interaction_0", "没有人物，或人物未与场景/设施发生可见互动。", "无人通常成立，但仍需人工判断。"],
    ["1", "有互动", "human_scene_interaction_1", "人物正在观看、使用、参与、触碰或以行动回应场景/设施。", "合影等边界行为须在校准中统一。"],
  ] },
  { dimension: "V15", groupZh: "品牌标识", parentField: "cs_brand", confidenceZh: "品牌标识维度置信度", confidenceField: "cs_brand_confidence", labels: [
    ["0", "品牌标识未出现", "cs_brand_0", "没有清晰、可识别的品牌文字或标识。", "模糊、局部或推测性标识按本标签记录。"],
    ["1", "品牌标识出现", "cs_brand_1", "出现清晰、可识别的品牌文字或标识。", "品牌可与住宿、餐饮、交通同时出现。"],
  ] },
  { dimension: "V15", groupZh: "住宿设施", parentField: "cs_accom", confidenceZh: "住宿设施维度置信度", confidenceField: "cs_accom_confidence", labels: [
    ["0", "住宿设施未出现", "cs_accom_0", "未出现可辨认的住宿设施或明确住宿场景。", "与视觉主体字段独立。"],
    ["1", "住宿设施出现", "cs_accom_1", "出现酒店、民宿、客房、前台等住宿设施或明确场景。", "可与品牌标识同时成立。"],
  ] },
  { dimension: "V15", groupZh: "餐饮场所", parentField: "cs_rest", confidenceZh: "餐饮场所维度置信度", confidenceField: "cs_rest_confidence", labels: [
    ["0", "餐饮场所未出现", "cs_rest_0", "未出现可辨认的餐饮场所或明确餐饮场景。", "单纯食物特写不必然等于餐饮场所。"],
    ["1", "餐饮场所出现", "cs_rest_1", "出现餐厅、咖啡馆、酒吧、摊档等餐饮场所或明确场景。", "可与品牌标识同时成立。"],
  ] },
  { dimension: "V15", groupZh: "交通工具", parentField: "cs_trans", confidenceZh: "交通工具维度置信度", confidenceField: "cs_trans_confidence", labels: [
    ["0", "交通工具未出现", "cs_trans_0", "未出现可辨认的交通工具。", "背景中不可辨识物体不计。"],
    ["1", "交通工具出现", "cs_trans_1", "出现汽车、火车、船、飞机、自行车等可辨认交通工具。", "可与品牌标识同时成立。"],
  ] },
];

const guide = workbook.worksheets.add("填写说明");
guide.showGridLines = false;
guide.mergeCells("A1:F1");
guide.getRange("A1").values = [["视觉轨逐标签单列人工编码模板"]];
styleTitle(guide.getRange("A1:F1"));
guide.mergeCells("A2:F2");
guide.getRange("A2").values = [["适用：共同校准｜编码表 v3.6.1｜模板 visual-coding-wide-v2.0"]];
guide.getRange("A2:F2").format = { fill: colors.sky, font: { bold: true, color: colors.navy2, size: 10 }, verticalAlignment: "center" };
guide.getRange("A2:F2").format.rowHeightPx = 28;
guide.mergeCells("A4:F4");
guide.getRange("A4").values = [["本版核心变化"]];
styleSection(guide.getRange("A4:F4"), colors.amber);
guide.mergeCells("A5:F8");
guide.getRange("A5").values = [["27个候选标签全部拆成独立列。每列只填写：1=该标签符合当前图片；0=该标签不符合；UNRESOLVED=当前无法判断该标签是否成立。共同校准阶段不在表格中强制单选、互斥或唯一选中，因此同一原维度可以出现多个1。待基数规则冻结后，再由转换程序派生或折叠为正式字段。"]];
styleBody(guide.getRange("A5:F8"));
guide.getRange("A5:F8").format.fill = colors.amberLight;
guide.mergeCells("A10:F10");
guide.getRange("A10").values = [["填写流程"]];
styleSection(guide.getRange("A10:F10"));
guide.getRange("A11:B16").values = [
  ["1｜核对记录", "确认 round_id、post_id、img_id、图片路径/地址与盲标槽位。"],
  ["2｜逐图独立判断", "每张图片单独编码，不使用同帖其他图片替代当前图片证据。"],
  ["3｜逐列判断标签", "对27个标签列分别填1、0或UNRESOLVED，不跳过未选标签。"],
  ["4｜填写维度置信度", "10个原判断维度各填1—5分；置信度1—2必须补低置信备注。"],
  ["5｜记录涉及标签", "低置信备注中的 label_field_names 使用本表字段名，多个字段用 | 分隔。"],
  ["6｜完成后导出", "“人工标注”和“低置信备注”分别导出为UTF-8 CSV。"],
];
for (let row = 11; row <= 16; row += 1) guide.mergeCells(`B${row}:F${row}`);
styleBody(guide.getRange("A11:F16"));
guide.getRange("A11:A16").format = { fill: colors.tealLight, font: { bold: true, color: colors.teal }, verticalAlignment: "top", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
guide.mergeCells("A18:F18");
guide.getRange("A18").values = [["判断与数据约束"]];
styleSection(guide.getRange("A18:F18"), colors.navy2);
guide.getRange("A19:B24").values = [
  ["全部标签必填", "空白表示漏填，不等于0；即使明显不符合也要明确填0。"],
  ["不强制互斥", "表格不设置维度内选中数限制；这是校准期的无损记录策略。"],
  ["置信度按维度", "每个原判断维度共用一个置信度列；它评价该维度整组标签判断的稳定性。"],
  ["状态使用", "若某个候选标签无法判断，该标签列填UNRESOLVED；置信度1时至少一个相关标签列应为UNRESOLVED。"],
  ["禁止手写JSON", "编码员不填写confidence_json或confidence_notes_json，转换程序后续生成。"],
  ["版本与时间", "编码时间使用ISO 8601；不得改列头，版本固定为v3.6.1与visual-coding-wide-v2.0。"],
];
for (let row = 19; row <= 24; row += 1) guide.mergeCells(`B${row}:F${row}`);
styleBody(guide.getRange("A19:F24"));
guide.getRange("A19:A24").format = { fill: colors.grayLight, font: { bold: true, color: colors.navy2 }, verticalAlignment: "top", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
guide.getRange("A1:A24").format.columnWidthPx = 126;
guide.getRange("B1:F24").format.columnWidthPx = 150;
guide.freezePanes.freezeRows(2);

const coding = workbook.worksheets.add("人工标注");
coding.showGridLines = false;
const metadataHeaders = [["标注记录编号", "annotation_id"], ["标注轮次", "round_id"], ["标注阶段", "annotation_stage"], ["帖子编号", "post_id"], ["图片序号", "img_id"], ["平台", "platform"], ["图片本地路径", "image_path"], ["图片原始地址", "img_url"], ["盲标槽位", "annotator_slot"]];
const trailingHeaders = [["编码员标识", "coder"], ["编码时间", "annotated_at"], ["编码表版本", "codebook_version"], ["模板版本", "template_schema_version"]];
const headers = metadataHeaders.map(([zh, field]) => `${zh}（${field}）`);
const labelColumnIndices = [];
const confidenceColumnIndices = [];
for (const group of labelGroups) {
  for (const [, labelZh, field] of group.labels) {
    labelColumnIndices.push(headers.length);
    headers.push(`${labelZh}（${field}）`);
  }
  confidenceColumnIndices.push(headers.length);
  headers.push(`${group.confidenceZh}（${group.confidenceField}）`);
}
for (const [zh, field] of trailingHeaders) headers.push(`${zh}（${field}）`);
coding.getRangeByIndexes(0, 0, 1, headers.length).values = [headers];
styleHeader(coding.getRangeByIndexes(0, 0, 1, metadataHeaders.length), colors.gray);
for (const index of labelColumnIndices) styleHeader(coding.getRangeByIndexes(0, index, 1, 1), colors.teal);
for (const index of confidenceColumnIndices) styleHeader(coding.getRangeByIndexes(0, index, 1, 1), colors.amber);
styleHeader(coding.getRangeByIndexes(0, headers.length - trailingHeaders.length, 1, trailingHeaders.length), colors.gray);
coding.getRangeByIndexes(1, 0, DATA_ROWS, headers.length).format = { font: { color: colors.ink, size: 10 }, verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: "#E6EAED" } };
coding.getRangeByIndexes(1, 0, DATA_ROWS, metadataHeaders.length).format.fill = colors.grayLight;
for (const index of labelColumnIndices) {
  const range = coding.getRangeByIndexes(1, index, DATA_ROWS, 1);
  range.format.fill = colors.tealLight;
  applyList(range, [0, 1, "UNRESOLVED"]);
  applyIndicatorFormatting(range);
}
for (const index of confidenceColumnIndices) {
  const range = coding.getRangeByIndexes(1, index, DATA_ROWS, 1);
  range.format.fill = colors.amberLight;
  range.dataValidation = { rule: { type: "whole", operator: "between", formula1: 1, formula2: 5 } };
  applyConfidenceFormatting(range);
}
coding.getRangeByIndexes(1, headers.length - trailingHeaders.length, DATA_ROWS, trailingHeaders.length).format.fill = colors.grayLight;
applyList(coding.getRangeByIndexes(1, 2, DATA_ROWS, 1), ["CALIBRATION", "BLIND_PILOT", "FORMAL"]);
applyList(coding.getRangeByIndexes(1, 8, DATA_ROWS, 1), [1, 2]);
applyList(coding.getRangeByIndexes(1, headers.length - 2, DATA_ROWS, 1), ["v3.6.1"]);
applyList(coding.getRangeByIndexes(1, headers.length - 1, DATA_ROWS, 1), ["visual-coding-wide-v2.0"]);
coding.getRangeByIndexes(1, 0, DATA_ROWS, metadataHeaders.length - 1).format.numberFormat = "@";
coding.getRangeByIndexes(1, headers.length - trailingHeaders.length, DATA_ROWS, trailingHeaders.length).format.numberFormat = "@";
coding.freezePanes.freezeRows(1);
coding.freezePanes.freezeColumns(9);
coding.getRangeByIndexes(1, 0, DATA_ROWS, headers.length).format.rowHeightPx = 24;
for (let index = 0; index < headers.length; index += 1) {
  let width = 126;
  if ([6, 7].includes(index)) width = 220;
  else if (index === 2) width = 125;
  else if (index === 8) width = 105;
  else if (labelColumnIndices.includes(index)) width = 150;
  else if (confidenceColumnIndices.includes(index)) width = 138;
  else if (index === headers.length - 3) width = 170;
  else if (index === headers.length - 1) width = 165;
  coding.getRangeByIndexes(0, index, DATA_ROWS + 1, 1).format.columnWidthPx = width;
}

const notes = workbook.worksheets.add("低置信备注");
notes.showGridLines = false;
const noteHeaders = ["低置信记录编号（note_id）", "标注记录编号（annotation_id）", "标注轮次（round_id）", "帖子编号（post_id）", "图片序号（img_id）", "低置信维度（field_name）", "涉及标签列（label_field_names）", "置信度（confidence）", "原因代码（reason_code）", "备选判断（alternative_values）", "简短说明（low_confidence_note）", "编码员标识（coder）", "编码时间（annotated_at）", "编码表版本（codebook_version）", "模板版本（template_schema_version）"];
notes.getRange("A1:O1").values = [noteHeaders];
styleHeader(notes.getRange("A1:E1"), colors.gray);
styleHeader(notes.getRange("F1:K1"), colors.amber);
styleHeader(notes.getRange("L1:O1"), colors.gray);
notes.getRange(`A2:O${DATA_ROWS + 1}`).format = { font: { color: colors.ink, size: 10 }, verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: "#E6EAED" } };
notes.getRange(`A2:E${DATA_ROWS + 1}`).format.fill = colors.grayLight;
notes.getRange(`F2:K${DATA_ROWS + 1}`).format.fill = colors.amberLight;
notes.getRange(`L2:O${DATA_ROWS + 1}`).format.fill = colors.grayLight;
applyList(notes.getRange(`F2:F${DATA_ROWS + 1}`), labelGroups.map((group) => group.parentField));
applyList(notes.getRange(`H2:H${DATA_ROWS + 1}`), [1, 2]);
applyList(notes.getRange(`I2:I${DATA_ROWS + 1}`), ["BOUNDARY", "CONTEXT", "CONFLICT", "EVIDENCE_MISSING", "OTHER"]);
applyList(notes.getRange(`N2:N${DATA_ROWS + 1}`), ["v3.6.1"]);
applyList(notes.getRange(`O2:O${DATA_ROWS + 1}`), ["visual-coding-wide-v2.0"]);
applyConfidenceFormatting(notes.getRange(`H2:H${DATA_ROWS + 1}`));
notes.getRange(`A2:G${DATA_ROWS + 1}`).format.numberFormat = "@";
notes.getRange(`I2:O${DATA_ROWS + 1}`).format.numberFormat = "@";
notes.freezePanes.freezeRows(1);
notes.freezePanes.freezeColumns(5);
const noteWidths = [135, 150, 115, 115, 90, 155, 260, 90, 145, 220, 290, 120, 170, 115, 165];
noteWidths.forEach((width, index) => notes.getRangeByIndexes(0, index, DATA_ROWS + 1, 1).format.columnWidthPx = width);
notes.getRange(`2:${DATA_ROWS + 1}`).format.rowHeightPx = 24;

const labels = workbook.worksheets.add("标签说明");
labels.showGridLines = false;
labels.mergeCells("A1:I1");
labels.getRange("A1").values = [["视觉轨逐标签单列映射、定义与置信度说明"]];
styleTitle(labels.getRange("A1:I1"));
labels.mergeCells("A2:I2");
labels.getRange("A2").values = [["每个候选标签都有独立字段名；主表按0/1/UNRESOLVED记录，不在校准模板中预设互斥关系。"]];
labels.getRange("A2:I2").format = { fill: colors.sky, font: { bold: true, color: colors.navy2 }, verticalAlignment: "center" };
labels.getRange("A4:I4").values = [["维度", "原判断维度", "原字段名", "原代码/值", "中文标签", "单列字段名", "单元格取值", "操作性定义", "边界提示"]];
styleHeader(labels.getRange("A4:I4"), colors.teal);
const labelRows = [];
for (const group of labelGroups) {
  for (const [originalCode, labelZh, field, definition, boundary] of group.labels) labelRows.push([group.dimension, group.groupZh, group.parentField, originalCode, labelZh, field, "0 / 1 / UNRESOLVED", definition, boundary]);
}
labels.getRange(`A5:I${4 + labelRows.length}`).values = labelRows;
styleBody(labels.getRange(`A5:I${4 + labelRows.length}`));
let cursor = 6 + labelRows.length;
labels.mergeCells(`A${cursor}:I${cursor}`);
labels.getRange(`A${cursor}`).values = [["置信度量表（按原判断维度填写）"]];
styleSection(labels.getRange(`A${cursor}:I${cursor}`), colors.amber);
labels.getRange(`A${cursor + 1}:I${cursor + 5}`).values = [
  ["置信度", "5", "直接、清晰", "整组标签判断稳定", "几乎无合理替代", "无需备注", "高置信", "", ""],
  ["置信度", "4", "证据清晰", "只有轻微语境依赖", "无实质竞争判断", "通常无需备注", "高置信", "", ""],
  ["置信度", "3", "有依据", "存在边界或单一替代", "需要审慎复核", "可选备注", "中等置信", "", ""],
  ["置信度", "2", "证据较弱或冲突", "高度依赖语境或有多个替代", "仍记录当前判断", "必须填写低置信备注", "低置信", "", ""],
  ["置信度", "1", "不足以稳定判断", "至少一个相关标签列填UNRESOLVED", "进入问题队列", "必须填写低置信备注", "未解决", "", ""],
];
styleBody(labels.getRange(`A${cursor + 1}:I${cursor + 5}`));
labels.getRange(`A${cursor + 1}:I${cursor + 2}`).format.fill = colors.greenLight;
labels.getRange(`A${cursor + 3}:I${cursor + 3}`).format.fill = colors.amberLight;
labels.getRange(`A${cursor + 4}:I${cursor + 5}`).format.fill = colors.redLight;
cursor += 7;
labels.mergeCells(`A${cursor}:I${cursor}`);
labels.getRange(`A${cursor}`).values = [["低置信原因代码"]];
styleSection(labels.getRange(`A${cursor}:I${cursor}`), colors.gray);
labels.getRange(`A${cursor + 1}:I${cursor + 5}`).values = [
  ["原因", "BOUNDARY", "标签边界", "候选标签之间边界不清", "在label_field_names列出涉及标签", "说明边界", "", "", ""],
  ["原因", "CONTEXT", "语境依赖", "离开上下文难以稳定判断", "说明需要的上下文", "不得借用同帖其他图片替代当前图片证据", "", "", ""],
  ["原因", "CONFLICT", "证据冲突", "画面存在相互矛盾线索", "列出冲突线索", "可保留多个1或使用UNRESOLVED", "", "", ""],
  ["原因", "EVIDENCE_MISSING", "证据缺失", "图片质量、遮挡或信息不足", "说明缺失证据", "通常降低置信度", "", "", ""],
  ["原因", "OTHER", "其他", "以上原因均不适用", "必须写明具体原因", "谨慎使用", "", "", ""],
];
styleBody(labels.getRange(`A${cursor + 1}:I${cursor + 5}`));
cursor += 7;
labels.mergeCells(`A${cursor}:I${cursor}`);
labels.getRange(`A${cursor}`).values = [["单元格状态说明"]];
styleSection(labels.getRange(`A${cursor}:I${cursor}`), colors.navy2);
labels.getRange(`A${cursor + 1}:I${cursor + 3}`).values = [
  ["状态", "1", "标签成立", "该标签符合当前图片", "同维度允许多个1", "校准期不强制互斥", "", "", ""],
  ["状态", "0", "标签不成立", "该标签不符合当前图片", "必须显式填写", "空白不等于0", "", "", ""],
  ["状态", "UNRESOLVED", "无法判断", "当前证据不足以确定该标签是否成立", "必须填写低置信备注", "进入问题队列", "", "", ""],
];
styleBody(labels.getRange(`A${cursor + 1}:I${cursor + 3}`));
labels.getRange(`A${cursor + 1}:I${cursor + 1}`).format.fill = colors.greenLight;
labels.getRange(`A${cursor + 2}:I${cursor + 2}`).format.fill = colors.grayLight;
labels.getRange(`A${cursor + 3}:I${cursor + 3}`).format.fill = colors.redLight;
const labelWidths = [70, 110, 175, 100, 150, 230, 155, 315, 325];
labelWidths.forEach((width, index) => labels.getRangeByIndexes(0, index, cursor + 3, 1).format.columnWidthPx = width);
labels.freezePanes.freezeRows(4);

const options = workbook.worksheets.add("下拉选项");
options.showGridLines = false;
const optionHeaders = ["indicator_value", "confidence", "reason_code", "field_name", "annotation_stage", "annotator_slot"];
options.getRange("A1:F1").values = [optionHeaders];
styleHeader(options.getRange("A1:F1"), colors.navy2);
const optionColumns = [[0, 1, "UNRESOLVED"], [1, 2, 3, 4, 5], ["BOUNDARY", "CONTEXT", "CONFLICT", "EVIDENCE_MISSING", "OTHER"], labelGroups.map((group) => group.parentField), ["CALIBRATION", "BLIND_PILOT", "FORMAL"], [1, 2]];
const maxOptionRows = Math.max(...optionColumns.map((column) => column.length));
const optionRows = Array.from({ length: maxOptionRows }, (_, rowIndex) => optionColumns.map((column) => column[rowIndex] ?? ""));
options.getRange(`A2:F${maxOptionRows + 1}`).values = optionRows;
styleBody(options.getRange(`A2:F${maxOptionRows + 1}`));
options.getRange(`A2:F${maxOptionRows + 1}`).format.fill = colors.grayLight;
options.mergeCells("A13:F14");
options.getRange("A13").values = [["审计页：标签列统一使用indicator_value。校准结束后若形成互斥或单选规则，须先更新研究决议与编码表，再更新转换程序；不要直接删列。"]];
options.getRange("A13:F14").format = { fill: colors.amberLight, font: { bold: true, color: "#7A4B00" }, verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: colors.grid } };
options.getRange("A1:F14").format.columnWidthPx = 170;
options.getRange("D1:D14").format.columnWidthPx = 210;
options.freezePanes.freezeRows(1);

const keyInspection = await workbook.inspect({ kind: "table", range: "人工标注!A1:AX3", include: "values,formulas", tableMaxRows: 3, tableMaxCols: 50 });
const labelInspection = await workbook.inspect({ kind: "table", range: `标签说明!A1:I${Math.min(cursor + 3, 55)}`, include: "values,formulas", tableMaxRows: 55, tableMaxCols: 9 });
const errorScan = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "最终公式错误扫描" });
const renderSpecs = [
  ["填写说明", "A1:F24", "guide.png"], ["人工标注", "A1:AX12", "coding.png"],
  ["低置信备注", "A1:O12", "low-confidence-notes.png"], ["标签说明", `A1:I${cursor + 3}`, "labels.png"],
  ["下拉选项", "A1:F14", "options.png"],
];
for (const [sheetName, range, fileName] of renderSpecs) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, fileName), new Uint8Array(await preview.arrayBuffer()));
}
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
await fs.copyFile(outputPath, repositoryPath);
console.log(JSON.stringify({ outputPath, repositoryPath, previewDir, columnCount: headers.length, labelColumnCount: labelColumnIndices.length, confidenceColumnCount: confidenceColumnIndices.length, keyInspection: keyInspection.ndjson, labelInspection: labelInspection.ndjson, errorScan: errorScan.ndjson }, null, 2));
