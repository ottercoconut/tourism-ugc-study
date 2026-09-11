"""远端清洗的SSH/Rsync传输适配，不持有密码、不决定模型或业务状态。

使用用户已认证的SSH连接；网络失败直接抛出而不重新启动推理。Rsync只复制明确
路径，不使用删除选项；删除必须由远端回执核验接口另行执行。
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResearchSSHTransport:
    """无凭证连接配置；code/root分别是服务器固定代码和该轮私有目录。"""

    destination: str
    port: int
    control_path: Path
    code: Path
    root: Path
    manifest_sha256: str

    def ssh_command(self) -> list[str]:
        """返回固定SSH选项；不允许交互等待密码或向服务器转发认证代理。"""
        return ["ssh", "-p", str(self.port), "-o", "BatchMode=yes", "-o", "ForwardAgent=no",
                "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                "-o", "ControlPath=" + str(self.control_path)]

    def action(self, action: str, batch: str, accepted_sha256: str | None = None) -> dict[str, Any]:
        """调用服务器唯一批次接口；命令参数全部shell引用，不接受任意远端代码。"""
        command = [str(self.code / ".venv/bin/python"),
                   str(self.code / "scripts/cleaning_remote_batch.py"), action,
                   "--round-root", str(self.root), "--expected-manifest-sha256", self.manifest_sha256,
                   "--batch", batch]
        if accepted_sha256 is not None:
            command.extend(["--accepted-transfer-sha256", accepted_sha256])
        result = subprocess.run(self.ssh_command() + [self.destination, shlex.join(command)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"remote_{action}_exit_{result.returncode}:" + result.stderr[-1500:])
        return json.loads(result.stdout)

    def copy(self, source: str, destination: str) -> None:
        """通过已认证连接复制文件；断连保留部分文件供相同任务恢复，绝不删除源。"""
        subprocess.run(["rsync", "-rtp", "--checksum", "--partial", "--timeout=90",
                        "-e", shlex.join(self.ssh_command()), source, destination],
                       stdin=subprocess.DEVNULL, check=True, timeout=300)

    def upload_input(self, local_root: Path, filename: str) -> None:
        """只上传当前批次；远端父目录须已在部署阶段创建。"""
        self.copy(str(local_root / filename), f"{self.destination}:{self.root / filename}")

    def download_result(self, local_root: Path, name: str) -> Path:
        """回传完整原生输出、日志和封存清单；本方法不声称内容已通过验收。"""
        output = local_root / "inference" / name
        output.mkdir(parents=True, exist_ok=True)
        receipts = local_root / "remote-receipts"
        receipts.mkdir(exist_ok=True)
        self.copy(f"{self.destination}:{self.root / 'inference' / name}/", str(output) + "/")
        self.copy(f"{self.destination}:{self.root / 'logs'}/", str(local_root / "logs") + "/")
        manifest = receipts / f"{name}-transfer.json"
        self.copy(f"{self.destination}:{self.root / 'remote-jobs' / name / 'transfer-manifest.json'}", str(manifest))
        return manifest
