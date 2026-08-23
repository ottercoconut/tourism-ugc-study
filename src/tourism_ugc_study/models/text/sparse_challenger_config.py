"""首轮 sparse challenger 的预登记配置解析与候选展开。"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class SparseChallengerConfigError(RuntimeError):
    """challenger 配置违反冻结契约时抛出的去敏异常。

    Attributes:
        reason_code: 不含文本、帖子身份或本机路径的稳定失败码。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化稳定失败。

        Args:
            reason_code: 供 CLI、测试和 artifact 使用的失败码。
        """

        super().__init__("formal sparse challenger configuration failed")
        self.reason_code = reason_code


@dataclass(frozen=True)
class SparseCandidateSpec:
    """一个可复现的稀疏文本候选配置。

    Attributes:
        candidate_id: 候选规范参数的截断 SHA-256。
        family: ``baseline_anchor``、``tfidf_linear_svc``、``nbsvm`` 或
            ``tfidf_logistic_regression``。
        representation: ``tfidf`` 或 ``binary_count_nb_log_ratio``。
        classifier: ``linear_svc`` 或 ``logistic_regression``。
        ngram_range: 字符 n-gram 闭区间。
        min_df: 最小文档频次。
        C: 线性分类器正则强度。
        probability: ``group_oof_sigmoid`` 或 ``native_predict_proba``。
        sublinear_tf: TF-IDF 是否使用 sublinear TF；NB-SVM 为 ``False``。
        binary: 计数表示是否二值化；非 NB-SVM 为 ``False``。
        alpha: NB log-count ratio 平滑常数；非 NB-SVM 为 ``None``。
        solver: 逻辑回归 solver；其他模型为 ``None``。
    """

    candidate_id: str
    family: str
    representation: str
    classifier: str
    ngram_range: tuple[int, int]
    min_df: int
    C: float
    probability: str
    sublinear_tf: bool
    binary: bool
    alpha: float | None
    solver: str | None


@dataclass(frozen=True)
class SparseChallengerPlan:
    """内容寻址的首轮 sparse challenger 计划。

    Attributes:
        plan_id: 完整配置摘要的截断身份。
        plan_sha256: 完整配置的规范 SHA-256。
        random_seed: 全部嵌套分组折和模型的固定种子。
        baseline_model_id: 当前 baseline 身份。
        reference_csv_sha256: 当前最终参考 CSV 摘要。
        train_manifest_sha256: 训练成员摘要。
        validation_manifest_sha256: 验证成员摘要。
        test_manifest_sha256: 锁定测试成员摘要。
        acceptance_policy_sha256: UGC 安全优先验收策略摘要。
        outer_folds: 目标外层折数。
        inner_folds: 目标内层折数。
        minimum_folds: 允许失败关闭前的最小折数。
        diagnostic_cutoff: 只用于开发安全诊断的固定分界。
        baseline_spec: 同折重建的 baseline anchor。
        candidates: 预登记的54个 challenger 配置。
    """

    plan_id: str
    plan_sha256: str
    random_seed: int
    baseline_model_id: str
    reference_csv_sha256: str
    train_manifest_sha256: str
    validation_manifest_sha256: str
    test_manifest_sha256: str
    acceptance_policy_sha256: str
    outer_folds: int
    inner_folds: int
    minimum_folds: int
    diagnostic_cutoff: float
    baseline_spec: SparseCandidateSpec
    candidates: tuple[SparseCandidateSpec, ...]


_ROOT_FIELDS = frozenset(
    {
        "artifact_kind",
        "status",
        "random_seed",
        "baseline",
        "acceptance",
        "evaluation",
        "common",
        "families",
        "failure_behavior",
    }
)
_BASELINE_FIELDS = frozenset(
    {
        "model_id",
        "reference_csv_sha256",
        "train_manifest_sha256",
        "validation_manifest_sha256",
        "test_manifest_sha256",
    }
)
_ACCEPTANCE_FIELDS = frozenset({"policy_sha256", "decision_rule"})
_EVALUATION_FIELDS = frozenset(
    {
        "scope",
        "outer_folds",
        "inner_folds",
        "minimum_folds",
        "selection_rule",
        "diagnostic_cutoff",
        "diagnostic_cutoff_is_routing_threshold",
        "validation_role",
        "test_status",
    }
)
_COMMON_FIELDS = frozenset(
    {
        "analyzer",
        "max_df",
        "class_weight",
        "title_body_channels",
        "platform_used",
    }
)
_FAMILY_FIELDS = frozenset(
    {"baseline_anchor", "tfidf_linear_svc", "nbsvm", "tfidf_logistic_regression"}
)


def _exact_mapping(
    value: Any,
    fields: frozenset[str],
    reason_code: str,
) -> Mapping[str, Any]:
    """要求一个节点是精确键集合的映射。"""

    if not isinstance(value, Mapping) or set(value) != fields:
        raise SparseChallengerConfigError(reason_code)
    return value


def _require_sha256(value: Any, reason_code: str) -> str:
    """要求小写十六进制 SHA-256。"""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SparseChallengerConfigError(reason_code)
    return value


def _canonical_sha256(value: object) -> str:
    """计算规范 JSON 的 SHA-256。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_spec(
    *,
    family: str,
    representation: str,
    classifier: str,
    ngram_range: tuple[int, int],
    min_df: int,
    C: float,
    probability: str,
    sublinear_tf: bool,
    binary: bool = False,
    alpha: float | None = None,
    solver: str | None = None,
) -> SparseCandidateSpec:
    """生成带稳定身份的候选参数。"""

    projection = {
        "family": family,
        "representation": representation,
        "classifier": classifier,
        "ngram_range": list(ngram_range),
        "min_df": min_df,
        "C": C,
        "probability": probability,
        "sublinear_tf": sublinear_tf,
        "binary": binary,
        "alpha": alpha,
        "solver": solver,
    }
    return SparseCandidateSpec(
        candidate_id=_canonical_sha256(projection)[:32],
        family=family,
        representation=representation,
        classifier=classifier,
        ngram_range=ngram_range,
        min_df=min_df,
        C=C,
        probability=probability,
        sublinear_tf=sublinear_tf,
        binary=binary,
        alpha=alpha,
        solver=solver,
    )


def _validate_frozen_values(raw: Mapping[str, Any]) -> None:
    """拒绝与预登记矩阵不同的任意值。"""

    baseline = _exact_mapping(
        raw["baseline"], _BASELINE_FIELDS, "sparse_challenger_baseline_invalid"
    )
    acceptance = _exact_mapping(
        raw["acceptance"], _ACCEPTANCE_FIELDS, "sparse_challenger_acceptance_invalid"
    )
    evaluation = _exact_mapping(
        raw["evaluation"], _EVALUATION_FIELDS, "sparse_challenger_evaluation_invalid"
    )
    common = _exact_mapping(
        raw["common"], _COMMON_FIELDS, "sparse_challenger_common_invalid"
    )
    families = _exact_mapping(
        raw["families"], _FAMILY_FIELDS, "sparse_challenger_families_invalid"
    )
    if (
        raw.get("artifact_kind") != "formal-cleaning-sparse-challenger-plan"
        or raw.get("status") != "frozen_not_fit"
        or raw.get("random_seed") != 20260728
        or raw.get("failure_behavior") != "retain_baseline"
        or baseline.get("model_id") != "9cd30922aabf7fb2e2ba42e5a0396cfd"
        or acceptance.get("decision_rule") != "ugc_safety_first"
        or evaluation
        != {
            "scope": "train_nested_group_oof",
            "outer_folds": 5,
            "inner_folds": 4,
            "minimum_folds": 2,
            "selection_rule": "related_safety_then_log_loss_then_pr_auc",
            "diagnostic_cutoff": 0.50,
            "diagnostic_cutoff_is_routing_threshold": False,
            "validation_role": "unique_winner_directional_check_only",
            "test_status": "locked_not_opened",
        }
        or common
        != {
            "analyzer": "char",
            "max_df": 0.995,
            "class_weight": "balanced",
            "title_body_channels": False,
            "platform_used": False,
        }
    ):
        raise SparseChallengerConfigError("sparse_challenger_plan_invalid")
    for field in (
        "reference_csv_sha256",
        "train_manifest_sha256",
        "validation_manifest_sha256",
        "test_manifest_sha256",
    ):
        _require_sha256(baseline.get(field), "sparse_challenger_baseline_invalid")
    _require_sha256(
        acceptance.get("policy_sha256"), "sparse_challenger_acceptance_invalid"
    )
    expected_families = {
        "baseline_anchor": {
            "representation": "tfidf",
            "classifier": "linear_svc",
            "ngram_range": [2, 5],
            "min_df": 2,
            "sublinear_tf": True,
            "C": 1.0,
            "probability": "group_oof_sigmoid",
        },
        "tfidf_linear_svc": {
            "representation": "tfidf",
            "classifier": "linear_svc",
            "ngram_ranges": [[2, 5], [3, 5], [2, 6]],
            "min_df_values": [1, 2],
            "C_values": [0.3, 1.0, 3.0],
            "sublinear_tf": True,
            "probability": "group_oof_sigmoid",
        },
        "nbsvm": {
            "representation": "binary_count_nb_log_ratio",
            "classifier": "linear_svc",
            "ngram_ranges": [[2, 5], [3, 5], [2, 6]],
            "min_df_values": [1, 2],
            "C_values": [0.3, 1.0, 3.0],
            "binary": True,
            "alpha": 1.0,
            "log_count_normalization": "class_l1",
            "probability": "group_oof_sigmoid",
        },
        "tfidf_logistic_regression": {
            "representation": "tfidf",
            "classifier": "logistic_regression",
            "ngram_ranges": [[2, 5], [3, 5], [2, 6]],
            "min_df_values": [1, 2],
            "C_values": [0.3, 1.0, 3.0],
            "sublinear_tf": True,
            "solver": "liblinear",
            "probability": "native_predict_proba",
        },
    }
    if dict(families) != expected_families:
        raise SparseChallengerConfigError("sparse_challenger_families_invalid")


def _expand_candidates(families: Mapping[str, Any]) -> tuple[SparseCandidateSpec, ...]:
    """按预登记笛卡尔积展开54个候选。"""

    values: list[SparseCandidateSpec] = []
    for family in ("tfidf_linear_svc", "nbsvm", "tfidf_logistic_regression"):
        details = families[family]
        for ngram, min_df, C in itertools.product(
            details["ngram_ranges"],
            details["min_df_values"],
            details["C_values"],
        ):
            values.append(
                _candidate_spec(
                    family=family,
                    representation=str(details["representation"]),
                    classifier=str(details["classifier"]),
                    ngram_range=(int(ngram[0]), int(ngram[1])),
                    min_df=int(min_df),
                    C=float(C),
                    probability=str(details["probability"]),
                    sublinear_tf=bool(details.get("sublinear_tf", False)),
                    binary=bool(details.get("binary", False)),
                    alpha=(
                        float(details["alpha"])
                        if "alpha" in details
                        else None
                    ),
                    solver=(str(details["solver"]) if "solver" in details else None),
                )
            )
    if len(values) != 54 or len({item.candidate_id for item in values}) != 54:
        raise SparseChallengerConfigError("sparse_challenger_candidate_matrix_invalid")
    return tuple(values)


def load_sparse_challenger_plan(path: str | Path) -> SparseChallengerPlan:
    """加载并完整校验首轮 sparse challenger 计划。

    Args:
        path: 预登记 challenger YAML。

    Returns:
        展开54个候选并绑定当前 baseline/验收策略的冻结计划。

    Raises:
        SparseChallengerConfigError: 文件不可读、字段未知、数值非法或矩阵漂移。
    """

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SparseChallengerConfigError("sparse_challenger_config_unreadable") from exc
    raw = _exact_mapping(loaded, _ROOT_FIELDS, "sparse_challenger_config_invalid")
    _validate_frozen_values(raw)
    baseline = raw["baseline"]
    acceptance = raw["acceptance"]
    evaluation = raw["evaluation"]
    families = raw["families"]
    anchor = families["baseline_anchor"]
    baseline_spec = _candidate_spec(
        family="baseline_anchor",
        representation=str(anchor["representation"]),
        classifier=str(anchor["classifier"]),
        ngram_range=(int(anchor["ngram_range"][0]), int(anchor["ngram_range"][1])),
        min_df=int(anchor["min_df"]),
        C=float(anchor["C"]),
        probability=str(anchor["probability"]),
        sublinear_tf=bool(anchor["sublinear_tf"]),
    )
    plan_sha256 = _canonical_sha256(raw)
    values = (
        float(evaluation["diagnostic_cutoff"]),
        float(raw["common"]["max_df"]),
    )
    if any(not math.isfinite(value) for value in values):
        raise SparseChallengerConfigError("sparse_challenger_numeric_invalid")
    return SparseChallengerPlan(
        plan_id=plan_sha256[:32],
        plan_sha256=plan_sha256,
        random_seed=int(raw["random_seed"]),
        baseline_model_id=str(baseline["model_id"]),
        reference_csv_sha256=str(baseline["reference_csv_sha256"]),
        train_manifest_sha256=str(baseline["train_manifest_sha256"]),
        validation_manifest_sha256=str(baseline["validation_manifest_sha256"]),
        test_manifest_sha256=str(baseline["test_manifest_sha256"]),
        acceptance_policy_sha256=str(acceptance["policy_sha256"]),
        outer_folds=int(evaluation["outer_folds"]),
        inner_folds=int(evaluation["inner_folds"]),
        minimum_folds=int(evaluation["minimum_folds"]),
        diagnostic_cutoff=float(evaluation["diagnostic_cutoff"]),
        baseline_spec=baseline_spec,
        candidates=_expand_candidates(families),
    )
