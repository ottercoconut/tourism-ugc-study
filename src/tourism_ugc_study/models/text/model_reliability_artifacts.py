"""双模型纯预测人口框与 Wave A 盲标任务的不可变 artifact。"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from tourism_ugc_study.cleaning.config import StableCleaningConfig
from tourism_ugc_study.cleaning.text_config import TextCleaningConfig

from .model_reliability_config import (
    ModelReliabilityPlan,
    load_model_reliability_plan,
)
from .model_reliability_evaluation import (
    LabeledWaveMember,
    ModelReliabilityEvaluationError,
    evaluate_wave_a,
)
from .model_reliability_study import (
    ScoredEvaluationMember,
    load_eligible_evaluation_population,
    pair_model_scores,
    sample_wave_a,
    wave_a_stratum_counts,
)
from .qwen_embedding_config import load_qwen_embedding_plan
from .qwen_embedding_runtime import (
    LocalQwenHeadTailEncoder,
    validate_qwen_model_directory,
)
from .qwen_head_tail_artifacts import load_frozen_qwen_head_tail_model
from .qwen_head_tail_config import load_qwen_head_tail_plan
from .sparse_challenger_artifacts import load_frozen_sparse_challenger_model


_WAVE_A_ANNOTATION_FILENAME = "wave-a-tourism-relevance-annotation.csv"


class ModelReliabilityArtifactError(RuntimeError):
    """评价 artifact 输入、谱系或不可变性失败时抛出的去敏异常。"""

    def __init__(self, reason_code: str) -> None:
        """保存不泄露正文、身份或本机路径的稳定失败码。"""

        super().__init__("formal model reliability artifact failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ScoredFrameResult:
    """双模型只预测人口框的去敏结果。"""

    frame_id: str
    status: str
    reused: bool
    eligible_count: int
    eligible_component_count: int
    excluded_count: int
    sparse_model_id: str
    qwen_model_id: str
    stratum_counts: Mapping[str, int]
    encoding_diagnostics: Mapping[str, Any]
    package_manifest_sha256: str
    scored_frame_sha256: str
    fit_call_count: int
    test_status: str
    threshold_status: str
    audit_status: str


@dataclass(frozen=True)
class WaveAResult:
    """Wave A 盲标任务与隐藏抽样映射的去敏结果。"""

    wave_id: str
    status: str
    reused: bool
    sample_count: int
    stratum_population_counts: Mapping[str, int]
    stratum_sample_counts: Mapping[str, int]
    task_sha256: str
    private_map_sha256: str
    package_manifest_sha256: str
    labels_entered_fit: bool
    test_status: str
    threshold_status: str
    audit_status: str


@dataclass(frozen=True)
class WaveAEvaluationResult:
    """Wave A 初标探索性评价包的去敏结果。"""

    evaluation_id: str
    status: str
    reused: bool
    sample_count: int
    determinate_count: int
    uncertain_count: int
    sparse_metrics: Mapping[str, Any]
    qwen_metrics: Mapping[str, Any]
    package_manifest_sha256: str
    report_sha256: str
    labels_entered_fit: bool
    may_select_model: bool
    test_status: str
    threshold_status: str
    audit_status: str


def _canonical_bytes(value: object) -> bytes:
    """生成排序、禁止 NaN 且以换行结尾的规范 JSON。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算 artifact 摘要并隐藏路径。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelReliabilityArtifactError(
            "model_reliability_artifact_unreadable"
        ) from exc
    return digest.hexdigest()


def _load_json(path: Path, reason_code: str) -> Mapping[str, Any]:
    """读取顶层必须为映射的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelReliabilityArtifactError(reason_code) from exc
    if not isinstance(value, Mapping):
        raise ModelReliabilityArtifactError(reason_code)
    return value


def _manifest_identity(path: Path) -> tuple[Mapping[str, Any], str]:
    """读取运行 manifest 并返回内容与实际摘要。"""

    manifest = _load_json(path, "model_reliability_model_manifest_unreadable")
    return manifest, _file_sha256(path)


def _validate_sparse_binding(package: Path, plan: ModelReliabilityPlan) -> None:
    """确认 sparse 包身份、安全状态和未开启测试边界。"""

    manifest, digest = _manifest_identity(package / "training-manifest.json")
    if (
        digest != plan.sparse.package_manifest_sha256
        or manifest.get("artifact_kind") != "formal-cleaning-sparse-challenger"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("run_id") != plan.sparse.run_id
        or manifest.get("candidate_model_id") != plan.sparse.model_id
        or manifest.get("acceptance_status") != "passed"
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_sparse_binding_invalid"
        )


def _validate_code_version(value: str) -> str:
    """要求完整 Git SHA，保证首次人口预测绑定可复现代码。"""

    version = value.strip()
    if (
        len(version) != 40
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_code_version_invalid"
        )
    return version


def _scored_frame_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> ScoredFrameResult:
    """从已验证 manifest 构造去敏输出。"""

    return ScoredFrameResult(
        frame_id=str(manifest["frame_id"]),
        status=str(manifest["status"]),
        reused=reused,
        eligible_count=int(manifest["eligible_count"]),
        eligible_component_count=int(manifest["eligible_component_count"]),
        excluded_count=int(manifest["excluded_count"]),
        sparse_model_id=str(manifest["sparse_model_id"]),
        qwen_model_id=str(manifest["qwen_model_id"]),
        stratum_counts=dict(manifest["stratum_counts"]),
        encoding_diagnostics=dict(manifest["encoding_diagnostics"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        scored_frame_sha256=str(manifest["artifacts"]["scored_frame"]["sha256"]),
        fit_call_count=int(manifest["fit_call_count"]),
        test_status=str(manifest["test_status"]),
        threshold_status=str(manifest["threshold_status"]),
        audit_status=str(manifest["audit_status"]),
    )


def _validate_scored_package(
    package: Path,
    *,
    expected_frame_id: str,
    expected_manifest_sha256: str,
    plan: ModelReliabilityPlan,
) -> ScoredFrameResult:
    """完整校验既有人口预测包，禁止缺少外部摘要的静默复用。"""

    manifest_path = package / "frame-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelReliabilityArtifactError(
            "model_reliability_frame_manifest_unreadable"
        ) from exc
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-model-reliability-scored-frame"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("frame_id") != expected_frame_id
        or manifest.get("plan_sha256") != plan.plan_sha256
        or manifest.get("status") != "FRAME_SCORED"
        or manifest.get("fit_call_count") != 0
        or manifest.get("labels_read") is not False
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("test_probabilities_present") is not False
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_frame_manifest_invalid"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {"scored_frame", "plan"}:
        raise ModelReliabilityArtifactError(
            "model_reliability_frame_manifest_invalid"
        )
    for name, filename in (("scored_frame", "scored-frame.json"), ("plan", "plan.yaml")):
        details = artifacts[name]
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelReliabilityArtifactError(
                "model_reliability_frame_artifact_hash_mismatch"
            )
    return _scored_frame_result(manifest, manifest_bytes, reused=True)


def score_model_reliability_frame(
    reference_csv: str | Path,
    derived_db: str | Path,
    sparse_package: str | Path,
    qwen_package: str | Path,
    qwen_base_plan_path: str | Path,
    qwen_projection_plan_path: str | Path,
    model_dir: str | Path,
    reliability_plan_path: str | Path,
    artifact_root: str | Path,
    *,
    config: StableCleaningConfig,
    normalization_config: TextCleaningConfig,
    code_version: str,
    expected_existing_manifest_sha256: str | None = None,
    show_progress: bool = True,
) -> ScoredFrameResult:
    """对合格人口执行两个既有模型各一次纯预测并原子封存。

    本函数没有任何 ``fit``、校准、阈值或标签入口。Qwen 虽未通过既有开发
    验收，仍可作为研究 comparator 被同一新样本评价；这不改变其部署状态。
    """

    del config  # 稳定框架由调用方加载；人口文本只使用独立 normalization 配置。
    version = _validate_code_version(code_version)
    plan = load_model_reliability_plan(reliability_plan_path)
    base_plan = load_qwen_embedding_plan(qwen_base_plan_path)
    projection_plan = load_qwen_head_tail_plan(qwen_projection_plan_path)
    if (
        projection_plan.qwen_base_plan_sha256 != base_plan.plan_sha256
        or projection_plan.sparse_run_id != plan.sparse.run_id
        or projection_plan.sparse_model_id != plan.sparse.model_id
        or projection_plan.sparse_package_manifest_sha256
        != plan.sparse.package_manifest_sha256
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_qwen_plan_binding_invalid"
        )
    sparse_directory = Path(sparse_package).expanduser().resolve()
    qwen_directory = Path(qwen_package).expanduser().resolve()
    _validate_sparse_binding(sparse_directory, plan)
    sparse_model = load_frozen_sparse_challenger_model(sparse_directory)
    qwen_model = load_frozen_qwen_head_tail_model(
        qwen_directory,
        expected_run_id=plan.qwen.run_id,
        expected_manifest_sha256=plan.qwen.package_manifest_sha256,
        plan=projection_plan,
    )
    snapshot = validate_qwen_model_directory(model_dir, plan=base_plan)
    encoder = LocalQwenHeadTailEncoder(
        model_dir, base_plan=base_plan, projection_plan=projection_plan
    )
    population = load_eligible_evaluation_population(
        reference_csv,
        derived_db,
        plan=plan,
        normalization_config=normalization_config,
    )
    frame_id = _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": "paired-frozen-model-population-scoring-v1",
                "code_version": version,
                "plan_sha256": plan.plan_sha256,
                "projection_member_sha256": population.projection_member_sha256,
                "leakage_output_sha256": population.leakage_output_sha256,
                "sparse_manifest_sha256": plan.sparse.package_manifest_sha256,
                "qwen_manifest_sha256": plan.qwen.package_manifest_sha256,
                "encoder_snapshot_sha256": snapshot.snapshot_sha256,
            }
        )
    )[:32]
    root = Path(artifact_root).expanduser().resolve()
    package = root / frame_id
    if package.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelReliabilityArtifactError(
                "model_reliability_existing_frame_requires_manifest_hash"
            )
        return _validate_scored_package(
            package,
            expected_frame_id=frame_id,
            expected_manifest_sha256=expected_existing_manifest_sha256,
            plan=plan,
        )
    texts = [item.normalized_model_text for item in population.members]
    sparse_probabilities = sparse_model.predict_p_unrelated(texts)
    encoded = encoder.encode_head_tail_with_diagnostics(
        texts, show_progress=show_progress
    )
    qwen_probabilities = qwen_model.predict_p_unrelated(encoded.embeddings)
    scored = pair_model_scores(
        population.members, sparse_probabilities, qwen_probabilities
    )
    scored_payload = {
        "artifact_kind": "formal-cleaning-model-reliability-scored-members",
        "frame_id": frame_id,
        "records": [asdict(item) for item in scored],
        "labels_present": False,
        "platform_used": False,
    }
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{frame_id}.", dir=root)
    )
    try:
        (temporary / "scored-frame.json").write_bytes(
            _canonical_bytes(scored_payload)
        )
        shutil.copyfile(reliability_plan_path, temporary / "plan.yaml")
        artifacts = {
            "scored_frame": {
                "filename": "scored-frame.json",
                "sha256": _file_sha256(temporary / "scored-frame.json"),
            },
            "plan": {
                "filename": "plan.yaml",
                "sha256": _file_sha256(temporary / "plan.yaml"),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-model-reliability-scored-frame",
            "artifact_status": "immutable",
            "frame_id": frame_id,
            "status": "FRAME_SCORED",
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "eligible_count": len(scored),
            "eligible_component_count": population.eligible_component_count,
            "excluded_count": population.excluded_count,
            "sparse_model_id": plan.sparse.model_id,
            "qwen_model_id": plan.qwen.model_id,
            "qwen_role": "research_comparator_failed_development_gate",
            "stratum_counts": wave_a_stratum_counts(scored),
            "encoding_diagnostics": asdict(encoded.diagnostics),
            "fit_call_count": 0,
            "sparse_prediction_call_count": 1,
            "qwen_prediction_call_count": 1,
            "labels_read": False,
            "test_status": "locked_not_opened",
            "test_probabilities_present": False,
            "threshold_status": "UNSET",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
            "lineage": {
                "code_version": version,
                "candidate_build_id": plan.candidate_build_id,
                "leakage_build_id": plan.leakage_build_id,
                "leakage_output_sha256": population.leakage_output_sha256,
                "projection_member_sha256": population.projection_member_sha256,
                "encoder_snapshot_sha256": snapshot.snapshot_sha256,
                "sparse_package_manifest_sha256": plan.sparse.package_manifest_sha256,
                "qwen_package_manifest_sha256": plan.qwen.package_manifest_sha256,
            },
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "frame-manifest.json").write_bytes(manifest_bytes)
        temporary.rename(package)
        temporary = None
        return _scored_frame_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _load_scored_members(
    scored_package: Path,
    *,
    expected_manifest_sha256: str,
    plan: ModelReliabilityPlan,
) -> tuple[ScoredEvaluationMember, ...]:
    """校验人口框并加载不含正文的配对概率。"""

    manifest = _load_json(
        scored_package / "frame-manifest.json",
        "model_reliability_frame_manifest_unreadable",
    )
    frame_id = str(manifest.get("frame_id", ""))
    _validate_scored_package(
        scored_package,
        expected_frame_id=frame_id,
        expected_manifest_sha256=expected_manifest_sha256,
        plan=plan,
    )
    payload = _load_json(
        scored_package / "scored-frame.json",
        "model_reliability_scored_frame_invalid",
    )
    records = payload.get("records")
    if (
        payload.get("artifact_kind")
        != "formal-cleaning-model-reliability-scored-members"
        or payload.get("frame_id") != frame_id
        or payload.get("labels_present") is not False
        or payload.get("platform_used") is not False
        or not isinstance(records, list)
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_scored_frame_invalid"
        )
    try:
        result = tuple(ScoredEvaluationMember(**record) for record in records)
    except (TypeError, ValueError) as exc:
        raise ModelReliabilityArtifactError(
            "model_reliability_scored_frame_invalid"
        ) from exc
    if len(result) != plan.expected_eligible_count:
        raise ModelReliabilityArtifactError(
            "model_reliability_scored_frame_invalid"
        )
    return result


def prepare_wave_a_package(
    reference_csv: str | Path,
    derived_db: str | Path,
    scored_package: str | Path,
    reliability_plan_path: str | Path,
    artifact_root: str | Path,
    *,
    normalization_config: TextCleaningConfig,
    expected_scored_manifest_sha256: str,
    expected_existing_manifest_sha256: str | None = None,
) -> WaveAResult:
    """从已封存双模型人口框生成隐藏模型答案的 Wave A 盲标任务。"""

    plan = load_model_reliability_plan(reliability_plan_path)
    scored_directory = Path(scored_package).expanduser().resolve(strict=True)
    scored = _load_scored_members(
        scored_directory,
        expected_manifest_sha256=expected_scored_manifest_sha256,
        plan=plan,
    )
    population = load_eligible_evaluation_population(
        reference_csv,
        derived_db,
        plan=plan,
        normalization_config=normalization_config,
    )
    text_by_identity = {
        item.identity: (
            item.normalized_model_text,
            item.normalized_sha256,
            item.platform_key,
        )
        for item in population.members
    }
    sample = sample_wave_a(scored, plan=plan)
    scored_manifest_sha256 = _file_sha256(
        scored_directory / "frame-manifest.json"
    )
    wave_id = _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": "paired-model-wave-a-stratified-srs-v2",
                "annotation_filename": _WAVE_A_ANNOTATION_FILENAME,
                "plan_sha256": plan.plan_sha256,
                "scored_manifest_sha256": scored_manifest_sha256,
                "sample": [asdict(item) for item in sample],
            }
        )
    )[:32]
    root = Path(artifact_root).expanduser().resolve()
    package = root / wave_id
    if package.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelReliabilityArtifactError(
                "model_reliability_existing_wave_a_requires_manifest_hash"
            )
        manifest_bytes = (package / "wave-manifest.json").read_bytes()
        if _sha256_bytes(manifest_bytes) != expected_existing_manifest_sha256:
            raise ModelReliabilityArtifactError(
                "model_reliability_wave_a_manifest_hash_mismatch"
            )
        manifest = json.loads(manifest_bytes)
        return _wave_a_result(manifest, manifest_bytes, reused=True)
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{wave_id}.", dir=root)
    )
    try:
        task_path = temporary / _WAVE_A_ANNOTATION_FILENAME
        with task_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "task_id",
                    "sample_run_id",
                    "normalized_model_text",
                    "tourism_label",
                ),
            )
            writer.writeheader()
            for item in sample:
                text, normalized_sha256, _platform_key = text_by_identity[
                    (item.source_post_id, item.source_version)
                ]
                if normalized_sha256 != item.normalized_sha256:
                    raise ModelReliabilityArtifactError(
                        "model_reliability_wave_a_text_binding_mismatch"
                    )
                writer.writerow(
                    {
                        "task_id": item.task_id,
                        "sample_run_id": wave_id,
                        "normalized_model_text": text,
                        "tourism_label": "",
                    }
                )
        private_records = []
        for item in sample:
            _text, _normalized_sha256, platform_key = text_by_identity[
                (item.source_post_id, item.source_version)
            ]
            private_records.append(
                {**asdict(item), "platform_key": platform_key}
            )
        private_payload = {
            "artifact_kind": "formal-cleaning-model-reliability-wave-a-private-map",
            "wave_id": wave_id,
            "records": private_records,
            "hidden_from_annotation": [
                "source_post_id",
                "source_version",
                "platform_key",
                "model_name",
                "model_probability",
                "sampling_stratum",
                "selection_reason",
                "inclusion_probability",
                "analysis_weight",
            ],
            "labels_entered_fit": False,
            "platform_used": False,
        }
        (temporary / "private-map.json").write_bytes(
            _canonical_bytes(private_payload)
        )
        shutil.copyfile(reliability_plan_path, temporary / "plan.yaml")
        stratum_sample_counts: dict[str, int] = {}
        stratum_population_counts: dict[str, int] = {}
        for item in sample:
            stratum_sample_counts[item.stratum] = item.stratum_sample_count
            stratum_population_counts[item.stratum] = item.stratum_population_count
        artifacts = {
            "review_task": {
                "filename": _WAVE_A_ANNOTATION_FILENAME,
                "sha256": _file_sha256(task_path),
            },
            "private_map": {
                "filename": "private-map.json",
                "sha256": _file_sha256(temporary / "private-map.json"),
            },
            "plan": {
                "filename": "plan.yaml",
                "sha256": _file_sha256(temporary / "plan.yaml"),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-model-reliability-wave-a",
            "artifact_status": "immutable",
            "wave_id": wave_id,
            "status": "WAVE_A_LABELING",
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "scored_frame_id": _load_json(
                scored_directory / "frame-manifest.json",
                "model_reliability_frame_manifest_unreadable",
            )["frame_id"],
            "scored_manifest_sha256": scored_manifest_sha256,
            "sample_count": len(sample),
            "stratum_population_counts": dict(sorted(stratum_population_counts.items())),
            "stratum_sample_counts": dict(sorted(stratum_sample_counts.items())),
            "sampling_unit": "post",
            "leakage_component_role": "exclusion_and_cluster_variance",
            "labels_entered_fit": False,
            "fit_call_count": 0,
            "model_probabilities_hidden": True,
            "test_status": "locked_not_opened",
            "threshold_status": "UNSET",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "wave-manifest.json").write_bytes(manifest_bytes)
        temporary.rename(package)
        temporary = None
        return _wave_a_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _validate_wave_a_package(
    package: Path,
    *,
    expected_manifest_sha256: str,
    plan: ModelReliabilityPlan,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """校验 Wave A manifest、任务、私有映射和禁止训练边界。"""

    manifest_path = package / "wave-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelReliabilityArtifactError(
            "model_reliability_wave_a_manifest_unreadable"
        ) from exc
    if (
        _sha256_bytes(manifest_bytes) != expected_manifest_sha256
        or not isinstance(manifest, Mapping)
        or manifest.get("artifact_kind")
        != "formal-cleaning-model-reliability-wave-a"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("status") != "WAVE_A_LABELING"
        or manifest.get("plan_sha256") != plan.plan_sha256
        or manifest.get("labels_entered_fit") is not False
        or manifest.get("fit_call_count") != 0
        or manifest.get("model_probabilities_hidden") is not True
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_wave_a_manifest_invalid"
        )
    artifacts = manifest.get("artifacts")
    expected = {
        "review_task": _WAVE_A_ANNOTATION_FILENAME,
        "private_map": "private-map.json",
        "plan": "plan.yaml",
    }
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(expected):
        raise ModelReliabilityArtifactError(
            "model_reliability_wave_a_manifest_invalid"
        )
    for name, filename in expected.items():
        details = artifacts[name]
        if (
            not isinstance(details, Mapping)
            or details.get("filename") != filename
            or _file_sha256(package / filename) != details.get("sha256")
        ):
            raise ModelReliabilityArtifactError(
                "model_reliability_wave_a_artifact_hash_mismatch"
            )
    private_map = _load_json(
        package / "private-map.json",
        "model_reliability_wave_a_private_map_invalid",
    )
    if (
        private_map.get("artifact_kind")
        != "formal-cleaning-model-reliability-wave-a-private-map"
        or private_map.get("wave_id") != manifest.get("wave_id")
        or private_map.get("labels_entered_fit") is not False
        or private_map.get("platform_used") is not False
        or not isinstance(private_map.get("records"), list)
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_wave_a_private_map_invalid"
        )
    return manifest, private_map


def _load_completed_wave_a_labels(
    completed_csv: str | Path, private_map: Mapping[str, Any]
) -> tuple[LabeledWaveMember, ...]:
    """把已完成 CSV 与隐藏映射严格一一绑定，不接受缺行或额外任务。"""

    raw_records = private_map["records"]
    expected_by_task = {
        str(record["task_id"]): record for record in raw_records
    }
    if len(expected_by_task) != len(raw_records):
        raise ModelReliabilityArtifactError(
            "model_reliability_wave_a_private_map_invalid"
        )
    path = Path(completed_csv)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != [
                "task_id",
                "sample_run_id",
                "normalized_model_text",
                "tourism_label",
            ]:
                raise ValueError
            completed = list(reader)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise ModelReliabilityArtifactError(
            "model_reliability_completed_csv_invalid"
        ) from exc
    if (
        len(completed) != len(expected_by_task)
        or {row["task_id"] for row in completed} != set(expected_by_task)
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_completed_membership_mismatch"
        )
    labeled: list[LabeledWaveMember] = []
    for row in completed:
        hidden = expected_by_task[row["task_id"]]
        text_sha256 = hashlib.sha256(
            row["normalized_model_text"].encode("utf-8")
        ).hexdigest()
        if (
            text_sha256 != hidden["normalized_sha256"]
            or row["sample_run_id"] != private_map["wave_id"]
            or row["tourism_label"] not in {"related", "unrelated", "uncertain"}
        ):
            raise ModelReliabilityArtifactError(
                "model_reliability_completed_label_invalid"
            )
        try:
            labeled.append(
                LabeledWaveMember(
                    task_id=row["task_id"],
                    source_post_id=int(hidden["source_post_id"]),
                    source_version=int(hidden["source_version"]),
                    component_id=str(hidden["component_id"]),
                    stratum=str(hidden["stratum"]),
                    inclusion_probability=float(hidden["inclusion_probability"]),
                    analysis_weight=float(hidden["analysis_weight"]),
                    sparse_p_unrelated=float(hidden["sparse_p_unrelated"]),
                    qwen_p_unrelated=float(hidden["qwen_p_unrelated"]),
                    tourism_label=row["tourism_label"],
                )
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelReliabilityArtifactError(
                "model_reliability_completed_label_invalid"
            ) from exc
    labeled.sort(key=lambda item: item.task_id)
    return tuple(labeled)


def evaluate_wave_a_package(
    completed_csv: str | Path,
    wave_a_package: str | Path,
    scored_package: str | Path,
    reliability_plan_path: str | Path,
    artifact_root: str | Path,
    *,
    expected_wave_manifest_sha256: str,
    expected_scored_manifest_sha256: str,
    expected_existing_manifest_sha256: str | None = None,
) -> WaveAEvaluationResult:
    """封存 Wave A 初标的探索性加权成对评价；不作模型选择。"""

    plan = load_model_reliability_plan(reliability_plan_path)
    wave_directory = Path(wave_a_package).expanduser().resolve(strict=True)
    scored_directory = Path(scored_package).expanduser().resolve(strict=True)
    wave_manifest, private_map = _validate_wave_a_package(
        wave_directory,
        expected_manifest_sha256=expected_wave_manifest_sha256,
        plan=plan,
    )
    scored = _load_scored_members(
        scored_directory,
        expected_manifest_sha256=expected_scored_manifest_sha256,
        plan=plan,
    )
    labeled = _load_completed_wave_a_labels(completed_csv, private_map)
    if len(labeled) != plan.wave_a_sample_size:
        raise ModelReliabilityArtifactError(
            "model_reliability_completed_membership_mismatch"
        )
    report = evaluate_wave_a(
        labeled,
        scored,
        probability_grid=plan.probability_grid,
        coverage_grid=plan.coverage_grid,
        random_seed=plan.random_seed,
        confidence_level=plan.confidence_level,
    )
    completed_sha256 = _file_sha256(Path(completed_csv))
    evaluation_id = _sha256_bytes(
        _canonical_bytes(
            {
                "algorithm_id": "paired-wave-a-design-weighted-evaluation-v1",
                "plan_sha256": plan.plan_sha256,
                "wave_manifest_sha256": expected_wave_manifest_sha256,
                "scored_manifest_sha256": expected_scored_manifest_sha256,
                "completed_csv_sha256": completed_sha256,
            }
        )
    )[:32]
    root = Path(artifact_root).expanduser().resolve()
    package = root / evaluation_id
    if package.exists():
        if expected_existing_manifest_sha256 is None:
            raise ModelReliabilityArtifactError(
                "model_reliability_existing_evaluation_requires_manifest_hash"
            )
        manifest_bytes = (package / "evaluation-manifest.json").read_bytes()
        if _sha256_bytes(manifest_bytes) != expected_existing_manifest_sha256:
            raise ModelReliabilityArtifactError(
                "model_reliability_evaluation_manifest_hash_mismatch"
            )
        manifest = json.loads(manifest_bytes)
        return _wave_a_evaluation_result(manifest, manifest_bytes, reused=True)
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{evaluation_id}.", dir=root)
    )
    try:
        (temporary / "evaluation-report.json").write_bytes(
            _canonical_bytes(report)
        )
        (temporary / "labeled-records.json").write_bytes(
            _canonical_bytes(
                {
                    "artifact_kind": "formal-cleaning-model-reliability-wave-a-labels",
                    "evaluation_id": evaluation_id,
                    "records": [asdict(item) for item in labeled],
                    "labels_entered_fit": False,
                    "platform_used": False,
                }
            )
        )
        shutil.copyfile(reliability_plan_path, temporary / "plan.yaml")
        artifacts = {
            "report": {
                "filename": "evaluation-report.json",
                "sha256": _file_sha256(temporary / "evaluation-report.json"),
            },
            "labeled_records": {
                "filename": "labeled-records.json",
                "sha256": _file_sha256(temporary / "labeled-records.json"),
            },
            "plan": {
                "filename": "plan.yaml",
                "sha256": _file_sha256(temporary / "plan.yaml"),
            },
        }
        manifest = {
            "artifact_kind": "formal-cleaning-model-reliability-wave-a-evaluation",
            "artifact_status": "immutable",
            "evaluation_id": evaluation_id,
            "status": "WAVE_A_EXPLORATORY_COMPLETE",
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "wave_id": wave_manifest["wave_id"],
            "wave_manifest_sha256": expected_wave_manifest_sha256,
            "scored_manifest_sha256": expected_scored_manifest_sha256,
            "completed_csv_sha256": completed_sha256,
            "sample_count": report["sample_count"],
            "determinate_count": report["determinate_count"],
            "uncertain_count": report["uncertain_count"],
            "overall_metrics": report["overall_metrics"],
            "labels_entered_fit": False,
            "fit_call_count": 0,
            "may_select_model": False,
            "may_freeze_threshold": False,
            "wave_b_status": "awaiting_researcher_policy",
            "test_status": "locked_not_opened",
            "threshold_status": "UNSET",
            "audit_status": "UNSET",
            "auto_cleaning_decisions_present": False,
            "platform_used": False,
            "artifacts": artifacts,
        }
        manifest_bytes = _canonical_bytes(manifest)
        (temporary / "evaluation-manifest.json").write_bytes(manifest_bytes)
        temporary.rename(package)
        temporary = None
        return _wave_a_evaluation_result(manifest, manifest_bytes, reused=False)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _wave_a_evaluation_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> WaveAEvaluationResult:
    """从已验证评价 manifest 构造去敏摘要。"""

    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-reliability-wave-a-evaluation"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("status") != "WAVE_A_EXPLORATORY_COMPLETE"
        or manifest.get("labels_entered_fit") is not False
        or manifest.get("fit_call_count") != 0
        or manifest.get("may_select_model") is not False
        or manifest.get("may_freeze_threshold") is not False
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_evaluation_manifest_invalid"
        )
    metrics = manifest["overall_metrics"]
    return WaveAEvaluationResult(
        evaluation_id=str(manifest["evaluation_id"]),
        status=str(manifest["status"]),
        reused=reused,
        sample_count=int(manifest["sample_count"]),
        determinate_count=int(manifest["determinate_count"]),
        uncertain_count=int(manifest["uncertain_count"]),
        sparse_metrics=dict(metrics["sparse"]),
        qwen_metrics=dict(metrics["qwen"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        report_sha256=str(manifest["artifacts"]["report"]["sha256"]),
        labels_entered_fit=bool(manifest["labels_entered_fit"]),
        may_select_model=bool(manifest["may_select_model"]),
        test_status=str(manifest["test_status"]),
        threshold_status=str(manifest["threshold_status"]),
        audit_status=str(manifest["audit_status"]),
    )


def _wave_a_result(
    manifest: Mapping[str, Any], manifest_bytes: bytes, *, reused: bool
) -> WaveAResult:
    """从 Wave A manifest 构造去敏输出。"""

    if (
        manifest.get("artifact_kind")
        != "formal-cleaning-model-reliability-wave-a"
        or manifest.get("artifact_status") != "immutable"
        or manifest.get("status") != "WAVE_A_LABELING"
        or manifest.get("labels_entered_fit") is not False
        or manifest.get("fit_call_count") != 0
        or manifest.get("test_status") != "locked_not_opened"
        or manifest.get("threshold_status") != "UNSET"
        or manifest.get("audit_status") != "UNSET"
        or manifest.get("auto_cleaning_decisions_present") is not False
        or manifest.get("platform_used") is not False
    ):
        raise ModelReliabilityArtifactError(
            "model_reliability_wave_a_manifest_invalid"
        )
    return WaveAResult(
        wave_id=str(manifest["wave_id"]),
        status=str(manifest["status"]),
        reused=reused,
        sample_count=int(manifest["sample_count"]),
        stratum_population_counts=dict(manifest["stratum_population_counts"]),
        stratum_sample_counts=dict(manifest["stratum_sample_counts"]),
        task_sha256=str(manifest["artifacts"]["review_task"]["sha256"]),
        private_map_sha256=str(manifest["artifacts"]["private_map"]["sha256"]),
        package_manifest_sha256=_sha256_bytes(manifest_bytes),
        labels_entered_fit=bool(manifest["labels_entered_fit"]),
        test_status=str(manifest["test_status"]),
        threshold_status=str(manifest["threshold_status"]),
        audit_status=str(manifest["audit_status"]),
    )


def render_model_reliability_result(
    result: ScoredFrameResult | WaveAResult | WaveAEvaluationResult,
    *,
    output_format: str = "human",
) -> str:
    """把人口框或 Wave A 结果渲染为稳定 JSON 或中文摘要。"""

    if output_format == "json":
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    if output_format != "human":
        raise ModelReliabilityArtifactError(
            "model_reliability_output_format_invalid"
        )
    if isinstance(result, ScoredFrameResult):
        return "\n".join(
            [
                "双模型外部评价人口框",
                "====================",
                f"人口框 ID：{result.frame_id}",
                f"状态：{result.status}；artifact：{'复用' if result.reused else '新建并封存'}",
                f"合格人口：{result.eligible_count} 条 / {result.eligible_component_count} 个分量",
                f"排除参考泄漏分量成员：{result.excluded_count} 条",
                f"sparse：{result.sparse_model_id}",
                f"Qwen comparator：{result.qwen_model_id}",
                f"Wave A 人口分层：{dict(result.stratum_counts)}",
                f"Qwen 超长输入：{result.encoding_diagnostics['original_over_limit_count']}；"
                f"仍省略中段：{result.encoding_diagnostics['middle_omitted_count']}",
                "fit 调用：0；人工标签读取：0",
                "锁定测试：locked_not_opened；阈值：UNSET；审计策略：UNSET。",
            ]
        )
    if isinstance(result, WaveAResult):
        return "\n".join(
            [
                "Wave A 新盲标任务",
                "=================",
                f"Wave ID：{result.wave_id}",
                f"状态：{result.status}；artifact：{'复用' if result.reused else '新建并封存'}",
                f"任务数：{result.sample_count}",
                f"人口分层：{dict(result.stratum_population_counts)}",
                f"样本分配：{dict(result.stratum_sample_counts)}",
                "盲法：隐藏两个模型名称、概率、分层、平台和入选原因",
                "新标签：evaluation-only，尚未进入任何 fit",
                "锁定测试：locked_not_opened；阈值：UNSET；审计策略：UNSET。",
            ]
        )
    sparse = result.sparse_metrics
    qwen = result.qwen_metrics
    return "\n".join(
        [
            "Wave A 双模型探索性评价",
            "======================",
            f"评价 ID：{result.evaluation_id}",
            f"状态：{result.status}；artifact：{'复用' if result.reused else '新建并封存'}",
            f"初标：{result.sample_count}；确定标签：{result.determinate_count}；uncertain：{result.uncertain_count}",
            "设计加权总体指标",
            f"  sparse：accuracy={sparse['weighted_accuracy'] * 100:.2f}%；"
            f"UGC误排={sparse['weighted_related_to_unrelated_rate'] * 100:.2f}%；"
            f"log loss={sparse['weighted_log_loss']:.4f}；PR-AUC={sparse['weighted_pr_auc_unrelated']:.4f}",
            f"  Qwen：accuracy={qwen['weighted_accuracy'] * 100:.2f}%；"
            f"UGC误排={qwen['weighted_related_to_unrelated_rate'] * 100:.2f}%；"
            f"log loss={qwen['weighted_log_loss']:.4f}；PR-AUC={qwen['weighted_pr_auc_unrelated']:.4f}",
            "固定概率/覆盖率风险曲线与 component-bootstrap 区间见 evaluation-report.json。",
            "当前基础报告只允许比较，不选择模型、不冻结阈值；完整双阈值选择入口尚未执行。",
            "新标签：evaluation-only，尚未进入任何 fit。",
            "锁定测试：locked_not_opened；阈值：UNSET；审计策略：UNSET。",
        ]
    )
