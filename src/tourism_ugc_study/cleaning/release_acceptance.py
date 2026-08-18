"""formal 分析发布的不可伪造验收服务。

本模块把质量门重检、冻结 SQLite 的只读复验、artifact 校验、attestation 构造
和接受事务集中在一个边界内。调用方只提供发布身份与已经重建验证过的本地包
目录，不能提供或武装任意证明哈希。真实文件复验成功后，本模块才在当前连接
内部临时注册严格按 ``attestation -> run -> release`` 消费的 guard；提交或回滚
后始终恢复恒假实现，避免把应用授权能力泄漏给普通仓储调用方。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .release_artifact import ReleaseArtifactError, verify_release_artifact
from .snapshot import open_source_readonly, sha256_file


class ReleaseAcceptanceServiceError(RuntimeError):
    """表示 formal 验收质量门、文件证明或事务不满足。

    ``reason_code`` 为去敏机器码；固定异常消息不会包含绝对路径、SQLite 详情或
    原始内容。调用方收到异常时不得写 attestation 或改变 accepted 状态。
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__("release acceptance service failed")
        self.reason_code = reason_code


class ReleaseAcceptanceVerificationError(ReleaseAcceptanceServiceError):
    """表示快照或 artifact 无法形成可信 formal 验收证明。"""


@dataclass(frozen=True)
class ReleaseAcceptanceEvidence:
    """可直接写入验收 attestation 表的完整文件复验证明。

    快照字段同时证明 URI ``mode=ro``、``query_only=1``、``quick_check=ok``、
    前后文件 SHA/size 与冻结父表一致；artifact 哈希来自通过固定文件集合校验后
    的 ``artifact-manifest.json``。``attestation_sha256`` 覆盖其余全部字段和
    ``verified_at_utc``，可绑定一次连接级接受 guard。
    """

    release_id: str
    run_id: str
    snapshot_sha256: str
    snapshot_size_bytes: int
    snapshot_access_mode: str
    snapshot_query_only: int
    snapshot_integrity_check: str
    artifact_manifest_sha256: str
    attestation_sha256: str
    verified_at_utc: str


@dataclass(frozen=True)
class ReleaseAcceptanceRequest:
    """验收服务所需的最小请求，不允许调用方传入证明摘要。

    质量门、快照哈希、artifact manifest 哈希和父表状态一律由服务在派生库中重读。
    ``artifact_dir`` 只是已发布包位置，不参与科研身份，也不会写入 attestation。
    """

    release_id: str
    run_id: str
    artifact_dir: Path


@dataclass(frozen=True)
class ReleaseAcceptanceOutcome:
    """验收事务提交后的最小状态回执。"""

    release_id: str
    run_id: str
    seal_status: str
    run_status: str
    reused: bool


@dataclass
class _SequentialGuard:
    """记录当前连接 guard 已消费到的阶段，仅在一次事务内存活。"""

    evidence: ReleaseAcceptanceEvidence
    next_stage_index: int = 0

    _STAGES = ("attestation", "run", "release")

    def __call__(self, *candidate: object) -> int:
        """只为完整证据字段和当前唯一合法阶段返回一次真值。

        参数数量、类型、值或阶段不匹配时返回 0 且不推进状态；合法调用一旦
        消费便不能重放。该闭包有状态，注册 SQLite UDF 时不得标记 deterministic。
        """

        if self.next_stage_index >= len(self._STAGES):
            return 0
        expected = _guard_arguments(
            self.evidence, stage=self._STAGES[self.next_stage_index]
        )
        if tuple(candidate) != expected:
            return 0
        self.next_stage_index += 1
        return 1

    @property
    def fully_consumed(self) -> bool:
        """返回三个授权阶段是否都恰好消费一次。"""

        return self.next_stage_index == len(self._STAGES)


def _canonical_sha256(value: object) -> str:
    """计算键序稳定的 JSON SHA-256，用于绑定完整证明字段。"""

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    """分块计算 artifact manifest 哈希，避免读取大小假设。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_acceptance_evidence(
    *,
    release_id: str,
    run_id: str,
    snapshot_path: str | Path,
    expected_snapshot_sha256: str,
    expected_snapshot_size_bytes: int,
    artifact_dir: str | Path,
    expected_artifact_manifest_sha256: str,
    verified_at_utc: str,
) -> ReleaseAcceptanceEvidence:
    """当场复验冻结 SQLite 快照和不可变 artifact，并返回证明。

    快照必须是非符号链接普通文件。函数在只读打开前后分别比较 SHA-256 与
    size，中间使用项目统一的 ``open_source_readonly``（URI ``mode=ro``）打开，
    断言 ``PRAGMA query_only`` 为 1 且 ``PRAGMA quick_check`` 只返回 ``ok``。
    任意字节文件、损坏 SQLite、竞态修改、快照父表不一致、包篡改或 artifact
    manifest 不一致均抛出去敏异常，不返回部分证明。
    """

    snapshot = Path(snapshot_path)
    artifact = Path(artifact_dir)
    try:
        if not snapshot.is_file() or snapshot.is_symlink():
            raise ReleaseAcceptanceVerificationError("snapshot_file_missing")
        size_before = snapshot.stat().st_size
        sha_before = sha256_file(snapshot)
        if (
            size_before != expected_snapshot_size_bytes
            or sha_before != expected_snapshot_sha256
        ):
            raise ReleaseAcceptanceVerificationError("snapshot_sha256_mismatch")

        try:
            # sqlite3.Connection 的上下文管理器只控制事务，不负责 close；显式
            # closing 保证 quick_check 成功或失败后都立即释放只读文件句柄。
            with closing(open_source_readonly(snapshot)) as source:
                query_only = int(source.execute("PRAGMA query_only").fetchone()[0])
                quick_check_rows = tuple(
                    str(row[0]) for row in source.execute("PRAGMA quick_check").fetchall()
                )
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ReleaseAcceptanceVerificationError(
                "snapshot_integrity_check_failed"
            ) from exc
        if query_only != 1:
            raise ReleaseAcceptanceVerificationError("snapshot_not_query_only")
        if quick_check_rows != ("ok",):
            raise ReleaseAcceptanceVerificationError(
                "snapshot_integrity_check_failed"
            )

        size_after = snapshot.stat().st_size
        sha_after = sha256_file(snapshot)
        if size_after != size_before or sha_after != sha_before:
            raise ReleaseAcceptanceVerificationError("snapshot_changed_during_acceptance")

        verify_release_artifact(artifact)
        artifact_manifest_sha256 = _file_sha256(
            artifact / "artifact-manifest.json"
        )
        if artifact_manifest_sha256 != expected_artifact_manifest_sha256:
            raise ReleaseAcceptanceVerificationError(
                "release_artifact_manifest_mismatch"
            )
    except ReleaseAcceptanceVerificationError:
        raise
    except (OSError, ReleaseArtifactError) as exc:
        raise ReleaseAcceptanceVerificationError("release_artifact_invalid") from exc

    payload = {
        "release_id": release_id,
        "run_id": run_id,
        "snapshot_sha256": sha_after,
        "snapshot_size_bytes": size_after,
        "snapshot_access_mode": "mode=ro",
        "snapshot_query_only": query_only,
        "snapshot_integrity_check": "ok",
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "verified_at_utc": verified_at_utc,
    }
    return ReleaseAcceptanceEvidence(
        **payload,
        attestation_sha256=_canonical_sha256(payload),
    )


def _guard_arguments(
    evidence: ReleaseAcceptanceEvidence,
    *,
    stage: str,
) -> tuple[object, ...]:
    """按 v25 trigger 协议规范化完整证明字段与阶段。

    参数顺序是 schema 与服务之间的内部协议：运行、发布、快照哈希/大小、只读
    证据、artifact 哈希、attestation 哈希、复验时间和阶段。绑定全部字段可防止
    只猜中父表三元组便复用授权。
    """

    return (
        evidence.run_id,
        evidence.release_id,
        evidence.snapshot_sha256,
        evidence.snapshot_size_bytes,
        evidence.snapshot_access_mode,
        evidence.snapshot_query_only,
        evidence.snapshot_integrity_check,
        evidence.artifact_manifest_sha256,
        evidence.attestation_sha256,
        evidence.verified_at_utc,
        stage,
    )


def _deny_acceptance(*candidate: object) -> int:
    """恢复普通连接的恒假 guard；任何参数组合都不能授权。"""

    del candidate
    return 0


def _install_sequential_guard(
    connection: sqlite3.Connection,
    evidence: ReleaseAcceptanceEvidence,
) -> _SequentialGuard:
    """在当前连接内部安装一次性 guard，不向仓储暴露武装接口。"""

    guard = _SequentialGuard(evidence=evidence)
    connection.create_function(
        "release_acceptance_guard",
        -1,
        guard,
        # guard 会消费阶段，故绝不能声明为确定性 SQL 函数。
        deterministic=False,
    )
    return guard


def _restore_deny_guard(connection: sqlite3.Connection) -> None:
    """无论提交或回滚都把连接恢复为无条件拒绝状态。"""

    connection.create_function(
        "release_acceptance_guard",
        -1,
        _deny_acceptance,
        deterministic=True,
    )


def _require_release_state(
    connection: sqlite3.Connection,
    *,
    request: ReleaseAcceptanceRequest,
) -> sqlite3.Row:
    """按显式身份重读发布、运行、快照和 artifact 父证据。

    不存在或 artifact manifest 不唯一时安全失败，不选择其他发布或最新记录。
    """

    row = connection.execute(
        """
        SELECT release.*, run.status AS run_status,
               snapshot.snapshot_path, snapshot.snapshot_sha256,
               snapshot.snapshot_size_bytes, snapshot.input_contract_status,
               artifact.manifest_sha256 AS artifact_manifest_sha256
        FROM analysis_release_builds AS release
        JOIN cleaning_runs AS run ON run.run_id = release.run_id
        JOIN source_snapshots AS snapshot
          ON snapshot.snapshot_id = release.source_snapshot_id
        LEFT JOIN analysis_release_manifests AS artifact
          ON artifact.release_id = release.release_id
         AND artifact.manifest_kind = 'artifact'
        WHERE release.release_id = ? AND release.run_id = ?
        """,
        (request.release_id, request.run_id),
    ).fetchone()
    if row is None:
        raise ReleaseAcceptanceServiceError("analysis_release_not_found")
    if row["artifact_manifest_sha256"] is None:
        raise ReleaseAcceptanceServiceError("release_artifact_manifest_mismatch")
    return row


def _validate_quality_gates(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    request: ReleaseAcceptanceRequest,
) -> None:
    """从冻结父记录重新验证 formal 接受所需的全部结构质量门。

    本函数不信任构建时计数或调用方自报通过状态；数据库决定、任务和输入契约
    均在当前事务中重读。
    """

    if row["release_mode"] != "formal":
        raise ReleaseAcceptanceServiceError("smoke_release_cannot_be_accepted")
    if row["seal_status"] not in {"finalized", "accepted"}:
        raise ReleaseAcceptanceServiceError("finalized_formal_release_required")
    if row["input_contract_status"] != "accepted":
        raise ReleaseAcceptanceServiceError("source_snapshot_input_rejected")
    if connection.execute(
        """
        SELECT 1 FROM post_decisions
        WHERE decision_build_id = ? AND decision_action = 'review' LIMIT 1
        """,
        (row["post_decision_build_id"],),
    ).fetchone() is not None:
        raise ReleaseAcceptanceServiceError("post_review_decisions_remaining")
    if connection.execute(
        """
        SELECT 1 FROM stage_tasks
        WHERE run_id = ? AND required = 1 AND status NOT IN ('succeeded', 'skipped')
        LIMIT 1
        """,
        (request.run_id,),
    ).fetchone() is not None:
        raise ReleaseAcceptanceServiceError("required_tasks_incomplete")


def _verify_from_state(
    *,
    row: sqlite3.Row,
    request: ReleaseAcceptanceRequest,
    verified_at_utc: str,
) -> ReleaseAcceptanceEvidence:
    """只使用数据库父证据复验文件，调用方不能替换期望哈希。"""

    return verify_acceptance_evidence(
        release_id=request.release_id,
        run_id=request.run_id,
        snapshot_path=Path(str(row["snapshot_path"])),
        expected_snapshot_sha256=str(row["snapshot_sha256"]),
        expected_snapshot_size_bytes=int(row["snapshot_size_bytes"]),
        artifact_dir=request.artifact_dir,
        expected_artifact_manifest_sha256=str(row["artifact_manifest_sha256"]),
        verified_at_utc=verified_at_utc,
    )


def _verify_stored_attestation(
    connection: sqlite3.Connection,
    *,
    evidence: ReleaseAcceptanceEvidence,
) -> None:
    """幂等接受时复核既有证明的稳定文件字段，不重写历史复验时间。"""

    stored = connection.execute(
        """
        SELECT * FROM analysis_release_acceptance_attestations
        WHERE release_id = ? AND run_id = ?
        """,
        (evidence.release_id, evidence.run_id),
    ).fetchone()
    if stored is None:
        raise ReleaseAcceptanceServiceError("release_acceptance_attestation_missing")
    stable_fields = (
        "snapshot_sha256",
        "snapshot_size_bytes",
        "snapshot_access_mode",
        "snapshot_query_only",
        "snapshot_integrity_check",
        "artifact_manifest_sha256",
    )
    if any(stored[field] != getattr(evidence, field) for field in stable_fields):
        raise ReleaseAcceptanceServiceError(
            "release_acceptance_attestation_mismatch"
        )


def _insert_attestation(
    connection: sqlite3.Connection,
    *,
    evidence: ReleaseAcceptanceEvidence,
) -> None:
    """写入经过当前事务文件复验的不可变证明。"""

    connection.execute(
        """
        INSERT INTO analysis_release_acceptance_attestations(
          release_id, run_id, snapshot_sha256, snapshot_size_bytes,
          snapshot_access_mode, snapshot_query_only,
          snapshot_integrity_check, artifact_manifest_sha256,
          attestation_sha256, verified_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence.release_id,
            evidence.run_id,
            evidence.snapshot_sha256,
            evidence.snapshot_size_bytes,
            evidence.snapshot_access_mode,
            evidence.snapshot_query_only,
            evidence.snapshot_integrity_check,
            evidence.artifact_manifest_sha256,
            evidence.attestation_sha256,
            evidence.verified_at_utc,
        ),
    )


def accept_verified_release(
    connection: sqlite3.Connection,
    *,
    request: ReleaseAcceptanceRequest,
    accepted_at_utc: str,
) -> ReleaseAcceptanceOutcome:
    """复验并原子接受一个 formal finalized 发布。

    执行顺序为：重读质量门；若已接受则重新只读验证文件并核对历史证明；否则
    开启 ``BEGIN IMMEDIATE``，再次重读质量门和真实文件，随后安装只绑定完整
    证明的顺序 guard，依次写 attestation、接受 run、接受 release，再提交。
    任一步失败都会回滚；guard 在提交或回滚后均恢复恒假，不存在部分接受状态。
    """

    # 不信任连接由谁创建或此前注册过什么同名 UDF；服务入口先撤销任何遗留
    # 授权，只有本次真实文件复验完成后才安装内部的一次性闭包。
    _restore_deny_guard(connection)
    row = _require_release_state(connection, request=request)
    _validate_quality_gates(connection, row=row, request=request)
    if row["seal_status"] == "accepted":
        if row["run_status"] != "accepted":
            raise ReleaseAcceptanceServiceError("accepted_release_run_mismatch")
        evidence = _verify_from_state(
            row=row,
            request=request,
            verified_at_utc=accepted_at_utc,
        )
        _verify_stored_attestation(connection, evidence=evidence)
        return ReleaseAcceptanceOutcome(
            request.release_id,
            request.run_id,
            "accepted",
            "accepted",
            True,
        )
    if row["run_status"] == "accepted":
        raise ReleaseAcceptanceServiceError(
            "run_already_accepted_by_other_release"
        )

    guard: _SequentialGuard | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _require_release_state(connection, request=request)
        _validate_quality_gates(connection, row=current, request=request)
        if current["seal_status"] != "finalized":
            raise ReleaseAcceptanceServiceError("finalized_formal_release_required")
        if current["run_status"] == "accepted":
            raise ReleaseAcceptanceServiceError(
                "run_already_accepted_by_other_release"
            )

        # 文件 I/O 在写事务内完成，避免复验与状态迁移之间留下可竞争窗口。
        evidence = _verify_from_state(
            row=current,
            request=request,
            verified_at_utc=accepted_at_utc,
        )
        guard = _install_sequential_guard(connection, evidence)
        _insert_attestation(connection, evidence=evidence)
        run_update = connection.execute(
            """
            UPDATE cleaning_runs
            SET status = 'accepted', reason_code = 'analysis_release_accepted',
                finished_at_utc = ?, updated_at_utc = ?
            WHERE run_id = ? AND status != 'accepted'
            """,
            (accepted_at_utc, accepted_at_utc, request.run_id),
        )
        if run_update.rowcount != 1:
            raise ReleaseAcceptanceServiceError("release_acceptance_run_conflict")
        release_update = connection.execute(
            """
            UPDATE analysis_release_builds
            SET seal_status = 'accepted', accepted_at_utc = ?
            WHERE release_id = ? AND run_id = ? AND seal_status = 'finalized'
            """,
            (accepted_at_utc, request.release_id, request.run_id),
        )
        if release_update.rowcount != 1:
            raise ReleaseAcceptanceServiceError("release_acceptance_release_conflict")
        if not guard.fully_consumed:
            raise ReleaseAcceptanceServiceError("release_acceptance_guard_incomplete")

        persisted = _require_release_state(connection, request=request)
        if (
            persisted["seal_status"] != "accepted"
            or persisted["run_status"] != "accepted"
        ):
            raise ReleaseAcceptanceServiceError("release_acceptance_not_persisted")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        # 即使 trigger、磁盘 I/O、提交或回滚抛错，也不能让授权留在复用连接上。
        _restore_deny_guard(connection)

    return ReleaseAcceptanceOutcome(
        request.release_id,
        request.run_id,
        "accepted",
        "accepted",
        False,
    )
