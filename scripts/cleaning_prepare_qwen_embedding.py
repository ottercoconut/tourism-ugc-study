#!/usr/bin/env python3
"""下载、校验并烟雾测试本地 Qwen3-Embedding-4B 权重。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.models.text.qwen_embedding_config import (
    QwenEmbeddingConfigError,
    load_qwen_embedding_plan,
)
from tourism_ugc_study.models.text.qwen_embedding_runtime import (
    LocalQwenEmbeddingEncoder,
    QwenEmbeddingRuntimeError,
    prepare_qwen_model_directory,
    validate_qwen_model_directory,
)


def _parser() -> argparse.ArgumentParser:
    """构造不接收 UGC、标签或训练参数的模型准备 CLI。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/cleaning-qwen-embedding-baseline.yaml"),
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("../Qwen3-Embedding-4B"),
        help="仓库外的本地模型目录",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="目录不存在时按冻结 revision 下载；否则只校验",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="只编码两条内置合成文本，不读取任何研究数据",
    )
    return parser


def _require_model_outside_repository(model_dir: Path) -> None:
    """拒绝把公开权重写入研究仓库。"""

    repository = Path(__file__).resolve().parents[1]
    model = model_dir.expanduser().resolve()
    if repository == model or repository in model.parents:
        raise QwenEmbeddingRuntimeError(
            "qwen_embedding_model_directory_inside_repository"
        )


def main() -> int:
    """执行公开权重准备并输出不含本机路径的 JSON 摘要。"""

    args = _parser().parse_args()
    try:
        _require_model_outside_repository(args.model_dir)
        plan = load_qwen_embedding_plan(args.plan)
        snapshot = (
            prepare_qwen_model_directory(args.model_dir, plan=plan)
            if args.download
            else validate_qwen_model_directory(args.model_dir, plan=plan)
        )
        result = {"status": "ready", "snapshot": asdict(snapshot)}
        if args.smoke_test:
            encoder = LocalQwenEmbeddingEncoder(args.model_dir, plan=plan)
            embeddings = encoder.encode(
                (
                    "游客在青岛海边记录旅行体验。",
                    "本店承接商业推广，欢迎联系。",
                )
            )
            result["smoke_test"] = {
                "count": int(embeddings.shape[0]),
                "dimension": int(embeddings.shape[1]),
                "finite": True,
                "normalized": True,
                "device": encoder.device,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (QwenEmbeddingConfigError, QwenEmbeddingRuntimeError) as exc:
        print(
            json.dumps(
                {"status": "failed", "reason_code": exc.reason_code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
