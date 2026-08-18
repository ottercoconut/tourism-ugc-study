import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const sourcePath = "/Users/a123/Desktop/david/tourism-ugc-study/data/annotations/templates/all-label-manual-coding.xlsx";
const outputPath = "/Users/a123/Desktop/david/tourism-ugc-study/outputs/01a00946-0f20-7350-b451-aa37764ba271/置信度列相邻版.xlsx";
const source = await SpreadsheetFile.importXlsx(await FileBlob.load(sourcePath));
const output = await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath));
const hash = (value) => crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex");
const fieldName = (header) => String(header ?? "").match(/（([^（）]+)）$/u)?.[1] ?? String(header ?? "");

const sheetInfo = await output.inspect({ kind: "sheet", include: "id,name", maxChars: 5000 });
console.log("SHEET_RANGES");
console.log(sheetInfo.ndjson);

for (const name of ["填写说明", "低置信备注", "文本证据", "标签总表", "下拉选项"]) {
  const before = source.worksheets.getItem(name).getUsedRange();
  const after = output.worksheets.getItem(name).getUsedRange();
  const same = hash({ values: before.values, formulas: before.formulas }) === hash({ values: after.values, formulas: after.formulas });
  if (!same) throw new Error(`${name} 的内容发生变化`);
}

for (const name of ["帖子标注", "文本标注", "图像标注"]) {
  const beforeSheet = source.worksheets.getItem(name);
  const afterSheet = output.worksheets.getItem(name);
  const beforeUsed = beforeSheet.getUsedRange();
  const afterUsed = afterSheet.getUsedRange();
  const beforeHeaders = beforeUsed.values?.[0] ?? [];
  const rawAfterHeaders = afterUsed.values?.[0] ?? [];
  const afterHeaders = rawAfterHeaders.slice(0, beforeHeaders.length);
  if (rawAfterHeaders.slice(beforeHeaders.length).some((value) => value !== null)) {
    throw new Error(`${name} 出现意外的非空尾列`);
  }
  const beforeByField = new Map();
  for (let col = 0; col < beforeHeaders.length; col += 1) {
    const range = beforeSheet.getRangeByIndexes(0, col, beforeUsed.values.length, 1);
    beforeByField.set(fieldName(beforeHeaders[col]), hash({ values: range.values, formulas: range.formulas }));
  }
  for (let col = 0; col < afterHeaders.length; col += 1) {
    const range = afterSheet.getRangeByIndexes(0, col, afterUsed.values.length, 1);
    const field = fieldName(afterHeaders[col]);
    if (hash({ values: range.values, formulas: range.formulas }) !== beforeByField.get(field)) {
      throw new Error(`${name} 的 ${field} 内容未保持不变`);
    }
  }
  console.log(JSON.stringify({ name, rows: afterUsed.values.length, cols: afterHeaders.length, fields: afterHeaders.map(fieldName) }));
}

const errors = await output.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
if ((errors.ndjson ?? "").includes('"kind":"match"')) throw new Error(errors.ndjson);
const previewDir = "/Users/a123/Desktop/david/tourism-ugc-study/outputs/01a00946-0f20-7350-b451-aa37764ba271/previews-exported-final";
await fs.mkdir(previewDir, { recursive: true });
for (const [sheetName, fileName] of [
  ["填写说明", "guide.png"],
  ["帖子标注", "post-coding.png"],
  ["文本标注", "text-coding.png"],
  ["图像标注", "image-coding.png"],
  ["低置信备注", "low-confidence.png"],
  ["文本证据", "text-evidence.png"],
  ["标签总表", "catalog.png"],
  ["下拉选项", "options.png"],
]) {
  const rendered = await output.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, fileName), new Uint8Array(await rendered.arrayBuffer()));
}
console.log(`PREVIEWS ${previewDir}`);
console.log("VERIFIED");
