"""图片复核薄 CLI 的断网、去敏和端到端合成验收。"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

from tourism_ugc_study.cleaning.image_keep_audit_repository import IMAGE_AUDIT_COLUMNS
from tourism_ugc_study.cleaning.image_review_annotation import (
    IMAGE_ADJUDICATION_COLUMNS,
    IMAGE_ANNOTATION_COLUMNS,
)
from tests.cleaning.test_image_cli import _network_block_environment, _run_script
from tests.cleaning.test_image_decision_repository import (
    _build_with_tiny_duplicate,
    _fill_by_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def _review_cli(
    derived: Path,
    environment: dict[str, str],
    *arguments: str,
):
    """在 socket 封锁子进程中调用图片复核脚本。"""

    return _run_script(
        "cleaning_review_images.py",
        "--derived-db",
        str(derived),
        "--config",
        str(CONFIG_PATH),
        *arguments,
        env=environment,
    )


def _assert_private_free(result, tmp_path: Path) -> None:
    """CLI stdout/stderr 不得回显本机路径、URL 或图片文件名。"""

    output = result.stdout + result.stderr
    assert str(tmp_path) not in output
    assert "http://" not in output and "https://" not in output
    assert "tiny.png" not in output and "route.png" not in output


def _fill_audit_csv(template: Path, output: Path) -> None:
    """把审计导出表填为有效内容，保留运行生成的身份字段。"""

    with template.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or ())
        rows = list(reader)
    for row in rows:
        row["technical_noise_label"] = "valid_content"
        row["reason_codes"] = "synthetic_route_plan"
        row["annotator_hash"] = "9" * 64
        row["annotated_at_utc"] = "2026-07-31T05:00:00Z"
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _complete_review_gate_cli(
    derived: Path,
    environment: dict[str, str],
    build_id: str,
    tmp_path: Path,
) -> list[object]:
    """经断网 CLI 完成 pilot/boundary 双标与一致率评估。"""

    results: list[object] = []
    for run_index, kind in enumerate(("pilot", "boundary"), start=1):
        created = _review_cli(
            derived,
            environment,
            "create-review",
            "--candidate-build-id",
            build_id,
            "--kind",
            kind,
        )
        assert created.returncode == 0, created.stderr
        review_id = json.loads(created.stdout)["review_run_id"]
        planned = _review_cli(
            derived,
            environment,
            "create-double-plan",
            "--review-run-id",
            review_id,
            "--kind",
            "boundary",
        )
        assert planned.returncode == 0, planned.stderr
        results.extend((created, planned))
        for slot in (1, 2):
            template = tmp_path / f"gate-{kind}-{slot}-template.csv"
            exported = _review_cli(
                derived,
                environment,
                "export-review",
                "--review-run-id",
                review_id,
                "--assignment-slot",
                str(slot),
                "--output",
                str(template),
            )
            assert exported.returncode == 0, exported.stderr
            with template.open("r", encoding="utf-8", newline="") as stream:
                identities = [
                    row["fingerprint_id"] for row in csv.DictReader(stream)
                ]
            completed = tmp_path / f"gate-{kind}-{slot}.csv"
            _fill_by_fingerprint(
                template,
                completed,
                labels={identity: "valid_content" for identity in identities},
                annotator=f"{run_index * 2 + slot:x}" * 64,
            )
            imported = _review_cli(
                derived,
                environment,
                "import-review",
                "--input",
                str(completed),
                "--imported-by-hash",
                f"{run_index * 2 + slot + 5:x}" * 64,
            )
            assert imported.returncode == 0, imported.stderr
            results.extend((exported, imported))
        agreement = _review_cli(
            derived,
            environment,
            "agreement",
            "--review-run-id",
            review_id,
        )
        assert agreement.returncode == 0, agreement.stderr
        assert json.loads(agreement.stdout)["evaluation_status"] == "passed"
        results.append(agreement)
    return results


def test_review_cli_runs_full_offline_chain_and_keeps_source_images_unchanged(
    tmp_path: Path,
) -> None:
    derived, _config, build = _build_with_tiny_duplicate(tmp_path)
    image_paths = (tmp_path / "images" / "tiny.png", tmp_path / "images" / "route.png")
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in image_paths}
    environment = _network_block_environment(tmp_path)

    created = _review_cli(
        derived,
        environment,
        "create-review",
        "--candidate-build-id",
        build.build_id,
        "--kind",
        "candidate_review",
    )
    assert created.returncode == 0, created.stderr
    review_id = json.loads(created.stdout)["review_run_id"]
    _assert_private_free(created, tmp_path)

    slot1_template = tmp_path / "slot1-template.csv"
    exported1 = _review_cli(
        derived,
        environment,
        "export-review",
        "--review-run-id",
        review_id,
        "--assignment-slot",
        "1",
        "--output",
        str(slot1_template),
    )
    assert exported1.returncode == 0, exported1.stderr
    _assert_private_free(exported1, tmp_path)
    with sqlite3.connect(derived) as connection:
        tiny_rep = connection.execute(
            """
            SELECT c.representative_fingerprint_id FROM image_exact_clusters c
            JOIN image_exact_cluster_members m ON m.build_id = c.build_id
              AND m.cluster_id = c.cluster_id
            JOIN image_candidate_build_members bm ON bm.build_id = m.build_id
              AND bm.fingerprint_id = m.fingerprint_id
            WHERE c.build_id = ? AND bm.source_image_id = 1
            """,
            (build.build_id,),
        ).fetchone()[0]
    with slot1_template.open("r", encoding="utf-8", newline="") as stream:
        ids = [row["fingerprint_id"] for row in csv.DictReader(stream)]
    slot1 = tmp_path / "slot1.csv"
    labels = {identity: "valid_content" for identity in ids}
    labels[tiny_rep] = "site_ui"
    _fill_by_fingerprint(slot1_template, slot1, labels=labels, annotator="1" * 64)
    imported1 = _review_cli(
        derived,
        environment,
        "import-review",
        "--input",
        str(slot1),
        "--imported-by-hash",
        "a" * 64,
    )
    assert imported1.returncode == 0, imported1.stderr
    _assert_private_free(imported1, tmp_path)

    planned = _review_cli(
        derived,
        environment,
        "create-double-plan",
        "--review-run-id",
        review_id,
        "--kind",
        "proposed_exclusion",
    )
    assert planned.returncode == 0, planned.stderr
    assert json.loads(planned.stdout)["member_count"] == 1
    slot2_template = tmp_path / "slot2-template.csv"
    exported2 = _review_cli(
        derived,
        environment,
        "export-review",
        "--review-run-id",
        review_id,
        "--assignment-slot",
        "2",
        "--output",
        str(slot2_template),
    )
    assert exported2.returncode == 0, exported2.stderr
    slot2 = tmp_path / "slot2.csv"
    _fill_by_fingerprint(
        slot2_template,
        slot2,
        labels={tiny_rep: "site_ui"},
        annotator="2" * 64,
    )
    imported2 = _review_cli(
        derived,
        environment,
        "import-review",
        "--input",
        str(slot2),
        "--imported-by-hash",
        "b" * 64,
    )
    assert imported2.returncode == 0, imported2.stderr

    blocked_decision = _review_cli(
        derived,
        environment,
        "build-decisions",
        "--candidate-build-id",
        build.build_id,
    )
    assert blocked_decision.returncode == 1
    assert json.loads(blocked_decision.stderr)["reason_code"] == "image_review_gate_pilot_missing"
    gate_results = _complete_review_gate_cli(
        derived, environment, build.build_id, tmp_path
    )

    decision = _review_cli(
        derived,
        environment,
        "build-decisions",
        "--candidate-build-id",
        build.build_id,
    )
    assert decision.returncode == 0, decision.stderr
    decision_payload = json.loads(decision.stdout)
    assert decision_payload["exclude_count"] == 1
    decision_id = decision_payload["decision_build_id"]
    propagated = _review_cli(
        derived,
        environment,
        "propagate-sha",
        "--decision-build-id",
        decision_id,
    )
    assert propagated.returncode == 0, propagated.stderr
    assert json.loads(propagated.stdout)["propagated_member_count"] == 2

    audit = _review_cli(
        derived,
        environment,
        "create-audit",
        "--decision-build-id",
        decision_id,
        "--round-number",
        "1",
    )
    assert audit.returncode == 0, audit.stderr
    audit_id = json.loads(audit.stdout)["audit_round_id"]
    audit_template = tmp_path / "audit-template.csv"
    exported_audit = _review_cli(
        derived,
        environment,
        "export-audit",
        "--audit-round-id",
        audit_id,
        "--output",
        str(audit_template),
    )
    assert exported_audit.returncode == 0, exported_audit.stderr
    audit_csv = tmp_path / "audit.csv"
    _fill_audit_csv(audit_template, audit_csv)
    imported_audit = _review_cli(
        derived,
        environment,
        "import-audit",
        "--input",
        str(audit_csv),
    )
    assert imported_audit.returncode == 0, imported_audit.stderr
    evaluated = _review_cli(
        derived,
        environment,
        "evaluate-audit",
        "--audit-round-id",
        audit_id,
    )
    assert evaluated.returncode == 0, evaluated.stderr
    assert json.loads(evaluated.stdout)["evaluation_status"] == "passed"

    for result in (
        planned,
        exported2,
        imported2,
        blocked_decision,
        *gate_results,
        decision,
        propagated,
        audit,
        exported_audit,
        imported_audit,
        evaluated,
    ):
        _assert_private_free(result, tmp_path)
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in image_paths}
    assert after == before


def test_repository_templates_match_runtime_csv_contracts() -> None:
    templates = PROJECT_ROOT / "data" / "annotations" / "templates"
    expected = {
        "image-technical-noise-annotations.csv": IMAGE_ANNOTATION_COLUMNS,
        "image-technical-noise-adjudications.csv": IMAGE_ADJUDICATION_COLUMNS,
        "image-keep-audit-annotations.csv": IMAGE_AUDIT_COLUMNS,
    }
    for name, columns in expected.items():
        with (templates / name).open("r", encoding="utf-8", newline="") as stream:
            assert tuple(csv.reader(stream).__next__()) == columns
