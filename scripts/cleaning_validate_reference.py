#!/usr/bin/env python3
"""只读校验唯一最终700条不重复建模参考 CSV 与 finalized manifest。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.config import (
    ConfigurationError,
    load_cleaning_config_bundle,
)
from tourism_ugc_study.cleaning.reference_evidence import (
    ReferenceEvidenceError,
    validate_reference_evidence,
)


def _parser() -> argparse.ArgumentParser:
    """构造最终参考证据只读校验命令行。

    Returns:
        只接受最终 CSV、唯一配对 manifest 和派生库的参数解析器。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/cleaning.yaml"))
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--derived-db", type=Path, required=True)
    return parser


def main() -> int:
    """执行最终参考证据校验并输出去敏摘要。

    Returns:
        校验成功时为 ``0``；配置或证据失败时为 ``2``。
    """

    args = _parser().parse_args()
    try:
        config, normalization_config = load_cleaning_config_bundle(args.config)
        result = validate_reference_evidence(
            args.csv,
            args.manifest,
            args.derived_db,
            expected_label_guide_version=config.label_guide_version,
            expected_normalization_rule_id=str(
                config.artifacts["normalization_version_lock"]
            ),
            normalization_config=normalization_config,
        )
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
    except (ConfigurationError, ReferenceEvidenceError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason_code": getattr(exc, "reason_code", "configuration_invalid"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
