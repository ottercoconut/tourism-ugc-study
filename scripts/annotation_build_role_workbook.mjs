/**
 * 生成V0作者身份人工试编码工作簿。
 *
 * 工作簿只承载人工输入和证据摘录，不计算SC、覆盖状态或最终角色，避免把程序
 * 派生字段混入编码员判断。输出是可复用空模板；实际含作者信息的任务文件继续
 * 保存在Git忽略的私有轮次目录中。
 */

import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { FileBlob, SpreadsheetFile, Workbook } = require("@oai/artifact-tool");

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "..");
const outputDir = path.join(repoRoot, "outputs", "role-pilot-20260824");
const renderDir = path.join(outputDir, "renders");
const referencePath = path.join(
  repoRoot,
  "data",
  "annotations",
  "templates",
  "all-label-manual-coding.xlsx",
);
const outputPath = path.join(outputDir, "V0作者身份试编码模板.xlsx");

await fs.mkdir(renderDir, { recursive: true });

// 先读取并渲染现有内容编码簿，沿用其“说明—任务表—标签表”的清晰层级。
const referenceBlob = await FileBlob.load(referencePath);
const referenceBook = await SpreadsheetFile.importXlsx(referenceBlob);
const referenceSheets = await referenceBook.inspect({
  kind: "sheet",
  include: "id,name",
  maxChars: 3000,
});
await fs.writeFile(
  path.join(outputDir, "reference-inspect.ndjson"),
  referenceSheets.ndjson ?? String(referenceSheets),
  "utf8",
);
const referencePreview = await referenceBook.render({
  sheetName: "填写说明",
  range: "A1:H28",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  path.join(renderDir, "reference-填写说明.png"),
  new Uint8Array(await referencePreview.arrayBuffer()),
);

const workbook = Workbook.create();
const instructionSheet = workbook.worksheets.add("填写说明");
const codingSheet = workbook.worksheets.add("作者身份标注");
const evidenceSheet = workbook.worksheets.add("证据材料");
const labelSheet = workbook.worksheets.add("标签说明");
const valueSheet = workbook.worksheets.add("值域");

const colors = {
  navy: "#17365D",
  system: "#E7E6E6",
  scope: "#D9EAF7",
  vertical: "#E2F0D9",
  ea: "#FCE4D6",
  ce: "#E4DFEC",
  role: "#DDEBF7",
  note: "#FFF2CC",
  white: "#FFFFFF",
  border: "#B7C9DA",
  text: "#1F2937",
};

function styleTitle(sheet, range) {
  sheet.getRange(range).format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white, size: 16 },
    verticalAlignment: "center",
    horizontalAlignment: "left",
  };
}

function styleTable(range, fill = colors.white) {
  range.format = {
    fill,
    font: { color: colors.text, size: 10 },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: colors.border },
  };
}

// 填写说明
instructionSheet.showGridLines = false;
instructionSheet.mergeCells("A1:F2");
instructionSheet.getRange("A1").values = [["V0 作者身份人工试编码模板"]];
styleTitle(instructionSheet, "A1:F2");
instructionSheet.getRange("A4:F4").values = [[
  "步骤",
  "编码员动作",
  "允许填写",
  "不得填写/推断",
  "证据要求",
  "处理结果",
]];
instructionSheet.getRange("A5:F12").values = [
  ["1", "先阅读证据材料，不查看文本任务或互动结果", "—", "粉丝量决定角色；目标帖内容标签", "只使用当前任务证据包", "保持V0/T1隔离"],
  ["2", "判断主体范围", "actor_scope及置信度/证据ID", "把机构或多人账号写成UNK", "引用可识别主体性质的证据", "机构/多人最终角色由程序派生NA"],
  ["3", "判断内容垂类", "content_vertical及置信度/证据ID", "根据单篇偶然内容直接断定", "查看跨来源主题稳定性", "人工原始判断"],
  ["4", "判断EA专业权威", "一个或多个EA代码、置信度、证据ID", "一般认证、高粉或单篇专业表达单独成立", "阳性代码逐项引用；多项用|连接", "程序汇总EA"],
  ["5", "判断CE消费者同伴导向", "一个或多个CE代码、置信度、证据ID", "低粉、未认证或单次体验单独成立", "至少两个不同日期第一手来源，并有持续同伴导向", "程序汇总CE"],
  ["6", "填写诊断性整体角色", "raw_role_response及置信度/证据ID", "覆盖EA/CE/SC或最终正式角色", "只作为规则一致性诊断", "最终角色仍由程序派生"],
  ["7", "低置信度补充说明", "置信度1—2时填写结构化备注", "留空低置信度原因", "说明缺失、冲突或边界", "进入共同校准议题"],
  ["8", "保存个人原始文件", "annotator_id与annotated_at", "覆盖另一编码员文件或裁决表", "两份原始编码保持独立", "PILOT_ONLY共同校准"],
];
styleTable(instructionSheet.getRange("A4:F12"));
instructionSheet.getRange("A4:F4").format = {
  fill: colors.navy,
  font: { bold: true, color: colors.white },
  wrapText: true,
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: colors.border },
};
instructionSheet.mergeCells("A14:F14");
instructionSheet.getRange("A14").values = [["关键规则"]];
instructionSheet.getRange("A14:F14").format = {
  fill: colors.role,
  font: { bold: true, color: colors.navy, size: 12 },
};
instructionSheet.getRange("A15:F20").values = [
  ["研究状态", "全部结果为PILOT_ONLY", "规则", "role-pilot-v0.1", "编码表", "v3.13.0"],
  ["CI", "固定UNAVAILABLE，不参与充分性或角色派生", "SC", "程序按≥3日期且跨度≥30天派生", "最终角色", "程序派生，编码员不手填"],
  ["NONE代码门", "只有≥3日期、≥30天且含可用主页或等价身份材料，才允许EA_NONE/CE_NONE", "缺失", "证据不足必须UNK", "ORDINARY", "不得解释为普通游客"],
  ["KOL_TYPE", "PERSONAL + EA=1 + CE=0 + SC=1", "KOC_TYPE", "PERSONAL + EA=0 + CE=1 + SC=1", "HYBRID", "PERSONAL + EA=1 + CE=1 + SC=1"],
  ["规模", "粉丝量只形成独立触达档位", "认证", "一般认证不能单独决定角色", "互动", "点赞/评论等不能单独决定角色"],
  ["证据ID", "多项使用竖线|连接", "criterion codes", "多选同样用|连接", "置信度", "1最低，5最高"],
];
styleTable(instructionSheet.getRange("A15:F20"), "#F8FAFC");
instructionSheet.getRange("A1:F20").format.rowHeight = 34;
instructionSheet.getRange("A1:F2").format.rowHeight = 32;
[11, 35, 27, 37, 31, 31].forEach((width, index) => {
  instructionSheet.getRangeByIndexes(0, index, 20, 1).format.columnWidth = width;
});
instructionSheet.freezePanes.freezeRows(4);

// 作者身份标注主表：所有标签均与自己的置信度、证据列相邻。
const codingHeaders = [
  "任务ID\ntask_id",
  "轮次状态\nround_status",
  "作者快照ID\nauthor_snapshot_id",
  "证据清单ID\nevidence_manifest_id",
  "平台\nplatform",
  "主体范围\nactor_scope",
  "主体范围置信度\nactor_scope_confidence",
  "主体范围证据ID\nactor_scope_evidence_ids",
  "内容垂类\ncontent_vertical",
  "内容垂类置信度\ncontent_vertical_confidence",
  "内容垂类证据ID\ncontent_vertical_evidence_ids",
  "专业权威代码\nexpert_authority_criterion_codes",
  "专业权威置信度\nexpert_authority_confidence",
  "专业权威证据ID\nexpert_authority_evidence_ids",
  "消费者同伴代码\nconsumer_experience_criterion_codes",
  "消费者同伴置信度\nconsumer_experience_confidence",
  "消费者同伴证据ID\nconsumer_experience_evidence_ids",
  "诊断性整体角色\nraw_role_response",
  "整体角色置信度\nraw_role_confidence",
  "整体角色证据ID\nraw_role_evidence_ids",
  "社群关系状态\ncommunity_relation_status",
  "低置信度说明\nlow_confidence_note",
  "编码员ID\nannotator_id",
  "编码时间\nannotated_at",
  "角色规则版本\nrole_rule_version",
  "编码表版本\ncodebook_version",
];
codingSheet.getRange("A1:Z1").values = [codingHeaders];
styleTable(codingSheet.getRange("A1:Z101"));
codingSheet.getRange("A1:E1").format.fill = colors.system;
codingSheet.getRange("F1:H1").format.fill = colors.scope;
codingSheet.getRange("I1:K1").format.fill = colors.vertical;
codingSheet.getRange("L1:N1").format.fill = colors.ea;
codingSheet.getRange("O1:Q1").format.fill = colors.ce;
codingSheet.getRange("R1:U1").format.fill = colors.role;
codingSheet.getRange("V1:V1").format.fill = colors.note;
codingSheet.getRange("W1:Z1").format.fill = colors.system;
codingSheet.getRange("A1:Z1").format.font = { bold: true, color: colors.navy, size: 10 };
codingSheet.getRange("A1:Z1").format.horizontalAlignment = "center";
codingSheet.getRange("A1:Z1").format.verticalAlignment = "center";
codingSheet.getRange("A1:Z1").format.rowHeight = 56;
codingSheet.getRange("B2:B101").values = Array.from({ length: 100 }, () => ["PILOT_ONLY"]);
codingSheet.getRange("U2:U101").values = Array.from({ length: 100 }, () => ["UNAVAILABLE"]);
codingSheet.getRange("Y2:Y101").values = Array.from({ length: 100 }, () => ["role-pilot-v0.1"]);
codingSheet.getRange("Z2:Z101").values = Array.from({ length: 100 }, () => ["v3.13.0"]);
codingSheet.getRange("F2:F101").dataValidation = {
  rule: { type: "list", values: ["PERSONAL_CREATOR", "ORGANIZATION", "MULTI_AUTHOR", "UNCLEAR"] },
};
codingSheet.getRange("I2:I101").dataValidation = {
  rule: { type: "list", values: ["TRAVEL", "FOOD", "LIFESTYLE", "GENERAL", "OTHER", "UNK"] },
};
codingSheet.getRange("R2:R101").dataValidation = {
  rule: { type: "list", values: ["KOL_TYPE", "KOC_TYPE", "HYBRID", "ORDINARY", "UNK", "NA"] },
};
for (const column of ["G", "J", "M", "P", "S"]) {
  codingSheet.dataValidations.add({
    range: `${column}2:${column}101`,
    rule: { type: "whole", operator: "between", formula1: 1, formula2: 5 },
  });
}
codingSheet.getRange("X2:X101").format.numberFormat = "yyyy-mm-dd hh:mm";
const codingWidths = [
  24, 14, 24, 24, 12, 20, 14, 24, 22, 14, 24, 38, 14, 24, 34, 14, 24, 18, 14, 24, 18, 32, 16, 20, 20, 16,
];
codingWidths.forEach((width, index) => {
  codingSheet.getRangeByIndexes(0, index, 101, 1).format.columnWidth = width;
});
codingSheet.getRange("A2:Z101").format.rowHeight = 34;
codingSheet.freezePanes.freezeRows(1);
codingSheet.freezePanes.freezeColumns(5);

// 证据材料表只提供判断材料，不包含粉丝量、互动量或文本任务标签。
const evidenceHeaders = [
  "任务ID\ntask_id",
  "作者快照ID\nauthor_snapshot_id",
  "证据清单ID\nevidence_manifest_id",
  "平台\nplatform",
  "证据来源ID\nevidence_source_id",
  "来源类型\nsource_type",
  "来源发布时间\nsource_published_at",
  "抓取时间\ncaptured_at",
  "昵称原文\ndisplay_name_raw",
  "主页URL\nprofile_url_raw",
  "简介原文\nbio_raw",
  "认证原文\nverification_raw",
  "标题原文\ntitle_raw",
  "内容原文\ncontent_text_raw",
];
evidenceSheet.getRange("A1:N1").values = [evidenceHeaders];
styleTable(evidenceSheet.getRange("A1:N501"));
evidenceSheet.getRange("A1:N1").format = {
  fill: colors.navy,
  font: { bold: true, color: colors.white, size: 10 },
  wrapText: true,
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: colors.border },
};
evidenceSheet.getRange("A1:N1").format.rowHeight = 56;
[24, 24, 24, 12, 24, 16, 20, 20, 20, 32, 36, 30, 40, 70].forEach((width, index) => {
  evidenceSheet.getRangeByIndexes(0, index, 501, 1).format.columnWidth = width;
});
evidenceSheet.getRange("A2:N501").format.rowHeight = 46;
evidenceSheet.getRange("G2:H501").format.numberFormat = "yyyy-mm-dd hh:mm";
evidenceSheet.freezePanes.freezeRows(1);
evidenceSheet.freezePanes.freezeColumns(4);

// 标签说明
const labelRows = [
  ["字段", "代码", "中文含义", "判断边界"],
  ["actor_scope", "PERSONAL_CREATOR", "单一自然人创作者", "身份足以确认且内容由单一自然人持续生产"],
  ["actor_scope", "ORGANIZATION", "组织/官方账号", "机构、品牌、媒体、政务或景区主体；最终角色NA"],
  ["actor_scope", "MULTI_AUTHOR", "多人共同账号", "无法归属于单一自然人的编辑部、团队或多人轮换；最终角色NA"],
  ["actor_scope", "UNCLEAR", "主体不清", "材料不足以确认个人、组织或多人；最终角色UNK"],
  ["content_vertical", "TRAVEL", "旅行垂类", "历史内容持续以旅行、目的地或旅行决策为主"],
  ["content_vertical", "FOOD", "美食垂类", "历史内容持续以餐饮、美食或消费体验为主"],
  ["content_vertical", "LIFESTYLE", "生活方式垂类", "以生活方式为持续主线，旅行只是其中一类"],
  ["content_vertical", "GENERAL", "综合内容", "跨多个主题持续创作，无法归入单一垂类"],
  ["content_vertical", "OTHER", "其他垂类", "有稳定垂类但不属于以上类别"],
  ["content_vertical", "UNK", "垂类未知", "历史材料不足或主题边界冲突"],
  ["EA", "EA_CREDENTIAL", "旅游相关专业资质", "必须可审计，昵称自称不成立"],
  ["EA", "EA_DOMAIN_OCCUPATION", "旅游相关职业", "职业与旅行决策或旅游服务直接相关"],
  ["EA", "EA_DOMAIN_VERIFICATION", "领域身份认证", "一般黄V、等级或徽章本身不成立"],
  ["EA", "EA_INSTITUTION_AFFILIATION", "专业机构关联", "品牌露出或疑似合作不成立"],
  ["EA", "EA_SPECIALIST_HISTORY", "持续专业化历史", "须由非目标历史来源支持，单篇专业措辞不成立"],
  ["EA", "EA_NONE", "覆盖充分且无EA阳性项", "只有3日期/30天且有主页或等价身份材料才可填"],
  ["EA", "EA_UNK", "EA证据不足/冲突", "缺失不得改写为EA_NONE"],
  ["CE", "CE_FIRSTHAND_REPEAT", "重复第一手旅行/消费经历", "至少两个不同日期的独立来源"],
  ["CE", "CE_PEER_ORIENTATION", "持续同伴经验导向", "持续面向同伴提供经验或建议；单个口语词不成立"],
  ["CE", "CE_NONE", "覆盖充分且CE不成立", "只有3日期/30天且有主页或等价身份材料才可填"],
  ["CE", "CE_UNK", "CE证据不足/冲突", "缺失不得改写为CE_NONE"],
  ["raw_role_response", "KOL_TYPE", "诊断性KOL型", "只作一致性诊断，不能覆盖程序派生"],
  ["raw_role_response", "KOC_TYPE", "诊断性KOC型", "只作一致性诊断，不能覆盖程序派生"],
  ["raw_role_response", "HYBRID", "诊断性混合型", "EA和CE可同时成立"],
  ["raw_role_response", "ORDINARY", "诊断性其他充分个人型", "不得解释为普通游客"],
  ["raw_role_response", "UNK", "关键证据不足", "身份、时间或EA/CE/SC任一关键证据不足"],
  ["raw_role_response", "NA", "个人角色不适用", "机构或多人账号"],
  ["community_relation_status", "UNAVAILABLE", "关系性社群证据不可用", "固定值；不参与充分性或角色派生"],
  ["confidence", "1—5", "逐字段置信度", "1最低、5最高；1—2必须填写低置信度说明"],
];
labelSheet.getRange(`A1:D${labelRows.length}`).values = labelRows;
styleTable(labelSheet.getRange(`A1:D${labelRows.length}`));
labelSheet.getRange("A1:D1").format = {
  fill: colors.navy,
  font: { bold: true, color: colors.white },
  horizontalAlignment: "center",
  borders: { preset: "all", style: "thin", color: colors.border },
};
[25, 34, 34, 62].forEach((width, index) => {
  labelSheet.getRangeByIndexes(0, index, labelRows.length, 1).format.columnWidth = width;
});
labelSheet.getRange(`A2:D${labelRows.length}`).format.rowHeight = 32;
labelSheet.freezePanes.freezeRows(1);

// 值域表保留完整列表，便于复制和校验；多选代码不能用Excel原生下拉完成。
const valueRows = [
  ["字段", "允许值/填写方法"],
  ["actor_scope", "PERSONAL_CREATOR / ORGANIZATION / MULTI_AUTHOR / UNCLEAR"],
  ["content_vertical", "TRAVEL / FOOD / LIFESTYLE / GENERAL / OTHER / UNK"],
  ["EA代码", "EA_CREDENTIAL / EA_DOMAIN_OCCUPATION / EA_DOMAIN_VERIFICATION / EA_INSTITUTION_AFFILIATION / EA_SPECIALIST_HISTORY / EA_NONE / EA_UNK；多选用|连接"],
  ["CE代码", "CE_FIRSTHAND_REPEAT / CE_PEER_ORIENTATION / CE_NONE / CE_UNK；多选用|连接"],
  ["raw_role_response", "KOL_TYPE / KOC_TYPE / HYBRID / ORDINARY / UNK / NA"],
  ["community_relation_status", "UNAVAILABLE（固定，不修改）"],
  ["confidence", "1 / 2 / 3 / 4 / 5"],
  ["evidence_ids", "一个或多个evidence_source_id；多项用|连接"],
  ["round_status", "PILOT_ONLY（固定）"],
  ["role_rule_version", "role-pilot-v0.1（固定）"],
  ["codebook_version", "v3.13.0（固定）"],
];
valueSheet.getRange(`A1:B${valueRows.length}`).values = valueRows;
styleTable(valueSheet.getRange(`A1:B${valueRows.length}`));
valueSheet.getRange("A1:B1").format = {
  fill: colors.navy,
  font: { bold: true, color: colors.white },
  horizontalAlignment: "center",
  borders: { preset: "all", style: "thin", color: colors.border },
};
valueSheet.getRange(`A1:A${valueRows.length}`).format.columnWidth = 30;
valueSheet.getRange(`B1:B${valueRows.length}`).format.columnWidth = 110;
valueSheet.getRange(`A2:B${valueRows.length}`).format.rowHeight = 34;
valueSheet.freezePanes.freezeRows(1);

// 逐表结构检查、公式错误扫描和视觉渲染。
const checks = {};
for (const [sheetName, range] of [
  ["填写说明", "A1:F20"],
  ["作者身份标注", "A1:Z16"],
  ["证据材料", "A1:N12"],
  ["标签说明", `A1:D${labelRows.length}`],
  ["值域", `A1:B${valueRows.length}`],
]) {
  const inspection = await workbook.inspect({
    kind: "region",
    sheetId: sheetName,
    range,
    maxChars: 5000,
    tableMaxRows: 16,
    tableMaxCols: 26,
  });
  checks[sheetName] = inspection.ndjson ?? String(inspection);
  const preview = await workbook.render({
    sheetName,
    range,
    scale: sheetName === "作者身份标注" ? 0.8 : 1,
    format: "png",
  });
  await fs.writeFile(
    path.join(renderDir, `${sheetName}.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );
}
await fs.writeFile(
  path.join(outputDir, "workbook-inspect.json"),
  JSON.stringify(checks, null, 2),
  "utf8",
);
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
await fs.writeFile(
  path.join(outputDir, "formula-error-scan.ndjson"),
  errors.ndjson ?? String(errors),
  "utf8",
);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, renderDir, sheets: 5 }, null, 2));
