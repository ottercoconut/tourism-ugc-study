"""从冻结采集快照重建参考集使用的规范化文本投影。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .text_config import TextCleaningConfig
from .text_normalize import normalize_post_text


class ReferenceProjectionError(ValueError):
    """冻结源快照或规范化投影违反契约时的稳定失败。

    异常消息只使用稳定 reason code，不回显正文、作者身份或本机路径。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化只公开稳定失败码的投影异常。

        Args:
            reason_code: 供调用方、CLI 和测试使用的稳定失败码。
        """

        super().__init__("reference text projection failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ProjectedReferenceText:
    """单个稳定源身份从冻结快照得到的模型文本。

    Attributes:
        source_post_id: 派生库与冻结源快照共享的稳定帖子身份。
        source_version: 候选构建冻结时的帖子版本。
        normalized_model_text: 可进入候选计算或最终 CSV 的模型文本。
        normalized_sha256: 模型文本的 SHA-256。
        structure_status: 结构三分状态。
        structure_reason_code: 不含正文的稳定结构原因。
        output_sha256: 规则、运行时、文本和证据共同形成的输出摘要。
    """

    source_post_id: int
    source_version: int
    normalized_model_text: str
    normalized_sha256: str
    structure_status: str
    structure_reason_code: str
    output_sha256: str


@dataclass(frozen=True)
class ReferenceTextProjection:
    """候选人口的冻结文本投影及不含正文的谱系摘要。

    Attributes:
        by_identity: 稳定源身份到文本投影的只读映射。
        source_snapshot_id: 候选构建绑定的冻结源快照身份。
        source_snapshot_sha256: 冻结源快照文件摘要。
        normalization_rule_id: 当前结构提取和文本规范化规则身份。
        member_sha256: 全人口投影成员摘要。
        format_counts: 正文结构格式的去敏计数。
        invalid_reason_counts: 结构不可用原因的去敏计数。
    """

    by_identity: Mapping[tuple[int, int], ProjectedReferenceText]
    source_snapshot_id: str
    source_snapshot_sha256: str
    normalization_rule_id: str
    member_sha256: str
    format_counts: Mapping[str, int]
    invalid_reason_counts: Mapping[str, int]


def _readonly_connection(path: str | Path, reason_code: str) -> sqlite3.Connection:
    """以 URI 只读模式打开 SQLite，并隐藏本机路径。"""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise ReferenceProjectionError(reason_code) from exc


def _file_sha256(path: Path) -> str:
    """流式计算源快照哈希，避免把大型 SQLite 文件读入内存。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReferenceProjectionError("reference_source_snapshot_unreadable") from exc
    return digest.hexdigest()


def _reject_sqlite_sidecars(path: Path) -> None:
    """拒绝会使单文件哈希无法覆盖真实读取状态的 SQLite 旁文件。

    Args:
        path: 已解析的冻结 SQLite 主文件。

    Raises:
        ReferenceProjectionError: 存在 WAL、共享内存或回滚日志旁文件。

    Notes:
        SQLite 普通只读连接仍会合并已提交 WAL。冻结快照契约只绑定主文件
        SHA-256，因此必须先证明没有任何可改变可见页面的旁文件。
    """

    if any(
        path.with_name(path.name + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ReferenceProjectionError("reference_source_snapshot_sidecar_present")


def _canonical_sha256(value: object) -> str:
    """计算不含路径和时间的规范 JSON 哈希。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_reference_text_projection(
    derived_db: str | Path,
    *,
    candidate_build_id: str,
    config: TextCleaningConfig,
) -> ReferenceTextProjection:
    """从候选构建绑定的冻结采集快照重做规范化文本。

    Args:
        derived_db: 保存候选身份和冻结源快照谱系的只读派生库。
        candidate_build_id: 最终参考流程使用的完整候选人口身份。
        config: 当前冻结的结构提取与文本规范化规则。

    Returns:
        覆盖候选人口全部身份的不可变文本投影。

    Raises:
        ReferenceProjectionError: 候选构建不完整、快照哈希不匹配、成员缺失、
            身份重复，或数据库无法保持只读。

    Notes:
        本函数从不回写正式采集库或派生库。旧派生文本仅保留为输入谱系，不能
        作为新的模型字段来源。
    """

    derived = _readonly_connection(
        derived_db, "reference_projection_database_readonly_open_failed"
    )
    try:
        build = derived.execute(
            """
            SELECT source_snapshot_id, status, is_complete_corpus,
                   processed_post_count
            FROM text_candidate_builds WHERE build_id = ?
            """,
            (candidate_build_id,),
        ).fetchone()
        if build is None or build["status"] != "finalized" or not int(
            build["is_complete_corpus"]
        ):
            raise ReferenceProjectionError("reference_projection_build_not_finalized")
        snapshot = derived.execute(
            """
            SELECT snapshot_path, snapshot_sha256, input_contract_status
            FROM source_snapshots WHERE snapshot_id = ?
            """,
            (str(build["source_snapshot_id"]),),
        ).fetchone()
        if snapshot is None or str(snapshot["input_contract_status"]) != "accepted":
            raise ReferenceProjectionError("reference_projection_snapshot_not_accepted")
        members = derived.execute(
            """
            SELECT c.source_post_id, c.source_version
            FROM text_candidate_corpus_members AS c
            JOIN source_post_versions AS v
              ON v.source_post_id = c.source_post_id
             AND v.source_version = c.source_version
            WHERE c.build_id = ?
            ORDER BY c.source_post_id, c.source_version
            """,
            (candidate_build_id,),
        ).fetchall()
        if int(derived.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise ReferenceProjectionError("reference_projection_database_not_query_only")
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise ReferenceProjectionError(
            "reference_projection_database_contract_invalid"
        ) from exc
    finally:
        derived.close()
    try:
        processed_post_count = int(build["processed_post_count"])
        if any(
            type(row[0]) is not int
            or type(row[1]) is not int
            or row[0] <= 0
            or row[1] <= 0
            for row in members
        ):
            raise ValueError
        identities = [(row[0], row[1]) for row in members]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceProjectionError(
            "reference_projection_database_contract_invalid"
        ) from exc
    if len(members) != processed_post_count:
        raise ReferenceProjectionError("reference_projection_population_incomplete")
    if len(set(identities)) != len(identities):
        raise ReferenceProjectionError("reference_projection_identity_not_unique")
    try:
        snapshot_path = Path(str(snapshot["snapshot_path"])).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ReferenceProjectionError("reference_source_snapshot_not_found") from exc
    _reject_sqlite_sidecars(snapshot_path)
    if _file_sha256(snapshot_path) != str(snapshot["snapshot_sha256"]):
        raise ReferenceProjectionError("reference_source_snapshot_hash_mismatch")
    source = _readonly_connection(
        snapshot_path, "reference_source_snapshot_readonly_open_failed"
    )
    wanted_ids = {identity[0] for identity in identities}
    try:
        source.execute("BEGIN")
        rows = source.execute(
            """
            SELECT id, title, content_text, status
            FROM web_posts ORDER BY id
            """
        ).fetchall()
        if int(source.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise ReferenceProjectionError("reference_source_snapshot_not_query_only")
    except sqlite3.Error as exc:
        raise ReferenceProjectionError("reference_source_snapshot_contract_invalid") from exc
    finally:
        source.close()
    _reject_sqlite_sidecars(snapshot_path)
    if _file_sha256(snapshot_path) != str(snapshot["snapshot_sha256"]):
        raise ReferenceProjectionError("reference_source_snapshot_hash_mismatch")
    try:
        source_by_id: dict[int, sqlite3.Row] = {}
        for row in rows:
            source_post_id = row["id"]
            if type(source_post_id) is not int or source_post_id <= 0:
                raise ValueError
            if source_post_id not in wanted_ids:
                continue
            if any(
                value is not None and not isinstance(value, str)
                for value in (row["title"], row["content_text"], row["status"])
            ):
                raise ValueError
            source_by_id[source_post_id] = row
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceProjectionError("reference_source_snapshot_value_invalid") from exc
    if set(source_by_id) != wanted_ids:
        raise ReferenceProjectionError("reference_source_snapshot_member_missing")
    projections: dict[tuple[int, int], ProjectedReferenceText] = {}
    format_counts: Counter[str] = Counter()
    invalid_counts: Counter[str] = Counter()
    for source_post_id, source_version in identities:
        row = source_by_id[source_post_id]
        normalized = normalize_post_text(
            row["title"],
            row["content_text"],
            source_status=row["status"],
            config=config,
        )
        body_evidence = normalized.evidence["structured_text"]["body"]
        format_counts[str(body_evidence["format_id"])] += 1
        if normalized.structure_status != "usable":
            invalid_counts[normalized.structure_reason_code] += 1
        projections[(source_post_id, source_version)] = ProjectedReferenceText(
            source_post_id=source_post_id,
            source_version=source_version,
            normalized_model_text=normalized.model_text,
            normalized_sha256=normalized.normalized_sha256,
            structure_status=normalized.structure_status,
            structure_reason_code=normalized.structure_reason_code,
            output_sha256=normalized.output_sha256,
        )
    member_payload = [
        {
            "identity": [item.source_post_id, item.source_version],
            "normalized_sha256": item.normalized_sha256,
            "output_sha256": item.output_sha256,
            "structure_reason_code": item.structure_reason_code,
            "structure_status": item.structure_status,
        }
        for item in projections.values()
    ]
    return ReferenceTextProjection(
        by_identity=MappingProxyType(projections),
        source_snapshot_id=str(build["source_snapshot_id"]),
        source_snapshot_sha256=str(snapshot["snapshot_sha256"]),
        normalization_rule_id=config.version_lock,
        member_sha256=_canonical_sha256(member_payload),
        format_counts=MappingProxyType(dict(sorted(format_counts.items()))),
        invalid_reason_counts=MappingProxyType(dict(sorted(invalid_counts.items()))),
    )
