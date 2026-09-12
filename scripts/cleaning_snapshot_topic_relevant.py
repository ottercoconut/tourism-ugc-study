#!/usr/bin/env python3
"""将源库 topic_relevant=1 的内容登记为当前研究输入；不清洗、不推理。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.source_snapshot import create_current_snapshot


def main() -> None:
    """解析显式源/目标路径，调用快照持久化并输出不含正文的摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--current-pointer", type=Path, default=Path("data/processed/current-source.json"))
    args = parser.parse_args()
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        version += "+dirty"
    result = create_current_snapshot(args.source_db, args.output_db, args.current_pointer, code_version=version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
