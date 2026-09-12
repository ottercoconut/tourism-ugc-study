#!/usr/bin/env python3
"""核验全量清洗结果并生成独立候选库和人工审查材料，不发布最终keep。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.research_round_result_artifacts import assemble_round


def main() -> None:
    """要求已提交装配代码和显式输出目录；成功后只打印无正文汇总。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--round-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args()
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip():
        raise ValueError("round_result_worktree_dirty")
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    result = assemble_round(args.round_root, args.output, args.expected_manifest_sha256,
                            workspace=args.workspace, code_version=version)
    print(json.dumps({key: value for key, value in result.items() if key != "batch_receipts"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
