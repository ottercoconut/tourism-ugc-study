#!/usr/bin/env python3
"""等待已启动清洗完成，单次验收并发布待人工审查候选，不发布正式keep。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.research_round_completion import complete_when_ready


def main() -> None:
    """显式绑定已启动轮次和新输出目录，装配代码与推理代码分别记录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--round-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args()
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip():
        raise ValueError("round_completion_worktree_dirty")
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    complete_when_ready(args.workspace, args.round_root, args.output, args.expected_manifest_sha256,
                        code_version=version)


if __name__ == "__main__":
    main()
