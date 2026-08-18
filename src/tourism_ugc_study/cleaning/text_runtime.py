"""确定性文本算法所依赖的 Python、Unicode 与数值库运行时身份。"""

from __future__ import annotations

import hashlib
import json
import platform
import unicodedata
from typing import Mapping

import numpy
import regex
import scipy
import sklearn


def text_runtime_versions() -> Mapping[str, str]:
    """返回会影响规范化、特征或相似度结果的完整运行时版本。"""

    return {
        "implementation": platform.python_implementation(),
        "numpy": numpy.__version__,
        "python": platform.python_version(),
        "regex": regex.__version__,
        "scikit-learn": sklearn.__version__,
        "scipy": scipy.__version__,
        "unicode": unicodedata.unidata_version,
    }


def text_runtime_sha256() -> str:
    """对运行时版本映射计算规范 SHA-256。"""

    payload = json.dumps(
        text_runtime_versions(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_runtime_version_lock() -> str:
    """形成可写入主配置和阶段版本的运行时锁。"""

    return f"text-runtime-v1+sha256:{text_runtime_sha256()}"
