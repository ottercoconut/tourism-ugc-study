"""研究封存的文件边界、独立克隆及不可变保护；不决定研究标签或人口。

所有路径限于调用方显式指定的深层资产根；拒绝符号链接、硬链接、特殊文件和
SQLite旁文件。macOS的uchg用于防止误写，不被解释为不可撤销的硬件WORM存储。
"""

from __future__ import annotations

import ctypes
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from itertools import batched
from pathlib import Path
from typing import Any

from .source_snapshot import file_sha256


def owned_path(workspace: Path, relative: str) -> Path:
    """解析data内至少三级或results内至少二级的精确资产根，拒绝逃逸和符号链接。"""
    item = Path(relative)
    if (item.is_absolute() or not item.parts or ".." in item.parts
            or item.parts[0] not in {"data", "results"}
            or len(item.parts) < (3 if item.parts[0] == "data" else 2)):
        raise ValueError("freeze_path_scope_invalid")
    path = workspace / item
    if path.resolve() != path or any(parent.is_symlink() for parent in (path, *path.parents) if parent != workspace.parent):
        raise ValueError("freeze_symlink_forbidden")
    return path


def clone_file(source: Path, destination: Path) -> None:
    """以APFS写时复制创建独立inode，不回退到硬链接或静默复制超大媒体库。

    目标必须不存在；不修改源权限/标志。非macOS或不支持克隆的文件系统抛出
    OSError/NotImplementedError，由调用方停止并报告存储边界。
    """
    if source.is_symlink() or not source.is_file() or destination.exists():
        raise ValueError("freeze_clone_source_or_destination_invalid")
    function = getattr(ctypes.CDLL(None, use_errno=True), "clonefile", None)
    if function is None:
        raise NotImplementedError("freeze_requires_clonefile")
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    function.restype = ctypes.c_int
    destination.parent.mkdir(parents=True, exist_ok=True)
    if function(os.fsencode(source), os.fsencode(destination), 0):
        raise OSError(ctypes.get_errno(), "freeze_clonefile_failed")
    if (source.stat().st_dev, source.stat().st_ino) == (destination.stat().st_dev, destination.stat().st_ino):
        raise ValueError("freeze_clone_not_independent")


def asset_files(workspace: Path, roots: list[str]) -> list[Path]:
    """枚举精确封存根的全部普通文件，拒绝嵌套根、别名、SQLite旁文件和缺失根。"""
    paths = [owned_path(workspace, value) for value in roots]
    if len(set(paths)) != len(paths) or any(a != b and a in b.parents for a in paths for b in paths):
        raise ValueError("freeze_overlapping_roots")
    files = []
    for root in paths:
        if not root.exists():
            raise FileNotFoundError("freeze_asset_missing:" + str(root.relative_to(workspace)))
        for path in ([root] if root.is_file() else sorted(root.rglob("*"))):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError("freeze_nonregular_asset")
            if stat.S_ISREG(mode):
                if path.stat().st_nlink != 1 or path.name.endswith(("-wal", "-shm", "-journal")):
                    raise ValueError("freeze_hardlink_or_sqlite_sidecar")
                if path.suffix == ".sqlite" and any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                    raise ValueError("freeze_sqlite_sidecar_present")
                files.append(path)
    return sorted(files)


def inventory_files(workspace: Path, roots: list[str]) -> list[dict[str, Any]]:
    """生成有序全文件SHA清单，包含零字节锁文件；不把修改时间当作内容身份。"""
    def describe(path: Path) -> dict[str, Any]:
        return {"path": str(path.relative_to(workspace)), "sha256": file_sha256(path),
                "size_bytes": path.stat().st_size}

    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = []
        for batch in batched(asset_files(workspace, roots), 128):
            entries.extend(pool.map(describe, batch))
        return entries


def verify_inventory(workspace: Path, roots: list[str], entries: list[dict[str, Any]]) -> None:
    """按实际文件集合和字节摘要复核，任何新增、缺失、篡改或路径逃逸均失败。"""
    if inventory_files(workspace, roots) != entries:
        raise ValueError("freeze_inventory_mismatch")


def protect_assets(workspace: Path, roots: list[str]) -> None:
    """只对已校验资产设置0400/0500及uchg，目录自下而上；不提供自动解冻。

    操作可在同一清单下幂等继续；失败保留已经保护的资产，不撤销标志、不删数据。
    必须先完成内容校验；保护完成仍需调用verify_protection独立检查。
    """
    if not hasattr(os, "chflags"):
        raise NotImplementedError("freeze_requires_macos_uchg")
    files = asset_files(workspace, roots)
    directories = set()
    for value in roots:
        root = owned_path(workspace, value)
        if root.is_dir():
            directories.add(root)
            directories.update(path for path in root.rglob("*") if path.is_dir())
    for path in [*files, *sorted(directories, key=lambda p: len(p.parts), reverse=True)]:
        info = path.stat()
        expected = 0o500 if path.is_dir() else 0o400
        if info.st_flags & stat.UF_IMMUTABLE:
            if stat.S_IMODE(info.st_mode) != expected:
                raise ValueError("freeze_existing_immutable_mode_mismatch")
            continue
        os.chmod(path, expected)
        os.chflags(path, info.st_flags | stat.UF_IMMUTABLE)


def verify_protection(workspace: Path, roots: list[str]) -> int:
    """检查所有文件及根内目录的不可写位和uchg；返回受保护普通文件数。"""
    files = asset_files(workspace, roots)
    paths = set(files)
    for value in roots:
        root = owned_path(workspace, value)
        if root.is_dir():
            paths.add(root)
            paths.update(path for path in root.rglob("*") if path.is_dir())
    for path in paths:
        info = path.stat()
        expected = 0o500 if path.is_dir() else 0o400
        if stat.S_IMODE(info.st_mode) != expected or not getattr(info, "st_flags", 0) & stat.UF_IMMUTABLE:
            raise ValueError("freeze_asset_not_protected:" + str(path.relative_to(workspace)))
    return len(files)
