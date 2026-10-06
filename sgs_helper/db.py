"""SQLite 构建：把 ``data/`` 下的 YAML 全量重建为 ``build/sgs.db``。

要求（与 README 的承诺一致）：

- 可以从零完整重建：每次构建都**删掉旧库重新建**，不存在“增量残留”。
- 重复执行不会产生重复数据。
- 记录构建元信息（schema 版本、Git commit、YAML 清单指纹、警告数），便于追溯。

模型精简后这里承担两件“构建期自动推导”的事：

- 卡牌是一层的 :class:`Card`：同一规则卡不因为花色 / 点数不同拆成多条，分类（category_1/2/3）与拓展包（card_set）落库，效果文本与关联技能拆成 ``effect_text`` / ``effect_skills``；
- FAQ / 技能文本等正文里的**关联实体**按名称自动识别，拼进 ``search_index.body``，
  不再要求人工维护 ``related_*``；
- 检索用的规范化正文（原 ``normalized_text``）只在这里生成，不写回 YAML。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    KIND_BY_NAME,
    KIND_ORDER,
    MODE_LABELS,
)
from .loader import DataBundle, Origin, manifest_digest
from .validate import ValidationResult, validate_data_dir

#: 生成物结构版本；改动表结构时递增，便于以后做迁移提示
SCHEMA_VERSION = "4"

#: 每种实体的标量/多值列（``id`` 恒为主键，不在此列出）
COLUMNS: dict[str, tuple[str, ...]] = {
    "product": ("name", "release_year", "mode", "general_sets", "card_sets"),
    "general": ("name",),
    "general_variant": (
        "general_id", "variant", "title", "general_number", "illustrator", "image",
        "mode", "faction", "hp", "gender", "product_id", "general_set", "zhulianbihe",
        "review_note",
    ),
    "skill": ("name", "review_note"),
    "skill_version": (
        "skill_id", "mode", "text", "version_label", "product_id", "previous_version_id",
        "review_note",
    ),
    "card": (
        "category_1", "category_2", "category_3", "name", "image", "card_set",
        "effect_text", "effect_skills", "review_note",
    ),
    "faq": ("question", "answer", "mode"),
    "keyword": ("name", "definition", "mode"),
    "timing": ("name", "description", "mode"),
}

#: 以 JSON 文本落库的多值列
JSON_COLUMNS: frozenset[str] = frozenset({"general_sets", "card_sets", "zhulianbihe"})

_INT_COLUMNS = frozenset({"release_year", "yaml_line"})
_REAL_COLUMNS = frozenset({"hp"})

#: 还会保存来源的实体（其余实体的来源由 product_id 定位）
SOURCE_KINDS: frozenset[str] = frozenset({"skill_version", "faq", "keyword", "timing"})

#: source_key 列（只有 SOURCE_KINDS 有）
SOURCE_TRACE_COLUMN = "source_key"

#: 每个实体表都会追加的可追溯性列
TRACE_COLUMNS: tuple[str, ...] = ("yaml_file", "yaml_line")

#: 用于“按名称自动识别关联”的实体类型
NAME_KINDS: tuple[str, ...] = ("general", "skill", "card", "keyword", "timing")

#: 索引：(名称, 定义)
INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_general_variants_general", "general_variants(general_id)"),
    ("idx_general_variants_mode", "general_variants(mode)"),
    ("idx_general_variants_product", "general_variants(product_id)"),
    ("idx_variant_skills_variant", "variant_skills(variant_id)"),
    ("idx_variant_skills_skill", "variant_skills(skill_id)"),
    ("idx_skill_versions_skill", "skill_versions(skill_id, mode)"),
    ("idx_skill_versions_prev", "skill_versions(previous_version_id)"),
    ("idx_cards_category", "cards(category_1, category_2)"),
    ("idx_cards_card_set", "cards(card_set)"),
    ("idx_faqs_mode", "faqs(mode)"),
    ("idx_keywords_mode", "keywords(mode)"),
    ("idx_timings_mode", "timings(mode)"),
    ("idx_search_index_kind", "search_index(kind)"),
)

DEFAULT_DB = Path("build/sgs.db")


class DataValidationError(RuntimeError):
    """数据校验未通过，拒绝建库。"""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("数据校验未通过，已终止建库")
        self.result = result


@dataclass
class BuildResult:
    db_path: Path
    counts: dict[str, int]
    source_count: int
    with_fts: bool
    warnings: list[Any] = field(default_factory=list)
    git: dict[str, str] = field(default_factory=dict)
    manifest: str = ""

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        lines = [
            f"已生成数据库：{self.db_path}",
            "实体统计：" + " / ".join(
                f"{KIND_BY_NAME[kind].label} {self.counts.get(kind, 0)}" for kind in KIND_ORDER
            ),
            f"总计 {self.total} 条实体，来源 {self.source_count} 条，"
            f"全文检索 FTS5：{'已启用' if self.with_fts else '不可用（回退 LIKE 检索）'}",
            f"YAML 清单指纹：{self.manifest[:16]}…  警告 {len(self.warnings)} 条",
        ]
        if self.git.get("commit"):
            lines.append(
                f"Git：{self.git.get('branch', '?')} {self.git['commit'][:10]}"
                f"{'（工作区有未提交改动）' if self.git.get('dirty') == '1' else ''}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------- 工具函数


def source_key(source: Any) -> str | None:
    """把精简后的 source 结构（type + reference）规范化为可去重的 key。"""
    if source is None:
        return None
    payload = "|".join(
        [str(getattr(source, "type", "")), str(getattr(source, "reference", ""))]
    )
    return "src_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def cjk_spaced(text: str) -> str:
    """把 CJK 字符逐个空格隔开，让 FTS5 的 unicode61 分词器也能按字匹配。

    FTS5 默认会把一整串汉字当成一个 token，导致“集智”搜不到“集智测试”；
    用短语查询 ``"集 智"`` 配合这种预分词即可解决。
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            out.append(f" {char} ")
        else:
            out.append(char)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def fts_query(text: str) -> str | None:
    """把用户输入转成 FTS5 查询：汉字按短语、英文按前缀。"""
    parts: list[str] = []
    for run in re.findall(r"[\u3400-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF]+", text):
        parts.append('"' + " ".join(run) + '"')
    for word in re.findall(r"[A-Za-z0-9_]+", text):
        parts.append(f"{word}*")
    return " AND ".join(parts) if parts else None


def _column_type(column: str) -> str:
    if column in _INT_COLUMNS:
        return "INTEGER"
    if column in _REAL_COLUMNS:
        return "REAL"
    return "TEXT"


def _column_value(entity: Any, column: str) -> Any:
    if column == "effect_text":
        return str(getattr(getattr(entity, "effect", None), "text", "") or "")
    if column == "effect_skills":
        skills = getattr(getattr(entity, "effect", None), "skills", None) or []
        return json.dumps(
            [{"skill_id": ref.skill_id, "version_id": ref.version_id} for ref in skills],
            ensure_ascii=False,
        )
    value = getattr(entity, column, None)
    if column in JSON_COLUMNS:
        return json.dumps(list(value or []), ensure_ascii=False)
    if isinstance(value, dt.date):
        return value.isoformat()
    if value is None:
        return None
    if isinstance(value, bool):  # pragma: no cover - 当前模型没有 bool 字段
        return int(value)
    return value


def _trace_columns(kind: str) -> tuple[str, ...]:
    if kind in SOURCE_KINDS:
        return (SOURCE_TRACE_COLUMN, *TRACE_COLUMNS)
    return TRACE_COLUMNS


# ---------------------------------------------------------------- 按名称自动识别


def build_name_index(bundle: DataBundle) -> list[tuple[str, str, str]]:
    """``(name, kind, id)`` 列表，名称长的在前，用于正文里的实体名识别。

    单字名字（例如「杀」）不参与，避免把整段规则文本刷成链接。
    """
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for kind in NAME_KINDS:
        for entity_id, entity in bundle.all(kind).items():
            name = str(getattr(entity, "name", "") or "").strip()
            if len(name) < 2 or name in seen:
                continue
            seen.add(name)
            rows.append((name, kind, entity_id))
    rows.sort(key=lambda row: (-len(row[0]), row[0]))
    return rows


def detect_mentions(text: str, index: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """在文本里按名称做最长优先匹配，返回 ``[(name, kind, id)]``，去重且名称长的在前。"""
    if not text:
        return []
    return [(name, kind, entity_id) for name, kind, entity_id in index if name in text]


# ---------------------------------------------------------------- 建表


def _create_schema(conn: sqlite3.Connection, with_fts: bool) -> bool:
    for kind in KIND_ORDER:
        spec = KIND_BY_NAME[kind]
        columns = ["id TEXT PRIMARY KEY"]
        for column in COLUMNS[kind]:
            columns.append(f"{column} {_column_type(column)}")
        for column in _trace_columns(kind):
            columns.append(f"{column} {_column_type(column)}")
        conn.execute(f"CREATE TABLE {spec.table} (\n  " + ",\n  ".join(columns) + "\n)")

    conn.execute(
        "CREATE TABLE variant_skills (\n"
        "  variant_id TEXT NOT NULL,\n"
        "  general_id TEXT NOT NULL,\n"
        "  skill_id TEXT NOT NULL,\n"
        "  version_id TEXT,\n"
        "  position INTEGER NOT NULL,\n"
        "  PRIMARY KEY (variant_id, skill_id)\n"
        ")"
    )
    conn.execute(
        "CREATE TABLE sources (\n"
        "  source_key TEXT PRIMARY KEY,\n"
        "  type TEXT, reference TEXT\n"
        ")"
    )
    conn.execute(
        "CREATE TABLE search_index (\n"
        "  kind TEXT NOT NULL,\n"
        "  entity_id TEXT NOT NULL,\n"
        "  name TEXT, body TEXT,\n"
        "  PRIMARY KEY (kind, entity_id)\n"
        ")"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    for name, definition in INDEXES:
        conn.execute(f"CREATE INDEX {name} ON {definition}")

    fts_ok = False
    if with_fts:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE fts_search USING fts5(\n"
                "  kind UNINDEXED, entity_id UNINDEXED, text, tokenize='unicode61'\n"
                ")"
            )
            fts_ok = True
        except sqlite3.OperationalError:
            fts_ok = False  # 环境没有 FTS5：查询层自动回退 LIKE
    return fts_ok


# ---------------------------------------------------------------- 检索文本


def _names_of(bundle: DataBundle, kind: str, ids: list[str]) -> str:
    names = []
    for ref in ids:
        entity = bundle.get(kind, ref)
        if entity is not None:
            names.append(str(getattr(entity, "name", ref)))
        else:
            names.append(ref)
    return " ".join(x for x in names if x)


def _search_row(
    bundle: DataBundle,
    kind: str,
    entity: Any,
    index: list[tuple[str, str, str]],
) -> tuple[str, str]:
    """返回 ``(name, body)``，供 SQLite LIKE 与 FTS5 检索使用。

    ``body`` 里包含“按名称自动识别出来的关联实体名”，这正是原来 ``related_*``
    手工字段想解决的问题。
    """
    name = str(getattr(entity, "name", "") or entity.id)
    parts: list[str] = []

    def mention_names(text: str) -> list[str]:
        return [hit[0] for hit in detect_mentions(text, index)]

    if kind == "product":
        parts += list(entity.general_sets)
        parts += list(entity.card_sets)
        parts.append(MODE_LABELS.get(entity.mode, entity.mode or ""))
        parts.append("武将牌 " + " ".join(entity.general_sets))
        parts.append("游戏牌 " + " ".join(entity.card_sets))
    elif kind == "general":
        pass
    elif kind == "general_variant":
        general = bundle.get("general", entity.general_id)
        general_name = str(getattr(general, "name", entity.general_id))
        name = f"{general_name}（{entity.variant}）"
        parts += [
            entity.variant, entity.title, entity.faction, entity.general_set or "",
            general_name, "、".join(entity.zhulianbihe or []), entity.review_note,
        ]
        parts.append(MODE_LABELS.get(entity.mode, entity.mode or ""))
        if entity.general_number:
            parts.append(entity.general_number)
        parts.append(_names_of(bundle, "skill", [ref.skill_id for ref in entity.skills]))
    elif kind == "skill":
        parts.append(entity.review_note)
        for version in bundle.all("skill_version").values():
            if version.skill_id == entity.id:
                parts += [
                    version.text, version.version_label,
                    MODE_LABELS.get(version.mode, version.mode),
                ]
    elif kind == "skill_version":
        skill = bundle.get("skill", entity.skill_id)
        skill_name = str(getattr(skill, "name", entity.skill_id))
        name = f"{skill_name} {entity.version_label}".strip()
        parts += [
            entity.text, entity.version_label, entity.review_note,
            MODE_LABELS.get(entity.mode, entity.mode), entity.skill_id,
        ]
        parts += mention_names(entity.text)
    elif kind == "card":
        effect = getattr(entity, "effect", None)
        text = str(getattr(effect, "text", "") or "")
        parts += [
            entity.category_1, entity.category_2, entity.category_3,
            entity.name, entity.card_set or "", entity.review_note, text,
        ]
        parts.append(
            _names_of(bundle, "skill", [ref.skill_id for ref in (effect.skills if effect else [])])
        )
        parts += mention_names(text)
    elif kind == "faq":
        name = entity.question if len(entity.question) <= 60 else entity.question[:60] + "…"
        parts += [entity.question, entity.answer, MODE_LABELS.get(entity.mode, entity.mode)]
        parts += mention_names(f"{entity.question}\n{entity.answer}")
    elif kind == "keyword":
        parts += [entity.definition, MODE_LABELS.get(entity.mode, entity.mode)]
        parts += mention_names(entity.definition)
    elif kind == "timing":
        parts += [entity.description, MODE_LABELS.get(entity.mode, entity.mode)]
        parts += mention_names(entity.description)

    return name, " ".join(str(p) for p in parts if p)


# ---------------------------------------------------------------- 写入


def _insert_entities(conn: sqlite3.Connection, bundle: DataBundle) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in KIND_ORDER:
        spec = KIND_BY_NAME[kind]
        entity_map = bundle.all(kind)
        counts[kind] = len(entity_map)
        trace = _trace_columns(kind)
        columns = ["id", *COLUMNS[kind], *trace]
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {spec.table} ({', '.join(columns)}) VALUES ({placeholders})"
        rows = []
        for entity_id, entity in sorted(entity_map.items()):
            origin = bundle.origin_of(kind, entity_id) or Origin("?", 0, "")
            values = [entity.id]
            values += [_column_value(entity, column) for column in COLUMNS[kind]]
            if SOURCE_TRACE_COLUMN in trace:
                values.append(source_key(getattr(entity, "source", None)))
            values += [f"data/{origin.path}", origin.line]
            rows.append(values)
        conn.executemany(sql, rows)
    return counts


def _insert_sources(conn: sqlite3.Connection, bundle: DataBundle) -> int:
    rows: dict[str, tuple] = {}
    for kind in KIND_ORDER:
        for entity in bundle.all(kind).values():
            source = getattr(entity, "source", None)
            key = source_key(source)
            if key is None or key in rows:
                continue
            rows[key] = (
                key,
                str(getattr(source, "type", "")),
                str(getattr(source, "reference", "")),
            )
    conn.executemany(
        "INSERT INTO sources (source_key, type, reference) VALUES (?, ?, ?)",
        list(rows.values()),
    )
    return len(rows)


def _insert_relations(conn: sqlite3.Connection, bundle: DataBundle) -> None:
    rows = []
    for variant_id, variant in sorted(bundle.all("general_variant").items()):
        for position, ref in enumerate(variant.skills):
            rows.append(
                (variant_id, variant.general_id, ref.skill_id, ref.version_id, position)
            )
    conn.executemany(
        "INSERT OR IGNORE INTO variant_skills "
        "(variant_id, general_id, skill_id, version_id, position) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _insert_search_index(conn: sqlite3.Connection, bundle: DataBundle) -> None:
    index = build_name_index(bundle)
    rows = []
    fts_rows = []
    for kind in KIND_ORDER:
        for entity_id, entity in sorted(bundle.all(kind).items()):
            name, body = _search_row(bundle, kind, entity, index)
            rows.append((kind, entity_id, name, body))
            fts_rows.append((kind, entity_id, cjk_spaced(f"{name} {body}")))
    conn.executemany(
        "INSERT INTO search_index (kind, entity_id, name, body) VALUES (?, ?, ?, ?)",
        rows,
    )
    try:
        conn.executemany(
            "INSERT INTO fts_search (kind, entity_id, text) VALUES (?, ?, ?)", fts_rows
        )
    except sqlite3.OperationalError:
        pass  # 没有 FTS5 时忽略


def _git_info(repo_dir: Path) -> dict[str, str]:
    info: dict[str, str] = {}

    def run(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(repo_dir), *args],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return None

    head = run("rev-parse", "HEAD")
    if head is not None and head.returncode == 0:
        info["commit"] = head.stdout.strip()
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    if branch is not None and branch.returncode == 0:
        info["branch"] = branch.stdout.strip()
    status = run("status", "--porcelain")
    if status is not None and status.returncode == 0:
        info["dirty"] = "1" if status.stdout.strip() else "0"
    return info


def _write_meta(
    conn: sqlite3.Connection,
    bundle: DataBundle,
    data_dir: Path,
    manifest: str,
    fts_ok: bool,
    result: ValidationResult,
    counts: dict[str, int],
    source_count: int,
) -> None:
    git = _git_info(data_dir.parent)
    meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "yaml_manifest_sha256": manifest,
        "fts5": "1" if fts_ok else "0",
        "source_count": str(source_count),
        "entity_total": str(sum(counts.values())),
        "validation_errors": str(len(result.errors)),
        "validation_warnings": str(len(result.warnings)),
        "git_commit": git.get("commit", ""),
        "git_branch": git.get("branch", ""),
        "git_dirty": git.get("dirty", ""),
    }
    for kind, count in counts.items():
        meta[f"count.{kind}"] = str(count)
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [(key, str(value)) for key, value in meta.items()],
    )


# ---------------------------------------------------------------- 入口


def build_database(
    data_dir: Path,
    db_path: Path = DEFAULT_DB,
    *,
    with_fts: bool = True,
    allow_invalid: bool = False,
    result: ValidationResult | None = None,
) -> BuildResult:
    """从 YAML 全量重建 SQLite。默认先校验，校验失败直接终止。"""
    data_dir = Path(data_dir)
    db_path = Path(db_path)

    if result is None:
        result = validate_data_dir(data_dir)
    if not result.ok and not allow_invalid:
        raise DataValidationError(result)

    bundle = result.bundle
    manifest = manifest_digest(data_dir) if data_dir.is_dir() else ""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        fts_ok = _create_schema(conn, with_fts)
        counts = _insert_entities(conn, bundle)
        source_count = _insert_sources(conn, bundle)
        _insert_relations(conn, bundle)
        _insert_search_index(conn, bundle)
        _write_meta(conn, bundle, data_dir, manifest, fts_ok, result, counts, source_count)
        conn.commit()
    finally:
        conn.close()

    return BuildResult(
        db_path=db_path,
        counts=counts,
        source_count=source_count,
        with_fts=fts_ok,
        warnings=list(result.warnings),
        git=_git_info(data_dir.parent),
        manifest=manifest,
    )
