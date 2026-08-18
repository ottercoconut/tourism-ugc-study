import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const root = "/Users/a123/Desktop/david/tourism-ugc-study";
const workDir = path.join(root, "outputs/01a00946-0f20-7350-b451-aa37764ba271");
const currentPath = path.join(root, "data/annotations/templates/all-label-manual-coding.xlsx");
const baselinePath = path.join(workDir, "全标签人工编码模板.xlsx");
const previewDir = path.join(workDir, "previews-current-before-reorder");

const current = await SpreadsheetFile.importXlsx(await FileBlob.load(currentPath));
const baseline = await SpreadsheetFile.importXlsx(await FileBlob.load(baselinePath));

console.log("BASELINE_POST_HEADERS");
console.log(JSON.stringify(baseline.worksheets.getItem("帖子标注").getUsedRange().values?.[0] ?? []));

await fs.mkdir(previewDir, { recursive: true });

const sheetSummary = await current.inspect({
  kind: "sheet",
  include: "id,name",
  maxChars: 5000,
});
console.log("SHEETS");
console.log(sheetSummary.ndjson);

function cellAddress(rowIndex, colIndex) {
  let n = colIndex + 1;
  let letters = "";
  while (n > 0) {
    const rem = (n - 1) % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    n = Math.floor((n - 1) / 26);
  }
  return `${letters}${rowIndex + 1}`;
}

for (const name of ["帖子标注", "文本标注", "图像标注"]) {
  const sheet = current.worksheets.getItem(name);
  const baselineSheet = baseline.worksheets.getItem(name);
  const used = sheet.getUsedRange();
  const baselineUsed = baselineSheet.getUsedRange();
  const values = used.values ?? [];
  const baselineValues = baselineUsed.values ?? [];
  const headers = values[0] ?? [];
  const diffCells = [];
  const rowCount = Math.max(values.length, baselineValues.length);
  const colCount = Math.max(headers.length, baselineValues[0]?.length ?? 0);
  for (let r = 0; r < rowCount; r += 1) {
    for (let c = 0; c < colCount; c += 1) {
      const a = values[r]?.[c] ?? null;
      const b = baselineValues[r]?.[c] ?? null;
      if (a !== b) diffCells.push(cellAddress(r, c));
    }
  }
  console.log(`MAIN ${name}`);
  console.log(JSON.stringify({
    rowCount: values.length,
    colCount: headers.length,
    headers,
    differingValueCellCount: diffCells.length,
    differingValueCellsFirst50: diffCells.slice(0, 50),
    tableCount: sheet.tables.items.length,
    conditionalFormattingCount: sheet.conditionalFormattings.items.length,
    dataValidationCount: sheet.dataValidations.items.length,
  }));
}

const styleCheck = await current.inspect({
  kind: "computedStyle",
  sheetId: "帖子标注",
  range: "A1:AU5",
  maxChars: 4000,
});
console.log("STYLE_SAMPLE");
console.log(styleCheck.ndjson);

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
  const rendered = await current.render({
    sheetName,
    autoCrop: "all",
    scale: 1,
    format: "png",
  });
  await fs.writeFile(path.join(previewDir, fileName), new Uint8Array(await rendered.arrayBuffer()));
}
console.log(`PREVIEWS ${previewDir}`);
