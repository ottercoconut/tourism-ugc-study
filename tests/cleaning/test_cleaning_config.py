from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tourism_ugc_study.cleaning.config import ConfigurationError, load_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning-v2.4.yaml"


def test_load_v24_config_records_versions_and_defaults() -> None:
    config = load_config(CONFIG_PATH)

    assert config.protocol_version == "2.4"
    assert config.text_label_guide_version == "text-relevance-v1.0"
    assert config.image_label_guide_version == "image-noise-v1.0"
    assert config.random_seed == 20260728
    assert config.incremental.max_posts_per_batch == 1000
    assert config.algorithm_versions["derived_schema"] == 1
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
