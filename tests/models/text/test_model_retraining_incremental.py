"""冻结模型新增批次纯预测、规范化与盲表契约测试。"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tourism_ugc_study.cleaning.config import load_cleaning_config_bundle
from tourism_ugc_study.models.text.model_retraining_config import (
    load_model_retraining_plan,
)
from tourism_ugc_study.models.text.model_retraining_incremental import (
    INCREMENTAL_INPUT_COLUMNS,
    IncrementalInferenceMember,
    ModelRetrainingIncrementalError,
    _manual_task_bytes,
    load_incremental_input_csv,
    score_incremental_batch_package,
    score_incremental_member,
)
from tourism_ugc_study.models.text.qwen_embedding_cache import (
    QwenEmbeddingCacheLookup,
)
from tourism_ugc_study.models.text.model_retraining_delivery_config import (
    load_model_retraining_delivery_plan,
)


ROOT = Path(__file__).resolve().parents[3]


class _FrozenPredictOnlyModel:
    """只暴露预测接口的合成融合模型；不存在fit方法。"""

    candidate_name = "logit_fusion"
    candidate_id = "a" * 32

    def __init__(self, probability: float) -> None:
        """保存固定概率及实际收到的模型特征。"""

        self.probability = probability
        self.received_texts: list[str] = []
        self.received_embeddings: np.ndarray | None = None

    def predict_p_unrelated(
        self, texts: list[str], embeddings: np.ndarray | None
    ) -> np.ndarray:
        """记录输入并返回固定概率，不允许其他字段进入。"""

        self.received_texts = list(texts)
        self.received_embeddings = embeddings
        return np.asarray([self.probability], dtype=float)


class _MemoryEmbeddingCache:
    """不落盘的合成缓存，用于隔离路由与artifact测试。"""

    namespace_id = "e" * 32

    def __init__(self, *, cache_hit: bool = True) -> None:
        """保存固定命中状态。"""

        self.cache_hit = cache_hit

    def get_or_encode(
        self, text: str, *, normalized_sha256: str
    ) -> QwenEmbeddingCacheLookup:
        """返回与正文摘要绑定的合成向量。"""

        assert text.startswith("[TITLE]")
        return QwenEmbeddingCacheLookup(
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
            cache_namespace_id=self.namespace_id,
            cache_key=f"{self.namespace_id}:{normalized_sha256}",
            cache_hit=self.cache_hit,
            receipt_sha256="f" * 64,
        )


def _write_input(path: Path, rows: list[dict[str, object]]) -> None:
    """写入严格六列合成新增批次。"""

    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(INCREMENTAL_INPUT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def _member(index: int = 1) -> IncrementalInferenceMember:
    """构造不含真实UGC的已规范化新增成员。"""

    import hashlib

    text = "[TITLE]\n合成标题\n[BODY]\n合成正文"
    return IncrementalInferenceMember(
        member_key=f"member-{index}",
        source_post_id=9000 + index,
        source_version=1,
        component_id=f"component-{index}",
        normalized_model_text=text,
        normalized_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def test_incremental_csv_is_normalized_inside_program_without_platform() -> None:
    """新增批次只接收六列私有输入，并由程序生成训练同契约正文。"""

    plan = load_model_retraining_plan(
        ROOT / "configs" / "cleaning-model-retraining.yaml"
    )
    _config, normalization = load_cleaning_config_bundle(
        ROOT / "configs" / "cleaning.yaml"
    )
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        input_csv = Path(directory) / "new-batch.csv"
        _write_input(
            input_csv,
            [
                {
                    "source_post_id": 9001,
                    "source_version": 1,
                    "component_id": "new-component-1",
                    "title": "  合成 标题  ",
                    "body": "合成正文",
                    "source_status": "",
                }
            ],
        )
        members = load_incremental_input_csv(
            input_csv, plan=plan, normalization_config=normalization
        )
    assert len(members) == 1
    assert members[0].normalized_model_text.startswith("[TITLE]\n")
    assert "合成 标题" in members[0].normalized_model_text
    assert members[0].component_id == "new-component-1"


def test_incremental_csv_preserves_field_longer_than_default_limit(tmp_path: Path) -> None:
    """扩充人口出现20万字符正文，CSV必须完整读取并恢复全局字段上限。"""
    plan = load_model_retraining_plan(ROOT / "configs/cleaning-model-retraining.yaml")
    _, normalization = load_cleaning_config_bundle(ROOT / "configs/cleaning.yaml")
    path = tmp_path / "long-input.csv"
    body = "合成旅游长正文。" * 26000
    _write_input(path, [{"source_post_id": 999001, "source_version": 1,
                        "component_id": "synthetic-long", "title": "合成标题",
                        "body": body, "source_status": "ok"}])
    old_limit = csv.field_size_limit()
    csv.field_size_limit(131072)
    try:
        members = load_incremental_input_csv(path, plan=plan, normalization_config=normalization)
        assert len(members) == 1
        assert members[0].normalized_model_text.endswith(body)
        assert csv.field_size_limit() == 131072
    finally:
        csv.field_size_limit(old_limit)


def test_incremental_csv_rejects_platform_or_duplicate_identity(tmp_path: Path) -> None:
    """平台列和重复源身份都不能静默进入新增批次模型路径。"""

    plan = load_model_retraining_plan(
        ROOT / "configs" / "cleaning-model-retraining.yaml"
    )
    _config, normalization = load_cleaning_config_bundle(
        ROOT / "configs" / "cleaning.yaml"
    )
    wrong = tmp_path / "wrong.csv"
    wrong.write_text(
        ",".join((*INCREMENTAL_INPUT_COLUMNS, "platform_key")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ModelRetrainingIncrementalError) as error:
        load_incremental_input_csv(
            wrong, plan=plan, normalization_config=normalization
        )
    assert error.value.reason_code == (
        "model_retraining_incremental_input_header_invalid"
    )

    duplicate = tmp_path / "duplicate.csv"
    row = {
        "source_post_id": 9001,
        "source_version": 1,
        "component_id": "component",
        "title": "合成标题",
        "body": "合成正文",
        "source_status": "",
    }
    _write_input(duplicate, [row, row])
    with pytest.raises(ModelRetrainingIncrementalError) as error:
        load_incremental_input_csv(
            duplicate, plan=plan, normalization_config=normalization
        )
    assert error.value.reason_code == (
        "model_retraining_incremental_input_identity_invalid"
    )


@pytest.mark.parametrize(
    ("probability", "expected_action"),
    [(0.31, "auto_keep"), (0.50, "manual_review"), (0.96, "auto_exclude")],
)
def test_incremental_prediction_uses_frozen_model_without_fit(
    probability: float, expected_action: str
) -> None:
    """新记录只调用Qwen完整编码与冻结predict，并使用0.31/0.96。"""

    model = _FrozenPredictOnlyModel(probability)
    record = score_incremental_member(
        _member(),
        model=model,  # type: ignore[arg-type]
        embedding_cache=_MemoryEmbeddingCache(),  # type: ignore[arg-type]
        T_keep=0.31,
        T_exclude=0.96,
    )
    assert not hasattr(model, "fit")
    assert record.provisional_action == expected_action
    assert model.received_texts == [_member().normalized_model_text]
    assert model.received_embeddings is not None
    assert model.received_embeddings.shape == (1, 2)


def test_incremental_manual_task_is_blind_utf8_bom_four_column_csv() -> None:
    """新增批次中间层同步产出同一盲四列表，身份概率仅留私有映射。"""

    model = _FrozenPredictOnlyModel(0.50)
    record = score_incremental_member(
        _member(),
        model=model,  # type: ignore[arg-type]
        embedding_cache=_MemoryEmbeddingCache(),  # type: ignore[arg-type]
        T_keep=0.31,
        T_exclude=0.96,
    )
    payload, private_map = _manual_task_bytes((record,), batch_id="b" * 32)
    assert payload.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    assert tuple(reader.fieldnames or ()) == (
        "task_id",
        "sample_run_id",
        "normalized_model_text",
        "tourism_label",
    )
    row = list(reader)[0]
    assert row["tourism_label"] == ""
    assert "source_post_id" not in row
    assert "p_unrelated" not in row
    assert len(private_map) == 1
    assert private_map[0]["source_post_id"] == record.source_post_id
    assert private_map[0]["p_unrelated"] == 0.50


def test_incremental_package_is_content_addressed_and_strictly_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """动态批次应封存输入、概率、盲表和零fit状态，并依赖哈希复用。"""

    plan = load_model_retraining_plan(
        ROOT / "configs" / "cleaning-model-retraining.yaml"
    )
    delivery_plan = load_model_retraining_delivery_plan(
        ROOT / "configs" / "cleaning-model-retraining-delivery.yaml"
    )
    _config, normalization = load_cleaning_config_bundle(
        ROOT / "configs" / "cleaning.yaml"
    )
    input_csv = tmp_path / "new-batch.csv"
    _write_input(
        input_csv,
        [
            {
                "source_post_id": 9001,
                "source_version": 1,
                "component_id": "new-component-1",
                "title": "合成标题",
                "body": "合成正文",
                "source_status": "",
            }
        ],
    )
    snapshot = SimpleNamespace(
        documents=tuple(
            SimpleNamespace(identity=(index, 1)) for index in range(1, 1301)
        )
    )
    model = _FrozenPredictOnlyModel(0.50)
    policy_manifest = {
        "status": "RESEARCHER_SELECTED_ROUTING_FROZEN",
        "automatic_routing_authorized": True,
        "acceptance_basis": "researcher_accepted_post_hoc_risk",
        "policy_id": "p" * 32,
        "selected": {
            "candidate_name": delivery_plan.selection.candidate_name,
            "candidate_id": delivery_plan.selection.candidate_id,
            "T_keep": 0.31,
            "T_exclude": 0.96,
        },
    }
    model.candidate_id = delivery_plan.selection.candidate_id
    monkeypatch.setattr(
        "tourism_ugc_study.models.text.model_retraining_incremental."
        "load_retraining_snapshot_package",
        lambda *args, **kwargs: (snapshot, {"snapshot_id": "s" * 32}),
    )
    monkeypatch.setattr(
        "tourism_ugc_study.models.text.model_retraining_incremental."
        "load_retraining_routing_policy_package",
        lambda *args, **kwargs: (model, policy_manifest),
    )
    artifact_root = tmp_path / "artifacts"
    result = score_incremental_batch_package(
        input_csv,
        tmp_path / "snapshot",
        tmp_path / "policy",
        artifact_root,
        plan=plan,
        delivery_plan=delivery_plan,
        normalization_config=normalization,
        expected_snapshot_manifest_sha256="a" * 64,
        expected_policy_manifest_sha256="b" * 64,
        code_version="c" * 40,
        embedding_cache=_MemoryEmbeddingCache(),  # type: ignore[arg-type]
    )
    assert result.count == 1
    assert result.manual_task_count == 1
    assert result.fit_call_count == 0
    assert result.embedding_cache_hit_count == 1
    assert result.embedding_cache_miss_count == 0
    assert result.reused is False
    package = artifact_root / result.batch_id
    assert (package / "incremental-scored-records.json").is_file()
    assert (package / "tourism-relevance-predictions.csv").is_file()
    prediction_reader = csv.DictReader(
        io.StringIO(
            (package / "tourism-relevance-predictions.csv")
            .read_bytes()
            .decode("utf-8-sig")
        )
    )
    assert tuple(prediction_reader.fieldnames or ()) == (
        "source_post_id",
        "source_version",
        "component_id",
        "normalized_model_text",
        "p_unrelated",
        "routing_action",
        "provisional_tourism_label",
    )
    prediction = list(prediction_reader)[0]
    assert prediction["routing_action"] == "manual_review"
    assert prediction["provisional_tourism_label"] == ""
    assert (
        package / "manual-review-tourism-relevance-annotation.csv"
    ).read_bytes().startswith(b"\xef\xbb\xbf")

    reused = score_incremental_batch_package(
        input_csv,
        tmp_path / "snapshot",
        tmp_path / "policy",
        artifact_root,
        plan=plan,
        delivery_plan=delivery_plan,
        normalization_config=normalization,
        expected_snapshot_manifest_sha256="a" * 64,
        expected_policy_manifest_sha256="b" * 64,
        code_version="c" * 40,
        embedding_cache=_MemoryEmbeddingCache(),  # type: ignore[arg-type]
        expected_existing_manifest_sha256=result.manifest_sha256,
    )
    assert reused.reused is True
    assert reused.manifest_sha256 == result.manifest_sha256
