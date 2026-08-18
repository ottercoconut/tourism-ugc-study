#!/usr/bin/env python3
"""构建、校验、查询或显式接受一个分析发布。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.analysis_release_repository import (  # noqa: E402
    AnalysisReleaseRepositoryError,
    accept_release,
    build_release,
    get_release_status,
    verify_release,
)


class _SafeArgumentParser(argparse.ArgumentParser):
    """让参数错误也只输出稳定 JSON，不回显用户提供的路径或取值。"""

    def error(self, message: str) -> None:
        """忽略 argparse 的含输入详情消息，以去敏错误结束。"""

        del message
        print(
            json.dumps(
                {"status": "failed", "reason_code": "invalid_arguments"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    """构造四个显式发布子命令；任何命令都不提供 latest 选项。"""

    parser = _SafeArgumentParser(description="操作一个显式数据清洗分析发布。")
    parser.add_argument("--derived-db", required=True, help="独立派生 SQLite")
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="构建并封存数据库发布和本地包")
    build.add_argument("--run-id", required=True, help="显式运行标识")
    build.add_argument("--release-id", required=True, help="显式发布标识")
    build.add_argument("--release-mode", required=True, choices=("formal", "smoke"))
    build.add_argument("--post-decision-build-id", required=True)
    build.add_argument("--text-dedup-build-id", required=True)
    build.add_argument("--text-keep-audit-evaluation-id", required=True)
    build.add_argument("--output-root", required=True, help="本地不可变发布根目录")

    verify = subcommands.add_parser("verify", help="复验数据库与本地不可变包")
    _add_scoped_release_arguments(verify, require_output_root=True)

    status = subcommands.add_parser("status", help="查询一个显式发布状态")
    _add_scoped_release_arguments(status, require_output_root=False)

    accept = subcommands.add_parser(
        "accept-release", help="显式接受一个 formal finalized 发布"
    )
    _add_scoped_release_arguments(accept, require_output_root=True)
    return parser


def _add_scoped_release_arguments(
    parser: argparse.ArgumentParser,
    *,
    require_output_root: bool,
) -> None:
    """为查询类命令添加相同的显式 run/release 作用域。"""

    parser.add_argument("--run-id", required=True, help="显式运行标识")
    parser.add_argument("--release-id", required=True, help="显式发布标识")
    if require_output_root:
        parser.add_argument("--output-root", required=True, help="本地发布根目录")


def main(argv: list[str] | None = None) -> int:
    """执行单个发布动作，并保证成功/失败标准输出均不含绝对路径。

    成功返回 0 并输出 dataclass 的去敏 JSON；仓储、SQLite、文件系统或未知运行
    失败返回 1，stderr 仅包含 ``status`` 与 ``reason_code``。参数契约错误返回
    2，同样使用去敏 JSON，不打印 usage 中夹带的原始值。
    """

    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_release(
                args.derived_db,
                run_id=args.run_id,
                release_id=args.release_id,
                release_mode=args.release_mode,
                post_decision_build_id=args.post_decision_build_id,
                text_dedup_build_id=args.text_dedup_build_id,
                text_keep_audit_evaluation_id=args.text_keep_audit_evaluation_id,
                output_root=args.output_root,
            )
        elif args.command == "verify":
            result = verify_release(
                args.derived_db,
                run_id=args.run_id,
                release_id=args.release_id,
                output_root=args.output_root,
            )
        elif args.command == "status":
            result = get_release_status(
                args.derived_db,
                run_id=args.run_id,
                release_id=args.release_id,
            )
        else:
            result = accept_release(
                args.derived_db,
                run_id=args.run_id,
                release_id=args.release_id,
                output_root=args.output_root,
            )
    except AnalysisReleaseRepositoryError as exc:
        reason_code = exc.reason_code
    except Exception:
        # CLI 是去敏边界；未预见异常不能把 traceback、路径、SQL 或内容泄漏到
        # 控制台。仓储测试仍直接调用 Python API 以定位实现缺陷。
        reason_code = "release_internal_error"
    else:
        payload = asdict(result)
        payload["status"] = payload.get("seal_status", "completed")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    print(
        json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
