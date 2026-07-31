from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.cleaning.config import ConfigurationError, load_config
from tourism_ugc_study.annotation.config import annotation_config
from tourism_ugc_study.models.text.config import relevance_config
from tourism_ugc_study.cleaning.text_runtime import text_runtime_version_lock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def test_load_v24_config_records_versions_and_defaults() -> None:
    config = load_config(CONFIG_PATH)

    assert config.protocol_version == "2.4"
    assert config.text_label_guide_version == "text-relevance-v1.0"
    assert config.image_label_guide_version == "image-noise-v1.0"
    assert config.random_seed == 20260728
    assert config.incremental.max_posts_per_batch == 1000
    assert config.algorithm_versions["derived_schema"] == 14
    annotation = annotation_config(config)
    assert annotation.initial_probability_size == 500
    assert annotation.probability_min_per_platform == 80
    assert annotation.initial_targeted_size == 200
    assert annotation.initial_double_label_size == 200
    assert annotation.minimum_raw_agreement == 0.80
    assert annotation.minimum_cohen_kappa == 0.70
    assert annotation.additional_double_label_size == 100
    relevance = relevance_config(config)
    assert relevance.ngram_range == (2, 5)
    assert relevance.c_grid == (0.1, 1.0, 10.0)
    assert relevance.temporal_test_min_per_platform == 20
    assert relevance.platform_stable_negative_min == 30
    assert relevance.low_risk_audit_fraction == 0.05
    assert relevance.low_risk_audit_min_per_platform == 50
    assert config.algorithm_versions["scheduler"] == "incremental-scheduler-v1"
    assert config.algorithm_versions["text_runtime"] == text_runtime_version_lock()
    assert config.image.pillow_version == "12.3.0"
    assert config.image.imagehash_version == "4.3.2"
    assert config.image.phash_hash_size == 8
    assert config.image.phash_highfreq_factor == 4
    assert config.image.candidate_hamming_max == 10
    assert config.image.extreme_aspect_ratio == 8.0
    assert len(config.sha256) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_token", "not-allowed"),
        ("output_path", "/Users/example/private"),
    ],
)
def test_config_rejects_secrets_and_absolute_paths(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw[field] = value
    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_config(invalid_path)


def test_config_hash_ignores_yaml_formatting(tmp_path: Path) -> None:
    original = load_config(CONFIG_PATH)
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    reformatted = tmp_path / "reformatted.yaml"
    reformatted.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )

    assert load_config(reformatted).sha256 == original.sha256


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phash_hash_size", True),
        ("candidate_hamming_max", 0),
        ("tiny_side_px", "64"),
        ("extreme_aspect_ratio", -1.0),
        ("pillow_version", ""),
    ],
)
def test_image_config_rejects_values_with_wrong_type_or_range(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """图片参数必须按声明类型解析，不能依赖调用处进行隐式转换。"""

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["image"][field] = value
    invalid_path = tmp_path / "invalid-image.yaml"
    invalid_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_config(invalid_path)
