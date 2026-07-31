"""图片清洗运行、冻结快照与本地清单的统一身份契约。

本模块只负责校验不可变谱系，不解析清单、不打开图片、也不写派生表。图片角色、
指纹、候选和调度投影在复用或写入证据前都调用同一入口，避免各阶段对“本次
运行究竟绑定哪份配置和快照”产生不同解释。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import CleaningConfig, matches_frozen_run
from .schema import DERIVED_SCHEMA_VERSION
from .snapshot import open_source_readonly, sha256_file


class ImageContractError(RuntimeError):
    """运行谱系不完整或身份不一致时抛出的去敏异常。

    `reason_code` 只表达可公开的失败类别，不包含快照路径、清单路径或源字段。
    校验失败表示当前操作不得读取既有结果或写入新证据，不属于图片模型失败。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("image run contract validation failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ImageRunContract:
    """通过统一校验后的图片运行身份。

    字段分别固定运行、快照、快照文件摘要、可选清单及图片根目录摘要。路径只在
    进程内用于只读打开冻结快照，不应出现在日志或公开回执中。对象一经返回，
    调用方即可在本次操作内安全复用对应证据；数据库仍是最终身份来源。
    """

    run_id: str
    source_snapshot_id: str
    snapshot_path: Path
    snapshot_sha256: str
    manifest_id: str | None
    root_identity_sha256: str | None


@contextmanager
def open_image_snapshot_readonly(
    contract: ImageRunContract,
) -> Iterator[sqlite3.Connection]:
    """按已校验契约安全重开冻结源快照，并统一隐藏底层读取错误。

    输入必须是 :func:`validate_image_run_contract` 返回的运行契约。上下文只以
    SQLite ``mode=ro`` 和 ``query_only`` 连接冻结快照，向调用方暂时提供查询
    连接，并在退出时关闭它；函数自身不写源库或派生库。文件在契约校验后消失、
    权限改变，或打开、查询、迭代及关闭期间发生 ``OSError``/SQLite 错误时，
    均转换为只含 ``snapshot_unreadable`` 的 :class:`ImageContractError`。因此
    调用方不得把可能包含机器路径的底层异常文本写入回执、日志或数据库。
    """

    try:
        # try 必须包围 yield：只有这样，调用方 execute/fetch/迭代期间抛出的
        # SQLite 异常才会沿同一去敏边界返回，而非越过仓储层泄露原始消息。
        with open_source_readonly(contract.snapshot_path) as source:
            yield source
    except (OSError, sqlite3.Error) as exc:
        raise ImageContractError("snapshot_unreadable") from exc


def validate_image_run_contract(
    connection: sqlite3.Connection,
    *,
    config: CleaningConfig,
    run_id: str | None = None,
    source_snapshot_id: str | None = None,
    manifest_id: str | None = None,
    root_identity_sha256: str | None = None,
) -> ImageRunContract:
    """校验并返回图片阶段共同使用的冻结运行契约。

    输入必须通过显式 `run_id` 与 `source_snapshot_id`，或通过已导入的
    `manifest_id` 唯一解析。函数核对运行配置摘要、协议、派生 schema 版本、
    快照接收状态、运行与快照双向绑定、清单归属、图片根摘要以及磁盘快照实际
    SHA-256。任何不一致都在调用方写入或复用证据前失败；函数自身无写操作，
    重复调用结果相同。路径缺失和摘要漂移均按身份失败处理，不泄露本地路径。
    """

    if manifest_id is None and (run_id is None or source_snapshot_id is None):
        raise ImageContractError("image_contract_identity_incomplete")
    row = connection.execute(
        """
        SELECT r.run_id, r.config_sha256, r.protocol_version,
               r.source_snapshot_id AS run_snapshot_id, r.status AS run_status,
               s.snapshot_id, s.run_id AS snapshot_run_id, s.snapshot_path,
               s.snapshot_sha256, s.input_contract_status,
               i.manifest_id, i.run_id AS manifest_run_id,
               i.source_snapshot_id AS manifest_snapshot_id,
               i.root_identity_sha256
        FROM cleaning_runs AS r
        JOIN source_snapshots AS s ON s.run_id = r.run_id
        LEFT JOIN image_manifest_imports AS i
          ON i.run_id = r.run_id AND i.manifest_id = ?
        WHERE (? IS NOT NULL AND i.manifest_id = ?)
           OR (? IS NULL AND r.run_id = ? AND s.snapshot_id = ?)
        """,
        (
            manifest_id,
            manifest_id,
            manifest_id,
            manifest_id,
            run_id,
            source_snapshot_id,
        ),
    ).fetchone()
    if row is None:
        reason = "image_manifest_missing" if manifest_id is not None else "image_run_or_snapshot_not_found"
        raise ImageContractError(reason)

    resolved_run_id = str(row["run_id"])
    resolved_snapshot_id = str(row["snapshot_id"])
    if run_id is not None and run_id != resolved_run_id:
        raise ImageContractError("image_run_mismatch")
    if source_snapshot_id is not None and source_snapshot_id != resolved_snapshot_id:
        raise ImageContractError("image_snapshot_mismatch")
    if row["run_snapshot_id"] != resolved_snapshot_id:
        raise ImageContractError("image_snapshot_mismatch")
    if row["snapshot_run_id"] != resolved_run_id:
        raise ImageContractError("image_snapshot_run_mismatch")
    if manifest_id is not None and (
        row["manifest_run_id"] != resolved_run_id
        or row["manifest_snapshot_id"] != resolved_snapshot_id
    ):
        raise ImageContractError("image_manifest_lineage_mismatch")
    if not matches_frozen_run(
        config,
        str(row["config_sha256"]),
        str(row["protocol_version"]),
    ):
        raise ImageContractError("run_config_mismatch")
    if config.algorithm_versions.get("derived_schema") != DERIVED_SCHEMA_VERSION:
        raise ImageContractError("derived_schema_version_mismatch")
    if row["input_contract_status"] != "accepted" or row["run_status"] == "input_rejected":
        raise ImageContractError("snapshot_input_rejected")
    if root_identity_sha256 is not None and row["root_identity_sha256"] != root_identity_sha256:
        raise ImageContractError("manifest_root_identity_conflict")

    snapshot_path = Path(str(row["snapshot_path"]))
    expected_snapshot_sha256 = str(row["snapshot_sha256"])
    if not snapshot_path.is_file():
        raise ImageContractError("snapshot_unreadable")
    try:
        actual_snapshot_sha256 = sha256_file(snapshot_path)
    except OSError as exc:
        # OSError 常包含机器绝对路径；只把固定 reason_code 交给仓储和 CLI，
        # 原异常仅保留为进程内 cause，不得进入标准输出或持久化详情。
        raise ImageContractError("snapshot_unreadable") from exc
    if actual_snapshot_sha256 != expected_snapshot_sha256:
        raise ImageContractError("snapshot_sha256_mismatch")
    return ImageRunContract(
        run_id=resolved_run_id,
        source_snapshot_id=resolved_snapshot_id,
        snapshot_path=snapshot_path,
        snapshot_sha256=expected_snapshot_sha256,
        manifest_id=None if row["manifest_id"] is None else str(row["manifest_id"]),
        root_identity_sha256=(
            None if row["root_identity_sha256"] is None else str(row["root_identity_sha256"])
        ),
    )
