#!/usr/bin/env python3
"""封存新一轮全量清洗输入、人工证据匹配、去重候选与冻结模型批次。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.research_round_artifacts import prepare_round


def main() -> None:
    """只处理显式新目录；要求已提交的代码身份，数据不进入Git。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("configs/cleaning-research-round.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip():
        raise ValueError("round_worktree_dirty")
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    result = prepare_round(args.workspace, args.config, args.output, code_version=version)
    print(json.dumps({key: value for key, value in result.items() if key not in ("round_config", "batches", "source_pointer")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
