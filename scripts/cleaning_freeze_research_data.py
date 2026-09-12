#!/usr/bin/env python3
"""准备、封存或只读校验原始语料与清洗候选；不执行推理或发布人工最终keep。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.research_freeze import prepare_freeze, seal_freeze, verify_freeze


def main() -> None:
    """变更动作需显式子命令，prepare要求干净已提交代码；verify不写任何文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--source-db", type=Path, required=True)
    prepare.add_argument("--source-root", type=Path, required=True)
    for action in ("seal", "verify"):
        command = actions.add_parser(action)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=args.workspace, text=True).strip():
            raise ValueError("freeze_prepare_requires_clean_committed_code")
        version = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.workspace, text=True).strip()
        result = prepare_freeze(args.workspace, args.config, args.source_db,
                                source_root=args.source_root, code_version=version)
    else:
        function = seal_freeze if args.action == "seal" else verify_freeze
        result = function(args.workspace, args.manifest, args.expected_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
