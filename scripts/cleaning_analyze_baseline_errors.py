#!/usr/bin/env python3
"""只分析冻结 baseline 的训练 OOF 与验证误差，不触碰锁定测试。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.models.text.baseline_error_analysis import (
    BaselineErrorAnalysisError,
    analyze_baseline_development_errors,
    apply_error_type_coding,
    render_baseline_development_error_summary,
)
from tourism_ugc_study.models.text.formal_training import FormalTrainingError


def _parser() -> argparse.ArgumentParser:
    """构造开发误差分析命令行。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--reference-csv", type=Path, required=True)
    parser.add_argument(
        "--error-coding",
        type=Path,
        help="可选的 finalized 人工错误类型编码 JSON",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    """运行去敏开发误差分析并原子写入 JSON artifact。

    Returns:
        成功时为 ``0``；证据或写入失败时为 ``2``。
    """

    args = _parser().parse_args()
    try:
        analysis = analyze_baseline_development_errors(
            args.package_dir,
            args.reference_csv,
        )
        if args.error_coding is not None:
            coding = json.loads(args.error_coding.read_text(encoding="utf-8"))
            if not isinstance(coding, dict):
                raise BaselineErrorAnalysisError(
                    "baseline_analysis_error_coding_contract_invalid"
                )
            analysis = apply_error_type_coding(analysis, coding)
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_text(
            json.dumps(analysis, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)
        print(render_baseline_development_error_summary(analysis))
    except (
        BaselineErrorAnalysisError,
        FormalTrainingError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason_code": getattr(
                        exc,
                        "reason_code",
                        "baseline_analysis_io_failed",
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
