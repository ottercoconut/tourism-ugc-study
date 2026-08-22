"""校验 700 条参考证据并生成 500/200 样本迁移 manifest。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tourism_ugc_study.cleaning.config import ConfigurationError, load_stable_config
from tourism_ugc_study.cleaning.reference_evidence import (
    ReferenceEvidenceError,
    validate_reference_evidence,
    validate_sample_migration_manifest,
    write_sample_migration_manifest,
)


def _parser() -> argparse.ArgumentParser:
    """构造参考证据校验命令行。

    Returns:
        只接受显式 CSV、manifest、派生库和迁移输出路径的参数解析器。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/cleaning.yaml"))
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--derived-db", type=Path, required=True)
    parser.add_argument("--migration-manifest", type=Path, required=True)
    parser.add_argument("--validate-existing-migration", action="store_true")
    return parser


def main() -> int:
    """执行只读证据校验和迁移 manifest 封存。

    Returns:
        全部校验成功时为 ``0``；配置或证据失败时为 ``2``。

    Notes:
        标准输出只包含去敏身份、哈希和计数，不输出正文、作者或本机路径。
    """

    args = _parser().parse_args()
    try:
        config = load_stable_config(args.config)
        reference = validate_reference_evidence(
            args.csv,
            args.manifest,
            args.derived_db,
            expected_label_guide_version=config.label_guide_version,
        )
        if args.validate_existing_migration:
            migration = validate_sample_migration_manifest(args.migration_manifest, args.derived_db)
        else:
            migration = write_sample_migration_manifest(args.derived_db, args.migration_manifest)
        print(json.dumps({
            "config_sha256": config.sha256,
            "reference": {
                "csv_sha256": reference.csv_sha256,
                "manifest_sha256": reference.manifest_sha256,
                "sample_run_id": reference.sample_run_id,
                "row_count": reference.row_count,
                "label_counts": reference.label_counts,
                "frame_counts": reference.frame_counts,
                "member_manifest_sha256": reference.member_manifest_sha256,
                "database_annotation_count": reference.database_annotation_count,
            },
            "migration": {
                "output_sha256": migration.output_sha256,
                "sample_run_id": migration.sample_run_id,
                "population_count": migration.population_count,
                "probability_count": migration.probability_count,
                "targeted_count": migration.targeted_count,
                "member_count": migration.member_count,
                "member_manifest_sha256": migration.member_manifest_sha256,
                "reused": migration.reused,
            },
        }, ensure_ascii=False, sort_keys=True))
    except (ConfigurationError, ReferenceEvidenceError) as exc:
        print(json.dumps({"status": "failed", "reason_code": getattr(exc, "reason_code", "configuration_invalid")}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
