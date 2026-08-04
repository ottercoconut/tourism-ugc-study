"""本地图片字节的只读技术校验、元数据净化与确定性指纹。

本模块不决定图片是否有研究价值。所有输出都是技术证据；路线图、攻略卡、
菜单等非实景内容与普通照片使用完全相同的读取和指纹规则。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import imagehash
from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError, __version__ as pillow_version


class ImageFingerprintError(RuntimeError):
    """本地图片缺失、声明冲突或无法解码时抛出的可分类异常。

    `reason_code` 不含路径或图片内容，供仓储层记录为可复核的 `blocked`；它不
    表示模型训练失败，也不会触发源文件写回。调用方可在文件补齐或替换后显式
    重试，已成功的不变指纹仍按行身份幂等复用。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("image fingerprint operation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ImageFileFingerprint:
    """不含路径和原始 EXIF 的确定性图片技术证据。

    文件 SHA、MIME、字节数、转正后的宽高、透明性、白名单 EXIF、pHash 参数
    及 Pillow/ImageHash 版本共同描述本次结果。字段不表达图片是否应清洗，也
    不保留 GPS、设备序列号、自由文本或绝对路径。对象创建后不可变。
    """

    file_sha256: str
    mime_type: str
    byte_size: int
    width_px: int
    height_px: int
    has_alpha: bool
    is_fully_transparent: bool
    sanitized_exif: Mapping[str, str | int | float]
    phash_hex: str
    phash_hash_size: int
    phash_highfreq_factor: int
    library_versions: Mapping[str, str]


_SAFE_EXIF_TAGS = {
    "Orientation",
    "ColorSpace",
    "ExifImageWidth",
    "ExifImageHeight",
    "PixelXDimension",
    "PixelYDimension",
    "ExposureTime",
    "FNumber",
    "ISOSpeedRatings",
    "PhotographicSensitivity",
    "FocalLength",
    "Flash",
    "WhiteBalance",
}


def sha256_file_stream(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """只读、分块计算一个本地文件的 SHA-256。

    `chunk_size` 控制内存上限且必须为正整数；非正值抛出含固定安全文本的
    `ValueError`。返回值只依赖文件字节。
    缺失或不可读分别抛出可去敏分类的 :class:`ImageFingerprintError`。函数不
    解码图片、不访问网络、不修改文件，重复读取同一稳定文件结果相同。
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(chunk_size), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise ImageFingerprintError("image_file_missing") from exc
    except OSError as exc:
        raise ImageFingerprintError("image_file_unreadable") from exc
    return digest.hexdigest()


def _safe_exif_scalar(value: object) -> str | int | float | None:
    """仅保留可稳定 JSON 化的标量，不序列化厂商二进制块或嵌套结构。"""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, float)):
        return value
    # Pillow 的 IFDRational 等数值对象可安全转为 float；失败时直接丢弃。
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def sanitize_exif(exif: Mapping[int, object]) -> dict[str, str | int | float]:
    """以白名单保留非定位技术字段，始终排除 GPS、序列号和自由文本。

    输入是 Pillow 解码出的标签映射，输出按标签名排序且只含可稳定 JSON 化的
    标量。使用白名单而非黑名单是为了让未知厂商标签默认不落库，避免未来 Pillow
    识别出新的设备或位置字段后无意扩大隐私面。
    """

    sanitized: dict[str, str | int | float] = {}
    for tag_id, value in exif.items():
        name = ExifTags.TAGS.get(tag_id, f"tag_{tag_id}")
        if name not in _SAFE_EXIF_TAGS:
            continue
        scalar = _safe_exif_scalar(value)
        if scalar is not None:
            sanitized[name] = scalar
    return dict(sorted(sanitized.items()))


def fingerprint_image_file(
    path: str | Path,
    *,
    expected_file_sha256: str,
    hash_size: int,
    highfreq_factor: int,
) -> ImageFileFingerprint:
    """只读校验单个文件并计算 EXIF 转正后的 pHash。

    输入包括路径、manifest 声明 SHA 和冻结的 pHash 参数；返回不含路径的
    :class:`ImageFileFingerprint`。先核对声明 SHA，再解码图片，因此哈希冲突与
    解码失败形成不同阻塞原因；解码后再次计算 SHA，防止并发替换造成文件摘要
    与 pHash 来自不同字节。函数不会覆盖 EXIF 方向、转码、联网或写回原文件，
    也不判定路线图、攻略卡等内容价值。
    """

    image_path = Path(path)
    file_sha256 = sha256_file_stream(image_path)
    if file_sha256 != expected_file_sha256:
        raise ImageFingerprintError("image_sha256_mismatch")
    try:
        byte_size = image_path.stat().st_size
        with Image.open(image_path) as source:
            source_format = source.format
            exif = sanitize_exif(source.getexif())
            normalized = ImageOps.exif_transpose(source)
            normalized.load()
            width, height = normalized.size
            bands = normalized.getbands()
            has_alpha = "A" in bands or "transparency" in normalized.info
            is_fully_transparent = False
            if has_alpha:
                alpha = normalized.convert("RGBA").getchannel("A")
                extrema = alpha.getextrema()
                is_fully_transparent = extrema is not None and extrema[1] == 0
            phash_hex = str(
                imagehash.phash(
                    normalized,
                    hash_size=hash_size,
                    highfreq_factor=highfreq_factor,
                )
            )
    except FileNotFoundError as exc:
        raise ImageFingerprintError("image_file_missing") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageFingerprintError("image_decode_failed") from exc
    # 解码前后复核同一文件，避免下载器并发替换导致 SHA 与 pHash 来自不同字节。
    if sha256_file_stream(image_path) != file_sha256:
        raise ImageFingerprintError("image_file_changed_during_read")
    if width <= 0 or height <= 0:
        raise ImageFingerprintError("image_dimensions_invalid")
    mime_type = Image.MIME.get(source_format or "", "application/octet-stream")
    return ImageFileFingerprint(
        file_sha256=file_sha256,
        mime_type=mime_type,
        byte_size=byte_size,
        width_px=width,
        height_px=height,
        has_alpha=has_alpha,
        is_fully_transparent=is_fully_transparent,
        sanitized_exif=exif,
        phash_hex=phash_hex,
        phash_hash_size=hash_size,
        phash_highfreq_factor=highfreq_factor,
        library_versions={
            "Pillow": pillow_version,
            "ImageHash": imagehash.__version__,
        },
    )


def current_image_library_versions() -> dict[str, str]:
    """返回参与解码和 pHash 的运行库版本。

    输出只含 `Pillow` 与 `ImageHash` 版本字符串，供任何文件打开前比对冻结配置。
    函数无 I/O 和隐私数据；版本不一致应由仓储层拒绝本次处理，而不是复用结果。
    """

    return {"Pillow": pillow_version, "ImageHash": imagehash.__version__}


def fingerprint_payload(fingerprint: ImageFileFingerprint) -> dict[str, object]:
    """把技术指纹投影为用于持久化与摘要的无路径字典。

    所有实际影响候选计算和审计复现的字段均被保留，绝对路径与原始 EXIF 不在
    输入对象中也不会被补入。函数不修改对象；同一对象重复投影字段和值一致。
    """

    return {
        "file_sha256": fingerprint.file_sha256,
        "mime_type": fingerprint.mime_type,
        "byte_size": fingerprint.byte_size,
        "width_px": fingerprint.width_px,
        "height_px": fingerprint.height_px,
        "has_alpha": fingerprint.has_alpha,
        "is_fully_transparent": fingerprint.is_fully_transparent,
        "sanitized_exif": fingerprint.sanitized_exif,
        "phash_hex": fingerprint.phash_hex,
        "phash_hash_size": fingerprint.phash_hash_size,
        "phash_highfreq_factor": fingerprint.phash_highfreq_factor,
        "library_versions": fingerprint.library_versions,
    }


def fingerprint_payload_sha256(fingerprint: ImageFileFingerprint) -> str:
    """计算技术证据的规范 SHA-256。

    输入先经 :func:`fingerprint_payload` 去路径投影，再以排序 JSON 编码；返回
    64 位十六进制身份，用于检测证据漂移而非替代原文件 SHA。函数无 I/O。
    """

    payload = json.dumps(
        fingerprint_payload(fingerprint),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
