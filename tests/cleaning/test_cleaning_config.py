"""3.1 文本清洗配置契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.annotation.config import annotation_config
from tourism_ugc_study.cleaning.config import ConfigurationError, load_config
from tourism_ugc_study.cleaning.text_runtime import text_runtime_version_lock
from tourism_ugc_study.models.text.config import relevance_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v3.1.yaml"


def test_load_v31_config_records_text_versions_and_defaults() -> None:
    config = load_config(CONFIG_PATH)

    assert config.protocol_version == "3.1"
    assert config.text_label_guide_version == "text-cleaning-v1.1"
    assert config.random_seed == 20260728
    assert config.algorithm_versions["derived_schema"] == 31
    assert config.algorithm_versions["text_runtime"] == text_runtime_version_lock()
    assert config.algorithm_versions["scheduler"] == "incremental-scheduler-v2-text-only"
    assert annotation_config(config).initial_probability_size == 500
    assert annotation_config(config).minimum_recheck_interval_days == 14
    assert relevance_config(config).ngram_range == (2, 5)
    assert len(config.sha256) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [("api_token", "not-allowed"), ("output_path", "/Users/example/private")],
)
def test_config_rejects_secrets_and_absolute_paths(
    tmp_path: Path, field: str, value: str
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
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )
    assert load_config(reformatted).sha256 == original.sha256
