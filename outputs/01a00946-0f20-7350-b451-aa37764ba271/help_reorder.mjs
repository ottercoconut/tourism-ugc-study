import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const workbook = await SpreadsheetFile.importXlsx(
  await FileBlob.load("/Users/a123/Desktop/david/tourism-ugc-study/data/annotations/templates/all-label-manual-coding.xlsx"),
);
const help = workbook.help("*", {
  search: "moveTo|moveFrom|insert|delete|worksheet.*delete|column.*move",
  include: "index,examples,notes",
  maxChars: 12000,
});
console.log(help.ndjson);
