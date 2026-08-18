import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const root = "/Users/a123/Desktop/david/tourism-ugc-study";
const workDir = path.join(root, "outputs/01a00946-0f20-7350-b451-aa37764ba271");
const inputPath = path.join(root, "data/annotations/templates/all-label-manual-coding.xlsx");
const outputPath = path.join(workDir, "置信度列相邻版.xlsx");
const previewDir = path.join(workDir, "previews-after-confidence-reorder");
const inspectedSourceSha256 = "7a59b6c9892a7fba3e6a18e942de09d2f20c3f17c0aec595a61bbd375c1db4ff";

const sha256 = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
const sourceBytes = await fs.readFile(inputPath);
const sourceSha256 = sha256(sourceBytes);
if (sourceSha256 !== inspectedSourceSha256) {
  throw new Error(`源工作簿在检查后又发生变化，拒绝覆盖：${sourceSha256}`);
}

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));

function fieldName(header) {
  const match = String(header ?? "").match(/（([^（）]+)）$/u);
  return match?.[1] ?? String(header ?? "");
}

const groupsBySheet = {
  帖子标注: [
    ["author_tier_confidence", ["author_tier_h_kol", "author_tier_w_kol", "author_tier_t_kol", "author_tier_koc"]],
    ["content_vertical_confidence", ["content_vertical_travel", "content_vertical_lifestyle", "content_vertical_food", "content_vertical_general", "content_vertical_other"]],
    ["has_commercial_disclosure_confidence", ["has_commercial_disclosure_0", "has_commercial_disclosure_1"]],
    ["destination_type_confidence", ["destination_type_nature", "destination_type_culture", "destination_type_city", "destination_type_resort", "destination_type_mixed"]],
  ],
  文本标注: [
    ["cs_inf_confidence", ["cs_inf"]],
    ["cs_edu_confidence", ["cs_edu"]],
    ["cs_emo_confidence", ["cs_emo"]],
    ["cs_int_confidence", ["cs_int"]],
    ["cs_oth_confidence", ["cs_oth"]],
    ["rs_n_confidence", ["rs_n"]],
    ["rs_r_confidence", ["rs_r"]],
    ["rs_n_geo_confidence", ["rs_n_geo"]],
    ["rs_n_wat_confidence", ["rs_n_wat"]],
    ["rs_n_bio_confidence", ["rs_n_bio"]],
    ["rs_n_cli_confidence", ["rs_n_cli"]],
    ["rs_r_his_confidence", ["rs_r_his"]],
    ["rs_r_arc_confidence", ["rs_r_arc"]],
    ["rs_r_gas_confidence", ["rs_r_gas"]],
    ["rs_r_fol_confidence", ["rs_r_fol"]],
    ["rs_r_act_confidence", ["rs_r_act"]],
    ["at_has_info_confidence", ["at_has_info"]],
    ["at_has_eval_confidence", ["at_has_eval"]],
    ["at_has_sug_confidence", ["at_has_sug"]],
    ["at_non_confidence", ["at_non"]],
    ["at_eval_subtype_confidence", ["at_eval_subtype_adm", "at_eval_subtype_sat", "at_eval_subtype_exc", "at_eval_subtype_dis", "at_eval_subtype_fru", "at_eval_subtype_nos", "at_eval_subtype_na"]],
    ["sentiment_judgeable_confidence", ["sentiment_judgeable_0", "sentiment_judgeable_1"]],
    ["sentiment_dir_confidence", ["sentiment_dir_pos", "sentiment_dir_neg", "sentiment_dir_mix", "sentiment_dir_neu", "sentiment_dir_na"]],
    ["asp_res_confidence", ["asp_res"]],
    ["asp_pri_confidence", ["asp_pri"]],
    ["asp_ser_confidence", ["asp_ser"]],
    ["asp_cro_confidence", ["asp_cro"]],
    ["asp_acc_confidence", ["asp_acc"]],
    ["asp_hyg_confidence", ["asp_hyg"]],
    ["is_que_confidence", ["is_que"]],
    ["is_dir_confidence", ["is_dir"]],
    ["is_soc_confidence", ["is_soc"]],
    ["is_non_confidence", ["is_non"]],
    ["sentiment_pos_val_confidence", ["sentiment_pos_val"]],
    ["sentiment_neg_val_confidence", ["sentiment_neg_val"]],
  ],
  图像标注: [
    ["visual_subject_confidence", ["visual_subject_vs_nat", "visual_subject_vs_arc", "visual_subject_vs_peo", "visual_subject_vs_fod", "visual_subject_vs_fac"]],
    ["scene_context_confidence", ["scene_context_sc_ico", "scene_context_sc_str", "scene_context_sc_oth"]],
    ["shot_scale_confidence", ["shot_scale_ss_clo", "shot_scale_ss_mid", "shot_scale_ss_pan"]],
    ["viewpoint_confidence", ["viewpoint_vp_grd", "viewpoint_vp_hig", "viewpoint_vp_aer"]],
    ["people_count_confidence", ["people_count_pc_non", "people_count_pc_sin", "people_count_pc_grp"]],
    ["human_scene_interaction_confidence", ["human_scene_interaction_0", "human_scene_interaction_1"]],
    ["cs_brand_confidence", ["cs_brand"]],
    ["cs_accom_confidence", ["cs_accom"]],
    ["cs_rest_confidence", ["cs_rest"]],
    ["cs_trans_confidence", ["cs_trans"]],
  ],
};

function buildTargetHeaders(headers, groups) {
  const headerByField = new Map(headers.map((header) => [fieldName(header), header]));
  const confidenceFields = new Set(groups.map(([confidence]) => confidence));
  const currentConfidenceFields = headers.map(fieldName).filter((field) => field.endsWith("_confidence"));
  const unmapped = currentConfidenceFields.filter((field) => !confidenceFields.has(field));
  if (unmapped.length) throw new Error(`存在未映射的置信度列：${unmapped.join(", ")}`);

  const baseHeaders = headers.filter((header) => !confidenceFields.has(fieldName(header)));
  const target = [];
  for (const header of baseHeaders) {
    target.push(header);
    const field = fieldName(header);
    for (const [confidence, members] of groups) {
      if (!headerByField.has(confidence)) continue;
      const presentMembers = members.filter((member) => headerByField.has(member));
      if (!presentMembers.length) throw new Error(`${confidence} 缺少相关标签列`);
      if (field === presentMembers.at(-1)) target.push(headerByField.get(confidence));
    }
  }
  if (target.length !== headers.length || new Set(target).size !== headers.length) {
    throw new Error("目标列顺序未能完整保留当前列集合");
  }
  return target;
}

function columnSignature(sheet, rowCount, colIndex) {
  const range = sheet.getRangeByIndexes(0, colIndex, rowCount, 1);
  return sha256(Buffer.from(JSON.stringify({ values: range.values, formulas: range.formulas })));
}

function readColumnWidth(sheet, rowCount, colIndex) {
  const width = sheet.getRangeByIndexes(0, colIndex, rowCount, 1).format.columnWidth;
  return Number.isFinite(width) ? width : null;
}

const reports = [];
for (const [sheetName, groups] of Object.entries(groupsBySheet)) {
  const sheet = workbook.worksheets.getItem(sheetName);
  const used = sheet.getUsedRange();
  const values = used.values ?? [];
  const headers = values[0] ?? [];
  const rowCount = values.length;
  const colCount = headers.length;
  const targetHeaders = buildTargetHeaders(headers, groups);

  const signatures = new Map();
  const widths = new Map();
  for (let col = 0; col < colCount; col += 1) {
    signatures.set(headers[col], columnSignature(sheet, rowCount, col));
    widths.set(headers[col], readColumnWidth(sheet, rowCount, col));
  }

  const currentOrder = [...headers];
  const scratch = sheet.getRangeByIndexes(0, colCount, rowCount, 1);
  let moveCount = 0;
  for (let destination = 0; destination < targetHeaders.length; destination += 1) {
    if (currentOrder[destination] === targetHeaders[destination]) continue;
    const source = currentOrder.indexOf(targetHeaders[destination], destination + 1);
    if (source < 0) throw new Error(`${sheetName} 无法找到目标列：${targetHeaders[destination]}`);

    scratch.copyFrom(sheet.getRangeByIndexes(0, source, rowCount, 1), "all");
    for (let col = source - 1; col >= destination; col -= 1) {
      sheet.getRangeByIndexes(0, col + 1, rowCount, 1)
        .copyFrom(sheet.getRangeByIndexes(0, col, rowCount, 1), "all");
    }
    sheet.getRangeByIndexes(0, destination, rowCount, 1).copyFrom(scratch, "all");
    const [movedHeader] = currentOrder.splice(source, 1);
    currentOrder.splice(destination, 0, movedHeader);
    moveCount += 1;
  }
  scratch.clear({ applyTo: "all" });

  for (let col = 0; col < targetHeaders.length; col += 1) {
    const width = widths.get(targetHeaders[col]);
    if (width !== null) sheet.getRangeByIndexes(0, col, rowCount, 1).format.columnWidth = width;
  }

  const afterHeaders = sheet.getRangeByIndexes(0, 0, 1, colCount).values?.[0] ?? [];
  if (JSON.stringify(afterHeaders) !== JSON.stringify(targetHeaders)) {
    throw new Error(`${sheetName} 列顺序验证失败`);
  }
  for (let col = 0; col < colCount; col += 1) {
    const header = afterHeaders[col];
    if (columnSignature(sheet, rowCount, col) !== signatures.get(header)) {
      throw new Error(`${sheetName} 列内容发生变化：${header}`);
    }
  }

  const fields = afterHeaders.map(fieldName);
  for (const [confidence, members] of groups) {
    const confidenceIndex = fields.indexOf(confidence);
    if (confidenceIndex < 0) continue;
    const presentMemberIndexes = members.map((member) => fields.indexOf(member)).filter((index) => index >= 0);
    if (confidenceIndex !== Math.max(...presentMemberIndexes) + 1) {
      throw new Error(`${sheetName} 的 ${confidence} 未紧邻相关标签组`);
    }
  }
  reports.push({ sheetName, rowCount, colCount, moveCount, targetHeaders });
}

const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
if ((formulaErrors.ndjson ?? "").includes('"kind":"match"')) {
  throw new Error(`检测到公式错误：${formulaErrors.ndjson}`);
}

await fs.mkdir(previewDir, { recursive: true });
const renderNames = [
  ["填写说明", "guide.png"],
  ["帖子标注", "post-coding.png"],
  ["文本标注", "text-coding.png"],
  ["图像标注", "image-coding.png"],
  ["低置信备注", "low-confidence.png"],
  ["文本证据", "text-evidence.png"],
  ["标签总表", "catalog.png"],
  ["下拉选项", "options.png"],
];
for (const [sheetName, fileName] of renderNames) {
  const rendered = await workbook.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, fileName), new Uint8Array(await rendered.arrayBuffer()));
}

await fs.mkdir(workDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
const outputSha256 = sha256(await fs.readFile(outputPath));

console.log(JSON.stringify({ sourceSha256, outputSha256, outputPath, previewDir, reports }));
