/**
 * 从已冻结的文本共同校准CSV生成两份可直接填写的人工工作簿。
 *
 * 生成器只读取旧轮次中的文本任务，保留帖子、片段、字段和值域，并将文本任务
 * 的版本记录更新为当前编码表v3.16.0。v3.16.0未改变V1—V6的标签语义，因此无需
 * 改写样本或人工答案。旧双任务包中的V0结构不符合v3.16.0，本脚本明确不读取、
 * 复制或改签任何V0文件。
 */

import crypto from "node:crypto";
import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { SpreadsheetFile, Workbook } = require("@oai/artifact-tool");

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "..");
const sourceDir = path.resolve(
  process.argv[2] ??
    path.join(
      repoRoot,
      "data",
      "annotations",
      "private",
      "round_20260824_role_text_calibration_v01",
      "text",
    ),
);
const outputDir = path.resolve(
  process.argv[3] ??
    path.join(
      repoRoot,
      "outputs",
      "01a03c9d-b209-7523-9c76-146e456b71de",
      "文本共同校准",
    ),
);
const renderDir = path.join(outputDir, "renders");

const CURRENT_CODEBOOK_VERSION = "v3.16.0";
const SOURCE_CODEBOOK_VERSION = "v3.13.0";
const WORKBOOK_SCHEMA_VERSION = "text-calibration-workbook-v2.0";

const coderJobs = [
  {
    key: "A",
    sourceName: "coder_a-calibration-coding.csv",
    outputName: "编码员A文本共同校准.xlsx",
  },
  {
    key: "B",
    sourceName: "coder_b-calibration-coding.csv",
    outputName: "编码员B文本共同校准.xlsx",
  },
];

const headers = [
  "原子判断编号\nannotation_id",
  "任务编号\nitem_id",
  "帖子编号\npost_id",
  "观察单位\nunit_type",
  "片段或帖子编号\nunit_id",
  "平台\nplatform",
  "待判断原文\nraw_text",
  "维度\ndimension_code",
  "字段名\nfield_name",
  "中文标签\nfield_label_zh",
  "允许填写的值\nvalid_values",
  "是否主观判断\nis_subjective",
  "判断结果（必填）\nlabel_value",
  "有疑问填?\nreview_flag",
  "疑问原因\nreason_code",
  "合理替代值\nalternative_values",
  "疑问一句说明\nreview_note",
  "最短充分证据\nevidence_quote",
  "重复证据起点\nevidence_start_if_repeated",
  "额外问题类型\nother_issue_type",
  "额外问题说明\nother_issue_note",
  "编码员编号\nannotator_id",
  "完成时间\nannotated_at",
  "编码表版本\ncodebook_version",
  "工作表契约版本\ntemplate_schema_version",
];

const colors = {
  navy: "#17365D",
  blue: "#D9EAF7",
  green: "#E2F0D9",
  yellow: "#FFF2CC",
  orange: "#FCE4D6",
  gray: "#E7E6E6",
  white: "#FFFFFF",
  border: "#B7C9DA",
  text: "#1F2937",
};

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

async function readSha256(filePath) {
  return sha256(await fs.readFile(filePath));
}

function lastRowFromAddress(address) {
  const match = /:[A-Z]+(\d+)$/.exec(address);
  if (!match) {
    throw new Error(`无法从工作表范围解析末行：${address}`);
  }
  return Number(match[1]);
}

function nonBlank(value) {
  return value !== null && value !== undefined && String(value).trim() !== "";
}

function questionFor(dimension, fieldName, label) {
  const exact = {
    has_commercial_disclosure:
      "整篇帖子是否直接写明广告、赞助、合作、受邀体验或推广？",
    destination_type: "整篇帖子主要属于哪一种目的地类型？",
    at_has_info: "这段话是否陈述了可以核验的事实？",
    at_has_eval: "这段话是否明确表达好坏、价值、满意或不满？",
    at_eval_subtype: "如果有评价，它最符合哪一种评价感受？",
    at_has_sug: "这段话是否提出建议、推荐、提醒或劝阻？",
    at_non: "这段话是否没有命中信息、评价和建议？",
    sentiment_judgeable: "评价或建议的情感方向是否能够判断？",
    sentiment_dir: "情感方向是正面、负面、混合还是真实中性？",
    sentiment_pos_val: "正面情感强度是0、1还是2？",
    sentiment_neg_val: "负面情感强度是0、1还是2？",
    asp_applicable: "这段话是否在评价或建议某个目的地属性？",
  };
  if (exact[fieldName]) return exact[fieldName];
  if (dimension === "V3") return `这段话是否使用了“${label}”这种表达方式？`;
  if (dimension === "V4") return `这段话是否直接提到或调用了“${label}”？`;
  if (dimension === "V5") return `这段话的语言或情感是否属于“${label}”？`;
  if (dimension === "V6") return `这段评价或建议是否指向“${label}”？`;
  return `当前材料是否符合“${label}”？`;
}

function reminderFor(dimension, fieldName) {
  if (dimension === "V1") return "只有明确披露才填1；品牌出现或营销语气不够。";
  if (dimension === "V2") return "按整篇帖子判断；系统预填信息不要修改。";
  if (dimension === "V3") return "多个标签可以同时为1；父类和子类都由人填写。";
  if (dimension === "V4") return "只标片段实际提到的资源；事件还要判断事件子类。";
  if (fieldName === "asp_applicable") return "先判断适用性；为0时其余V6字段全部NA。";
  if (dimension === "V6") return "只记录评价对象；正负方向由V5记录。";
  if (fieldName.startsWith("sentiment_")) {
    return "没有评价或建议时通常不适用；不要自行计算净情感。";
  }
  if (dimension === "V5") return "信息、评价和建议可以同时为1。";
  return "按当前片段和完整帖子语境判断。";
}

function applyTableStyle(range, fill = colors.white) {
  range.format = {
    fill,
    font: { color: colors.text, size: 10 },
    verticalAlignment: "top",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: colors.border },
  };
}

function buildInstructionSheet(workbook, coderKey) {
  const sheet = workbook.worksheets.add("开始这里");
  sheet.showGridLines = false;
  sheet.mergeCells("A1:H1");
  sheet.getRange("A1:H1").format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white, size: 16 },
    verticalAlignment: "center",
  };
  sheet.getRange("A1").values = [[`编码员${coderKey}：文本共同校准`]];
  sheet.getRange("A3:H8").values = [
    ["项目状态", "PILOT_ONLY", "任务阶段", "COMMON_CALIBRATION", "编码表", CURRENT_CODEBOOK_VERSION, "工作簿", WORKBOOK_SCHEMA_VERSION],
    ["你需要懂论文吗", "不需要", "你需要懂模型吗", "不需要", "你的任务", "按材料逐项判断", "第一原则", "只按证据，不猜研究期待"],
    ["个人文件", `只允许编码员${coderKey}填写`, "另一编码员答案", "锁定前不得查看", "讨论时点", "两份原始文件锁定后", "空白", "表示没有完成，不等于0"],
    ["文本顺序", "先V1—V2，再逐片段V3→V4→V5→V6", "片段边界", "不得修改seg_id", "多标签", "可以同时为1", "父类/子类", "全部由人填写"],
    ["疑问标记", "判断拿不准时在对应字段填?", "没有疑问", "保持空白，不必逐项打分", "阳性证据", "复制最短充分原文", "字符位置", "不需要手算"],
    ["系统参与", "人工阶段无模型建议、补值或纠错", "保存方式", "不覆盖旧文件", "交表前", "完成下方检查", "图片任务", "当前不要填写"],
  ];
  applyTableStyle(sheet.getRange("A3:H8"));
  sheet.getRange("A3:H3").format.fill = colors.blue;

  sheet.getRange("A10:H10").values = [["按这个顺序完成每一篇帖子"]];
  sheet.mergeCells("A10:H10");
  sheet.getRange("A10:H10").format = {
    fill: colors.blue,
    font: { bold: true, color: colors.navy, size: 12 },
  };
  sheet.getRange("A11:H18").values = [
    ["1", "先做整篇帖子", "V1明确商业披露", "V2主要目的地类型", "不要用品牌出现推断合作", "—", "—", "—"],
    ["2", "逐个seg_id", "先读当前片段", "必要时读完整帖子语境", "不改片段", "—", "—", "—"],
    ["3", "V3内容策略", "这段话怎么表达", "信息/解释/渲染/互动/其他", "可共现", "父类和子类都填", "—", "—"],
    ["4", "V4旅游资源", "这段话提到什么", "自然/人文及具体子类", "事件再填事件子类", "—", "—", "—"],
    ["5", "V5语言与情感", "信息/评价/建议/其他", "再判断方向和双向强度", "信息评价建议可共现", "—", "—", "—"],
    ["6", "V6目的地属性", "先判断是否适用", "再判断评价针对哪一方面", "为0时其余V6填NA", "—", "—", "—"],
    ["7", "逐字段补充", "拿不准才标?", "阳性摘最短证据", "标?时补原因和一句说明", "—", "—", "—"],
    ["8", "保存本批", "检查空白和疑问记录", "保存个人原始文件", "负责人锁定前不讨论", "—", "—", "—"],
  ];
  applyTableStyle(sheet.getRange("A11:H18"));

  sheet.getRange("A20:H20").values = [["判断值快速说明"]];
  sheet.mergeCells("A20:H20");
  sheet.getRange("A20:H20").format = {
    fill: colors.green,
    font: { bold: true, color: colors.navy, size: 12 },
  };
  sheet.getRange("A21:H26").values = [
    ["1", "有直接证据", "0", "适用但检查后没有", "NA", "前置条件没触发，不适用", "空白", "还没做"],
    ["UNK", "应该判断但材料不足；仅允许字段使用", "UNRESOLVED", "共同校准时规则无法唯一解决", "疑问标记?", "仅在拿不准时填写", "UNRESOLVED", "必须同时标?并说明"],
    ["无疑问", "review_flag留空", "有疑问", "review_flag填?", "原因", "从预设原因中选", "说明", "用一句话写清难点"],
    ["BOUNDARY", "标签边界不清", "CONTEXT", "依赖上下文", "CONFLICT", "证据冲突", "EVIDENCE_MISSING", "材料缺失"],
    ["证据原文", "从raw_text原样复制", "否定/转折", "会改义时必须一并复制", "重复位置", "同一引文重复时才填", "JSON/offset", "编码员不填写"],
    ["规则解决不了", "填UNRESOLVED并说明", "片段切分有误", "登记UNITIZATION_PROBLEM", "没有新标签", "不得自行新增", "疑问顺序", "先看标签速查，再查编码簿"],
  ];
  applyTableStyle(sheet.getRange("A21:H26"));

  sheet.getRange("A28:H28").values = [["交表前检查"]];
  sheet.mergeCells("A28:H28");
  sheet.getRange("A28:H28").format = {
    fill: colors.orange,
    font: { bold: true, color: colors.navy, size: 12 },
  };
  sheet.getRange("A29:H33").values = [
    ["□", "没有查看另一人的答案", "□", "应填字段没有空白", "□", "0/NA/UNK/UNRESOLVED没有混用", "□", "没有修改系统字段"],
    ["□", "拿不准的字段已标?", "□", "所有?都有原因和说明", "□", "阳性标签尽量有最短证据", "□", "没有修改seg_id或原文"],
    ["□", "没有发明新标签", "□", "没有使用模型建议", "□", "保存的是自己的文件", "□", "没有覆盖上一版原始文件"],
    ["完整说明", "docs/protocols/人工编码员操作指南.md", "编码定义", "docs/data-dictionary/编码簿_青岛旅游UGC编码框架.md", "冲突时", "以编码表v3.16.0为准", "图片轨", "当前不启动"],
    ["重要", "共同校准允许发现问题并修订规则", "但", "本批不计算正式信度", "下一步", "规则冻结后使用全新样本盲试标", "当前答案", "必须完整保留"],
  ];
  applyTableStyle(sheet.getRange("A29:H33"));

  const widths = [8, 25, 18, 25, 18, 25, 18, 25];
  widths.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, 33, 1).format.columnWidth = width;
  });
  sheet.getRange("A1:H33").format.wrapText = true;
  sheet.getRange("A1:H33").format.verticalAlignment = "top";
  sheet.getRange("A1:H1").format.rowHeight = 30;
  sheet.freezePanes.freezeRows(1);
  return sheet;
}

function styleCodingSheet(sheet, rowCount, coderKey) {
  sheet.showGridLines = false;
  sheet.getRange("A1:Y1").values = [headers];
  sheet.getRange("A1:Y1").format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white, size: 10 },
    verticalAlignment: "center",
    horizontalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: colors.border },
    rowHeight: 52,
  };
  sheet.getRange(`A2:L${rowCount}`).format.fill = colors.gray;
  sheet.getRange(`M2:U${rowCount}`).format.fill = colors.yellow;
  sheet.getRange(`V2:V${rowCount}`).format.fill = colors.blue;
  sheet.getRange(`W2:W${rowCount}`).format.fill = colors.yellow;
  sheet.getRange(`X2:Y${rowCount}`).format.fill = colors.gray;
  sheet.getRange(`A2:Y${rowCount}`).format.font = { color: colors.text, size: 9 };
  sheet.getRange(`G2:G${rowCount}`).format.wrapText = true;
  sheet.getRange(`J2:K${rowCount}`).format.wrapText = true;
  sheet.getRange(`Q2:R${rowCount}`).format.wrapText = true;
  sheet.getRange(`U2:U${rowCount}`).format.wrapText = true;
  sheet.getRange(`N2:N${rowCount}`).dataValidation = {
    rule: { type: "list", values: ["?"] },
  };
  sheet.getRange(`O2:O${rowCount}`).dataValidation = {
    rule: {
      type: "list",
      values: ["BOUNDARY", "CONTEXT", "CONFLICT", "EVIDENCE_MISSING", "OTHER"],
    },
  };
  sheet.getRange(`T2:T${rowCount}`).dataValidation = {
    rule: {
      type: "list",
      values: [
        "FRAMEWORK_GAP",
        "BOUNDARY_CASE",
        "COUNTEREXAMPLE",
        "CONTEXT_DEPENDENCE",
        "UNITIZATION_PROBLEM",
      ],
    },
  };

  const widths = [
    20, 18, 18, 14, 16, 11, 42, 10, 24, 22, 30, 12, 18, 12, 18, 18, 34,
    32, 16, 24, 34, 14, 22, 14, 25,
  ];
  widths.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, rowCount, 1).format.columnWidth = width;
  });
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(7);
  const table = sheet.tables.add(`A1:Y${rowCount}`, true, `CalibrationCoding${coderKey}`);
  table.style = "TableStyleMedium2";
  table.showBandedRows = true;
  table.showFilterButton = true;
}

function buildQuickReference(workbook, referenceRows) {
  const sheet = workbook.worksheets.add("标签速查");
  sheet.showGridLines = false;
  sheet.getRange("A1:G1").values = [[
    "维度",
    "字段名",
    "中文标签",
    "允许填写",
    "最直白的判断问题",
    "关键提醒",
    "是否允许疑问标记",
  ]];
  sheet.getRange("A2:G2").values = [[
    "使用方法",
    "先在主表看field_name",
    "再查本页同名行",
    "只能使用列出的值",
    "按问题回答，不猜研究期待",
    "仍不确定就查编码簿或记录UNRESOLVED",
    "1=拿不准时可填?；0=不得填写",
  ]];
  const values = referenceRows.map((row) => [
    row.dimension,
    row.fieldName,
    row.label,
    row.validValues,
    questionFor(row.dimension, row.fieldName, row.label),
    reminderFor(row.dimension, row.fieldName),
    row.isSubjective === "1" ? "是" : "否",
  ]);
  sheet.getRange(`A3:G${values.length + 2}`).values = values;
  applyTableStyle(sheet.getRange(`A1:G${values.length + 2}`));
  sheet.getRange("A1:G1").format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white },
    verticalAlignment: "center",
    horizontalAlignment: "center",
    wrapText: true,
  };
  sheet.getRange("A2:G2").format.fill = colors.yellow;
  const widths = [10, 27, 24, 34, 48, 42, 18];
  widths.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, values.length + 2, 1).format.columnWidth = width;
  });
  sheet.getRange(`A1:G${values.length + 2}`).format.wrapText = true;
  sheet.freezePanes.freezeRows(2);
  const table = sheet.tables.add(
    `A1:G${values.length + 2}`,
    true,
    "LabelQuickReference",
  );
  table.style = "TableStyleMedium4";
  table.showFilterButton = true;
  return sheet;
}

function reorderWorksheets(workbook, orderedNames) {
  // CSV导入必须从空工作簿开始，因而主表会先被创建。导出前通过同一
  // artifact-tool工作簿协议重排，让编码员打开文件时先看到“开始这里”。
  const order = new Map(orderedNames.map((name, index) => [name, index]));
  const proto = workbook.toProto();
  proto.sheets.sort((left, right) => {
    const leftIndex = order.get(left.name) ?? orderedNames.length;
    const rightIndex = order.get(right.name) ?? orderedNames.length;
    return leftIndex - rightIndex;
  });
  proto.sheets.forEach((sheet, index) => {
    sheet.index = index;
  });
  return Workbook.load(proto);
}

async function ensureOutputDoesNotExist(filePath) {
  try {
    await fs.access(filePath);
  } catch {
    return;
  }
  throw new Error(`拒绝覆盖已有人工工作簿：${filePath}`);
}

async function buildWorkbook(job) {
  const sourcePath = path.join(sourceDir, job.sourceName);
  const outputPath = path.join(outputDir, job.outputName);
  await ensureOutputDoesNotExist(outputPath);
  const csvText = await fs.readFile(sourcePath, "utf8");
  // CSV导入要求空工作簿，因此先导入主表，再追加说明和速查页。
  let workbook = await Workbook.fromCSV(csvText, { sheetName: "共同校准主表" });
  buildInstructionSheet(workbook, job.key);
  const codingSheet = workbook.worksheets.getItem("共同校准主表");
  const rowCount = lastRowFromAddress(codingSheet.getUsedRange().address);

  const inputCells = codingSheet.getRange(`M2:U${rowCount}`).values;
  if (inputCells.some((row) => row.some(nonBlank))) {
    throw new Error(`${job.sourceName}已包含人工响应，不能自动改签版本`);
  }
  const sourceVersions = codingSheet.getRange(`X2:X${rowCount}`).values.flat();
  if (sourceVersions.some((value) => String(value).trim() !== SOURCE_CODEBOOK_VERSION)) {
    throw new Error(`${job.sourceName}存在非${SOURCE_CODEBOOK_VERSION}来源记录`);
  }
  codingSheet.getRange(`X2:X${rowCount}`).values = Array.from(
    { length: rowCount - 1 },
    () => [CURRENT_CODEBOOK_VERSION],
  );
  codingSheet.getRange(`Y2:Y${rowCount}`).values = Array.from(
    { length: rowCount - 1 },
    () => ["calibration-coding-v2.0"],
  );

  const referenceCells = codingSheet.getRange(`H2:L${rowCount}`).values;
  const seen = new Set();
  const referenceRows = [];
  for (const row of referenceCells) {
    const [dimension, fieldName, label, validValues, isSubjective] = row.map(
      (value) => String(value ?? "").trim(),
    );
    if (!fieldName || seen.has(fieldName)) continue;
    seen.add(fieldName);
    referenceRows.push({ dimension, fieldName, label, validValues, isSubjective });
  }
  buildQuickReference(workbook, referenceRows);
  styleCodingSheet(codingSheet, rowCount, job.key);
  workbook = reorderWorksheets(workbook, ["开始这里", "共同校准主表", "标签速查"]);

  const keyInspection = await workbook.inspect({
    kind: "region",
    sheetId: "共同校准主表",
    range: "A1:Y12",
    maxChars: 5000,
    tableMaxRows: 12,
    tableMaxCols: 25,
    tableMaxCellChars: 100,
  });
  const errorScan = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: `编码员${job.key}公式错误扫描`,
  });

  for (const [sheetName, range, fileStem] of [
    ["开始这里", "A1:H33", `编码员${job.key}-开始这里`],
    ["共同校准主表", "A1:U14", `编码员${job.key}-共同校准主表`],
    ["标签速查", "A1:G24", `编码员${job.key}-标签速查`],
  ]) {
    const preview = await workbook.render({
      sheetName,
      range,
      scale: 1.2,
      format: "png",
    });
    await fs.writeFile(
      path.join(renderDir, `${fileStem}.png`),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }

  const exported = await SpreadsheetFile.exportXlsx(workbook);
  await exported.save(outputPath);
  const outputBytes = await fs.readFile(outputPath);
  return {
    coder: job.key,
    source_path: path.relative(repoRoot, sourcePath),
    source_sha256: sha256(Buffer.from(csvText, "utf8")),
    output_path: path.relative(repoRoot, outputPath),
    output_sha256: sha256(outputBytes),
    row_count: rowCount - 1,
    unique_field_count: referenceRows.length,
    inspect: keyInspection.ndjson ?? String(keyInspection),
    formula_error_scan: errorScan.ndjson ?? String(errorScan),
  };
}

await fs.mkdir(renderDir, { recursive: true });
const outputs = [];
for (const job of coderJobs) {
  outputs.push(await buildWorkbook(job));
}

const sourceSegments = path.join(sourceDir, "segments.csv");
const sourceItems = path.join(sourceDir, "items.csv");
const manifest = {
  artifact_type: "TEXT_COMMON_CALIBRATION_WORKBOOKS",
  status: "PILOT_ONLY",
  codebook_version: CURRENT_CODEBOOK_VERSION,
  workbook_schema_version: WORKBOOK_SCHEMA_VERSION,
  generated_at: new Date().toISOString(),
  source_round: "round_20260824_role_text_calibration_v01",
  source_codebook_version: SOURCE_CODEBOOK_VERSION,
  semantic_migration: {
    text_fields_changed: false,
    text_values_changed: false,
    posts_changed: false,
    segments_changed: false,
    human_responses_changed: false,
    reason:
      "v3.16.0未改变V1—V6标签和值域；仅把逐字段五级置信度改为可选疑问标记。",
    v0_files_included: false,
  },
  source_facts: {
    items_path: path.relative(repoRoot, sourceItems),
    items_sha256: await readSha256(sourceItems),
    segments_path: path.relative(repoRoot, sourceSegments),
    segments_sha256: await readSha256(sourceSegments),
  },
  outputs,
};
await fs.writeFile(
  path.join(outputDir, "文本共同校准清单.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
  "utf8",
);

process.stdout.write(`${JSON.stringify(manifest, null, 2)}\n`);
