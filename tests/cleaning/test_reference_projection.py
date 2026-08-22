"""冻结源快照文本投影的集成测试。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from tourism_ugc_study.cleaning.reference_projection import (
    ReferenceProjectionError,
    load_reference_text_projection,
)
from tourism_ugc_study.cleaning.text_config import load_text_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEXT_CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-text-normalization-v1.yaml"


def _sha256(path: Path) -> str:
    """计算测试文件摘要。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """构造最小冻结源快照和只读派生谱系。"""

    snapshot = tmp_path / "snapshot.sqlite"
    source = sqlite3.connect(snapshot)
    source.execute(
        "CREATE TABLE web_posts(id INTEGER PRIMARY KEY, title TEXT, "
        "content_text TEXT, status TEXT)"
    )
    source.executemany(
        "INSERT INTO web_posts VALUES (?, ?, ?, ?)",
        [
            (1, "标题", '{"ops":[{"insert":"正文\\n"}]}', "captured"),
            (2, "普通", "普通正文", "captured"),
        ],
    )
    source.commit()
    source.close()
    snapshot_hash = _sha256(snapshot)

    derived = tmp_path / "derived.sqlite"
    connection = sqlite3.connect(derived)
    connection.executescript(
        """
        CREATE TABLE text_candidate_builds(
            build_id TEXT PRIMARY KEY, source_snapshot_id TEXT, status TEXT,
            is_complete_corpus INTEGER, processed_post_count INTEGER
        );
        CREATE TABLE source_snapshots(
            snapshot_id TEXT PRIMARY KEY, snapshot_path TEXT,
            snapshot_sha256 TEXT, input_contract_status TEXT
        );
        CREATE TABLE text_candidate_corpus_members(
            build_id TEXT, source_post_id INTEGER, source_version INTEGER
        );
        CREATE TABLE source_post_versions(
            source_post_id INTEGER, source_version INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO text_candidate_builds VALUES ('build', 'snapshot', "
        "'finalized', 1, 2)"
    )
    connection.execute(
        "INSERT INTO source_snapshots VALUES ('snapshot', ?, ?, 'accepted')",
        (str(snapshot), snapshot_hash),
    )
    connection.executemany(
        "INSERT INTO text_candidate_corpus_members VALUES ('build', ?, 1)",
        [(1,), (2,)],
    )
    connection.executemany(
        "INSERT INTO source_post_versions VALUES (?, 1)", [(1,), (2,)]
    )
    connection.commit()
    connection.close()
    return derived, snapshot


def test_projection_rebuilds_clean_text_and_keeps_both_databases_readonly(
    tmp_path: Path,
) -> None:
    """投影须读取源字段、清除 Delta 结构并保持数据库字节不变。"""

    derived, snapshot = _build_fixture(tmp_path)
    before = (_sha256(derived), _sha256(snapshot))

    result = load_reference_text_projection(
        derived,
        candidate_build_id="build",
        config=load_text_config(TEXT_CONFIG_PATH),
    )

    assert result.by_identity[(1, 1)].normalized_model_text == (
        "[TITLE]\n标题\n[BODY]\n正文"
    )
    assert result.by_identity[(2, 1)].normalized_model_text == (
        "[TITLE]\n普通\n[BODY]\n普通正文"
    )
    assert result.format_counts == {"plain_text": 1, "quill_delta_json": 1}
    assert (_sha256(derived), _sha256(snapshot)) == before


def test_projection_rejects_snapshot_hash_mismatch(tmp_path: Path) -> None:
    """同一快照身份出现不同内容时必须失败关闭。"""

    derived, snapshot = _build_fixture(tmp_path)
    connection = sqlite3.connect(snapshot)
    connection.execute("UPDATE web_posts SET title = '篡改' WHERE id = 1")
    connection.commit()
    connection.close()

    with pytest.raises(ReferenceProjectionError) as error:
        load_reference_text_projection(
            derived,
            candidate_build_id="build",
            config=load_text_config(TEXT_CONFIG_PATH),
        )
    assert error.value.reason_code == "reference_source_snapshot_hash_mismatch"


def test_projection_rejects_snapshot_sqlite_sidecar(tmp_path: Path) -> None:
    """主文件哈希不得被 SQLite WAL 或日志旁文件绕过。"""

    derived, snapshot = _build_fixture(tmp_path)
    snapshot.with_name(snapshot.name + "-wal").write_bytes(b"unsealed")

    with pytest.raises(ReferenceProjectionError) as error:
        load_reference_text_projection(
            derived,
            candidate_build_id="build",
            config=load_text_config(TEXT_CONFIG_PATH),
        )

    assert error.value.reason_code == "reference_source_snapshot_sidecar_present"


def test_projection_database_value_error_uses_stable_reason_code(
    tmp_path: Path,
) -> None:
    """派生库类型污染不得泄漏原值或退化为裸 Python 异常。"""

    derived, _ = _build_fixture(tmp_path)
    connection = sqlite3.connect(derived)
    connection.execute(
        "UPDATE text_candidate_builds SET is_complete_corpus = 'invalid'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ReferenceProjectionError) as error:
        load_reference_text_projection(
            derived,
            candidate_build_id="build",
            config=load_text_config(TEXT_CONFIG_PATH),
        )

    assert error.value.reason_code == "reference_projection_database_contract_invalid"
    assert "invalid" not in str(error.value)


def test_projection_rejects_non_text_source_value(tmp_path: Path) -> None:
    """SQLite 动态类型不得把 BLOB Delta 转为可训练字符串。"""

    derived, snapshot = _build_fixture(tmp_path)
    connection = sqlite3.connect(snapshot)
    connection.execute(
        "UPDATE web_posts SET content_text = ? WHERE id = 1",
        (sqlite3.Binary(b'{"ops":[{"insert":"hidden"}]}'),),
    )
    connection.commit()
    connection.close()
    new_hash = _sha256(snapshot)
    connection = sqlite3.connect(derived)
    connection.execute(
        "UPDATE source_snapshots SET snapshot_sha256 = ?", (new_hash,)
    )
    connection.commit()
    connection.close()

    with pytest.raises(ReferenceProjectionError) as error:
        load_reference_text_projection(
            derived,
            candidate_build_id="build",
            config=load_text_config(TEXT_CONFIG_PATH),
        )

    assert error.value.reason_code == "reference_source_snapshot_value_invalid"


def test_projection_uri_encodes_snapshot_question_mark(tmp_path: Path) -> None:
    """文件名中的 URI 保留字符不得改变实际读取的冻结快照。"""

    derived, snapshot = _build_fixture(tmp_path)
    special = snapshot.with_name("snapshot?sealed.sqlite")
    snapshot.rename(special)
    connection = sqlite3.connect(derived)
    connection.execute(
        "UPDATE source_snapshots SET snapshot_path = ?, snapshot_sha256 = ?",
        (str(special), _sha256(special)),
    )
    connection.commit()
    connection.close()

    result = load_reference_text_projection(
        derived,
        candidate_build_id="build",
        config=load_text_config(TEXT_CONFIG_PATH),
    )

    assert result.by_identity[(1, 1)].normalized_model_text == (
        "[TITLE]\n标题\n[BODY]\n正文"
    )
