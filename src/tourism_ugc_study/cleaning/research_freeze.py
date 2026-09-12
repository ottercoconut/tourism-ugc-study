"""研究数据封存编排：准备新原始归档、封存清单、验证保护后登记冻结状态。

不回写抓取库、不改清洗标签、不冻结未来人工终审表。私有全文件清单与Git中的
无UGC登记分离；准备失败不登记FROZEN，封存失败不自动撤销已有uchg保护。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .research_freeze_files import (asset_files, inventory_files, owned_path, protect_assets,
                                    verify_inventory, verify_protection)
from .research_freeze_media import archive_media
from .research_freeze_validation import validate_completed_round
from .research_round_artifacts import load_verified_json, write_json
from .source_snapshot import create_archival_snapshot, file_sha256, verify_topic_subset


def _progress(stage: str) -> None:
    """输出可见且不含UGC的阶段进度，避免长时间静默。"""
    print(stage, flush=True)


def prepare_freeze(workspace: Path, config_path: Path, source_db: Path,
                   *, source_root: Path, code_version: str) -> dict[str, Any]:
    """按已提交配置创建新封存规格；资产仍需seal，准备完成不等于冻结完成。

    原始归档和元数据目录必须全新；任何失败保留已生成内容供调查，重做须使用
    新ID/目录。不切换研究输入，只验证全量归档中的topic=1子集与旧输入全列相同。
    """
    workspace = workspace.resolve()
    config = json.loads(config_path.read_text())
    raw_root = owned_path(workspace, config["raw_archive_root"])
    metadata = owned_path(workspace, config["metadata_root"])
    roots = [config["raw_archive_root"], config["source_snapshot"],
             str(Path(config["source_snapshot"]).with_suffix(".manifest.json")),
             config["candidate_root"], config["round_root"], *config["environment_files"]]
    if raw_root.exists() or metadata.exists():
        raise FileExistsError("freeze_output_exists_use_new_identity")
    # 所有路径先检查，禁止把输出置于既有待保护资产内部。
    paths = [owned_path(workspace, root) for root in [*roots, config["metadata_root"]]]
    if len(set(paths)) != len(paths) or any(a != b and a in b.parents for a in paths for b in paths):
        raise ValueError("freeze_overlapping_roots")
    asset_files(workspace, roots[1:])
    _progress("validating_completed_round_and_all_archived_vectors")
    cleaning = validate_completed_round(workspace, config)
    _progress("creating_raw_content_snapshot")
    raw_db = raw_root / "raw-crawl.sqlite"
    raw = create_archival_snapshot(source_db, raw_db, code_version=code_version)
    subset = verify_topic_subset(raw_db, owned_path(workspace, config["source_snapshot"]))
    _progress("archiving_raw_media_with_independent_APFS_clones")
    media = archive_media(raw_db, source_root, raw_root / "media-root", progress=_progress)
    _progress("hashing_all_frozen_asset_files")
    entries = inventory_files(workspace, roots)
    metadata.mkdir(parents=True)
    write_json(metadata / "frozen-files.json", entries)
    manifest = {
        "artifact_kind": "research-data-freeze-specification", "contract": "research-data-freeze-v1",
        "freeze_id": config["freeze_id"], "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "freeze_code_version": code_version, "config_sha256": file_sha256(config_path),
        "seal_required": True, "protection": "macOS_uchg_and_0400_files_0500_directories",
        "roots": roots, "inventory_filename": "frozen-files.json",
        "inventory_sha256": file_sha256(metadata / "frozen-files.json"),
        "file_count": len(entries), "logical_bytes": sum(r["size_bytes"] for r in entries),
        "raw_database": str(raw_db.relative_to(workspace)), "raw_database_sha256": raw["database"]["sha256"],
        "raw_post_count": raw["selected_posts"], "raw_topic_counts": raw["topic_counts"],
        "raw_platform_counts": raw["platform_counts"], "media": media,
        "source_snapshot": config["source_snapshot"],
        "source_snapshot_sha256": file_sha256(owned_path(workspace, config["source_snapshot"])),
        "topic_subset_full_rows_check": subset,
        "candidate_database": config["candidate_root"] + "/cleaning-candidates.sqlite",
        "candidate_database_sha256": file_sha256(owned_path(workspace, config["candidate_root"]) / "cleaning-candidates.sqlite"),
        "cleaning": cleaning, "round_root": config["round_root"],
        "excluded": ["live_collector_database_and_media", "collector_accounts_scheduler_discovery_tables",
                     "mutable_current_pointers", "final-kept.sqlite_future_human_publication",
                     "shared_encoder_weights_and_external_historical_dependencies"],
        "limits": ["same_disk_not_disaster_recovery_backup", "owner_can_explicitly_remove_uchg",
                   "internal_archive_not_public_UGC_release", "candidates_not_final_research_sample"],
    }
    write_json(metadata / "freeze-manifest.json", manifest)
    return {"status": "PREPARED_NOT_YET_FROZEN", "manifest": str((metadata / "freeze-manifest.json").relative_to(workspace)),
            "manifest_sha256": file_sha256(metadata / "freeze-manifest.json"), "file_count": len(entries)}


def verify_freeze(workspace: Path, manifest_path: Path, digest: str,
                  *, require_protection: bool = True) -> dict[str, Any]:
    """从外部可信SHA开始全量复核文件集合、字节和保护；不依赖活跃采集库。

    require_protection=False仅用于seal前验收，不得据此宣称FROZEN。跨平台复制可
    验证内容，但没有macOS保护不得返回保护通过。未知契约或元数据附加文件失败。
    """
    workspace = workspace.resolve()
    manifest_path = owned_path(workspace, str(manifest_path.absolute().relative_to(workspace)))
    spec = load_verified_json(manifest_path, digest)
    if spec["contract"] != "research-data-freeze-v1" or spec["inventory_filename"] != "frozen-files.json":
        raise ValueError("freeze_contract_invalid")
    meta_root = str(manifest_path.parent.relative_to(workspace))
    metadata_files = asset_files(workspace, [meta_root])
    if {p.name for p in metadata_files} != {"freeze-manifest.json", "frozen-files.json"} or len(metadata_files) != 2:
        raise ValueError("freeze_metadata_inventory_invalid")
    entries = load_verified_json(manifest_path.parent / "frozen-files.json", spec["inventory_sha256"])
    if len(entries) != spec["file_count"] or sum(r["size_bytes"] for r in entries) != spec["logical_bytes"]:
        raise ValueError("freeze_inventory_summary_invalid")
    _progress("verifying_all_file_bytes_and_exact_file_set")
    verify_inventory(workspace, spec["roots"], entries)
    if require_protection:
        _progress("verifying_uchg_and_readonly_modes")
        verify_protection(workspace, [*spec["roots"], meta_root])
    return {"status": "FROZEN" if require_protection else "CONTENT_VERIFIED_NOT_YET_FROZEN",
            "freeze_id": spec["freeze_id"], "manifest": str(manifest_path.relative_to(workspace)),
            "manifest_sha256": digest, "inventory_sha256": spec["inventory_sha256"],
            "file_count": spec["file_count"], "logical_bytes": spec["logical_bytes"],
            "raw_post_count": spec["raw_post_count"], "raw_image_count": spec["media"]["file_count"],
            "source_record_count": spec["cleaning"]["record_count"],
            "decision_counts": spec["cleaning"]["decision_counts"],
            "freeze_code_version": spec["freeze_code_version"],
            "human_review_status": "pending", "final_research_dataset_frozen": False}


def seal_freeze(workspace: Path, manifest_path: Path, digest: str) -> dict[str, Any]:
    """先验字节，再设保护，独立复核后才追加Git安全登记；绝不自动解冻。

    同一规格可在部分保护失败后重试。登记已有同ID不同SHA时拒绝；已有相同记录
    原样保留。当前输入/候选指针已换轮时只保留封存，不覆盖另一个任务的指针。
    """
    workspace = workspace.resolve()
    manifest_path = owned_path(workspace, str(manifest_path.absolute().relative_to(workspace)))
    verify_freeze(workspace, manifest_path, digest, require_protection=False)
    spec = load_verified_json(manifest_path, digest)
    _progress("applying_immutable_protection_to_assets_and_metadata")
    protect_assets(workspace, [*spec["roots"], str(manifest_path.parent.relative_to(workspace))])
    record = verify_freeze(workspace, manifest_path, digest)
    registry_path = workspace / "governance/research-data-freezes.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {"contract": "research-data-freeze-registry-v1", "freezes": []}
    existing = next((r for r in registry["freezes"] if r["freeze_id"] == record["freeze_id"]), None)
    if existing and existing["manifest_sha256"] != digest:
        raise ValueError("freeze_registry_identity_conflict")
    if not existing:
        record["sealed_at_utc"] = datetime.now(timezone.utc).isoformat()
        registry["freezes"].append(record)
        write_json(registry_path, registry)
    else:
        record = existing
    # 当前指针只是可变检索入口，冻结身份由Git登记与只读manifest确定。
    pointer_path = workspace / "data/processed/current-cleaning.json"
    pointer = json.loads(pointer_path.read_text())
    source_pointer_path = workspace / "data/processed/current-source.json"
    source_pointer = json.loads(source_pointer_path.read_text())
    if (pointer.get("candidate_database_sha256") == spec["candidate_database_sha256"]
            and source_pointer.get("snapshot_sha256") == spec["source_snapshot_sha256"]):
        write_json(workspace / "data/processed/current-freeze.json", record)
        pointer.update(data_freeze=record, inference_resume_authorized=False)
        source_pointer.update(data_freeze=record)
        write_json(pointer_path, pointer)
        write_json(source_pointer_path, source_pointer)
    return record
