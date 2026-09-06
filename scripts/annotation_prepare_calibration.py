#!/usr/bin/env python3
"""校验共同校准主表，并导出规范标签与负责人问题清单。"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Iterable, Mapping

from tourism_ugc_study.annotation.calibration import (
    CALIBRATION_INPUT_FIELDS,
    ISSUE_OUTPUT_FIELDS,
    LABEL_OUTPUT_FIELDS,
    convert_calibration_rows,
)


def _normalized_header(cell: str) -> str:
    """兼容Excel中的“中文名称+换行+字段名”双语表头。"""

    parts = tuple(part.strip() for part in cell.splitlines() if part.strip())
    for part in reversed(parts):
        if part in CALIBRATION_INPUT_FIELDS or part == "row_check":
            return part
    return cell.strip()


def _read_rows(path: Path) -> tuple[dict[str, str], ...]:
    """读取CSV，并容忍填写版Excel导出的标题说明行和双语表头。"""

    with path.open(encoding="utf-8-sig", newline="") as stream:
        csv_rows = tuple(csv.reader(stream))
    expected = tuple(CALIBRATION_INPUT_FIELDS)
    for header_index, cells in enumerate(csv_rows):
        header = tuple(_normalized_header(cell) for cell in cells)
        if header[: len(expected)] != expected:
            continue
        return tuple(
            dict(zip(header, row, strict=False))
            for row in csv_rows[header_index + 1 :]
        )
    raise ValueError(f"未找到共同校准主表表头：{path}")


def _write_rows(
    path: Path, fieldnames: Iterable[str], rows: Iterable[Mapping[str, object]]
) -> None:
    """创建父目录并以稳定列序写出UTF-8 CSV。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    """构建无隐式默认输入路径的命令行接口。"""

    parser = argparse.ArgumentParser(
        description="把编码员共同校准主表转换为规范labels与负责人问题清单"
    )
    parser.add_argument("input_csv", type=Path, help="编码员导出的共同校准主表CSV")
    parser.add_argument("--labels-output", type=Path, required=True)
    parser.add_argument("--issues-output", type=Path, required=True)
    parser.add_argument("--codebook-version", default="v3.16.0")
    return parser


def main() -> None:
    """执行只读输入、确定性转换和两个显式输出。"""

    args = build_parser().parse_args()
    conversion = convert_calibration_rows(
        _read_rows(args.input_csv),
        expected_codebook_version=args.codebook_version,
    )
    _write_rows(args.labels_output, LABEL_OUTPUT_FIELDS, conversion.labels)
    _write_rows(args.issues_output, ISSUE_OUTPUT_FIELDS, conversion.issues)


if __name__ == "__main__":
    main()
