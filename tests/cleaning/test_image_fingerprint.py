from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tourism_ugc_study.cleaning.image_fingerprint import (
    ImageFingerprintError,
    fingerprint_image_file,
    sanitize_exif,
    sha256_file_stream,
)


def _synthetic_route_map(path: Path) -> None:
    """生成不含真实 UGC 的路线图夹具，用于证明内容格式不会被排除。"""

    image = Image.new("RGB", (128, 96), "white")
    draw = ImageDraw.Draw(image)
    draw.line([(8, 80), (48, 40), (112, 16)], fill="red", width=5)
    draw.rectangle((40, 32, 56, 48), outline="blue", width=3)
    image.save(path, format="PNG")


def test_fingerprint_is_deterministic_and_does_not_modify_route_map(tmp_path: Path) -> None:
    path = tmp_path / "route-map.png"
    _synthetic_route_map(path)
    before = sha256_file_stream(path)

    first = fingerprint_image_file(
        path,
        expected_file_sha256=before,
        hash_size=8,
        highfreq_factor=4,
    )
    second = fingerprint_image_file(
        path,
        expected_file_sha256=before,
        hash_size=8,
        highfreq_factor=4,
    )

    assert first == second
    assert first.mime_type == "image/png"
    assert (first.width_px, first.height_px) == (128, 96)
    assert len(first.phash_hex) == 16
    assert sha256_file_stream(path) == before


def test_streaming_sha_rejects_nonpositive_chunks_and_is_stable(tmp_path: Path) -> None:
    """分块大小必须为正，合法大小不得改变文件摘要。"""

    path = tmp_path / "bytes.bin"
    payload = b"deterministic-image-bytes"
    path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    for chunk_size in (0, -1, -1024):
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            sha256_file_stream(path, chunk_size=chunk_size)
    assert sha256_file_stream(path, chunk_size=1) == expected
    assert sha256_file_stream(path, chunk_size=7) == expected


def test_exif_orientation_is_applied_but_gps_is_never_persisted(tmp_path: Path) -> None:
    path = tmp_path / "oriented.jpg"
    image = Image.new("RGB", (20, 10), "navy")
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, format="JPEG", exif=exif)
    result = fingerprint_image_file(
        path,
        expected_file_sha256=sha256_file_stream(path),
        hash_size=8,
        highfreq_factor=4,
    )

    assert (result.width_px, result.height_px) == (10, 20)
    assert result.sanitized_exif["Orientation"] == 6
    assert "GPSInfo" not in sanitize_exif({274: 1, 34853: {1: "N"}})


def test_transparent_and_failure_states_are_distinct(tmp_path: Path) -> None:
    transparent = tmp_path / "transparent.png"
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(transparent)
    result = fingerprint_image_file(
        transparent,
        expected_file_sha256=sha256_file_stream(transparent),
        hash_size=8,
        highfreq_factor=4,
    )
    assert result.has_alpha is True
    assert result.is_fully_transparent is True

    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not-an-image")
    with pytest.raises(ImageFingerprintError) as mismatch:
        fingerprint_image_file(
            corrupt,
            expected_file_sha256="f" * 64,
            hash_size=8,
            highfreq_factor=4,
        )
    assert mismatch.value.reason_code == "image_sha256_mismatch"

    with pytest.raises(ImageFingerprintError) as decode:
        fingerprint_image_file(
            corrupt,
            expected_file_sha256=hashlib.sha256(b"not-an-image").hexdigest(),
            hash_size=8,
            highfreq_factor=4,
        )
    assert decode.value.reason_code == "image_decode_failed"

    with pytest.raises(ImageFingerprintError) as missing:
        sha256_file_stream(tmp_path / "missing.png")
    assert missing.value.reason_code == "image_file_missing"
