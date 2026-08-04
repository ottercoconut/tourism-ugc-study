"""本地图片 manifest 的 CSV 契约、身份计算与路径边界校验。

本模块不读取源 SQLite，也不打开图片字节。它只验证上游清单的结构和路径
安全性，使持久化层和指纹层共享同一份不可变行身份。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from .image_role import RelationRole, decide_image_role


IMAGE_MANIFEST_COLUMNS: tuple[str, ...] = (
    "source_image_id",
    "source_post_id",
    "relation_role",
    "relative_path",
    "file_sha256",
    "parent_file_sha256",
    "transform_json",
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


class ImageManifestError(ValueError):
    """清单无法形成安全稳定映射时抛出的去敏异常。

    `reason_code` 描述结构、路径或行身份问题，`row_number` 仅在可定位到 CSV 行
    时提供。异常文本不回显路径、URL 或单元格原文；失败表示清单不能安全导入，
    与后续图片文件缺失所形成的可恢复 `blocked` 状态不同。
    """

    def __init__(self, reason_code: str, row_number: int | None = None) -> None:
        super().__init__("image manifest is invalid")
        self.reason_code = reason_code
        self.row_number = row_number


@dataclass(frozen=True)
class ImageManifestRow:
    """经过结构校验的清单行，只保存相对路径和内容身份。

    `source_image_id/source_post_id/relation_role` 绑定冻结源关系，`relative_path`
    必须受图片根目录约束；文件及父文件 SHA、规范化变换 JSON 共同进入
    `row_identity_sha256`。`validation_status/reason_code` 保留重复或冲突行的审计
    结果。对象不含绝对路径、URL 和图片字节，创建后不可变。
    """

    row_number: int
    source_image_id: int
    source_post_id: int
    relation_role: RelationRole
    relative_path: str
    expected_file_sha256: str
    parent_file_sha256: str | None
    transform_json: str
    row_identity_sha256: str
    validation_status: str = "accepted"
    reason_code: str | None = None


@dataclass(frozen=True)
class ParsedImageManifest:
    """整个 CSV 的确定性摘要与逐行校验结果。

    `source_sha256` 绑定原始 CSV 字节，`rows` 按原行序保存，包括因重复或冲突
    被拒绝的行。解析结果本身不表示文件已经下载或可解码，也不会写入数据库。
    """

    source_sha256: str
    rows: tuple[ImageManifestRow, ...]


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def root_identity_sha256(image_root: str | Path) -> str:
    """计算图片根目录的本机身份摘要。

    输入必须是现存目录；返回解析后绝对路径的 SHA-256，只将摘要持久化，从而
    区分同一相对清单在不同根目录下的文件集合。目录不存在时抛出
    :class:`ImageManifestError`。函数不枚举或打开目录内图片。
    """

    root = Path(image_root)
    if not root.is_dir():
        raise ImageManifestError("image_root_not_directory")
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()


def resolve_manifest_path(image_root: str | Path, relative_path: str) -> Path:
    """将规范相对路径限制在图片根目录内，并拒绝符号链接逃逸。

    返回值只供当前进程读取，不应持久化。文件可以尚未到位；`strict=False`
    仍会解析已存在的符号链接父级，因此下载前后的路径边界一致。绝对路径、
    非规范路径、Windows 分隔符和符号链接逃逸会失败；文件存在性和解码状态由
    指纹阶段另行记录为阻塞。函数不创建目录或文件。
    """

    root = Path(image_root).resolve()
    if not root.is_dir():
        raise ImageManifestError("image_root_not_directory")
    if not isinstance(relative_path, str) or not relative_path:
        raise ImageManifestError("relative_path_empty")
    if "\\" in relative_path or PureWindowsPath(relative_path).is_absolute():
        raise ImageManifestError("relative_path_not_portable")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute():
        raise ImageManifestError("absolute_path_forbidden")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ImageManifestError("relative_path_not_normalized")
    if pure.as_posix() != relative_path:
        raise ImageManifestError("relative_path_not_normalized")
    candidate = (root / Path(*pure.parts)).resolve(strict=False)
    if candidate == root:
        raise ImageManifestError("relative_path_is_root")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ImageManifestError("relative_path_escapes_root") from exc
    return candidate


def _positive_id(value: str | None, field: str, row_number: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError as exc:
        raise ImageManifestError(f"{field}_invalid", row_number) from exc
    if parsed <= 0 or str(parsed) != value:
        raise ImageManifestError(f"{field}_invalid", row_number)
    return parsed


def _sha256(value: str | None, field: str, row_number: int, *, optional: bool) -> str | None:
    text = value or ""
    if optional and not text:
        return None
    if _SHA256.fullmatch(text) is None:
        raise ImageManifestError(f"{field}_invalid", row_number)
    return text


def _parse_row(raw: dict[str, str | None], row_number: int, image_root: Path) -> ImageManifestRow:
    """解析一行并冻结变换 JSON；原图与派生裁剪采用不同完整性条件。"""

    source_image_id = _positive_id(raw.get("source_image_id"), "source_image_id", row_number)
    source_post_id = _positive_id(raw.get("source_post_id"), "source_post_id", row_number)
    role = raw.get("relation_role") or ""
    try:
        decision = decide_image_role(role)
    except ValueError as exc:
        raise ImageManifestError("relation_role_invalid", row_number) from exc
    relative_path = raw.get("relative_path") or ""
    try:
        resolve_manifest_path(image_root, relative_path)
    except ImageManifestError as exc:
        raise ImageManifestError(exc.reason_code, row_number) from exc
    expected_sha = _sha256(raw.get("file_sha256"), "file_sha256", row_number, optional=False)
    parent_sha = _sha256(
        raw.get("parent_file_sha256"),
        "parent_file_sha256",
        row_number,
        optional=True,
    )
    try:
        transform = json.loads(raw.get("transform_json") or "{}")
    except json.JSONDecodeError as exc:
        raise ImageManifestError("transform_json_invalid", row_number) from exc
    if not isinstance(transform, dict):
        raise ImageManifestError("transform_json_not_object", row_number)
    transform_json = json.dumps(transform, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if (parent_sha is None) != (not transform):
        raise ImageManifestError("derived_lineage_incomplete", row_number)
    identity_fields = {
        "source_image_id": source_image_id,
        "source_post_id": source_post_id,
        "relation_role": decision.relation_role,
        "relative_path": relative_path,
        "expected_file_sha256": expected_sha,
        "parent_file_sha256": parent_sha,
        "transform_json": transform_json,
    }
    return ImageManifestRow(
        row_number=row_number,
        source_image_id=source_image_id,
        source_post_id=source_post_id,
        relation_role=decision.relation_role,
        relative_path=relative_path,
        expected_file_sha256=str(expected_sha),
        parent_file_sha256=parent_sha,
        transform_json=transform_json,
        row_identity_sha256=_canonical_sha256(identity_fields),
    )


def parse_image_manifest(manifest_path: str | Path, image_root: str | Path) -> ParsedImageManifest:
    """读取 UTF-8 CSV 并返回稳定行身份；同一源 ID 的多重映射显式拒绝。

    输入是公开列顺序固定的 UTF-8 CSV 和只用于路径边界校验的图片根目录；输出
    包含原 CSV 字节摘要与全部规范化行。同一路径可以合法映射多个源图片关系，
    因为一个文件可能被多个帖子复用。
    相反，同一源图片 ID 必须只有一个映射；重复或冲突行会全部保留为审计行，
    但不会进入角色和指纹处理。结构或单行格式错误整体失败，重复调用不写文件、
    不访问网络，并产生相同身份。
    """

    path = Path(manifest_path)
    root = Path(image_root)
    try:
        source_bytes = path.read_bytes()
    except OSError as exc:
        raise ImageManifestError("manifest_unreadable") from exc
    try:
        text = source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImageManifestError("manifest_encoding_invalid") from exc
    # StringIO 保留 CSV 引号内换行；直接 splitlines 会改变合法 JSON 字段的字节语义。
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != IMAGE_MANIFEST_COLUMNS:
        raise ImageManifestError("manifest_columns_invalid")
    rows = tuple(_parse_row(raw, index, root) for index, raw in enumerate(reader, start=2))

    by_source: dict[int, list[ImageManifestRow]] = {}
    for row in rows:
        by_source.setdefault(row.source_image_id, []).append(row)
    resolved: list[ImageManifestRow] = []
    for row in rows:
        peers = by_source[row.source_image_id]
        if len(peers) == 1:
            resolved.append(row)
            continue
        identities = {peer.row_identity_sha256 for peer in peers}
        reason = (
            "duplicate_source_image_id"
            if len(identities) == 1
            else "source_image_mapping_conflict"
        )
        status = "rejected" if len(identities) == 1 else "source_conflict"
        resolved.append(replace(row, validation_status=status, reason_code=reason))
    return ParsedImageManifest(
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        rows=tuple(resolved),
    )
