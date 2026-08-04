"""分析发布薄 CLI 的显式作用域与去敏错误测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import cleaning_release
from tourism_ugc_study.cleaning.analysis_release_repository import (
    AnalysisReleaseBuildResult,
    AnalysisReleaseRepositoryError,
    AnalysisReleaseStatus,
)


def test_build_cli_forwards_every_explicit_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """build 只转发显式 run/release、五个上游 ID、模式和输出根。"""

    captured: dict[str, object] = {}

    def fake_build(derived_db: str, **kwargs: object) -> AnalysisReleaseBuildResult:
        captured["derived_db"] = derived_db
        captured.update(kwargs)
        return AnalysisReleaseBuildResult(
            "release-1", "run-1", "smoke", "finalized", False, 1, 1, 1, 1, "a" * 64
        )

    monkeypatch.setattr(cleaning_release, "build_release", fake_build)
    exit_code = cleaning_release.main(
        [
            "--derived-db",
            str(tmp_path / "derived.sqlite"),
            "build",
            "--run-id",
            "run-1",
            "--release-id",
            "release-1",
            "--release-mode",
            "smoke",
            "--post-decision-build-id",
            "post-final",
            "--text-dedup-build-id",
            "dedup-1",
            "--text-keep-audit-evaluation-id",
            "text-eval",
            "--image-decision-build-id",
            "image-decisions",
            "--image-keep-audit-evaluation-id",
            "image-eval",
            "--output-root",
            str(tmp_path / "results"),
        ]
    )

    assert exit_code == 0
    assert captured["run_id"] == "run-1"
    assert captured["release_id"] == "release-1"
    assert captured["release_mode"] == "smoke"
    assert captured["post_decision_build_id"] == "post-final"
    assert captured["text_dedup_build_id"] == "dedup-1"
    assert captured["text_keep_audit_evaluation_id"] == "text-eval"
    assert captured["image_decision_build_id"] == "image-decisions"
    assert captured["image_keep_audit_evaluation_id"] == "image-eval"
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "finalized"


def test_status_cli_requires_both_release_and_run_scope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """status 不允许省略 run_id，也不会提供隐式 latest 回退。"""

    monkeypatch.setattr(
        cleaning_release,
        "get_release_status",
        lambda *args, **kwargs: AnalysisReleaseStatus(
            "release-1", "run-1", "smoke", "finalized", "paused", "quality_gate_pending"
        ),
    )
    with pytest.raises(SystemExit) as caught:
        cleaning_release.main(
            ["--derived-db", "derived.sqlite", "status", "--release-id", "release-1"]
        )
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().err) == {
        "reason_code": "invalid_arguments",
        "status": "failed",
    }


def test_repository_failure_json_does_not_echo_paths_or_tracebacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """仓储失败只输出 status/reason_code，不回显绝对路径和 traceback。"""

    sensitive_path = str(tmp_path / "private" / "derived.sqlite")

    def fail(*args: object, **kwargs: object) -> AnalysisReleaseStatus:
        raise AnalysisReleaseRepositoryError("analysis_release_not_found")

    monkeypatch.setattr(cleaning_release, "get_release_status", fail)
    assert (
        cleaning_release.main(
            [
                "--derived-db",
                sensitive_path,
                "status",
                "--run-id",
                "run-1",
                "--release-id",
                "release-1",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "reason_code": "analysis_release_not_found",
        "status": "failed",
    }
    assert sensitive_path not in captured.err
    assert "Traceback" not in captured.err


def test_unexpected_cli_failure_is_also_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未预见异常不能绕过去敏边界打印 Python traceback。"""

    def fail(*args: object, **kwargs: object) -> AnalysisReleaseStatus:
        raise RuntimeError("/private/path should never be emitted")

    monkeypatch.setattr(cleaning_release, "get_release_status", fail)
    assert (
        cleaning_release.main(
            [
                "--derived-db",
                "/private/derived.sqlite",
                "status",
                "--run-id",
                "run-1",
                "--release-id",
                "release-1",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err) == {
        "reason_code": "release_internal_error",
        "status": "failed",
    }
