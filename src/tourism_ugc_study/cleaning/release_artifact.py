"""构建并校验不可变的数据清洗本地发布包。

本模块只负责纯文件发布边界：调用者必须先把研究结果压缩成去标识的
JSON 摘要，再显式提供运行号、发布号与输出根目录。模块不会查询数据库，
也不会推断“最新”发布；同一发布目标一旦存在便拒绝覆盖。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


_ARTIFACT_MANIFEST = "artifact-manifest.json"
_PAYLOAD_FILES = {
    "report.json": "report",
    "release-manifest.json": "manifest",
    "member-summaries.json": "member_summaries",
}
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_HEX_HASH = re.compile(r"[0-9a-fA-F]{8,128}\Z")
_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")
_URL = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)", re.IGNORECASE)
_SENSITIVE_KEY_TOKENS = {
    "author", "creator", "user", "account", "profile", "nickname", "username",
    "handle", "phone", "email", "token", "secret", "password", "credential",
    "cookie", "url", "uri", "link", "path", "raw", "text", "title", "caption",
    "body",
}
_OPERATIONAL_ID_KEYS = {
    "run_id", "release_id", "build_id", "decision_id", "cluster_id", "audit_id",
    "task_id", "batch_id", "model_id", "manifest_id", "record_id", "member_id",
}


class ReleaseArtifactError(RuntimeError):
    """表示发布目标冲突、输入越界或磁盘包校验失败。"""


@dataclass(frozen=True)
class ReleaseArtifactResult:
    """一次成功发布的稳定结果，不包含临时目录或其他机器绝对路径。"""

    artifact_dir: Path
    run_id: str
    release_id: str
    file_count: int


@dataclass(frozen=True)
class ArtifactVerificationResult:
    """磁盘发布包通过完整性与内容边界校验后的摘要。"""

    artifact_dir: Path
    run_id: str
    release_id: str
    file_count: int


def publish_release_artifact(
    *,
    output_root: str | Path,
    run_id: str,
    release_id: str,
    report: Any,
    manifest: Any,
    member_summaries: Any,
) -> ReleaseArtifactResult:
    """以同父目录临时写入和原子重命名发布三个去敏 JSON 摘要。

    ``run_id`` 与 ``release_id`` 都必须是单一路径段。发布前会递归检查调用者
    提供的数据，只接受计数、哈希、版本、理由码和明确声明为去标识的 ID。
    任一校验或写盘步骤失败都会清理临时目录；已有目标永远不会被覆盖。
    """

    safe_run_id = _validate_path_segment(run_id, "run_id")
    safe_release_id = _validate_path_segment(release_id, "release_id")
    payloads = {
        "report.json": report,
        "release-manifest.json": manifest,
        "member-summaries.json": member_summaries,
    }
    for filename, payload in payloads.items():
        _validate_payload(payload, location=filename)
        _serialize_json(payload, location=filename)

    root = Path(output_root)
    run_dir = root / safe_run_id
    target = run_dir / safe_release_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise ReleaseArtifactError(f"发布目标已存在，拒绝覆盖：{safe_run_id}/{safe_release_id}")

    lock_path = run_dir / f".{safe_release_id}.publish.lock"
    lock_fd: int | None = None
    temporary: Path | None = None
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ReleaseArtifactError(f"发布目标正在写入：{safe_run_id}/{safe_release_id}") from exc
        if target.exists() or target.is_symlink():
            raise ReleaseArtifactError(f"发布目标已存在，拒绝覆盖：{safe_run_id}/{safe_release_id}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{safe_release_id}.tmp-", dir=run_dir))
        entries: list[dict[str, object]] = []
        for filename, payload in payloads.items():
            path = temporary / filename
            _write_json(path, payload)
            entries.append(
                {"relative_path": filename, "sha256": _sha256(path), "size": path.stat().st_size}
            )
        artifact_manifest = {
            "schema_version": "1",
            "run_id": safe_run_id,
            "release_id": safe_release_id,
            "files": sorted(entries, key=lambda item: str(item["relative_path"])),
        }
        _write_json(temporary / _ARTIFACT_MANIFEST, artifact_manifest)
        _fsync_directory(temporary)
        os.rename(temporary, target)
        temporary = None
        _fsync_directory(run_dir)
    except Exception:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        lock_path.unlink(missing_ok=True)

    return ReleaseArtifactResult(target, safe_run_id, safe_release_id, len(payloads))


def verify_release_artifact(artifact_dir: str | Path) -> ArtifactVerificationResult:
    """从磁盘重算所有哈希，并拒绝缺失、多余、符号链接或敏感内容。

    校验以包内 ``artifact-manifest.json`` 为唯一文件清单，但仍要求固定的三个
    业务摘要全部存在。该函数不依赖进程内发布状态，因此可用于离线交付验收。
    """

    directory = Path(artifact_dir)
    if not directory.is_dir() or directory.is_symlink():
        raise ReleaseArtifactError("发布包目录不存在，或目录本身是符号链接")
    release_id = _validate_path_segment(directory.name, "release_id")
    run_id = _validate_path_segment(directory.parent.name, "run_id")
    manifest_path = directory / _ARTIFACT_MANIFEST
    metadata = _read_json(manifest_path)
    entries = _validate_artifact_manifest(metadata, run_id=run_id, release_id=release_id)
    expected = set(_PAYLOAD_FILES) | {_ARTIFACT_MANIFEST}
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseArtifactError(f"发布包文件集合不一致：missing={missing}, extra={extra}")

    for filename, expected_digest, expected_size in entries:
        path = directory / filename
        if not path.is_file() or path.is_symlink():
            raise ReleaseArtifactError(f"发布包成员不是普通文件：{filename}")
        if path.stat().st_size != expected_size or _sha256(path) != expected_digest:
            raise ReleaseArtifactError(f"发布包成员已被篡改：{filename}")
        payload = _read_json(path)
        _validate_payload(payload, location=filename)
    return ArtifactVerificationResult(directory, run_id, release_id, len(entries))


def _validate_path_segment(value: str, field: str) -> str:
    """拒绝空值、点目录、绝对路径、分隔符和遍历语义。"""

    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ReleaseArtifactError(f"{field} 必须是非空的普通路径段")
    if Path(value).is_absolute() or "/" in value or "\\" in value or ".." in value:
        raise ReleaseArtifactError(f"{field} 不得包含绝对路径、分隔符或遍历片段")
    if not _SAFE_SEGMENT.fullmatch(value):
        raise ReleaseArtifactError(f"{field} 含有不允许的字符")
    return value


def _validate_payload(value: Any, *, location: str, semantic: str | None = None) -> None:
    """递归验证去敏摘要；容器可自由命名，叶子必须属于允许的语义类型。"""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ReleaseArtifactError(f"{location} 的 JSON 键必须是非空字符串")
            child_semantic = _semantic_for_key(key, inherited=semantic)
            _validate_payload(child, location=f"{location}.{key}", semantic=child_semantic)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _validate_payload(child, location=f"{location}[{index}]", semantic=semantic)
        return
    if value is None:
        return
    if semantic is None:
        raise ReleaseArtifactError(f"{location} 不是允许的去敏摘要字段")
    _validate_scalar(value, semantic=semantic, location=location)


def _semantic_for_key(key: str, *, inherited: str | None) -> str | None:
    """根据键名识别叶子语义，同时阻断作者标识和凭据等敏感键。"""

    lowered = key.lower()
    if lowered == "count" or lowered == "total" or lowered.endswith("_count"):
        return "count"
    if lowered == "counts" or lowered.endswith("_counts"):
        return "count"
    if lowered in {"sha256", "hash", "digest"} or lowered.endswith(("_sha256", "_hash", "_digest")):
        return "hash"
    if lowered == "hashes" or lowered.endswith("_hashes"):
        return "hash"
    if lowered == "version" or lowered.endswith("_version"):
        return "version"
    if lowered == "versions" or lowered.endswith("_versions"):
        return "version"
    if lowered in {"reason_code", "reason_codes"} or lowered.endswith(("_reason_code", "_reason_codes")):
        return "reason"
    tokens = {token for token in re.split(r"[^a-z0-9]+", lowered) if token}
    if tokens & _SENSITIVE_KEY_TOKENS:
        raise ReleaseArtifactError(f"敏感字段不得进入发布包：{key}")
    if lowered in _OPERATIONAL_ID_KEYS or lowered.startswith(("deidentified_", "anonymous_")) and lowered.endswith("_id"):
        return "id"
    if lowered in {"ids", "deidentified_ids", "anonymous_ids"}:
        return "id"
    return inherited if inherited in {"count", "hash", "version", "reason", "id"} else None


def _validate_scalar(value: Any, *, semantic: str, location: str) -> None:
    """按字段语义约束标量，避免利用合法键夹带文本或路径。"""

    if semantic == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReleaseArtifactError(f"{location} 必须是非负整数计数")
        return
    if not isinstance(value, str) or not value:
        raise ReleaseArtifactError(f"{location} 必须是非空字符串")
    if _URL.search(value) or value.startswith(("/", "~")) or _WINDOWS_ABSOLUTE.match(value):
        raise ReleaseArtifactError(f"{location} 不得包含 URL 或绝对路径")
    if "/../" in value or "\\..\\" in value or value in {".", ".."}:
        raise ReleaseArtifactError(f"{location} 不得包含路径遍历")
    if semantic == "hash":
        if not _HEX_HASH.fullmatch(value):
            raise ReleaseArtifactError(f"{location} 必须是十六进制哈希")
    elif not _SAFE_CODE.fullmatch(value):
        raise ReleaseArtifactError(f"{location} 只能保存去标识 ID、版本或理由码")


def _serialize_json(payload: Any, *, location: str) -> bytes:
    """生成确定性的 UTF-8 JSON，并把不可序列化错误转换为领域异常。"""

    try:
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReleaseArtifactError(f"{location} 不是可序列化的有限 JSON") from exc


def _write_json(path: Path, payload: Any) -> None:
    """完整写入、刷新并同步单个 JSON 文件。"""

    data = _serialize_json(payload, location=path.name)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path) -> Any:
    """读取 JSON，并用一致的领域异常报告缺失或语法损坏。"""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseArtifactError(f"无法读取合法 JSON：{path.name}") from exc


def _sha256(path: Path) -> str:
    """以分块方式计算文件 SHA-256，避免依赖文件规模。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_artifact_manifest(
    metadata: Any, *, run_id: str, release_id: str
) -> list[tuple[str, str, int]]:
    """验证内部清单结构、身份绑定与固定成员集合。"""

    if not isinstance(metadata, dict) or set(metadata) != {"schema_version", "run_id", "release_id", "files"}:
        raise ReleaseArtifactError("artifact-manifest.json 结构不合法")
    if metadata["schema_version"] != "1" or metadata["run_id"] != run_id or metadata["release_id"] != release_id:
        raise ReleaseArtifactError("发布包清单与所在运行/发布目录不一致")
    files = metadata["files"]
    if not isinstance(files, list):
        raise ReleaseArtifactError("发布包清单 files 必须是数组")
    entries: list[tuple[str, str, int]] = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"relative_path", "sha256", "size"}:
            raise ReleaseArtifactError("发布包清单成员结构不合法")
        filename, digest, size = entry["relative_path"], entry["sha256"], entry["size"]
        if filename not in _PAYLOAD_FILES or not isinstance(digest, str) or not _HEX_HASH.fullmatch(digest):
            raise ReleaseArtifactError("发布包清单成员路径或哈希不合法")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ReleaseArtifactError("发布包清单成员大小不合法")
        entries.append((filename, digest, size))
    if {entry[0] for entry in entries} != set(_PAYLOAD_FILES) or len(entries) != len(_PAYLOAD_FILES):
        raise ReleaseArtifactError("发布包清单存在缺失或重复成员")
    return entries


def _fsync_directory(path: Path) -> None:
    """同步目录项；不支持目录 fsync 的平台由上层文件同步提供最低保证。"""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
