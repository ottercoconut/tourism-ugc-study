#!/usr/bin/env python3
"""汇总V0共同校准或独立盲试标的分歧、支持数与信度。"""

from __future__ import annotations

import argparse
from pathlib import Path

from tourism_ugc_study.annotation.role_pilot_reporting import (
    summarize_role_round,
    write_role_summary,
)


def build_parser() -> argparse.ArgumentParser:
    """建立不覆盖原始编码和既有汇总的命令行接口。"""

    parser = argparse.ArgumentParser(description="汇总两份V0作者身份编码")
    parser.add_argument("--coder-a", type=Path, required=True)
    parser.add_argument("--coder-b", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("CALIBRATION", "BLIND_PILOT"), default="CALIBRATION"
    )
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--disagreements-output", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260824)
    return parser


def main() -> None:
    """执行只读汇总，并把新结果写入独立追加文件。"""

    args = build_parser().parse_args()
    summary, discrepancies = summarize_role_round(
        args.coder_a,
        args.coder_b,
        args.coverage,
        mode=args.mode,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
    )
    write_role_summary(
        args.summary_output,
        args.disagreements_output,
        summary,
        discrepancies,
    )


if __name__ == "__main__":
    main()
