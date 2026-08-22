"""正式清洗框架的配置、参考证据、迁移 manifest 与内部 schema 契约测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from tourism_ugc_study.cleaning.config import (
    ConfigurationError,
    load_cleaning_config_bundle,
    load_stable_config,
)
from tourism_ugc_study.cleaning.formal_schema import migrate_formal_schema


ROOT = Path(__file__).resolve().parents[2]
GUIDE_VERSION = "text-cleaning-v1.5"
HASH = "a" * 64
OTHER_HASH = "b" * 64


def _write_stable_config(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> Path:
    """复制稳定配置，并允许测试修改其公开字段。

    Args:
        tmp_path: pytest 临时目录。
        mutate: 接收可变配置字典的回调。

    Returns:
        临时配置文件路径。
    """

    raw = yaml.safe_load((ROOT / "configs/cleaning.yaml").read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "cleaning.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_config_bundle_reuses_cwd_fallback_for_external_config_copy(
    tmp_path: Path,
) -> None:
    """薄 CLI 不得自行改变规范化规则相对路径的解析语义。"""

    path = _write_stable_config(tmp_path, lambda _raw: None)
    stable, normalization = load_cleaning_config_bundle(path)

    assert normalization.version_lock == stable.artifacts["normalization_version_lock"]


def test_stable_config_is_complete_deeply_immutable_and_platform_free() -> None:
    config = load_stable_config(ROOT / "configs/cleaning.yaml")
    original_hash = config.sha256

    assert config.split["temporal_test_fraction"] == 0.20
    assert config.reference["contract"] == "final-nonduplicate-model-reference"
    assert config.reference["duplicate_ngram_range"] == (3, 5)
    assert config.reference["duplicate_similarity_threshold"] == 0.80
    assert config.reference["duplicate_threshold_role"] == "candidate_only"
    assert config.text["ngram_range"] == (2, 5)
    assert config.text["classifier"] == "linear_svm"
    assert config.text["svm_c"] == 1.0
    assert config.text["calibration_folds"] == 5
    assert config.routing["T_keep"] == "UNSET"
    assert config.routing["T_exclude"] == "UNSET"
    assert config.audit["sample_size"] == "UNSET"
    assert config.artifacts["normalization_version_lock"].startswith(
        "text-normalization-v1+sha256:"
    )
    assert config.reference["replacement_platform_quota"] is False
    assert config.reference["replacement_platform_sort"] is False
    assert "platform" not in str(config.text).casefold()
    assert "platform" not in str(config.split).casefold()
    with pytest.raises(TypeError):
        config.raw["text"]["class_weight"] = "changed"  # type: ignore[index]
    with pytest.raises(AttributeError):
        config.text["ngram_range"].append(6)  # type: ignore[union-attr]
    assert config.sha256 == original_hash


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.__setitem__("protocol_version", "3.3"),
        lambda raw: raw["split"].__setitem__("platform_min", 30),
        lambda raw: raw["text"].__setitem__("class_weight", None),
        lambda raw: raw["text"].__setitem__("svm_c", 0.5),
        lambda raw: raw["text"].__setitem__("calibration_folds", 10),
        lambda raw: raw["text"].__setitem__("ngram_range", [1, 5]),
        lambda raw: raw["split"].__setitem__("validation_fraction", 0.1),
        lambda raw: raw.__setitem__("label_guide_version", "wrong-guide"),
        lambda raw: raw["routing"].__setitem__("T_keep", 0.1),
        lambda raw: raw["artifacts"].__setitem__(
            "normalization_version_lock", "text-normalization-v1+sha256:" + "0" * 64
        ),
    ],
)
def test_stable_config_rejects_unfrozen_or_unknown_values(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    path = _write_stable_config(tmp_path, mutate)
    with pytest.raises(ConfigurationError):
        load_stable_config(path)


def _formal_connection(tmp_path: Path) -> sqlite3.Connection:
    """创建启用正式 schema 的测试连接。"""

    connection = sqlite3.connect(tmp_path / "formal.sqlite")
    migrate_formal_schema(connection)
    return connection


def _insert_model(connection: sqlite3.Connection, *, status: str = "frozen") -> None:
    """插入最小模型 artifact。"""

    connection.execute(
        """
        INSERT INTO cleaning_model_artifacts VALUES (
            'model-1', 'char-tfidf-linear-svm', 'normalized_model_text', 'sigmoid',
            'model.joblib', ?, 'calibration.joblib', ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (HASH, OTHER_HASH, HASH, OTHER_HASH, HASH, OTHER_HASH, HASH, status, "2026-01-01"),
    )


def _insert_unset_policy(connection: sqlite3.Connection) -> None:
    """插入禁止自动路由的未冻结策略。"""

    connection.execute(
        """
        INSERT INTO cleaning_threshold_policies(
            policy_id, policy_status, model_id, test_acceptance_status,
            automatic_routing_enabled, policy_sha256, created_at_utc
        ) VALUES ('policy-unset', 'UNSET', 'model-1', 'UNSET', 0, ?, '2026-01-01')
        """,
        (HASH,),
    )


def _insert_frozen_policy(connection: sqlite3.Connection) -> None:
    """插入仅用于边界契约测试的完整冻结策略。"""

    connection.execute(
        """
        INSERT INTO cleaning_threshold_policies(
            policy_id, policy_status, model_id, t_keep, t_exclude,
            audit_event_definition, audit_sample_size, audit_max_event_rate,
            audit_upper_confidence_limit, minimum_test_metrics_json,
            minimum_automatic_coverage, test_acceptance_manifest_sha256,
            test_acceptance_status, recovery_gate, automatic_routing_enabled,
            policy_sha256, created_at_utc
        ) VALUES (
            'policy-frozen', 'frozen', 'model-1', 0.2, 0.8,
            '残留无关', 100, 0.03, 0.05, '{"f1": 0.8}', 0.5, ?,
            'passed', '新策略并通过复核', 1, ?, '2026-01-01'
        )
        """,
        (OTHER_HASH, HASH),
    )


def _insert_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    policy_id: str,
    expected_count: int,
) -> None:
    """插入处于 building 状态的冻结模型推理运行。"""

    connection.execute(
        """
        INSERT INTO cleaning_inference_runs(
            inference_run_id, model_id, policy_id, target_batch_id,
            input_manifest_sha256, expected_prediction_count, fit_called,
            status, created_at_utc
        ) VALUES (?, 'model-1', ?, ?, ?, ?, 0, 'building', '2026-01-01')
        """,
        (run_id, policy_id, f"batch-{run_id}", HASH, expected_count),
    )


def _insert_prediction(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    policy_id: str,
    post_id: int,
    probability: float,
    action: str,
) -> None:
    """插入单条路由预测。"""

    connection.execute(
        """
        INSERT INTO cleaning_predictions VALUES (
            ?, ?, 1, ?, ?, 'model-1', ?, '2026-01-01'
        )
        """,
        (run_id, post_id, probability, action, policy_id),
    )


def test_formal_schema_enables_foreign_keys_and_rejects_orphans(tmp_path: Path) -> None:
    connection = _formal_connection(tmp_path)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO cleaning_threshold_policies(
                policy_id, policy_status, model_id, test_acceptance_status,
                automatic_routing_enabled, policy_sha256, created_at_utc
            ) VALUES ('orphan', 'UNSET', 'missing', 'UNSET', 0, ?, '2026-01-01')
            """,
            (HASH,),
        )
    connection.close()


def test_unset_policy_forces_all_predictions_to_manual_review(tmp_path: Path) -> None:
    connection = _formal_connection(tmp_path)
    _insert_model(connection)
    _insert_unset_policy(connection)
    _insert_run(connection, run_id="run-unset", policy_id="policy-unset", expected_count=1)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_prediction(
            connection,
            run_id="run-unset",
            policy_id="policy-unset",
            post_id=1,
            probability=0.01,
            action="auto_keep",
        )
    _insert_prediction(
        connection,
        run_id="run-unset",
        policy_id="policy-unset",
        post_id=1,
        probability=0.01,
        action="manual_review",
    )
    connection.close()


def test_frozen_policy_requires_complete_audit_and_test_gates(tmp_path: Path) -> None:
    connection = _formal_connection(tmp_path)
    _insert_model(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO cleaning_threshold_policies(
                policy_id, policy_status, model_id, t_keep, t_exclude,
                test_acceptance_status, automatic_routing_enabled,
                policy_sha256, created_at_utc
            ) VALUES ('incomplete', 'frozen', 'model-1', 0.2, 0.8,
                      'passed', 1, ?, '2026-01-01')
            """,
            (HASH,),
        )
    connection.close()


@pytest.mark.parametrize(
    ("post_id", "probability", "action"),
    [
        (1, 0.2, "auto_keep"),
        (2, 0.21, "manual_review"),
        (3, 0.79, "manual_review"),
        (4, 0.8, "auto_exclude"),
    ],
)
def test_frozen_policy_enforces_three_route_boundaries(
    tmp_path: Path, post_id: int, probability: float, action: str
) -> None:
    connection = _formal_connection(tmp_path)
    _insert_model(connection)
    _insert_frozen_policy(connection)
    _insert_run(connection, run_id="run-frozen", policy_id="policy-frozen", expected_count=1)
    _insert_prediction(
        connection,
        run_id="run-frozen",
        policy_id="policy-frozen",
        post_id=post_id,
        probability=probability,
        action=action,
    )
    connection.close()


def test_inference_completion_requires_exact_prediction_count(tmp_path: Path) -> None:
    connection = _formal_connection(tmp_path)
    _insert_model(connection)
    _insert_unset_policy(connection)
    _insert_run(connection, run_id="run-1", policy_id="policy-unset", expected_count=1)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE cleaning_inference_runs
            SET status='completed', output_manifest_sha256=?, completed_at_utc='2026-01-02'
            WHERE inference_run_id='run-1'
            """,
            (HASH,),
        )
    _insert_prediction(
        connection,
        run_id="run-1",
        policy_id="policy-unset",
        post_id=1,
        probability=0.5,
        action="manual_review",
    )
    connection.execute(
        """
        UPDATE cleaning_inference_runs
        SET status='completed', output_manifest_sha256=?, completed_at_utc='2026-01-02'
        WHERE inference_run_id='run-1'
        """,
        (HASH,),
    )
    assert connection.execute(
        "SELECT status FROM cleaning_inference_runs WHERE inference_run_id='run-1'"
    ).fetchone()[0] == "completed"
    connection.close()


def test_manual_evidence_appends_override_and_current_view_selects_leaf(tmp_path: Path) -> None:
    connection = _formal_connection(tmp_path)
    _insert_model(connection)
    _insert_unset_policy(connection)
    _insert_run(connection, run_id="run-1", policy_id="policy-unset", expected_count=1)
    _insert_prediction(
        connection,
        run_id="run-1",
        policy_id="policy-unset",
        post_id=1,
        probability=0.5,
        action="manual_review",
    )
    connection.execute(
        """
        UPDATE cleaning_inference_runs
        SET status='completed', output_manifest_sha256=?, completed_at_utc='2026-01-02'
        WHERE inference_run_id='run-1'
        """,
        (HASH,),
    )
    connection.execute(
        """
        INSERT INTO cleaning_final_decisions VALUES (
            'decision-model', 1, 1, 'review', 'model_route', 'run-1', NULL,
            NULL, ?, '2026-01-01'
        )
        """,
        (HASH,),
    )
    connection.execute(
        """
        INSERT INTO cleaning_manual_evidence VALUES (
            'evidence-1', 'task-1', 1, 1, ?, ?, 'related', 'manual_review',
            ?, '2026-01-02'
        )
        """,
        (HASH, OTHER_HASH, GUIDE_VERSION),
    )
    connection.execute(
        """
        INSERT INTO cleaning_final_decisions VALUES (
            'decision-manual', 1, 1, 'keep', 'manual_evidence', NULL,
            'evidence-1', 'decision-model', ?, '2026-01-02'
        )
        """,
        (OTHER_HASH,),
    )

    current = connection.execute(
        "SELECT decision_id, decision_action FROM cleaning_current_decisions"
    ).fetchone()
    assert current == ("decision-manual", "keep")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO cleaning_final_decisions VALUES (
                'parallel-successor', 1, 1, 'keep', 'manual_evidence', NULL,
                'evidence-1', 'decision-model', ?, '2026-01-03'
            )
            """,
            ("c" * 64,),
        )
    connection.close()


def test_final_decision_rejects_mismatched_evidence_identity(tmp_path: Path) -> None:
    connection = _formal_connection(tmp_path)
    connection.execute(
        """
        INSERT INTO cleaning_manual_evidence VALUES (
            'evidence-1', 'task-1', 2, 1, ?, ?, 'related', 'reference',
            ?, '2026-01-01'
        )
        """,
        (HASH, OTHER_HASH, GUIDE_VERSION),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO cleaning_final_decisions VALUES (
                'decision-1', 1, 1, 'keep', 'manual_evidence', NULL,
                'evidence-1', NULL, ?, '2026-01-01'
            )
            """,
            (HASH,),
        )
    connection.close()


def test_formal_evidence_tables_are_append_only(tmp_path: Path) -> None:
    connection = _formal_connection(tmp_path)
    _insert_model(connection)
    _insert_unset_policy(connection)
    _insert_run(connection, run_id="run-1", policy_id="policy-unset", expected_count=1)
    _insert_prediction(
        connection,
        run_id="run-1",
        policy_id="policy-unset",
        post_id=1,
        probability=0.5,
        action="manual_review",
    )
    connection.execute(
        """
        INSERT INTO cleaning_manual_evidence VALUES (
            'evidence-1', 'task-1', 1, 1, ?, ?, 'related', 'reference',
            ?, '2026-01-01'
        )
        """,
        (HASH, OTHER_HASH, GUIDE_VERSION),
    )
    connection.execute(
        """
        INSERT INTO cleaning_final_decisions VALUES (
            'decision-1', 1, 1, 'keep', 'manual_evidence', NULL,
            'evidence-1', NULL, ?, '2026-01-01'
        )
        """,
        (HASH,),
    )
    for table in (
        "cleaning_model_artifacts",
        "cleaning_threshold_policies",
        "cleaning_inference_runs",
        "cleaning_predictions",
        "cleaning_manual_evidence",
        "cleaning_final_decisions",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE cleaning_predictions SET p_unrelated=0.9")
    connection.close()
