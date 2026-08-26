"""KOL/KOC 型作者试点的快照切片、派生规则与双任务包生成。

本模块只读取研究快照，并把身份共同校准与文本共同校准放入同一轮次的两个
隔离任务中。作者原始标识、主页材料和正文只写入 Git 忽略的私有目录；可提交
代码与模板不包含任何真实 UGC。模块拒绝覆盖既有轮次，避免两位编码员的原始
判断被后续运行静默替换。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import shutil
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence


ROLE_RULE_VERSION = "role-pilot-v0.1"
CODEBOOK_VERSION = "v3.13.0"
ROUND_STATUS = "PILOT_ONLY"
SEGMENTATION_RULE_VERSION = "pilot-segmentation-v0.1"

ACTOR_SCOPES = frozenset(
    {"PERSONAL_CREATOR", "ORGANIZATION", "MULTI_AUTHOR", "UNCLEAR"}
)
CONTENT_VERTICALS = frozenset(
    {"TRAVEL", "FOOD", "LIFESTYLE", "GENERAL", "OTHER", "UNK"}
)
EA_POSITIVE_CODES = frozenset(
    {
        "EA_CREDENTIAL",
        "EA_DOMAIN_OCCUPATION",
        "EA_DOMAIN_VERIFICATION",
        "EA_INSTITUTION_AFFILIATION",
        "EA_SPECIALIST_HISTORY",
    }
)
EA_CODES = EA_POSITIVE_CODES | {"EA_NONE", "EA_UNK"}
CE_POSITIVE_CODES = frozenset({"CE_FIRSTHAND_REPEAT", "CE_PEER_ORIENTATION"})
CE_CODES = CE_POSITIVE_CODES | {"CE_NONE", "CE_UNK"}
RAW_ROLE_VALUES = frozenset(
    {"KOL_TYPE", "KOC_TYPE", "HYBRID", "ORDINARY", "UNK", "NA"}
)

ROLE_CODING_FIELDS = (
    "task_id",
    "round_status",
    "author_snapshot_id",
    "evidence_manifest_id",
    "platform",
    "actor_scope",
    "actor_scope_confidence",
    "actor_scope_evidence_ids",
    "content_vertical",
    "content_vertical_confidence",
    "content_vertical_evidence_ids",
    "expert_authority_criterion_codes",
    "expert_authority_confidence",
    "expert_authority_evidence_ids",
    "consumer_experience_criterion_codes",
    "consumer_experience_confidence",
    "consumer_experience_evidence_ids",
    "raw_role_response",
    "raw_role_confidence",
    "raw_role_evidence_ids",
    "community_relation_status",
    "low_confidence_note",
    "annotator_id",
    "annotated_at",
    "role_rule_version",
    "codebook_version",
)

ROLE_EVIDENCE_FIELDS = (
    "task_id",
    "author_snapshot_id",
    "evidence_manifest_id",
    "platform",
    "evidence_source_id",
    "source_type",
    "source_published_at",
    "captured_at",
    "display_name_raw",
    "profile_url_raw",
    "bio_raw",
    "verification_raw",
    "title_raw",
    "content_text_raw",
)

RESTRICTED_LINKAGE_FIELDS = (
    "task_id",
    "author_snapshot_id",
    "platform_key",
    "author_platform_id_raw",
    "platform_post_id",
    "text_item_id",
    "linkage_purpose",
)

ADJUDICATION_FIELDS = (
    "adjudication_id",
    "task_id",
    "author_snapshot_id",
    "adjudication_status",
    "actor_scope_adjudicated",
    "content_vertical_adjudicated",
    "expert_authority_criterion_codes_adjudicated",
    "consumer_experience_criterion_codes_adjudicated",
    "adjudication_reason_code",
    "adjudication_note",
    "adjudicator_id",
    "adjudicated_at",
    "role_rule_version",
    "codebook_version",
)

TEXT_ITEM_FIELDS = (
    "item_id",
    "source_web_post_id",
    "platform_key",
    "sampling_stratum",
    "content_hash",
    "duplicate_cluster_id",
    "assignment_group",
    "codebook_version",
)

TEXT_SEGMENT_FIELDS = (
    "task_id",
    "item_id",
    "post_id",
    "seg_id",
    "platform",
    "raw_text",
    "seg_length",
    "segmentation_rule_version",
)

CALIBRATION_CODING_FIELDS = (
    "annotation_id",
    "item_id",
    "post_id",
    "unit_type",
    "unit_id",
    "platform",
    "raw_text",
    "dimension_code",
    "field_name",
    "field_label_zh",
    "valid_values",
    "is_subjective",
    "label_value",
    "confidence",
    "reason_code",
    "alternative_values",
    "low_confidence_note",
    "evidence_quote",
    "evidence_start_if_repeated",
    "other_issue_type",
    "other_issue_note",
    "annotator_id",
    "annotated_at",
    "codebook_version",
    "template_schema_version",
)


class RolePilotError(ValueError):
    """快照、编码输入或轮次输出违反试点契约时抛出。"""


@dataclass(frozen=True)
class RoleDerivation:
    """从裁决后的原子输入确定性派生的角色结果。"""

    expert_authority: str
    consumer_experience: str
    sustained_creation: str
    community_influence: str
    evidence_status: str
    creator_role: str


@dataclass(frozen=True)
class AuthorCandidate:
    """从研究切片汇总出的单个平台作者候选。"""

    platform: str
    author_platform_id: str
    post_count: int
    distinct_dates: int
    span_days: int
    profile_material_available: bool
    first_published_at: str
    last_published_at: str
    stratum: str

    @property
    def key(self) -> tuple[str, str]:
        """返回数据库内唯一作者复合键。"""

        return self.platform, self.author_platform_id


@dataclass(frozen=True)
class PilotPackageResult:
    """一次成功生成的双任务包摘要。"""

    output_dir: Path
    manifest_path: Path
    role_author_count: int
    text_post_count: int
    role_strata: Mapping[str, int]
    platform_role_counts: Mapping[str, int]
    platform_text_counts: Mapping[str, int]


@dataclass(frozen=True)
class TextFieldSpec:
    """共同校准长表中的单个文本原子字段。"""

    dimension_code: str
    field_name: str
    label_zh: str
    valid_values: str
    unit_type: str = "TEXT_SEGMENT"


def _binary_specs(
    dimension: str, fields: Sequence[tuple[str, str]], *, conditional: bool = False
) -> tuple[TextFieldSpec, ...]:
    values = "0|1|NA|UNK|UNRESOLVED" if conditional else "0|1|UNK|UNRESOLVED"
    return tuple(TextFieldSpec(dimension, name, label, values) for name, label in fields)


TEXT_FIELD_SPECS = (
    TextFieldSpec(
        "V1",
        "has_commercial_disclosure",
        "明确商业披露",
        "0|1|UNK|UNRESOLVED",
        "POST",
    ),
    TextFieldSpec(
        "V2",
        "destination_type",
        "主要目的地类型",
        "NATURE|CULTURE|CITY|RESORT|MIXED|UNK|UNRESOLVED",
        "POST",
    ),
    *_binary_specs(
        "V3",
        (
            ("cs_inf", "信息提供型"),
            ("cs_inf_itn", "行程与路线组织"),
            ("cs_inf_par", "实用参数提供"),
            ("cs_inf_pro", "操作指导与选择辅助"),
            ("cs_inf_oth", "其他信息提供方式"),
            ("cs_edu", "知识阐释型"),
            ("cs_edu_bkg", "背景知识介绍"),
            ("cs_edu_cau", "成因与机制解释"),
            ("cs_edu_mea", "意义与价值阐释"),
            ("cs_edu_oth", "其他知识阐释方式"),
            ("cs_emo", "情感渲染型"),
            ("cs_emo_sen", "感官意象"),
            ("cs_emo_atm", "氛围营造"),
            ("cs_emo_aff", "主体情绪与身体感受"),
            ("cs_emo_oth", "其他情感渲染方式"),
            ("cs_int", "互动激发型"),
            ("is_que", "面向受众提问"),
            ("is_dir", "互动行动号召"),
            ("is_soc", "社群与身份参与"),
            ("is_oth", "其他互动方式"),
            ("is_non", "无互动信号"),
            ("cs_oth", "其他或未命中策略"),
            ("cs_oth_gre", "寒暄与开场收束"),
            ("cs_oth_str", "结构过渡与篇章导航"),
            ("cs_oth_met", "创作与平台元话语"),
            ("cs_oth_sym", "纯符号与纯标签"),
            ("cs_oth_res", "其他残余表达"),
        ),
    ),
    *_binary_specs(
        "V4",
        (
            ("rs_n", "自然旅游资源"),
            ("rs_r", "人文旅游资源"),
            ("rs_n_geo", "地文景观"),
            ("rs_n_wat", "水域风光"),
            ("rs_n_bio", "生物景观"),
            ("rs_n_cli", "天象气候"),
            ("rs_r_his", "遗址遗迹"),
            ("rs_r_arc", "建筑设施"),
            ("rs_r_gas", "饮食文化"),
            ("rs_r_fol", "民俗风情"),
            ("rs_r_rec", "休闲游憩活动"),
            ("rs_r_evt", "节事与文体事件"),
            ("evt_spt", "体育赛事"),
            ("evt_per", "演艺活动"),
            ("evt_fes", "节庆会展"),
            ("evt_oth", "其他节事与文体事件"),
        ),
    ),
    *_binary_specs(
        "V5",
        (
            ("at_has_info", "信息性陈述"),
            ("at_has_eval", "评价性表达"),
            ("at_has_sug", "建议或推荐"),
            ("at_non", "其他或未命中语言功能"),
            ("sentiment_judgeable", "情感方向可判定"),
        ),
    ),
    TextFieldSpec(
        "V5",
        "at_eval_subtype",
        "评价表达亚类",
        "ADM|SAT|EXC|DIS|FRU|NOS|NA|UNK|UNRESOLVED",
    ),
    TextFieldSpec(
        "V5",
        "sentiment_dir",
        "情感方向",
        "POS|NEG|MIX|NEU|NA|UNK|UNRESOLVED",
    ),
    TextFieldSpec(
        "V5", "sentiment_pos_val", "正面情感强度", "0|1|2|NA|UNK|UNRESOLVED"
    ),
    TextFieldSpec(
        "V5", "sentiment_neg_val", "负面情感强度", "0|1|2|NA|UNK|UNRESOLVED"
    ),
    TextFieldSpec(
        "V6", "asp_applicable", "目的地属性指向适用", "0|1|UNK|UNRESOLVED"
    ),
    *_binary_specs(
        "V6",
        (
            ("asp_exp", "吸引力与体验"),
            ("asp_res", "资源或吸引物品质"),
            ("asp_act", "活动与游览体验"),
            ("asp_atm", "地方氛围与社会环境"),
            ("asp_sup", "消费、服务与设施"),
            ("asp_pri", "价格与性价比"),
            ("asp_ser", "服务态度与专业性"),
            ("asp_fac", "设施完备与使用体验"),
            ("asp_ops", "运营与可达"),
            ("asp_cro", "客流、排队与拥挤"),
            ("asp_mgt", "秩序与运营管理"),
            ("asp_acc", "交通与可达性"),
            ("asp_wel", "卫生、安全与保障"),
            ("asp_hyg", "环境与食品卫生"),
            ("asp_saf", "人身、财产与活动风险"),
            ("asp_sec", "治安、警示与应急保障"),
            ("asp_oth", "其他目的地属性"),
        ),
        conditional=True,
    ),
)


def _sha256_file(path: Path) -> str:
    """以分块方式计算文件SHA-256，避免把大型快照读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    """通过SQLite URI强制只读打开数据库。"""

    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _write_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    """以稳定列序写UTF-8 CSV；调用方负责保证目标位于新建轮次中。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def create_research_slice(
    source_database: Path,
    output_database: Path,
    metadata_output: Path,
    *,
    source_manifest: Path | None = None,
) -> dict[str, object]:
    """从全库快照创建只含规范化``web_posts``的只读研究切片。

    输出已存在时直接拒绝，避免无意覆盖工程伙伴交付的研究快照。源manifest若
    提供，则必须与实际文件SHA和行数一致。输出数据库不包含图片、评论、调度、
    登录或爬虫控制表。
    """

    source_database = source_database.resolve()
    output_database = output_database.resolve()
    metadata_output = metadata_output.resolve()
    if not source_database.is_file():
        raise RolePilotError(f"源数据库不存在：{source_database}")
    if output_database.exists() or metadata_output.exists():
        raise RolePilotError("研究切片或元数据已存在；为保护谱系，拒绝覆盖")

    source_sha = _sha256_file(source_database)
    manifest: dict[str, object] | None = None
    if source_manifest is not None:
        manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
        expected_sha = str(manifest.get("snapshot_sha256", ""))
        if expected_sha != source_sha:
            raise RolePilotError(
                f"源快照SHA不匹配：manifest={expected_sha} actual={source_sha}"
            )

    with _open_readonly_database(source_database) as source:
        integrity = tuple(row[0] for row in source.execute("PRAGMA integrity_check"))
        if integrity != ("ok",):
            raise RolePilotError(f"源数据库完整性检查失败：{integrity}")
        source_columns = {
            row[1] for row in source.execute("PRAGMA table_info(web_posts)")
        }
        required = {
            "platform_key",
            "platform_post_id",
            "source_url",
            "canonical_url",
            "author_platform_id",
            "author_display_name",
            "author_profile_url",
            "author_description",
            "author_verified",
            "author_verified_text",
            "author_followers_count",
            "content_text",
            "title",
            "published_at",
            "captured_at",
            "source_type",
            "keyword",
            "status",
        }
        missing = sorted(required - source_columns)
        if missing:
            raise RolePilotError(f"web_posts缺少字段：{', '.join(missing)}")
        source_count = int(source.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0])
        if manifest is not None:
            manifest_count = int(
                dict(manifest.get("table_row_counts", {})).get("web_posts", -1)
            )
            if manifest_count != source_count:
                raise RolePilotError(
                    f"源快照行数不匹配：manifest={manifest_count} actual={source_count}"
                )

        output_database.parent.mkdir(parents=True, exist_ok=True)
        metadata_output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="role-pilot-slice-", dir=output_database.parent
        ) as temporary_dir:
            temporary_database = Path(temporary_dir) / output_database.name
            destination = sqlite3.connect(temporary_database)
            try:
                destination.executescript(
                    """
                    PRAGMA journal_mode=DELETE;
                    PRAGMA synchronous=FULL;
                    PRAGMA user_version=1;
                    CREATE TABLE web_posts (
                        platform_key TEXT NOT NULL,
                        platform_post_id TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        author_platform_id TEXT NOT NULL,
                        author_display_name TEXT,
                        author_profile_url TEXT,
                        author_description TEXT,
                        author_verification_raw TEXT,
                        author_followers_count INTEGER,
                        follower_observation_status TEXT NOT NULL,
                        title TEXT,
                        content_text TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        captured_at TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        keyword TEXT,
                        parse_status TEXT NOT NULL,
                        PRIMARY KEY (platform_key, platform_post_id)
                    );
                    CREATE INDEX idx_role_pilot_author
                        ON web_posts(platform_key, author_platform_id);
                    CREATE INDEX idx_role_pilot_author_time
                        ON web_posts(platform_key, author_platform_id, published_at);
                    """
                )
                query = """
                    SELECT
                        platform_key,
                        platform_post_id,
                        COALESCE(NULLIF(TRIM(canonical_url), ''), source_url),
                        author_platform_id,
                        author_display_name,
                        author_profile_url,
                        author_description,
                        CASE
                            WHEN NULLIF(TRIM(author_verified_text), '') IS NOT NULL
                                THEN author_verified_text
                            WHEN author_verified = 1 THEN 'VERIFIED'
                            WHEN author_verified = 0 THEN 'UNVERIFIED'
                            ELSE NULL
                        END,
                        author_followers_count,
                        CASE
                            WHEN author_followers_count IS NOT NULL THEN 'OBSERVED'
                            ELSE 'MISSING'
                        END,
                        title,
                        content_text,
                        published_at,
                        captured_at,
                        source_type,
                        keyword,
                        status
                    FROM web_posts
                    ORDER BY platform_key, platform_post_id
                """
                insert = """
                    INSERT INTO web_posts VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                """
                cursor = source.execute(query)
                while True:
                    rows = cursor.fetchmany(1000)
                    if not rows:
                        break
                    destination.executemany(insert, (tuple(row) for row in rows))
                destination.commit()
                derived_integrity = tuple(
                    row[0] for row in destination.execute("PRAGMA integrity_check")
                )
                derived_count = int(
                    destination.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]
                )
                table_names = tuple(
                    row[0]
                    for row in destination.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                )
            finally:
                destination.close()
            if derived_integrity != ("ok",) or derived_count != source_count:
                raise RolePilotError("研究切片写后校验失败")
            if table_names != ("web_posts",):
                raise RolePilotError(f"研究切片出现非预期数据表：{table_names}")
            os.replace(temporary_database, output_database)

    derived_sha = _sha256_file(output_database)
    metadata: dict[str, object] = {
        "artifact": output_database.name,
        "artifact_kind": "role-pilot-research-slice",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema_version": "role-pilot-slice-v1",
        "source_snapshot": source_database.name,
        "source_snapshot_created_at": (
            None if manifest is None else manifest.get("created_at")
        ),
        "source_schema_version": (
            None if manifest is None else manifest.get("schema_version")
        ),
        "source_sha256": source_sha,
        "artifact_sha256": derived_sha,
        "web_posts_row_count": source_count,
        "sqlite_integrity_check": ["ok"],
        "tables": ["web_posts"],
        "read_only_delivery": True,
    }
    metadata_output.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_database.chmod(0o444)
    return metadata


def _parse_date(value: object) -> date | None:
    """尽量从ISO时间、日期或常见SQL时间文本中取得日历日期。"""

    text = "" if value is None else str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _profile_material_available(row: Mapping[str, object]) -> bool:
    """判断是否存在可审计主页材料；昵称本身不充当等价身份材料。"""

    def value(field: str) -> object:
        try:
            return row[field]
        except (KeyError, IndexError):
            return ""

    return any(
        str(value(field) or "").strip()
        for field in (
            "author_profile_url",
            "author_description",
            "author_verification_raw",
        )
    )


def collect_author_candidates(database: Path) -> tuple[AuthorCandidate, ...]:
    """按平台作者键汇总覆盖度，并分成丰富、边界和随机候选框。"""

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    with _open_readonly_database(database) as connection:
        for row in connection.execute(
            """
            SELECT platform_key, author_platform_id, published_at,
                   author_profile_url, author_description, author_verification_raw
            FROM web_posts
            WHERE author_platform_id IS NOT NULL
              AND TRIM(author_platform_id) <> ''
            ORDER BY platform_key, author_platform_id, published_at, platform_post_id
            """
        ):
            grouped[(row["platform_key"], row["author_platform_id"])].append(row)

    candidates: list[AuthorCandidate] = []
    for (platform, author_id), rows in grouped.items():
        dates = sorted({parsed for row in rows if (parsed := _parse_date(row["published_at"]))})
        distinct_dates = len(dates)
        span_days = (dates[-1] - dates[0]).days if len(dates) >= 2 else 0
        has_profile = any(_profile_material_available(row) for row in rows)
        longitudinal = distinct_dates >= 3 and span_days >= 30
        if longitudinal and has_profile:
            stratum = "HISTORY_RICH"
        elif longitudinal or has_profile or len(rows) >= 2:
            stratum = "BOUNDARY"
        else:
            stratum = "RANDOM_FRAME"
        candidates.append(
            AuthorCandidate(
                platform=platform,
                author_platform_id=author_id,
                post_count=len(rows),
                distinct_dates=distinct_dates,
                span_days=span_days,
                profile_material_available=has_profile,
                first_published_at="" if not dates else dates[0].isoformat(),
                last_published_at="" if not dates else dates[-1].isoformat(),
                stratum=stratum,
            )
        )
    return tuple(sorted(candidates, key=lambda item: item.key))


def _balanced_take(
    candidates: Sequence[AuthorCandidate], count: int, randomizer: random.Random
) -> tuple[AuthorCandidate, ...]:
    """在不创造伪配额的前提下轮转平台抽取候选。"""

    by_platform: dict[str, list[AuthorCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_platform[candidate.platform].append(candidate)
    for values in by_platform.values():
        randomizer.shuffle(values)
    selected: list[AuthorCandidate] = []
    platforms = sorted(by_platform)
    while len(selected) < count and platforms:
        progressed = False
        for platform in tuple(platforms):
            values = by_platform[platform]
            if values and len(selected) < count:
                selected.append(values.pop())
                progressed = True
            if not values:
                platforms.remove(platform)
        if not progressed:
            break
    return tuple(selected)


def select_role_authors(
    candidates: Sequence[AuthorCandidate], *, seed: int, total: int = 25
) -> tuple[tuple[AuthorCandidate, ...], dict[str, object]]:
    """按15丰富、5边界、5随机的目标框抽取V0作者。

    某层不足时不复制作者或降低证据定义，而把缺口重分配到尚未入选的候选，
    并在审计摘要中保留实际数量与缺口。
    """

    if total <= 0:
        raise RolePilotError("V0作者数必须为正整数")
    randomizer = random.Random(seed)
    target = {
        "HISTORY_RICH": min(15, total),
        "BOUNDARY": min(5, max(total - 15, 0)),
        "RANDOM_FRAME": max(total - 20, 0),
    }
    selected: list[AuthorCandidate] = []
    selected_keys: set[tuple[str, str]] = set()
    actual_by_requested_stratum: Counter[str] = Counter()
    shortages: dict[str, int] = {}
    for stratum in ("HISTORY_RICH", "BOUNDARY", "RANDOM_FRAME"):
        desired = target[stratum]
        available = [
            item
            for item in candidates
            if item.stratum == stratum and item.key not in selected_keys
        ]
        taken = _balanced_take(available, desired, randomizer)
        selected.extend(taken)
        selected_keys.update(item.key for item in taken)
        actual_by_requested_stratum[stratum] += len(taken)
        shortages[stratum] = max(desired - len(taken), 0)

    if len(selected) < total:
        remainder = [item for item in candidates if item.key not in selected_keys]
        fill = _balanced_take(remainder, total - len(selected), randomizer)
        selected.extend(fill)
        selected_keys.update(item.key for item in fill)
    if len(selected) != total:
        raise RolePilotError(f"可用作者不足：需要{total}，实际{len(selected)}")

    actual_strata = Counter(item.stratum for item in selected)
    audit: dict[str, object] = {
        "target_by_stratum": target,
        "initial_shortage_by_stratum": shortages,
        "actual_by_stratum": dict(sorted(actual_strata.items())),
        "platform_counts": dict(
            sorted(Counter(item.platform for item in selected).items())
        ),
        "platform_coverage_gap": sorted(
            set(item.platform for item in candidates)
            - set(item.platform for item in selected)
        ),
    }
    return tuple(selected), audit


def _component_from_codes(
    codes: Iterable[str], *, positive: frozenset[str], none_code: str, unk_code: str
) -> int | None:
    normalized = frozenset(code.strip() for code in codes if code.strip())
    if not normalized:
        return None
    if none_code in normalized and len(normalized) > 1:
        raise RolePilotError(f"{none_code}不能与其他criterion code共存")
    if unk_code in normalized and len(normalized) > 1:
        raise RolePilotError(f"{unk_code}不能与其他criterion code共存")
    if none_code in normalized:
        return 0
    if unk_code in normalized:
        return None
    if normalized <= positive:
        return 1
    raise RolePilotError(f"存在不受支持的criterion code：{sorted(normalized - positive)}")


def derive_creator_role(
    *,
    actor_scope: str,
    expert_authority_codes: Iterable[str],
    consumer_experience_codes: Iterable[str],
    distinct_source_dates: int,
    source_span_days: int,
    profile_material_available: bool,
) -> RoleDerivation:
    """按``role-pilot-v0.1``派生证据组件和KOL/KOC型角色。

    CI固定为``UNAVAILABLE``且完全不参与充分性和角色矩阵。组织或多人账号
    返回``NA``；材料不足时，即使编码员误填``EA_NONE/CE_NONE``也会拒绝，
    防止把“没抓到”误写为“明确不存在”。
    """

    if actor_scope not in ACTOR_SCOPES:
        raise RolePilotError(f"未知actor_scope：{actor_scope}")
    ea_codes = tuple(code.strip() for code in expert_authority_codes if code.strip())
    ce_codes = tuple(code.strip() for code in consumer_experience_codes if code.strip())
    if not set(ea_codes) <= EA_CODES:
        raise RolePilotError("EA代码超出封闭值域")
    if not set(ce_codes) <= CE_CODES:
        raise RolePilotError("CE代码超出封闭值域")

    if actor_scope in {"ORGANIZATION", "MULTI_AUTHOR"}:
        return RoleDerivation("NA", "NA", "NA", "UNAVAILABLE", "OUT_OF_SCOPE", "NA")
    if actor_scope == "UNCLEAR":
        return RoleDerivation("UNK", "UNK", "UNK", "UNAVAILABLE", "INSUFFICIENT", "UNK")

    coverage_sufficient = (
        distinct_source_dates >= 3
        and source_span_days >= 30
        and profile_material_available
    )
    if not coverage_sufficient and (
        "EA_NONE" in ea_codes or "CE_NONE" in ce_codes
    ):
        raise RolePilotError("证据包不足时不得填写EA_NONE或CE_NONE，必须使用UNK")
    expert = _component_from_codes(
        ea_codes,
        positive=EA_POSITIVE_CODES,
        none_code="EA_NONE",
        unk_code="EA_UNK",
    )
    consumer_code_set = frozenset(ce_codes)
    if "CE_NONE" in consumer_code_set and len(consumer_code_set) > 1:
        raise RolePilotError("CE_NONE不能与其他criterion code共存")
    if "CE_UNK" in consumer_code_set and len(consumer_code_set) > 1:
        raise RolePilotError("CE_UNK不能与其他criterion code共存")
    if consumer_code_set == CE_POSITIVE_CODES:
        consumer: int | None = 1
    elif consumer_code_set == {"CE_NONE"}:
        consumer = 0
    else:
        consumer = None
    sustained = 1 if distinct_source_dates >= 3 and source_span_days >= 30 else None
    evidence_sufficient = (
        coverage_sufficient
        and expert in {0, 1}
        and consumer in {0, 1}
        and sustained == 1
    )
    if not evidence_sufficient:
        return RoleDerivation(
            "UNK" if expert is None else str(expert),
            "UNK" if consumer is None else str(consumer),
            "UNK" if sustained is None else str(sustained),
            "UNAVAILABLE",
            "INSUFFICIENT",
            "UNK",
        )
    assert expert is not None and consumer is not None
    if expert == 1 and consumer == 0:
        role = "KOL_TYPE"
    elif expert == 0 and consumer == 1:
        role = "KOC_TYPE"
    elif expert == 1 and consumer == 1:
        role = "HYBRID"
    else:
        role = "ORDINARY"
    return RoleDerivation(
        str(expert), str(consumer), "1", "UNAVAILABLE", "SUFFICIENT", role
    )


def select_quantile_posts(rows: Sequence[Mapping[str, object]], limit: int = 5) -> tuple[Mapping[str, object], ...]:
    """确定性选择最早、最晚和时间分位点帖子，最多返回``limit``篇。"""

    if limit <= 0:
        raise RolePilotError("历史帖上限必须为正整数")
    ordered = sorted(
        rows,
        key=lambda row: (
            _parse_date(row.get("published_at")) or date.min,
            str(row.get("platform_post_id") or ""),
        ),
    )
    if len(ordered) <= limit:
        return tuple(ordered)
    if limit == 1:
        return (ordered[0],)
    positions = [round(index * (len(ordered) - 1) / (limit - 1)) for index in range(limit)]
    unique_positions: list[int] = []
    for position in positions:
        if position not in unique_positions:
            unique_positions.append(position)
    if len(unique_positions) < limit:
        for position in range(len(ordered)):
            if position not in unique_positions:
                unique_positions.append(position)
            if len(unique_positions) == limit:
                break
    return tuple(ordered[position] for position in sorted(unique_positions))


_SEGMENT_BOUNDARY = re.compile(r"(?<=[。！？；!?;])")


def segment_text(text: str) -> tuple[str, ...]:
    """按冻结标点与换行生成共同校准片段，不改写原文字词。

    v0.1只执行可完全复现的句末标点、分号和换行边界。逗号处的属性或情感
    切换需要在共同校准的问题队列中登记为``UNITIZATION_PROBLEM``，编码员仍
    对同一固定``seg_id``作答，不能各自改段。
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    segments: list[str] = []
    for line in normalized.split("\n"):
        stripped_line = line.strip()
        if not stripped_line:
            continue
        for part in _SEGMENT_BOUNDARY.split(stripped_line):
            stripped = part.strip()
            if stripped:
                segments.append(stripped)
    return tuple(segments) or ((normalized.strip() or "[EMPTY]"),)


def _hmac_identifier(secret: bytes, *parts: str, prefix: str) -> str:
    message = "\x1f".join(parts).encode("utf-8")
    token = hmac.new(secret, message, hashlib.sha256).hexdigest()[:16]
    return f"{prefix}-{token}"


def _fetch_author_rows(
    connection: sqlite3.Connection, candidate: AuthorCandidate
) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        """
        SELECT * FROM web_posts
        WHERE platform_key = ? AND author_platform_id = ?
        ORDER BY published_at, platform_post_id
        """,
        candidate.key,
    ).fetchall()
    return tuple(dict(row) for row in rows)


def _choose_text_posts(
    connection: sqlite3.Connection,
    *,
    excluded_authors: set[tuple[str, str]],
    count: int,
    seed: int,
) -> tuple[dict[str, object], ...]:
    rows = tuple(
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM web_posts
            WHERE content_text IS NOT NULL AND TRIM(content_text) <> ''
            ORDER BY platform_key, author_platform_id, published_at, platform_post_id
            """
        )
        if (row["platform_key"], row["author_platform_id"]) not in excluded_authors
    )
    by_author: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_author[(str(row["platform_key"]), str(row["author_platform_id"]))].append(row)
    randomizer = random.Random(seed)
    author_candidates = [
        AuthorCandidate(
            platform=key[0],
            author_platform_id=key[1],
            post_count=len(values),
            distinct_dates=0,
            span_days=0,
            profile_material_available=False,
            first_published_at="",
            last_published_at="",
            stratum="TEXT_RANDOM",
        )
        for key, values in by_author.items()
    ]
    authors = _balanced_take(author_candidates, count, randomizer)
    if len(authors) != count:
        raise RolePilotError(f"文本任务可用作者不足：需要{count}，实际{len(authors)}")
    selected: list[dict[str, object]] = []
    for author in authors:
        values = by_author[author.key]
        selected.append(values[randomizer.randrange(len(values))])
    return tuple(selected)


def _coding_row(
    *,
    task_id: str,
    item_id: str,
    post_id: str,
    unit_type: str,
    unit_id: str,
    platform: str,
    raw_text: str,
    spec: TextFieldSpec,
    annotator_id: str,
) -> dict[str, str]:
    annotation_id = hashlib.sha256(
        f"{task_id}\x1f{annotator_id}\x1f{unit_id}\x1f{spec.field_name}".encode()
    ).hexdigest()[:20]
    return {
        "annotation_id": f"ANN-{annotation_id}",
        "item_id": item_id,
        "post_id": post_id,
        "unit_type": unit_type,
        "unit_id": unit_id,
        "platform": platform,
        "raw_text": raw_text,
        "dimension_code": spec.dimension_code,
        "field_name": spec.field_name,
        "field_label_zh": spec.label_zh,
        "valid_values": spec.valid_values,
        "is_subjective": "1",
        "label_value": "",
        "confidence": "",
        "reason_code": "",
        "alternative_values": "",
        "low_confidence_note": "",
        "evidence_quote": "",
        "evidence_start_if_repeated": "",
        "other_issue_type": "",
        "other_issue_note": "",
        "annotator_id": annotator_id,
        "annotated_at": "",
        "codebook_version": CODEBOOK_VERSION,
        "template_schema_version": "calibration-coding-v1.0",
    }


def _file_manifest(root: Path, files: Iterable[Path]) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(files)
    ]


def prepare_pilot_package(
    database: Path,
    output_dir: Path,
    *,
    seed: int = 20260824,
    role_author_count: int = 25,
    text_post_count: int = 25,
    max_role_posts: int = 5,
    coder_ids: Sequence[str] = ("CODER_A", "CODER_B"),
    content_workbook_template: Path | None = None,
    database_metadata: Path | None = None,
) -> PilotPackageResult:
    """生成同轮、隔离、不可覆盖的V0与V1—V6共同校准任务包。"""

    database = database.resolve()
    output_dir = output_dir.resolve()
    if not database.is_file():
        raise RolePilotError(f"研究切片不存在：{database}")
    if len(coder_ids) != 2 or len(set(coder_ids)) != 2:
        raise RolePilotError("共同校准必须提供两个不同的匿名编码员ID")
    if output_dir.exists():
        raise RolePilotError("目标轮次已存在；原始编码不可覆盖")

    candidates = collect_author_candidates(database)
    role_authors, sampling_audit = select_role_authors(
        candidates, seed=seed, total=role_author_count
    )
    role_keys = {candidate.key for candidate in role_authors}
    secret = secrets.token_bytes(32)
    role_task_id = "V0-ROLE-CALIBRATION-20260824-V01"
    text_task_id = "V1-V6-TEXT-CALIBRATION-20260824-V01"
    db_sha = _sha256_file(database)

    with _open_readonly_database(database) as connection:
        text_posts = _choose_text_posts(
            connection,
            excluded_authors=role_keys,
            count=text_post_count,
            seed=seed + 1,
        )
        text_author_keys = {
            (str(row["platform_key"]), str(row["author_platform_id"]))
            for row in text_posts
        }
        if role_keys & text_author_keys:
            raise RolePilotError("V0与文本任务作者发生交叉")

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{output_dir.name}-", dir=output_dir.parent
        ) as temporary_parent:
            temporary_root = Path(temporary_parent) / output_dir.name
            role_dir = temporary_root / "role"
            text_dir = temporary_root / "text"
            role_dir.mkdir(parents=True)
            text_dir.mkdir(parents=True)

            evidence_rows: list[dict[str, object]] = []
            linkage_rows: list[dict[str, object]] = []
            role_template_rows_by_coder: dict[str, list[dict[str, object]]] = {
                coder: [] for coder in coder_ids
            }
            coverage_rows: list[dict[str, object]] = []

            for candidate in role_authors:
                author_snapshot_id = _hmac_identifier(
                    secret, *candidate.key, prefix="AUTH"
                )
                evidence_manifest_id = f"EVID-{author_snapshot_id[5:]}"
                rows = _fetch_author_rows(connection, candidate)
                selected_posts = select_quantile_posts(rows, max_role_posts)
                representative = rows[-1]
                profile_source_id = f"SRC-{author_snapshot_id[5:]}-PROFILE"
                evidence_rows.append(
                    {
                        "task_id": role_task_id,
                        "author_snapshot_id": author_snapshot_id,
                        "evidence_manifest_id": evidence_manifest_id,
                        "platform": candidate.platform,
                        "evidence_source_id": profile_source_id,
                        "source_type": "PROFILE",
                        "source_published_at": "",
                        "captured_at": representative.get("captured_at", ""),
                        "display_name_raw": representative.get("author_display_name", ""),
                        "profile_url_raw": representative.get("author_profile_url", ""),
                        "bio_raw": representative.get("author_description", ""),
                        "verification_raw": representative.get("author_verification_raw", ""),
                        "title_raw": "",
                        "content_text_raw": "",
                    }
                )
                for index, row in enumerate(selected_posts, start=1):
                    source_id = f"SRC-{author_snapshot_id[5:]}-P{index:02d}"
                    evidence_rows.append(
                        {
                            "task_id": role_task_id,
                            "author_snapshot_id": author_snapshot_id,
                            "evidence_manifest_id": evidence_manifest_id,
                            "platform": candidate.platform,
                            "evidence_source_id": source_id,
                            "source_type": "HISTORICAL_POST",
                            "source_published_at": row.get("published_at", ""),
                            "captured_at": row.get("captured_at", ""),
                            "display_name_raw": "",
                            "profile_url_raw": "",
                            "bio_raw": "",
                            "verification_raw": "",
                            "title_raw": row.get("title", ""),
                            "content_text_raw": row.get("content_text", ""),
                        }
                    )
                    linkage_rows.append(
                        {
                            "task_id": role_task_id,
                            "author_snapshot_id": author_snapshot_id,
                            "platform_key": candidate.platform,
                            "author_platform_id_raw": candidate.author_platform_id,
                            "platform_post_id": row.get("platform_post_id", ""),
                            "text_item_id": "",
                            "linkage_purpose": "V0_EVIDENCE",
                        }
                    )
                coverage_rows.append(
                    {
                        "author_snapshot_id": author_snapshot_id,
                        "sampling_stratum": candidate.stratum,
                        "available_post_count": candidate.post_count,
                        "displayed_post_count": len(selected_posts),
                        "distinct_source_dates": candidate.distinct_dates,
                        "source_span_days": candidate.span_days,
                        "profile_material_available": int(
                            candidate.profile_material_available
                        ),
                        "coverage_status": (
                            "SUFFICIENT_FOR_NONE_CODES"
                            if candidate.distinct_dates >= 3
                            and candidate.span_days >= 30
                            and candidate.profile_material_available
                            else "INSUFFICIENT_FOR_NONE_CODES"
                        ),
                    }
                )
                for coder in coder_ids:
                    role_template_rows_by_coder[coder].append(
                        {
                            "task_id": role_task_id,
                            "round_status": ROUND_STATUS,
                            "author_snapshot_id": author_snapshot_id,
                            "evidence_manifest_id": evidence_manifest_id,
                            "platform": candidate.platform,
                            "actor_scope": "",
                            "actor_scope_confidence": "",
                            "actor_scope_evidence_ids": "",
                            "content_vertical": "",
                            "content_vertical_confidence": "",
                            "content_vertical_evidence_ids": "",
                            "expert_authority_criterion_codes": "",
                            "expert_authority_confidence": "",
                            "expert_authority_evidence_ids": "",
                            "consumer_experience_criterion_codes": "",
                            "consumer_experience_confidence": "",
                            "consumer_experience_evidence_ids": "",
                            "raw_role_response": "",
                            "raw_role_confidence": "",
                            "raw_role_evidence_ids": "",
                            "community_relation_status": "UNAVAILABLE",
                            "low_confidence_note": "",
                            "annotator_id": coder,
                            "annotated_at": "",
                            "role_rule_version": ROLE_RULE_VERSION,
                            "codebook_version": CODEBOOK_VERSION,
                        }
                    )

            _write_csv(role_dir / "author-evidence.csv", ROLE_EVIDENCE_FIELDS, evidence_rows)
            _write_csv(
                role_dir / "coverage-derived.csv",
                (
                    "author_snapshot_id",
                    "sampling_stratum",
                    "available_post_count",
                    "displayed_post_count",
                    "distinct_source_dates",
                    "source_span_days",
                    "profile_material_available",
                    "coverage_status",
                ),
                coverage_rows,
            )
            for coder, rows in role_template_rows_by_coder.items():
                _write_csv(
                    role_dir / f"{coder.lower()}-role-coding.csv",
                    ROLE_CODING_FIELDS,
                    rows,
                )
            _write_csv(role_dir / "adjudication.csv", ADJUDICATION_FIELDS, ())

            text_items: list[dict[str, object]] = []
            text_segments: list[dict[str, object]] = []
            text_coding_by_coder: dict[str, list[dict[str, str]]] = {
                coder: [] for coder in coder_ids
            }
            for post_index, row in enumerate(text_posts, start=1):
                item_id = f"TXT-{post_index:03d}-{hashlib.sha256(str(row['platform_post_id']).encode()).hexdigest()[:8]}"
                post_id = item_id
                content_text = str(row.get("content_text") or "")
                text_items.append(
                    {
                        "item_id": item_id,
                        "source_web_post_id": row["platform_post_id"],
                        "platform_key": row["platform_key"],
                        "sampling_stratum": "AUTHOR_DISJOINT_RANDOM",
                        "content_hash": hashlib.sha256(content_text.encode()).hexdigest(),
                        "duplicate_cluster_id": "",
                        "assignment_group": "BOTH_CODERS_COMMON_CALIBRATION",
                        "codebook_version": CODEBOOK_VERSION,
                    }
                )
                linkage_rows.append(
                    {
                        "task_id": text_task_id,
                        "author_snapshot_id": "",
                        "platform_key": row["platform_key"],
                        "author_platform_id_raw": row["author_platform_id"],
                        "platform_post_id": row["platform_post_id"],
                        "text_item_id": item_id,
                        "linkage_purpose": "TEXT_AUTHOR_EXCLUSION_AUDIT",
                    }
                )
                segments = segment_text(content_text)
                for segment_index, segment in enumerate(segments, start=1):
                    seg_id = f"{item_id}-S{segment_index:03d}"
                    text_segments.append(
                        {
                            "task_id": text_task_id,
                            "item_id": item_id,
                            "post_id": post_id,
                            "seg_id": seg_id,
                            "platform": row["platform_key"],
                            "raw_text": segment,
                            "seg_length": len(segment),
                            "segmentation_rule_version": SEGMENTATION_RULE_VERSION,
                        }
                    )
                    for coder in coder_ids:
                        for spec in TEXT_FIELD_SPECS:
                            if spec.unit_type != "TEXT_SEGMENT":
                                continue
                            text_coding_by_coder[coder].append(
                                _coding_row(
                                    task_id=text_task_id,
                                    item_id=item_id,
                                    post_id=post_id,
                                    unit_type="TEXT_SEGMENT",
                                    unit_id=seg_id,
                                    platform=str(row["platform_key"]),
                                    raw_text=segment,
                                    spec=spec,
                                    annotator_id=coder,
                                )
                            )
                for coder in coder_ids:
                    for spec in TEXT_FIELD_SPECS:
                        if spec.unit_type != "POST":
                            continue
                        text_coding_by_coder[coder].append(
                            _coding_row(
                                task_id=text_task_id,
                                item_id=item_id,
                                post_id=post_id,
                                unit_type="POST",
                                unit_id=post_id,
                                platform=str(row["platform_key"]),
                                raw_text=content_text,
                                spec=spec,
                                annotator_id=coder,
                            )
                        )

            _write_csv(text_dir / "items.csv", TEXT_ITEM_FIELDS, text_items)
            _write_csv(text_dir / "segments.csv", TEXT_SEGMENT_FIELDS, text_segments)
            for coder, rows in text_coding_by_coder.items():
                _write_csv(
                    text_dir / f"{coder.lower()}-calibration-coding.csv",
                    CALIBRATION_CODING_FIELDS,
                    rows,
                )
            if content_workbook_template is not None:
                if not content_workbook_template.is_file():
                    raise RolePilotError(
                        f"内容Excel模板不存在：{content_workbook_template}"
                    )
                for coder in coder_ids:
                    shutil.copyfile(
                        content_workbook_template,
                        text_dir / f"{coder.lower()}-all-label-manual-coding.xlsx",
                    )

            _write_csv(
                temporary_root / "restricted-linkage.csv",
                RESTRICTED_LINKAGE_FIELDS,
                linkage_rows,
            )
            all_files = tuple(
                path
                for path in temporary_root.rglob("*")
                if path.is_file() and path.name != "manifest.json"
            )
            metadata = None
            if database_metadata is not None and database_metadata.is_file():
                metadata = json.loads(database_metadata.read_text(encoding="utf-8"))
            manifest_payload: dict[str, object] = {
                "round_id": output_dir.name,
                "round_status": ROUND_STATUS,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "advisor_confirmation": "PARALLEL_RECORD_NOT_GATE",
                "codebook_version": CODEBOOK_VERSION,
                "role_rule_version": ROLE_RULE_VERSION,
                "segmentation_rule_version": SEGMENTATION_RULE_VERSION,
                "database": {
                    "path": database.as_posix(),
                    "sha256": db_sha,
                    "metadata": metadata,
                    "opened_read_only": True,
                },
                "tasks": [
                    {
                        "task_id": role_task_id,
                        "dimension_scope": ["V0"],
                        "mode": "COMMON_CALIBRATION",
                        "member_count": len(role_authors),
                        "coder_ids": list(coder_ids),
                        "raw_outputs_are_distinct": True,
                        "community_relation_status": "UNAVAILABLE",
                        "sampling_audit": sampling_audit,
                    },
                    {
                        "task_id": text_task_id,
                        "dimension_scope": ["V1", "V2", "V3", "V4", "V5", "V6"],
                        "mode": "COMMON_CALIBRATION",
                        "member_count": len(text_posts),
                        "segment_count": len(text_segments),
                        "coder_ids": list(coder_ids),
                        "identity_columns_visible": False,
                        "visual_dimensions_started": False,
                    },
                ],
                "isolation_assertions": {
                    "role_and_text_author_sets_disjoint": not bool(
                        role_keys & text_author_keys
                    ),
                    "role_task_contains_t1_labels_or_engagement": False,
                    "text_task_contains_author_profile_followers_or_role": False,
                    "linkage_visible_to_coders": False,
                },
                "sampling": {
                    "seed": seed,
                    "max_role_posts": max_role_posts,
                    "role_platform_counts": dict(
                        sorted(Counter(item.platform for item in role_authors).items())
                    ),
                    "text_platform_counts": dict(
                        sorted(Counter(str(row["platform_key"]) for row in text_posts).items())
                    ),
                },
                "privacy": {
                    "git_commit_allowed": False,
                    "contains_restricted_author_material": True,
                    "mapping_release_gate": "BOTH_TASKS_LOCKED",
                    "pseudonym_key_sha256": hashlib.sha256(secret).hexdigest(),
                },
                "files": _file_manifest(temporary_root, all_files),
            }
            manifest_path = temporary_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_root, output_dir)

    return PilotPackageResult(
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        role_author_count=len(role_authors),
        text_post_count=len(text_posts),
        role_strata=dict(
            sorted(Counter(item.stratum for item in role_authors).items())
        ),
        platform_role_counts=dict(
            sorted(Counter(item.platform for item in role_authors).items())
        ),
        platform_text_counts=dict(
            sorted(Counter(str(row["platform_key"]) for row in text_posts).items())
        ),
    )
