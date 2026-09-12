"""冻结模型之外的显式推理设备配置与环境身份。

仅允许改变执行设备和同一帖内完整视图的批量大小；不接收模型、指令、窗口、
聚合、dtype或阈值覆盖。原训练计划与模型包摘要保持不变，新增运行身份用于
隔离CUDA与历史MPS缓存，避免把跨硬件结果宣称为逐位相同。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import yaml


class QwenInferenceExecutionError(ValueError):
    """显式运行配置缺失、摘要不符或越过模型冻结边界。"""

    def __init__(self, reason_code: str) -> None:
        """保存不含文本、凭证和私有路径的失败码。"""
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class QwenInferenceExecution:
    """已验证的运行配置；软件版本另行进入缓存和预测回执。"""

    profile_id: str
    device: str
    batch_size: int
    parameter_dtype: str
    output_dtype: str
    configuration_sha256: str

    def identity(self) -> dict[str, Any]:
        """返回可序列化运行身份，不加载模型或访问GPU。

        缺失实际依赖时让版本查询失败，不虚构环境；相同模型在不同软件环境
        下必须进入不同缓存命名空间。
        """
        return {**asdict(self), "software_versions": {
            name: version(name) for name in (
                "torch", "transformers", "sentence-transformers", "numpy",
                "scikit-learn", "tokenizers", "huggingface-hub",
            )}}


def load_inference_execution(path: Path, expected_sha256: str) -> QwenInferenceExecution:
    """校验外部摘要后读取独立设备配置，禁止借迁移改算法或精度。

    输入须恰好五个公开配置字段，设备只允许MPS或第一块CUDA卡、batch为1—16。
    配置不影响原冻结训练/模型身份；任何多余字段、非法类型或摘要不符均拒绝。
    """
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise QwenInferenceExecutionError("qwen_execution_config_hash_mismatch")
    data = yaml.safe_load(raw)
    if (not isinstance(data, dict)
            or set(data) != {"profile_id", "device", "batch_size", "parameter_dtype", "output_dtype"}
            or not isinstance(data["profile_id"], str) or not data["profile_id"].strip()
            or data["device"] not in ("mps", "cuda:0")
            or type(data["batch_size"]) is not int or not 1 <= data["batch_size"] <= 16
            or data["parameter_dtype"] != "bfloat16" or data["output_dtype"] != "float32"):
        raise QwenInferenceExecutionError("qwen_execution_config_invalid")
    return QwenInferenceExecution(**data, configuration_sha256=digest)
