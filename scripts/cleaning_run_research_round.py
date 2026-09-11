#!/usr/bin/env python3
"""以已提交的固定代码运行封存的新研究人口批次，不训练、不发布最终keep。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.research_round_execution import execute_round


def main() -> None:
    """解析显式运行绑定；仅重试开关允许恢复曾失败的调度。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--round-root", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    execute_round(args.workspace, Path(__file__).resolve().parents[1], args.round_root,
                  args.expected_manifest_sha256, retry_failed=args.retry_failed)


if __name__ == "__main__":
    main()
