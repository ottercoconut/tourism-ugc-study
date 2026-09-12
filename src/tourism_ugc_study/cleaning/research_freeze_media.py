"""按原始内容快照归档全部已下载正文图片，不下载新媒体、不读取控制面。

媒体字节必须匹配数据库SHA；APFS克隆使用独立inode，保留原relative local_path，
从而可用新的media_root解析。任一缺失、路径逃逸或摘要不符均停止，不删源文件。
"""

from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from itertools import batched
from pathlib import Path
from typing import Any, Callable

from .research_freeze_files import clone_file, owned_path
from .research_round_artifacts import write_json
from .source_snapshot import file_sha256


def archive_media(database: Path, source_root: Path, destination: Path,
                  *, progress: Callable[[str], None] = print) -> dict[str, Any]:
    """克隆新目录并逐文件验SHA；已有目标拒绝覆盖，失败保留部分输出供检查。

    source_root是采集仓库根，destination是独立媒体解析根。JSON清单含私有路径，
    只能存入Git忽略的归档目录。最多4个并发任务，内存队列有界，不占用91GiB副本。
    """
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("freeze_media_destination_exists")
    source_root = source_root.resolve(strict=True)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute("SELECT local_path,sha256 FROM web_post_images ORDER BY local_path,id").fetchall()
    expected = {}
    for relative, digest in rows:
        if not isinstance(relative, str) or Path(relative).parts[:2] != ("data", "media"):
            raise ValueError("freeze_media_path_invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("freeze_media_sha256_missing_or_invalid")
        if relative in expected and expected[relative] != digest:
            raise ValueError("freeze_media_path_hash_conflict")
        expected[relative] = digest
    destination.mkdir(parents=True)

    def archive(item: tuple[str, str]) -> dict[str, Any]:
        relative, digest = item
        original = owned_path(source_root, relative)
        target = destination / relative
        before = original.stat()
        clone_file(original, target)
        if file_sha256(target) != digest:
            raise ValueError("freeze_media_content_hash_mismatch")
        after = original.stat()
        if (before.st_mode, before.st_flags, before.st_size, before.st_mtime_ns) != (
                after.st_mode, after.st_flags, after.st_size, after.st_mtime_ns):
            raise ValueError("freeze_media_source_changed_during_clone")
        return {"local_path": relative, "sha256": digest, "size_bytes": target.stat().st_size}

    entries = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch in batched(expected.items(), 128):
            for entry in pool.map(archive, batch):
                entries.append(entry)
                if len(entries) % 5000 == 0:
                    progress(f"media_cloned_and_hashed={len(entries)}/{len(expected)}")
    inventory = destination.parent / "media-inventory.json"
    write_json(inventory, {"artifact_kind": "raw-media-byte-inventory", "entries": entries})
    return {"relationship_count": len(rows), "file_count": len(entries),
            "logical_bytes": sum(row["size_bytes"] for row in entries),
            "inventory_sha256": file_sha256(inventory),
            "storage_method": "APFS_clonefile_independent_inodes_copy_on_write",
            "resolution": "media-root / web_post_images.local_path",
            "missing_count": 0, "hash_mismatch_count": 0, "source_write_count": 0}
