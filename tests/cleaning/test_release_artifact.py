"""不可变本地发布包的边界与完整性测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.release_artifact import (
    ReleaseArtifactError,
    publish_release_artifact,
    verify_release_artifact,
)


def _payloads() -> dict[str, object]:
    """返回仅含去标识 ID、计数、哈希、版本和理由码的最小样例。"""

    return {
        "report": {"kept_count": 2, "excluded_count": 1, "reason_codes": ["invalid_structure"]},
        "manifest": {"schema_version": "23", "build_id": "build-001", "source_sha256": "a" * 64},
        "member_summaries": [
            {"deidentified_post_id": "post-hash-001", "decision_version": "1", "reason_code": "keep"}
        ],
    }


def _publish(root: Path):
    """用固定标识发布一个测试包。"""

    return publish_release_artifact(
        output_root=root,
        run_id="run-001",
        release_id="release-001",
        **_payloads(),
    )


def test_publish_atomically_writes_verifiable_package_without_latest(tmp_path: Path) -> None:
    """成功路径只留下最终目录、固定文件与可重算的相对路径清单。"""

    result = _publish(tmp_path / "results")

    assert result.artifact_dir == tmp_path / "results" / "run-001" / "release-001"
    assert {path.name for path in result.artifact_dir.iterdir()} == {
        "report.json", "release-manifest.json", "member-summaries.json", "artifact-manifest.json"
    }
    metadata = json.loads((result.artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
    assert {item["relative_path"] for item in metadata["files"]} == {
        "report.json", "release-manifest.json", "member-summaries.json"
    }
    assert not any(path.name == "latest" for path in tmp_path.rglob("*"))
    assert not any(".tmp-" in path.name or path.name.endswith(".publish.lock") for path in tmp_path.rglob("*"))
    assert verify_release_artifact(result.artifact_dir).file_count == 3


def test_existing_release_is_never_overwritten(tmp_path: Path) -> None:
    """同一运行号和发布号重复执行时拒绝幂等覆盖。"""

    first = _publish(tmp_path)
    original = (first.artifact_dir / "report.json").read_bytes()

    with pytest.raises(ReleaseArtifactError, match="拒绝覆盖"):
        _publish(tmp_path)

    assert (first.artifact_dir / "report.json").read_bytes() == original


@pytest.mark.parametrize("member", ["report.json", "release-manifest.json", "member-summaries.json"])
def test_verify_rejects_tampered_member(tmp_path: Path, member: str) -> None:
    """任一业务 JSON 被修改后都无法通过磁盘重算。"""

    result = _publish(tmp_path)
    (result.artifact_dir / member).write_text("{}\n", encoding="utf-8")

    with pytest.raises(ReleaseArtifactError, match="篡改"):
        verify_release_artifact(result.artifact_dir)


def test_verify_rejects_missing_and_extra_files(tmp_path: Path) -> None:
    """清单之外的文件以及清单成员缺失都属于失败。"""

    missing = _publish(tmp_path / "missing").artifact_dir
    (missing / "report.json").unlink()
    with pytest.raises(ReleaseArtifactError, match="文件集合不一致"):
        verify_release_artifact(missing)

    extra = _publish(tmp_path / "extra").artifact_dir
    (extra / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseArtifactError, match="文件集合不一致"):
        verify_release_artifact(extra)


@pytest.mark.parametrize(
    "run_id,release_id",
    [
        ("", "release"), (".", "release"), ("..", "release"), ("a/b", "release"),
        ("/absolute", "release"), ("run", "../release"), ("run", "a\\b"),
    ],
)
def test_publish_rejects_unsafe_path_segments(tmp_path: Path, run_id: str, release_id: str) -> None:
    """运行号和发布号不能改变目标目录层级。"""

    with pytest.raises(ReleaseArtifactError):
        publish_release_artifact(
            output_root=tmp_path, run_id=run_id, release_id=release_id, **_payloads()
        )


@pytest.mark.parametrize(
    "report",
    [
        {"raw_text": "青岛很好玩"},
        {"author_id": "person-001"},
        {"source_url": "https://example.test/post"},
        {"token": "abc"},
        {"reason_code": "/private/source"},
        {"post_id": "raw-platform-id"},
        {"note": "任意原始内容"},
    ],
)
def test_publish_rejects_sensitive_or_unclassified_payload(tmp_path: Path, report: object) -> None:
    """敏感键值与未归类文本均不能借报告入口进入发布包。"""

    payloads = _payloads()
    payloads["report"] = report
    with pytest.raises(ReleaseArtifactError):
        publish_release_artifact(
            output_root=tmp_path, run_id="run", release_id="release", **payloads
        )


def test_failed_write_removes_temporary_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """原子重命名前失败时清除临时目录和发布锁。"""

    import tourism_ugc_study.cleaning.release_artifact as module

    def fail_on_manifest(path: Path, payload: object) -> None:
        if path.name == "artifact-manifest.json":
            raise OSError("模拟磁盘失败")
        original_write(path, payload)

    original_write = module._write_json
    monkeypatch.setattr(module, "_write_json", fail_on_manifest)
    with pytest.raises(OSError, match="模拟磁盘失败"):
        _publish(tmp_path)

    run_dir = tmp_path / "run-001"
    assert not any(".tmp-" in path.name or path.name.endswith(".publish.lock") for path in run_dir.iterdir())
    assert not (run_dir / "release-001").exists()
