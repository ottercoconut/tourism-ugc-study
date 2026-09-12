#!/usr/bin/env python3
"""服务器单批启动/观察/回收入口；只处理固定轮次内的既有批次。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.research_remote_worker import (
    bind_remote_batch, inspect_remote_batch, launch_remote_batch,
    release_remote_batch, run_remote_batch,
)


def main() -> None:
    """解析无密码参数；run模式不写额外标准输出，保持传输日志封存后不可变。"""
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("launch", "inspect", "run", "release"))
    parser.add_argument("--round-root", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--accepted-transfer-sha256")
    args = parser.parse_args()
    ctx = bind_remote_batch(Path(__file__).resolve().parents[1], args.round_root,
                            args.expected_manifest_sha256, args.batch)
    if args.action == "run":
        run_remote_batch(ctx)
        return
    if args.action == "launch":
        result = launch_remote_batch(ctx)
    elif args.action == "inspect":
        result = inspect_remote_batch(ctx)
    else:
        result = release_remote_batch(ctx, args.accepted_transfer_sha256)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
