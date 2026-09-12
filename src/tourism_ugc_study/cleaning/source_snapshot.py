"""生成主题研究输入或全量原始内容归档，不执行清洗或模型推理。

源连接使用只读 URI、query_only 和单一读事务，包含事务开始前已提交的 WAL。
只复制内容表及其必要外键父表，不复制采集调度、登录态或媒体文件。新文件不
覆盖历史快照；只有create_current_snapshot更新输入指针，全量归档不改变人口。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FILTER = "topic_relevant = 1"
CONTRACT = "topic-relevant-source-snapshot-v1"


def file_sha256(path: Path) -> str:
    """流式返回既有文件的 SHA-256；不可读时传播 OSError。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _quote(name: str) -> str:
    """引用从 SQLite schema 取得的标识符，不把它解释为 SQL 表达式。"""
    return '"' + name.replace('"', '""') + '"'


def _rows_digest(rows: list[tuple[Any, ...]]) -> str:
    """对有序完整行编码计算摘要；未知非 JSON 字段类型失败，不隐式改写。"""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """在新暂存文件中写入私有 JSON；已存在时拒绝覆盖。"""
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _content_tables(source: sqlite3.Connection, *, topic_only: bool) -> list[tuple[str, str, str]]:
    """返回固定内容表及选择条件；全量归档也不复制账号、调度或发现记忆。"""
    predicate = FILTER if topic_only else "1"
    selected = "SELECT id FROM web_posts WHERE " + predicate
    tables = [
        ("schema_migrations", "1", "version"),
        ("source_platforms", "platform_key IN (SELECT platform_key FROM web_posts WHERE " + predicate + ")", "platform_key"),
        ("ctf_captures", "id IN (SELECT source_capture_id FROM web_posts WHERE " + predicate + ")", "id"),
        ("web_posts", predicate, "id"),
        ("web_post_images", "web_post_id IN (" + selected + ")", "id"),
    ]
    if source.execute("SELECT 1 FROM sqlite_master WHERE name='post_detail_repair_waivers' AND type='table'").fetchone():
        tables.append(("post_detail_repair_waivers", "web_post_id IN (" + selected + ")", "id"))
    return tables


def _copy_content(source: sqlite3.Connection, target: sqlite3.Connection, *, topic_only: bool = True) -> dict[str, Any]:
    """在同一个源读事务内复制筛选内容和必要关系，校验逐字段摘要与外键。

    SQL 条件固定，不接收任意过滤表达式。保留帖子/图片 ID、正文、全部列与
    显式索引；历史详情 waiver 仅保留所选帖子的记录，不改变其可用性。
    """
    source.execute("BEGIN")
    total = source.execute("SELECT count(*) FROM web_posts").fetchone()[0]
    columns = {row[1] for row in source.execute("PRAGMA table_info(web_posts)")}
    if "topic_relevant" not in columns:
        raise ValueError("source_topic_relevant_missing")
    invalid = source.execute(
        "SELECT count(*) FROM web_posts WHERE topic_relevant IS NULL "
        "OR topic_relevant NOT IN (0, 1) OR typeof(topic_relevant) != 'integer'"
    ).fetchone()[0]
    if invalid:
        raise ValueError("source_topic_relevant_invalid")
    tables = _content_tables(source, topic_only=topic_only)
    table_evidence: dict[str, Any] = {}
    for name, predicate, order in tables:
        ddl = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if not ddl:
            raise ValueError("source_required_table_missing:" + name)
        target.execute(ddl[0])
        names = [row[1] for row in source.execute("PRAGMA table_info(" + _quote(name) + ")")]
        fields = ",".join(map(_quote, names))
        rows = source.execute(f"SELECT {fields} FROM {_quote(name)} WHERE {predicate} ORDER BY {_quote(order)}").fetchall()
        placeholders = ",".join("?" for _ in names)
        target.executemany(f"INSERT INTO {_quote(name)} ({fields}) VALUES ({placeholders})", rows)
        for (index_sql,) in source.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name", (name,)):
            target.execute(index_sql)
        copied = target.execute(f"SELECT {fields} FROM {_quote(name)} ORDER BY {_quote(order)}").fetchall()
        digest = _rows_digest(rows)
        if digest != _rows_digest(copied):
            raise ValueError("snapshot_content_mismatch:" + name)
        table_evidence[name] = {"count": len(rows), "columns": names, "rows_sha256": digest}
    count = table_evidence["web_posts"]["count"]
    if not count:
        raise ValueError("source_selected_population_empty")
    if target.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("snapshot_foreign_key_check_failed")
    if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("snapshot_integrity_check_failed")
    image_count_mismatch = target.execute(
        "SELECT count(*) FROM web_posts p WHERE p.post_images_count != "
        "(SELECT count(*) FROM web_post_images i WHERE i.web_post_id=p.id)"
    ).fetchone()[0]
    if image_count_mismatch:
        raise ValueError("snapshot_post_images_count_mismatch")
    return {
        "source_total_posts": total,
        "selected_posts": count,
        "excluded_by_topic_flag": total - count,
        "table_evidence": table_evidence,
        "platform_counts": dict(target.execute("SELECT platform_key,count(*) FROM web_posts GROUP BY platform_key")),
        "foreign_key_check": "passed", "integrity_check": "passed",
        "row_content_preserved": True, "post_images_count_check": "passed",
    }


def create_current_snapshot(
    source_db: Path, output_db: Path, current_pointer: Path, *, code_version: str
) -> dict[str, Any]:
    """生成验收后的筛选快照及配对 manifest，并登记为当前研究输入。

    参数路径须指向不同文件，输出数据库及 manifest 必须不存在。旧指针内容
    保存在新 manifest 中，旧快照不移动、不删除、不改写。源库允许并发写入；
    完整行摘要绑定此次读事务，而源主文件前后哈希只是补充观察，不冒充含 WAL
    的事务快照哈希。失败不更新指针，暂存文件由本函数清理；已发布文件保留。
    """
    source_db = source_db.expanduser().resolve(strict=True)
    output_db = output_db.expanduser().resolve()
    current_pointer = current_pointer.expanduser().resolve()
    manifest_path = output_db.with_suffix(".manifest.json")
    paths = [source_db, output_db, manifest_path, current_pointer]
    if len(set(paths)) != len(paths):
        raise ValueError("snapshot_paths_overlap")
    if output_db.exists() or manifest_path.exists():
        raise FileExistsError("snapshot_output_already_exists")
    previous = json.loads(current_pointer.read_text()) if current_pointer.exists() else None
    started = datetime.now(timezone.utc).isoformat()
    source_before = file_sha256(source_db)
    output_db.parent.mkdir(parents=True, exist_ok=True)
    current_pointer.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".source-snapshot-", dir=output_db.parent) as stage:
        temporary = Path(stage)
        staged_db = temporary / "snapshot.sqlite"
        source = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(staged_db)
        os.chmod(staged_db, 0o600)
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute("PRAGMA trusted_schema=OFF")
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN")
            evidence = _copy_content(source, target)
            target.commit()
        finally:
            target.close()
            source.close()
        if any(Path(str(staged_db) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("snapshot_unexpected_sidecars")
        manifest = {
            "artifact_kind": "filtered-research-source-snapshot", "contract": CONTRACT,
            "status": "SOURCE_SNAPSHOT_READY_INFERENCE_NOT_RUN",
            "filter": {"table": "web_posts", "sql": FILTER},
            "source_path": str(source_db),
            "source_main_sha256_before": source_before,
            "source_main_sha256_after": file_sha256(source_db),
            "source_consistency": "single_read_transaction_including_committed_wal",
            "source_digest_scope": "main_file_only; selected transaction rows bound separately",
            "started_at_utc": started, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "code_version": code_version, "builder_sha256": file_sha256(Path(__file__)),
            "python_version": platform.python_version(), "sqlite_version": sqlite3.sqlite_version,
            "database": {"path": str(output_db), "sha256": file_sha256(staged_db), "size_bytes": staged_db.stat().st_size},
            "previous_current_pointer": previous,
            "source_write_count": 0, "source_records_deleted": 0,
            "model_loaded": False, "fit_call_count": 0, "predict_call_count": 0,
            "media_files_copied": False, "scope": "content_and_required_relations_only",
            **evidence,
        }
        staged_manifest = temporary / "manifest.json"
        _write_json(staged_manifest, manifest)
        # 同文件系统硬链接提供不可覆盖发布；只在二者已就绪后切换当前指针。
        os.link(staged_db, output_db)
        os.link(staged_manifest, manifest_path)
        pointer = {
            "contract": CONTRACT, "status": manifest["status"],
            "snapshot_path": str(output_db), "snapshot_sha256": manifest["database"]["sha256"],
            "manifest_path": str(manifest_path), "manifest_sha256": file_sha256(manifest_path),
            "selected_posts": evidence["selected_posts"], "filter": manifest["filter"],
            "activated_at_utc": manifest["completed_at_utc"],
            "historical_results_apply_to_current_snapshot": False,
        }
        with tempfile.TemporaryDirectory(prefix=".source-pointer-", dir=current_pointer.parent) as pointer_stage:
            staged_pointer = Path(pointer_stage) / "current.json"
            _write_json(staged_pointer, pointer)
            os.replace(staged_pointer, current_pointer)
    return pointer


def create_archival_snapshot(source_db: Path, output_db: Path, *, code_version: str) -> dict[str, Any]:
    """归档全部已入库图文记录，不筛选主题、不切换研究输入指针。

    使用单一只读事务复制原始全列、ID和图片关系；控制面不属于研究语料。
    输出及配对manifest必须不存在，失败不改源库；SQLite旁文件、外键或行摘要
    不一致均拒绝发布。媒体字节由独立归档层按此快照的路径/SHA绑定。
    """
    source_db = source_db.resolve(strict=True)
    output_db = output_db.resolve()
    manifest_path = output_db.with_suffix(".manifest.json")
    if source_db in (output_db, manifest_path):
        raise ValueError("snapshot_paths_overlap")
    if output_db.exists() or manifest_path.exists():
        raise FileExistsError("snapshot_output_already_exists")
    before = file_sha256(source_db)
    started = datetime.now(timezone.utc).isoformat()
    output_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".raw-snapshot-", dir=output_db.parent) as stage:
        staged = Path(stage) / "raw.sqlite"
        source = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(staged)
        os.chmod(staged, 0o600)
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute("PRAGMA trusted_schema=OFF")
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN")
            evidence = _copy_content(source, target, topic_only=False)
            topic_counts = dict(target.execute("SELECT topic_relevant,count(*) FROM web_posts GROUP BY topic_relevant"))
            target.commit()
        finally:
            target.close()
            source.close()
        if any(Path(str(staged) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("snapshot_unexpected_sidecars")
        result = {
            "artifact_kind": "raw-crawl-content-archive", "contract": "raw-crawl-content-archive-v1",
            "source_path": str(source_db), "source_consistency": "single_read_transaction_including_committed_wal",
            "source_main_sha256_before": before, "source_main_sha256_after": file_sha256(source_db),
            "source_digest_scope": "main_file_only; transaction rows bound separately",
            "source_write_count": 0, "source_records_deleted": 0, "filter": None,
            "scope": "all_web_posts_images_and_required_parents_without_control_plane",
            "started_at_utc": started, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "code_version": code_version, "builder_sha256": file_sha256(Path(__file__)),
            "python_version": platform.python_version(), "sqlite_version": sqlite3.sqlite_version,
            "database": {"path": str(output_db), "sha256": file_sha256(staged), "size_bytes": staged.stat().st_size},
            "topic_counts": topic_counts, **evidence,
        }
        staged_manifest = Path(stage) / "manifest.json"
        _write_json(staged_manifest, result)
        os.link(staged, output_db)
        os.link(staged_manifest, manifest_path)
    return result


def verify_topic_subset(archive_db: Path, reference_db: Path) -> dict[str, Any]:
    """逐列验证全量归档的主题子集与既有研究输入相同，不替换已清洗的输入。

    比较全部内容表、必要关系与有序完整行，而不是只比较数量或帖子ID。
    发现采集工作库已有实质变化时失败，不能把新的抓取内容拼进旧推理谱系。
    """
    archive = sqlite3.connect(archive_db.resolve().as_uri() + "?mode=ro", uri=True)
    reference = sqlite3.connect(reference_db.resolve().as_uri() + "?mode=ro", uri=True)
    result = {}
    try:
        for connection in (archive, reference):
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        for name, predicate, order in _content_tables(archive, topic_only=True):
            columns = [r[1] for r in archive.execute("PRAGMA table_info(" + _quote(name) + ")")]
            expected = [r[1] for r in reference.execute("PRAGMA table_info(" + _quote(name) + ")")]
            if columns != expected:
                raise ValueError("archive_research_schema_mismatch:" + name)
            fields = ",".join(map(_quote, columns))
            actual = archive.execute(f"SELECT {fields} FROM {_quote(name)} WHERE {predicate} ORDER BY {_quote(order)}").fetchall()
            prior = reference.execute(f"SELECT {fields} FROM {_quote(name)} ORDER BY {_quote(order)}").fetchall()
            digest = _rows_digest(actual)
            if len(actual) != len(prior) or digest != _rows_digest(prior):
                raise ValueError("archive_research_content_mismatch:" + name)
            result[name] = {"count": len(actual), "rows_sha256": digest}
    finally:
        archive.close()
        reference.close()
    return result
