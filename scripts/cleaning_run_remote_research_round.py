#!/usr/bin/env python3
"""本机启动服务器逐批推理与校验回传；SSH必须已认证，密码不进入参数或产物。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.research_remote_execution import execute_remote_round
from tourism_ugc_study.cleaning.research_remote_transport import ResearchSSHTransport


def main() -> None:
    """只解析运行路径与连接参数；核心状态和传输各由单一职责模块承担。"""
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--round-root", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--ssh-destination", required=True)
    parser.add_argument("--ssh-port", type=int, required=True)
    parser.add_argument("--ssh-control-path", type=Path, required=True)
    parser.add_argument("--remote-code-root", type=Path, required=True)
    parser.add_argument("--remote-round-root", type=Path, required=True)
    args = parser.parse_args()
    transport = ResearchSSHTransport(args.ssh_destination, args.ssh_port, args.ssh_control_path,
                                     args.remote_code_root, args.remote_round_root, args.expected_manifest_sha256)
    execute_remote_round(args.workspace, Path(__file__).resolve().parents[1], args.round_root,
                         args.expected_manifest_sha256, transport)


if __name__ == "__main__":
    main()
