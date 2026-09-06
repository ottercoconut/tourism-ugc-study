/**
 * 把固定全标签人工工作簿迁移到v3.16.0疑问标记契约。
 *
 * 脚本只修改空白模板，不接触任何人工答案。原二进制版本由Git提交和归档哈希
 * 保留。旧V0列不删除，以免破坏历史导出列位，但会灰显、收窄并明确标为停用；
 * V0现行任务继续使用独立作者身份工作簿。
 */

import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { FileBlob, SpreadsheetFile } = require("@oai/artifact-tool");

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "..");
const workbookPath = path.join(
  repoRoot,
  "data",
  "annotations",
  "templates",
  "all-label-manual-coding.xlsx",
);
const temporaryPath = `${workbookPath}.tmp.xlsx`;
const qaDir = path.join(
  repoRoot,
  "results",
  "manual-workbook-v316-qa",
);

const sheetNames = [
  "填写说明",
  "帖子标注",
  "文本标注",
  "图像标注",
  "低置信备注",
  "文本证据",
  "标签总表",
  "下拉选项",
];

function migratedValue(value) {
  if (typeof value !== "string") return value;
  return value
    .replaceAll("low_confidence_note", "review_note")
    .replaceAll("_confidence", "_review_flag")
    .replaceAll("confidence_field", "review_flag_field")
    .replaceAll("confidence", "review_flag")
    .replaceAll("低置信度", "疑问")
    .replaceAll("低置信", "疑问")
    .replaceAll("置信度", "疑问标记")
    .replaceAll("v3.11.0", "v3.16.0")
    .replaceAll("all-label-manual-coding-v2.5", "all-label-manual-coding-v2.6")
    .replaceAll("platform_ctrip", "platform_zhihu")
    .replaceAll("携程", "知乎");
}

const sourceBlob = await FileBlob.load(workbookPath);
const workbook = await SpreadsheetFile.importXlsx(sourceBlob);

for (const sheetName of sheetNames) {
  const sheet = workbook.worksheets.getItem(sheetName);
  const used = sheet.getUsedRange();
  used.values = used.values.map((row) => row.map(migratedValue));
}

const instructions = workbook.worksheets.getItem("填写说明");
instructions.getRange("B5").values = [[
  "V1—V2帖子字段每帖一行；旧V0列仅为历史兼容，已灰显停用，作者身份另用独立工作簿。",
]];
instructions.getRange("B6").values = [[
  "79个标签列 + 2个人工情感强度字段；每个主观字段旁保留可选疑问标记列，正常判断不填。",
]];
instructions.getRange("B7").values = [[
  "39个标签列；每个主观字段旁保留可选疑问标记列。每张图片独立一行；V11只记录直接可见状态。",
]];
instructions.getRange("B8").values = [[
  "现行标签结构不变；疑问标记只是编码员锁定后讨论的过程路标，不进入信度、论文或模型。",
]];
instructions.getRange("B13").values = [[
  "每个独立编码组使用一种颜色；标签与对应疑问标记列保持同色。颜色只用于浏览和分段导航。",
]];
instructions.getRange("A15:B15").values = [[
  "可选疑问标记",
  "每个字段先填标签值；确实拿不准时才在对应review_flag列填?，并在疑问页写原因和一句说明。UNRESOLVED必须标?。",
]];
instructions.getRange("B18").values = [[
  "各主表、疑问记录页和文本证据分别导出UTF-8 CSV；不得改列头或手写JSON。疑问页签名称暂保留旧称以兼容历史流程。",
]];
instructions.getRange("B20").values = [[
  "文件名固定为all-label-manual-coding.xlsx；版本只写在工作簿内部。本次内部模板版本为all-label-manual-coding-v2.6。",
]];
instructions.getRange("A21").values = [[
  "标签结构沿用现行编码表。正常判断只填标签值；拿不准才标?。编码表版本固定为v3.16.0，内部模板版本固定为all-label-manual-coding-v2.6。",
]];
instructions.getRange("A19:F21").format.fill = "#F8FAFC";
instructions.getRange("A19:F21").format.font = { color: "#1F2937", size: 10 };
instructions.getRange("A19:F21").format.wrapText = true;
instructions.getRange("A1:F1").format.font = { color: "#FFFFFF", bold: true, size: 16 };
instructions.getRange("A10:F10").format.font = { color: "#FFFFFF", bold: true, size: 11 };

const postSheet = workbook.worksheets.getItem("帖子标注");
for (const rangeAddress of ["E1:F201", "J1:Z201"]) {
  const range = postSheet.getRange(rangeAddress);
  range.format.fill = "#E7E6E6";
  range.format.font = { color: "#7F8C8D", italic: true, size: 9 };
  range.format.columnWidth = 4;
}
for (const column of ["E", "F", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z"]) {
  const header = postSheet.getRange(`${column}1`);
  const current = String(header.values[0][0] ?? "");
  header.values = [[current.startsWith("【旧V0停用】") ? current : `【旧V0停用】${current}`]];
}
postSheet.getRange("AI1").values = [["知乎（platform_zhihu）"]];

const notes = workbook.worksheets.getItem("低置信备注");
notes.getRange("A1:Q1").values = [[
  "疑问记录编号（note_id）",
  "观察单位（unit_type）",
  "标注记录编号（annotation_id）",
  "标注轮次（round_id）",
  "帖子编号（post_id）",
  "片段序号（seg_id）",
  "图片序号（img_id）",
  "疑问字段（field_name）",
  "涉及标签列（label_field_names）",
  "疑问标记（review_flag）",
  "原因代码（reason_code）",
  "备选判断（alternative_values）",
  "一句说明（review_note）",
  "编码员标识（coder）",
  "编码时间（annotated_at）",
  "编码表版本（codebook_version）",
  "模板版本（template_schema_version）",
]];

const options = workbook.worksheets.getItem("下拉选项");
options.getRange("D1:D6").values = [["review_flag"], ["?"], [null], [null], [null], [null]];
options.getRange("G1").values = [["review_flag_field"]];
options.getRange("J2:K2").values = [["v3.16.0", "all-label-manual-coding-v2.6"]];

const labelSheet = workbook.worksheets.getItem("标签总表");
labelSheet.getRange("M4").values = [["疑问标记字段"]];
labelSheet.getRange("A5:N15").format.fill = "#E7E6E6";
labelSheet.getRange("A5:N15").format.font = {
  color: "#7F8C8D",
  italic: true,
  size: 9,
};
for (let row = 5; row <= 15; row += 1) {
  const definition = labelSheet.getRange(`N${row}`);
  const current = String(definition.values[0][0] ?? "");
  definition.values = [[
    current.startsWith("已停用：")
      ? current
      : `已停用：V0改用独立作者身份工作簿。${current}`,
  ]];
}

await fs.mkdir(qaDir, { recursive: true });
const inspection = await workbook.inspect({
  kind: "match",
  searchTerm: "v3\\.11\\.0|all-label-manual-coding-v2\\.5|_confidence|low_confidence|置信度",
  options: { useRegex: true, maxResults: 500 },
  summary: "旧人工置信度与过期版本扫描",
});
await fs.writeFile(
  path.join(qaDir, "旧字段扫描.ndjson"),
  inspection.ndjson ?? String(inspection),
  "utf8",
);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "工作簿公式错误扫描",
});
await fs.writeFile(
  path.join(qaDir, "公式错误扫描.ndjson"),
  errors.ndjson ?? String(errors),
  "utf8",
);

for (const [sheetName, range] of [
  ["填写说明", "A1:F21"],
  ["帖子标注", "A1:AO14"],
  ["文本标注", "A1:U14"],
  ["低置信备注", "A1:Q12"],
  ["标签总表", "A1:N22"],
]) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(
    path.join(qaDir, `${sheetName}.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );
}

const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(temporaryPath);
await fs.rename(temporaryPath, workbookPath);

process.stdout.write(
  `${JSON.stringify({ workbook: path.relative(repoRoot, workbookPath), template_version: "all-label-manual-coding-v2.6", codebook_version: "v3.16.0", qa_dir: path.relative(repoRoot, qaDir) }, null, 2)}\n`,
);
