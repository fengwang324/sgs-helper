"""管理端的数据访问层：**直接面向 YAML 权威源**，SQLite 只在保存成功后重建。

为什么读也走 YAML 而不是 SQLite：

- YAML 是唯一权威源，永远是最新的；SQLite 可能落后（比如刚手工改过 YAML 没重建）；
- 管理端“看到的”和“编辑的”必须是同一份数据，否则会出现看着旧值改新值的事故。

写入路径严格固定为：

    浏览器表单 → 候选实体（内存） → 预校验（模型 + 交叉引用 + 一致性）
      ├─ 失败：一个字都不落盘，返回 文件/字段/原因
      └─ 通过：写 YAML → build_database() 全量重建 SQLite
                 └─ 若重建意外失败：用备份内容回滚 YAML，磁盘不残留半成品

搜索复用 ``db._search_row``（正文里已经包含自动识别出的关联实体名），
匹配排序与 ``repo.search`` 保持一致，不另写一套规则。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..constants import (
    KIND_BY_NAME,
    KIND_ORDER,
    MODE_LABELS,
    SOURCE_LABELS,
    mode_sort_key,
)
from ..db import (
    BuildResult,
    DataValidationError,
    _search_row,
    build_database,
    build_name_index,
    detect_mentions,
)
from ..loader import (
    DataBundle,
    Issue,
    Origin,
    RawEntity,
    load_data_dir,
    manifest_digest,
)
from ..schema import MODEL_BY_KIND
from ..validate import (
    REFERENCE_FIELDS,
    ValidationResult,
    iter_reference_values,
    validate_data_dir,
    validate_entities,
)
from . import yaml_writer
from .yaml_writer import BlockStr  # noqa: F401  （供模板/其它模块引用）

#: 正文里自动识别的实体类型（与 repo.mentions 的默认值一致）
MENTION_KINDS: tuple[str, ...] = ("general", "skill", "card", "keyword", "timing")


# ---------------------------------------------------------------- 展示辅助


def source_dict(entity: Any) -> dict | None:
    """展示用的 source：新模型只有 ``type`` + ``reference``。"""
    source = getattr(entity, "source", None)
    if source is None:
        return None
    source_type = getattr(source, "type", "")
    return {
        "type": source_type,
        "type_label": SOURCE_LABELS.get(str(source_type), str(source_type)),
        "reference": getattr(source, "reference", "") or "",
    }


def truncate(text: str, limit: int = 60) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _score_query(query: str, name: str, body: str) -> int | None:
    """与 ``repo.search`` 同一套排序规则（别名概念已彻底移除）：

    精确名称 0 > 名称前缀 2 > 名称包含 3 > 正文包含 5。
    """
    name = name or ""
    body = body or ""
    if name == query:
        return 0
    if name.startswith(query):
        return 2
    if query in name:
        return 3
    if query in body:
        return 5
    return None


def _search_row_pair(bundle: DataBundle, kind: str, entity: Any, name_index) -> tuple[str, str]:
    """调用 ``db._search_row`` 并归一化成 ``(name, body)``。"""
    try:
        row = _search_row(bundle, kind, entity, name_index)
    except TypeError:  # pragma: no cover - 兼容旧的 3 元组签名
        row = _search_row(bundle, kind, entity)  # type: ignore[call-arg]
    if len(row) >= 3:
        return str(row[0] or ""), str(row[-1] or "")
    return str(row[0] or ""), str(row[1] or "")


def _detect_mentions(
    text: str, index: list[tuple[str, str, str]], kinds: tuple[str, ...], limit: int
) -> list[tuple[str, str, str]]:
    """只保留指定 kind 的自动识别结果（``db.detect_mentions`` 已按名称长度降序）。"""
    allowed = set(kinds)
    return [row for row in detect_mentions(text, index) if row[1] in allowed][:limit]


def _history_chains(rows: list[dict]) -> list[list[str]]:
    """按 ``previous_id`` 串出历史版本链（返回 id 列表，只保留真正成链的）。"""
    by_id = {row["id"]: row for row in rows}
    referenced = {row["previous_id"] for row in rows if row.get("previous_id")}
    chains: list[list[str]] = []
    for start in sorted(by_id.values(), key=lambda row: row["id"]):
        if start["id"] in referenced:
            continue
        chain: list[str] = []
        cursor: str | None = start["id"]
        while cursor and cursor in by_id and cursor not in chain:
            chain.append(cursor)
            cursor = by_id[cursor].get("previous_id")
        if len(chain) > 1:
            chains.append(chain)
    return chains


@dataclass
class Change:
    """批量保存里的一项改动。

    ``old_id`` 为 ``None`` 表示**新增**；否则表示这一项是在改（或改名）``old_id`` 那条记录。
    ``expected_sha`` 是该记录的内容哈希（并发保护），``None`` 表示不做检查（仅程序化调用用）。
    """

    kind: str
    candidate: dict
    old_id: str | None = None
    expected_sha: str | None = None
    #: 新建记录时，用户刚上传的卡图文件名（名字随意，保存后要改成编号派生名）。
    #: 不放进 ``candidate``——模型是 ``extra="forbid"``，多余键会被校验拦下。
    uploaded_image: str | None = None

    @property
    def entity_id(self) -> str:
        return str(self.candidate.get("id") or "")


@dataclass
class SaveOutcome:
    """一次写操作的完整结果，供页面直接渲染。"""

    ok: bool
    kind: str
    entity_id: str = ""
    issues: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    path: str = ""
    build: BuildResult | None = None
    error: str = ""
    blocked: bool = False
    conflict: bool = False
    references: list[dict] = field(default_factory=list)
    rolled_back: bool = False
    #: 本次真正写入/删除的 YAML 文件
    written: list[str] = field(default_factory=list)
    #: 本次跟着改名的卡图文件（编号变了 → 卡图名跟着派生）
    moved_images: list[str] = field(default_factory=list)
    #: 校验错误按实体分组：{"skill_xxx": [Issue, ...]}，页面按技能/字段定位问题
    issues_by_entity: dict[str, list[Issue]] = field(default_factory=dict)
    #: 非错误的提示（例如“复用了内容完全相同的已有技能版本”）
    info: list[str] = field(default_factory=list)

    def format_issues(self) -> str:
        return "\n".join(issue.format() for issue in self.issues)

    def group_issues(self) -> dict[str, list[Issue]]:
        grouped: dict[str, list[Issue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.entity_id or "", []).append(issue)
        return grouped


# ---------------------------------------------------------------- 事务备份


class FileBackup:
    """写盘前的文件内容备份；失败时按字面回滚（新建的文件则删除）。

    **安全点**：回滚只在“文件内容仍然等于我们刚写进去的内容”时执行。
    如果写盘到回滚之间有人（另一个进程 / 你手工编辑）改了同一个文件，就跳过回滚并记录下来，
    绝不把别人的修改抹掉。
    """

    def __init__(self) -> None:
        self._saved: dict[Path, str | None] = {}
        self._written: dict[Path, str | None] = {}
        self.skipped: list[Path] = []

    def stash(self, path: Path) -> None:
        path = Path(path)
        if path in self._saved:
            return
        self._saved[path] = path.read_text(encoding="utf-8") if path.exists() else None

    def note_written(self, path: Path) -> None:
        """记录“我们写进去的内容”，回滚时用来判断文件是否被外部改动过。"""
        path = Path(path)
        try:
            self._written[path] = path.read_text(encoding="utf-8") if path.exists() else None
        except OSError:  # pragma: no cover
            self._written[path] = None

    def rollback(self) -> list[Path]:
        """回滚；返回因为“已被外部改动”而跳过的文件列表。"""
        self.skipped = []
        for path, text in self._saved.items():
            try:
                current = path.read_text(encoding="utf-8") if path.exists() else None
            except OSError:  # pragma: no cover
                current = None
            if path in self._written and current != self._written[path]:
                self.skipped.append(path)
                continue
            try:
                if text is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_text(text, encoding="utf-8")
            except OSError:  # pragma: no cover - 回滚失败只能记日志
                self.skipped.append(path)
        return self.skipped


# ---------------------------------------------------------------- Store


#: 国战编号前缀里的「类别」标记（不是势力，但希望能在同一个筛选里选到）
NUMBER_TAGS = {"AM": "野心家", "EM": "君主", "EMA": "君主"}


def _number_tag(number: str) -> str:
    n = (number or "").upper().replace(" ", "")
    n = n[2:] if n.startswith("G.") else n
    code = re.sub(r"\d+.*$", "", n).strip(".&/")
    return NUMBER_TAGS.get(code, "")


def _number_sort_key(number: str) -> str:
    """把 G.WEI 9 / G.WEI 010 / GJIN016 这类编号规范成可排序的字符串。

    去掉开头的 G./G-（国战前缀），数字左侧补零，于是「势力 + 编号」自然有序。
    """
    n = (number or "").upper().replace(" ", "")
    if not n:
        return "zzzz"
    n = re.sub(r"^G[.\-]?", "", n)
    n = re.sub(r"([A-Z])", r" \1", n).strip()
    return re.sub(r"\d+", lambda m: m.group(0).zfill(3), n)


def _faction_tokens(value: str) -> list[str]:
    """势力字段可能是「魏/蜀」这种双势力，按分隔符拆成 token。"""
    return [t for t in re.split(r"[/、,，\s]+", value or "") if t]


class AdminStore:
    """本地资料管理的数据访问对象。线程安全（单进程、少量并发足够）。"""

    #: 文件指纹的缓存时间（秒）。一次页面请求通常远小于它，
    #: 所以整页只 stat 一遍目录；同时又能在提交后的下一次请求看到外部改动。
    FINGERPRINT_TTL = 0.5

    def __init__(self, data_dir: Path, db_path: Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path)
        self.repo_dir = self.data_dir.parent
        self._lock = threading.RLock()
        self._snapshot: ValidationResult | None = None
        self._fingerprint: str = ""
        self._fingerprint_at: float = 0.0
        self._index: dict[str, dict[str, tuple[str, str]]] = {}
        self._digest: str = ""
        self._digest_fingerprint: str = ""
        #: Wiki：``(kind, id) -> [相关 FAQ]`` 反向索引，按文件指纹缓存
        self._faq_index: dict[tuple[str, str], list[dict]] | None = None
        self._faq_index_fingerprint: str = ""

    # ------------------------------------------------------------ 快照

    def _fingerprint_now(self) -> str:
        parts: list[str] = []
        for path, rel, _kind in _iter_yaml(data_dir=self.data_dir):
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover
                continue
            parts.append(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}")
        return "|".join(sorted(parts))

    def snapshot(self, *, force: bool = False) -> ValidationResult:
        """返回当前 YAML 的校验结果（含 bundle）。

        页面渲染时会**大量**访问 ``self.bundle``：如果每次访问都去 stat 一遍
        全部 YAML 文件，几百个文件的数据量下整页会慢到不可用。所以这里对
        “文件指纹”做了一个很短的缓存：一次请求只算一遍，跨请求最多每
        ``FINGERPRINT_TTL`` 秒重算一次。

        .. important::
           命中缓存的判断**只看 ``_fingerprint_at``**，不能在判断前就去算指纹。
           算一次指纹要 stat 全部 YAML 文件，数据量上来后比 TTL 还慢；
           先算后判会让缓存永远命中不了 —— 于是每个 ``self.bundle`` 都退化成
           一次全目录 stat（实测 81 张卡片的列表页会慢到 100 秒以上）。
           这里必须保持“先看时间、再决定是否 stat”的顺序。
        """
        with self._lock:
            if force or self._snapshot is None:
                fingerprint = self._fingerprint_now()
                self._snapshot = validate_data_dir(self.data_dir)
                self._fingerprint = fingerprint
                self._fingerprint_at = time.monotonic()
                self._index = {}
                self._faq_index = None
                return self._snapshot

            now = time.monotonic()
            if (now - self._fingerprint_at) <= self.FINGERPRINT_TTL:
                return self._snapshot  # type: ignore[return-value]

            # 超过 TTL 才真正 stat 目录，用来发现外部（手工改 YAML）改动
            fingerprint = self._fingerprint_now()
            self._fingerprint_at = now
            if fingerprint != self._fingerprint:
                self._snapshot = validate_data_dir(self.data_dir)
                self._fingerprint = fingerprint
                self._index = {}
                self._faq_index = None
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None
            self._fingerprint = ""
            self._fingerprint_at = 0.0
            self._index = {}
            self._digest = ""
            self._digest_fingerprint = ""
            self._faq_index = None
            self._faq_index_fingerprint = ""

    @property
    def bundle(self) -> DataBundle:
        return self.snapshot().bundle

    # ------------------------------------------------------------ 基础查询

    def entity(self, kind: str, entity_id: str | None) -> Any | None:
        if not entity_id:
            return None
        return self.bundle.get(kind, entity_id)

    def origin(self, kind: str, entity_id: str) -> Origin | None:
        return self.bundle.origin_of(kind, entity_id)

    def file_path(self, kind: str, entity_id: str) -> Path | None:
        origin = self.origin(kind, entity_id)
        if origin is None:
            return None
        return self.data_dir / origin.path

    def display_name(self, kind: str, entity: Any) -> str:
        if entity is None:
            return ""
        if kind == "skill_version":
            skill = self.bundle.get("skill", getattr(entity, "skill_id", ""))
            base = getattr(skill, "name", "") or getattr(entity, "skill_id", "")
            label = getattr(entity, "version_label", "") or ""
            return f"{base} {label}".strip()
        if kind == "faq":
            return truncate(getattr(entity, "question", ""), 50)
        if kind == "general_variant":
            general = self.bundle.get("general", getattr(entity, "general_id", ""))
            base = getattr(general, "name", "") or getattr(entity, "general_id", "")
            return f"{base}（{getattr(entity, 'variant', '')}）"
        return getattr(entity, "name", "") or entity.id

    def name_of(self, kind: str, entity_id: str | None) -> str:
        if not entity_id:
            return ""
        entity = self.bundle.get(kind, entity_id)
        if entity is None:
            return f"{entity_id}（缺失）"
        return self.display_name(kind, entity)

    def subtitle(self, kind: str, entity: Any) -> str:
        bundle = self.bundle
        if kind == "product":
            bits = [
                str(getattr(entity, "release_year", "") or ""),
                f"武将牌：{'、'.join(entity.general_sets)}" if entity.general_sets else "",
                f"游戏牌：{'、'.join(entity.card_sets)}" if entity.card_sets else "",
                MODE_LABELS.get(getattr(entity, "mode", "") or "", getattr(entity, "mode", "") or ""),
            ]
            return " · ".join(b for b in bits if b)
        if kind == "general":
            variants = [
                v for v in bundle.all("general_variant").values()
                if v.general_id == entity.id
            ]
            bits = []
            bits.append(f"{len(variants)} 个版本")
            if variants:
                bits.append("、".join(sorted(v.variant for v in variants)))
            return " · ".join(bits)
        if kind == "general_variant":
            bits = [
                getattr(entity, "faction", "") or "",
                f"{entity.hp} 体力" if getattr(entity, "hp", None) is not None else "",
                getattr(entity, "title", "") or "",
                self.set_display(getattr(entity, "general_set", None)),
                ("珠联璧合：" + "、".join(entity.zhulianbihe))
                if getattr(entity, "zhulianbihe", None) else "",
            ]
            skills = [
                self.display_name("skill", bundle.get("skill", ref.skill_id))
                for ref in getattr(entity, "skills", [])
            ]
            text = " · ".join(b for b in bits if b)
            if skills:
                text += "　技能：" + "、".join(skills)
            return text
        if kind == "skill":
            count = sum(
                1 for v in bundle.all("skill_version").values() if v.skill_id == entity.id
            )
            return f"{count} 个 SkillVersion"
        if kind == "skill_version":
            mode = MODE_LABELS.get(getattr(entity, "mode", ""), getattr(entity, "mode", ""))
            product = self.name_of("product", getattr(entity, "product_id", None))
            label = self._display_label(entity, product, mode)
            if not label and product:
                label = product          # 标签与产品名重复时，用产品名占这个位置
            return f"{mode} · {label}".strip(" ·")
        if kind == "card":
            effect = getattr(entity, "effect", None)
            text = str(getattr(effect, "text", "") or "")
            category = "/".join(
                b
                for b in (
                    getattr(entity, "category_1", "") or "",
                    getattr(entity, "category_2", "") or "",
                    getattr(entity, "category_3", "") or "",
                )
                if b
            )
            bits = [category]
            if getattr(entity, "card_set", None):
                bits.append(f"拓展包 {entity.card_set}")
            skills = [
                self.display_name("skill", bundle.get("skill", ref.skill_id))
                for ref in (getattr(effect, "skills", None) or [])
            ]
            summary = " · ".join(b for b in bits if b)
            if text:
                summary += f"　{truncate(text, 40)}"
            elif skills:
                summary += "　技能：" + "、".join(skills)
            return summary
        if kind == "faq":
            return truncate(getattr(entity, "answer", ""), 60)
        if kind == "keyword":
            return truncate(getattr(entity, "definition", ""), 60)
        if kind == "timing":
            return truncate(getattr(entity, "description", ""), 60)
        return ""

    def variants_of(self, general_id: str) -> list[Any]:
        """某个武将个体的全部版本（按版本标签排序）。"""
        return sorted(
            (
                v
                for v in self.bundle.all("general_variant").values()
                if v.general_id == general_id
            ),
            key=lambda v: (v.variant, v.id),
        )

    def variants_using_version(self, version_id: str) -> list[dict]:
        """显式绑定了某个 SkillVersion 的武将版本（详情页 / 删除影响都用它）。"""
        out: list[dict] = []
        for variant in self.bundle.all("general_variant").values():
            if any(ref.version_id == version_id for ref in (variant.skills or [])):
                out.append(
                    {
                        "id": variant.id,
                        "general_id": variant.general_id,
                        "name": self.display_name("general_variant", variant),
                    }
                )
        return out

    def set_display(self, name: str | None) -> str:
        """武将牌系列的展示文本：系列层级（GeneralSet 实体）已删除，名字本身就是全部信息。"""
        return name or ""

    def mode_of(self, kind: str, entity: Any) -> str | None:
        """实体的模式。

        ``Card`` 是单层的，没有 ``mode``：卡牌不绑定具体产品，归属由 ``card_set`` 表达。
        """
        return getattr(entity, "mode", None)

    def describe(self, kind: str, entity: Any) -> dict:
        """列表/搜索结果的一行。"""
        origin = self.origin(kind, entity.id)
        mode = self.mode_of(kind, entity)
        return {
            "kind": kind,
            "kind_label": KIND_BY_NAME[kind].label,
            "id": entity.id,
            "name": self.display_name(kind, entity),
            "subtitle": self.subtitle(kind, entity),
            "title": getattr(entity, "title", "") or "",
            "general_number": getattr(entity, "general_number", None),
            "general_id": getattr(entity, "general_id", None),
            "general_set": getattr(entity, "general_set", None),
            "general_sets": list(getattr(entity, "general_sets", []) or []),
            "card_sets": list(getattr(entity, "card_sets", []) or []),
            "category_1": getattr(entity, "category_1", "") or "",
            "category_2": getattr(entity, "category_2", "") or "",
            "category_3": getattr(entity, "category_3", "") or "",
            "card_set": getattr(entity, "card_set", None),
            "image": getattr(entity, "image", "") or "",
            "effect_text": str(getattr(getattr(entity, "effect", None), "text", "") or ""),
            "mode": mode,
            "mode_label": MODE_LABELS.get(mode, mode) if mode else "",
            "source": source_dict(entity),
            "file": f"data/{origin.path}" if origin else "",
            "line": origin.line if origin else 0,
            "entity": entity,
        }

    def entries(self, kind: str) -> list[tuple[str, Any]]:
        return sorted(self.bundle.all(kind).items(), key=lambda kv: kv[0])

    def counts(self) -> dict[str, int]:
        bundle = self.bundle
        return {kind: bundle.count(kind) for kind in KIND_ORDER}

    def flash(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "errors": len(snap.errors),
            "warnings": len(snap.warnings),
            "total": snap.bundle.total,
        }

    # ------------------------------------------------------------ 自动关联

    def mentions(
        self,
        text: str,
        kinds: tuple[str, ...] = MENTION_KINDS,
        limit: int = 12,
    ) -> list[dict]:
        """按实体名称在正文里自动识别关联实体，返回 ``[{"kind","id","name"}]``。

        这是 ``repo.mentions(conn, text, kinds=..., limit=...)`` 的薄封装：
        默认借用 SQLite 生成物（与 CLI / 小程序同一套识别逻辑）。数据库还没建好、
        或者 repo.py 还处在半迁移状态时，退回内存里的 YAML 快照，页面照常可用。
        """
        body = text or ""
        if not body.strip():
            return []
        hits = self._mentions_from_repo(body, kinds, limit)
        if hits is None:
            hits = self._mentions_from_bundle(body, kinds, limit)
        return hits[:limit]

    def _mentions_from_repo(
        self, text: str, kinds: tuple[str, ...], limit: int
    ) -> list[dict] | None:
        if not self.db_path.exists():
            return None
        try:
            from .. import repo
        except Exception:  # pragma: no cover - repo 导入失败时退回内存
            return None
        mentions_fn = getattr(repo, "mentions", None)
        index_fn = getattr(repo, "name_index", None)
        if mentions_fn is None and index_fn is None:
            return None
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.Error:  # pragma: no cover
            return None
        try:
            conn.row_factory = sqlite3.Row
            if mentions_fn is not None:
                found = mentions_fn(conn, text, kinds=tuple(kinds), limit=limit) or []
                return [
                    {
                        "kind": str(hit["kind"] if isinstance(hit, dict) else hit[0]),
                        "id": str(hit["id"] if isinstance(hit, dict) else hit[1]),
                        "name": str(hit["name"] if isinstance(hit, dict) else hit[2]),
                    }
                    for hit in found
                ]
            # 只有 name_index：自己按“最长名称优先”匹配
            index = index_fn(conn) or {}
            rows = sorted(
                ((str(name), str(value[0]), str(value[1])) for name, value in index.items()),
                key=lambda row: (-len(row[0]), row[0]),
            )
            return [
                {"kind": kind, "id": entity_id, "name": name}
                for name, kind, entity_id in _detect_mentions(text, rows, kinds, limit)
            ]
        except Exception:  # pragma: no cover - 生成物与 YAML 不同步时不阻塞页面
            return None
        finally:
            conn.close()

    def _mentions_from_bundle(
        self, text: str, kinds: tuple[str, ...], limit: int
    ) -> list[dict]:
        index = build_name_index(self.bundle)
        return [
            {"kind": kind, "id": entity_id, "name": name}
            for name, kind, entity_id in _detect_mentions(text, index, kinds, limit)
        ]

    def annotate(self, text: str, mentions: list[dict] | None = None) -> list[dict]:
        """把正文切成片段：普通文本 ``{"text": ...}`` 或可点击实体 ``{"kind","id","label"}``。

        模板按顺序渲染即可：既不需要 ``|safe``，也不会有 HTML 注入。
        """
        body = text or ""
        if not body:
            return []
        hits = mentions if mentions is not None else self.mentions(body)
        by_name: dict[str, dict] = {}
        for hit in hits or []:
            name = str(hit.get("name") or "")
            if len(name) >= 2 and name not in by_name:
                by_name[name] = hit

        matches: list[tuple[int, int, dict]] = []
        for name, hit in by_name.items():
            start = body.find(name)
            while start != -1:
                matches.append((start, start + len(name), hit))
                start = body.find(name, start + len(name))
        matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

        segments: list[dict] = []
        cursor = 0
        for start, end, hit in matches:
            if start < cursor:
                continue  # 与更长的匹配重叠，跳过
            if start > cursor:
                segments.append({"text": body[cursor:start]})
            segments.append(
                {"kind": hit.get("kind"), "id": hit.get("id"), "label": body[start:end]}
            )
            cursor = end
        if cursor < len(body):
            segments.append({"text": body[cursor:]})
        return segments

    def product_general_sets(self, product_id: str | None) -> list[str]:
        """某个产品声明的武将牌系列（武将编辑页 general_set 的候选来源）。"""
        product = self.entity("product", product_id)
        if product is None:
            return []
        return [str(value) for value in (product.general_sets or []) if str(value).strip()]

    # ------------------------------------------------------------ Wiki

    def wiki_nav_tree(self, category_1: str, *, counts: bool = True) -> list[dict]:
        """某个 ``category_1`` 下的二级 / 三级分类树（Wiki 导航与筛选用）。

        树 = ``CARD_CATEGORY_TREE`` 里登记的静态子树 ∪ **数据里实际出现过的值**。
        后者让「在录入页直接输入一个新分类名」立刻出现在 Wiki 导航里
        （不需要预先登记，也不需要新建实体）。

        ``counts`` 为真时统计每个节点的卡牌数（指示物当前 0 条是正常状态）。
        """
        from ..constants import CARD_CATEGORY_TREE

        static = CARD_CATEGORY_TREE.get(category_1, {})
        # 二级 -> 三级，保序：先静态树的顺序，再补数据里新出现的
        levels: dict[str, list[str]] = {name: list(children) for name, children in static.items()}
        tally: dict[tuple[str, str], int] = {}

        for card in self.bundle.all("card").values():
            if (getattr(card, "category_1", "") or "") != category_1:
                continue
            c2 = (getattr(card, "category_2", "") or "").strip()
            c3 = (getattr(card, "category_3", "") or "").strip()
            if c2 and c2 not in levels:
                levels[c2] = []
            if c2 and c3 and c3 not in levels.setdefault(c2, []):
                levels[c2].append(c3)
            if c2:
                tally[(c2, c3)] = tally.get((c2, c3), 0) + 1
                tally[(c2, "")] = tally.get((c2, ""), 0) + 1

        tree: list[dict] = []
        for c2, children in levels.items():
            third = [
                {"value": c3, "count": tally.get((c2, c3), 0) if counts else 0}
                for c3 in children
            ]
            tree.append(
                {
                    "value": c2,
                    "count": tally.get((c2, ""), 0) if counts else 0,
                    "children": third,
                }
            )
        return tree

    def _related_faq_index(self) -> dict[tuple[str, str], list[dict]]:
        """``(kind, entity_id) -> [相关 FAQ]`` 的反向索引（按快照缓存）。

        关联本身沿用既有的「按实体名称在正文里自动识别」机制，这里额外做两件事：

        1. **反向**：``repo.py`` 只有「给文本找实体」，没有「给实体找 FAQ」；
        2. **定相关度**：规则类 FAQ 动辄数千字、罗列上百个技能名，
           只按「命中就算相关」会让几乎所有技能都挂上同一批规则专题。
           所以按命中位置打分，只保留真正相关的（详见 :meth:`_faq_relevance`）。
        """
        cache_key = self._fingerprint
        if self._faq_index is not None and self._faq_index_fingerprint == cache_key:
            return self._faq_index

        index: dict[tuple[str, str], list[dict]] = {}
        for faq_id, faq in self.bundle.all("faq").items():
            question = str(getattr(faq, "question", "") or "")
            answer = str(getattr(faq, "answer", "") or "")
            for kind, entity_id, score in self._faq_relevance(question, answer):
                index.setdefault((kind, entity_id), []).append(
                    {"faq_id": faq_id, "score": score}
                )

        for hits in index.values():
            hits.sort(key=lambda row: (-row["score"], row["faq_id"]))
        self._faq_index = index
        self._faq_index_fingerprint = cache_key
        return index

    #: 命中在 question 里 = 用户问的就是它
    FAQ_SCORE_IN_QUESTION = 100
    #: 命中在 answer 开头 = 正文主要就是在讲它
    FAQ_SCORE_ANSWER_HEAD = 50
    #: 命中位置在 answer 前半段
    FAQ_SCORE_ANSWER_EARLY = 20
    #: 低于这个分数不算相关（避免超长规则专题把关联区刷满）
    FAQ_SCORE_MIN = 20
    #: 「开头」的判定字符数
    FAQ_HEAD_CHARS = 200

    def _faq_relevance(
        self, question: str, answer: str
    ) -> list[tuple[str, str, int]]:
        """一条 FAQ 与哪些实体相关（按命中位置打分）。"""
        scores: dict[tuple[str, str], int] = {}

        def bump(kind: str, entity_id: str, score: int) -> None:
            key = (kind, entity_id)
            if score > scores.get(key, 0):
                scores[key] = score

        for hit in self.mentions(question, limit=50):
            bump(str(hit["kind"]), str(hit["id"]), self.FAQ_SCORE_IN_QUESTION)

        if answer.strip():
            half = max(len(answer) // 2, 1)
            head = answer[: self.FAQ_HEAD_CHARS]
            early = answer[:half]
            for hit in self.mentions(answer, limit=200):
                name = str(hit.get("name") or "")
                kind, entity_id = str(hit["kind"]), str(hit["id"])
                if name and name in head:
                    bump(kind, entity_id, self.FAQ_SCORE_ANSWER_HEAD)
                elif name and name in early:
                    bump(kind, entity_id, self.FAQ_SCORE_ANSWER_EARLY)

        return [
            (kind, entity_id, score)
            for (kind, entity_id), score in scores.items()
            if score >= self.FAQ_SCORE_MIN
        ]

    def related_faqs(self, kind: str, entity_id: str, *, limit: int = 8) -> list[dict]:
        """提到某个实体的 FAQ，按相关度排序（详情页「相关 FAQ」区块用）。"""
        hits = self._related_faq_index().get((kind, entity_id), [])
        out: list[dict] = []
        for hit in hits[:limit]:
            faq = self.bundle.get("faq", hit["faq_id"])
            if faq is None:
                continue
            out.append(
                {
                    "id": faq.id,
                    "question": str(getattr(faq, "question", "") or ""),
                    "answer_excerpt": truncate(str(getattr(faq, "answer", "") or ""), 160),
                    "mode": getattr(faq, "mode", "") or "",
                    "score": hit["score"],
                }
            )
        return out

    def related_faq_counts(self, kind: str, ids: Iterable[str]) -> dict[str, int]:
        """批量取「某个实体有多少条相关 FAQ」（列表页角标用，避免逐行扫描）。"""
        index = self._related_faq_index()
        return {entity_id: len(index.get((kind, entity_id), [])) for entity_id in ids}

    # ------------------------------------------------------------ 检索

    def _build_index(self) -> dict[str, dict[str, tuple[str, str]]]:
        if self._index:
            return self._index
        bundle = self.bundle
        index: dict[str, dict[str, tuple[str, str]]] = {}
        name_index = build_name_index(bundle)
        for kind in KIND_ORDER:
            rows: dict[str, tuple[str, str]] = {}
            for entity_id, entity in bundle.all(kind).items():
                rows[entity_id] = _search_row_pair(bundle, kind, entity, name_index)
            index[kind] = rows
        self._index = index
        return index

    def search(
        self, query: str, kinds: Iterable[str] | None = None, limit: int = 50
    ) -> list[dict]:
        """跨类型检索。复用 ``repo`` 同样的排序规则，另外支持按 id 命中。"""
        text = (query or "").strip()
        if not text:
            return []
        index = self._build_index()
        kind_list = list(kinds) if kinds else list(KIND_ORDER)
        lowered = text.lower()

        scored: list[tuple] = []
        for kind in kind_list:
            for entity_id, (name, body) in index.get(kind, {}).items():
                score = _score_query(text, name, body)
                if score is None and entity_id == text:
                    score = -1  # id 精确命中排最前
                if score is None and lowered in entity_id.lower():
                    score = 6
                if score is None:
                    continue
                scored.append((score, KIND_ORDER.index(kind), name or entity_id, kind, entity_id))

        scored.sort(key=lambda item: item[:3])
        out: list[dict] = []
        for _score_value, _rank, _name, kind, entity_id in scored[:limit]:
            entity = self.bundle.get(kind, entity_id)
            if entity is not None:
                out.append(self.describe(kind, entity))
        return out

    def filter_rows(
        self,
        kind: str,
        *,
        q: str = "",
        mode: str = "",
        product: str = "",
        set_name: str = "",
        faction: str = "",
        category_1: str = "",
        category_2: str = "",
    ) -> list[dict]:
        """列表页筛选：q / mode / product_id / 系列（拓展包）/ 分类。

        ``set_name`` 在武将（版本）上表示「武将牌系列」（``general_set``），
        在卡牌上表示「拓展包」（``card_set``）——两者共用一个参数名。
        """
        bundle = self.bundle
        hits: dict[str, int] = {}
        if q.strip():
            for row in self.search(q, kinds=[kind], limit=10 ** 6):
                hits[row["id"]] = 1

        rows: list[dict] = []
        for entity_id, entity in self.entries(kind):
            if q.strip() and entity_id not in hits:
                continue
            if mode and self.mode_of(kind, entity) != mode:
                continue
            if product and getattr(entity, "product_id", None) != product:
                continue
            if set_name:
                if kind in {"general", "general_variant"}:
                    if getattr(entity, "general_set", None) != set_name:
                        continue
                elif kind == "card":
                    if getattr(entity, "card_set", None) != set_name:
                        continue
                else:
                    continue
            if category_1 and (kind != "card" or getattr(entity, "category_1", "") != category_1):
                continue
            if category_2 and (kind != "card" or getattr(entity, "category_2", "") != category_2):
                continue
            if faction:
                if kind == "general_variant":
                    tags = _faction_tokens(getattr(entity, "faction", ""))
                    tag = _number_tag(getattr(entity, "general_number", "") or "")
                    if tag:
                        tags.append(tag)
                    if faction not in tags:
                        continue
                elif kind == "general":
                    mine = set()
                    for v in bundle.all("general_variant").values():
                        if v.general_id == entity_id:
                            mine.update(_faction_tokens(getattr(v, "faction", "")))
                            t = _number_tag(getattr(v, "general_number", "") or "")
                            if t:
                                mine.add(t)
                    if faction not in mine:
                        continue
                else:
                    continue
            rows.append(self.describe(kind, entity))

        if kind == "skill_version":
            rows.sort(key=lambda r: (self.name_of("skill", getattr(r["entity"], "skill_id", "")),
                                     mode_sort_key(getattr(r["entity"], "mode", "common")), r["id"]))
        elif kind == "general_variant":
            rows.sort(key=lambda r: (_number_sort_key(getattr(r["entity"], "general_number", "") or ""),
                                     r["name"], r["id"]))
        elif kind == "general":
            first: dict[str, str] = {}
            for v in bundle.all("general_variant").values():
                key = _number_sort_key(getattr(v, "general_number", "") or "")
                if v.general_id not in first or key < first[v.general_id]:
                    first[v.general_id] = key
            rows.sort(key=lambda r: (first.get(r["id"], "zzzz"), r["name"], r["id"]))
        elif kind == "card":
            rows.sort(
                key=lambda r: (
                    r["category_1"], r["category_2"], r["category_3"], r["name"], r["id"]
                )
            )
        else:
            rows.sort(key=lambda r: (r["name"], r["id"]))
        return rows

    def filter_options_scoped(
        self, kind: str, *, category_1: str = ""
    ) -> dict[str, list[dict]]:
        """``filter_options`` 的「只看某一级分类」版本（Wiki 的游戏牌 / 指示物入口用）。

        ``filter_options`` 是按整个 kind 统计的，卡牌侧会把「游戏牌」的拓展包
        也算到「指示物」页面的下拉里。这里先按 ``category_1`` 收窄再统计。
        """
        if not category_1 or kind != "card":
            return self.filter_options(kind)

        options = self.filter_options(kind)
        cards = [
            card
            for card in self.bundle.all("card").values()
            if (getattr(card, "category_1", "") or "") == category_1
        ]
        sets: dict[str, int] = {}
        for card in cards:
            value = (getattr(card, "card_set", None) or "").strip()
            if value:
                sets[value] = sets.get(value, 0) + 1

        scoped = dict(options)
        scoped["sets"] = [
            {"value": value, "count": count}
            for value, count in sorted(sets.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return scoped

    def filter_options(self, kind: str) -> dict[str, list[dict]]:
        """当前数据里实际出现过的筛选值，避免下拉框给一堆空选项。"""
        bundle = self.bundle
        modes: dict[str, int] = {}
        products: dict[str, int] = {}
        sets: dict[str, int] = {}
        categories_1: dict[str, int] = {}
        categories_2: dict[str, int] = {}
        for _eid, entity in bundle.all(kind).items():
            mode = self.mode_of(kind, entity)
            if mode:
                modes[mode] = modes.get(mode, 0) + 1
            product = getattr(entity, "product_id", None)
            if product:
                products[product] = products.get(product, 0) + 1
            if kind in {"general", "general_variant"}:
                values = [getattr(entity, "general_set", None)]
            elif kind == "card":
                values = [getattr(entity, "card_set", None)]
            else:
                values = []
            for value in values:
                if value:
                    sets[value] = sets.get(value, 0) + 1
            if kind == "card":
                value_1 = getattr(entity, "category_1", "") or ""
                value_2 = getattr(entity, "category_2", "") or ""
                if value_1:
                    categories_1[value_1] = categories_1.get(value_1, 0) + 1
                if value_2:
                    categories_2[value_2] = categories_2.get(value_2, 0) + 1

        factions: dict[str, int] = {}
        if kind == "general_variant":
            for _eid, entity in bundle.all("general_variant").items():
                tags = set(_faction_tokens(getattr(entity, "faction", "")))
                t = _number_tag(getattr(entity, "general_number", "") or "")
                if t:
                    tags.add(t)
                for t in tags:
                    factions[t] = factions.get(t, 0) + 1
        elif kind == "general":
            for _eid, general in bundle.all("general").items():
                mine = set()
                for v in bundle.all("general_variant").values():
                    if v.general_id == general.id:
                        mine.update(_faction_tokens(getattr(v, "faction", "")))
                        t = _number_tag(getattr(v, "general_number", "") or "")
                        if t:
                            mine.add(t)
                for t in mine:
                    factions[t] = factions.get(t, 0) + 1

        def options(mapping: dict[str, int], label_of=lambda k: k) -> list[dict]:
            return [
                {"value": key, "label": label_of(key), "count": mapping[key]}
                for key in sorted(mapping)
            ]

        return {
            "modes": options(modes, lambda k: MODE_LABELS.get(k, k)),
            "products": options(products, lambda k: self.name_of("product", k)),
            "sets": options(sets, lambda k: k),
            "categories_1": options(categories_1, lambda k: k),
            "categories_2": options(categories_2, lambda k: k),
            "factions": options(factions, lambda k: k),
            "set_label": (
                "武将牌系列" if kind in {"general", "general_variant"}
                else "拓展包" if kind == "card"
                else "系列 / 拓展包"
            ),
        }

    # ------------------------------------------------------------ 录入辅助（给前端 JSON API）

    def skill_search(self, query: str, limit: int = 20) -> list[dict]:
        """技能搜索式组合框：按 名称 / id 匹配，名称命中优先（别名概念已删除）。"""
        text = (query or "").strip()
        bundle = self.bundle
        lowered = text.lower()
        scored: list[tuple] = []
        for skill_id, skill in bundle.all("skill").items():
            name = skill.name or ""
            version_count = sum(
                1 for v in bundle.all("skill_version").values() if v.skill_id == skill_id
            )
            if not text:
                score = (5, 0)
            elif name == text:
                score = (0, 0)
            elif name.startswith(text):
                score = (2, 0)
            elif lowered in skill_id.lower():
                score = (3, 0)
            elif text in name:
                score = (4, 0)
            else:
                continue
            scored.append((score[0], name, skill_id, name, version_count))
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return [
            {
                "id": skill_id,
                "name": name,
                "version_count": version_count,
                "exact": bool(text) and name == text,
            }
            for _score, _n, skill_id, name, version_count in scored[:limit]
        ]

    def version_display_label(self, version: Any) -> str:
        """给编辑页用：与产品名/模式名重复的版本标签不再单独显示一次。"""
        if version is None:
            return ""
        return self._display_label(
            version,
            self.name_of("product", getattr(version, "product_id", None)),
            MODE_LABELS.get(getattr(version, "mode", ""), getattr(version, "mode", "") or ""),
        )

    @staticmethod
    def _display_label(version: Any, product_name: str, mode_label: str) -> str:
        """版本标签如果与产品名/模式名重复，就不必再显示（避免「群狼环鼎 · 国战 · 群狼环鼎」）。"""
        label = (getattr(version, "version_label", "") or "").strip()
        if not label:
            return ""
        if label in {product_name.strip(), mode_label.strip()}:
            return ""
        return label

    def skill_version_options(
        self, skill_id: str, *, mode: str | None = None, product_id: str | None = None
    ) -> list[dict]:
        """某技能的可选版本，按“产品匹配 > 模式匹配 > common > 其它”排序。

        每项带上足够人判断的信息（版本标签/模式/产品/文本摘要/文本哈希），
        前端不仅能显示得像人话，还能据此判断“是不是已经有完全相同的版本”。
        """
        bundle = self.bundle
        rows: list[dict] = []
        for version in bundle.all("skill_version").values():
            if version.skill_id != skill_id:
                continue
            if product_id and version.product_id == product_id:
                rank = 0
            elif mode and version.mode == mode:
                rank = 1
            elif version.mode == "common":
                rank = 2
            else:
                rank = 3
            text = version.text or ""
            rows.append(
                {
                    "id": version.id,
                    "version_label": version.version_label or "",
                    "display_label": self._display_label(
                        version, self.name_of("product", version.product_id),
                        MODE_LABELS.get(version.mode, version.mode or ""),
                    ),
                    "mode": version.mode,
                    "mode_label": MODE_LABELS.get(version.mode, version.mode),
                    "product_id": version.product_id,
                    "product_name": self.name_of("product", version.product_id),
                    "previous_version_id": version.previous_version_id,
                    "text": text,
                    "text_excerpt": truncate(text, 60),
                    "text_sha": hashlib.sha1(text.strip().encode("utf-8")).hexdigest(),
                    "rank": rank,
                    "same_product": bool(product_id and version.product_id == product_id),
                    "same_mode": bool(mode and version.mode == mode),
                }
            )
        rows.sort(key=lambda r: (r["rank"], r["version_label"], r["id"]))
        return rows

    def version_by_content(
        self, skill_id: str, mode: str | None, product_id: str | None, text: str
    ) -> dict | None:
        """找一条内容完全相同的技能版本（避免重复录入的设备）。"""
        target = (text or "").strip()
        if not target:
            return None
        for version in self.bundle.all("skill_version").values():
            if version.skill_id != skill_id:
                continue
            if version.mode != (mode or "common"):
                continue
            if (version.product_id or None) != (product_id or None):
                continue
            if (version.text or "").strip() == target:
                return {"id": version.id, "version_label": version.version_label or ""}
        return None

    def product_search(self, query: str, limit: int = 20) -> list[dict]:
        """产品搜索式下拉：支持 名称 / ID / 年份。"""
        text = (query or "").strip()
        lowered = text.lower()
        rows: list[dict] = []
        for product_id, product in self.bundle.all("product").items():
            name = product.name or ""
            year = product.release_year
            haystack = f"{name} {product_id} {year or ''}".lower()
            if text and lowered not in haystack:
                continue
            score = (
                0 if name == text else
                1 if name.startswith(text) else
                2 if lowered in product_id.lower() else
                3 if text and text == str(year or "") else
                4 if text else
                5
            )
            rows.append(
                {
                    "id": product_id,
                    "name": name,
                    "release_year": year,
                    "mode": product.mode,
                    "mode_label": MODE_LABELS.get(product.mode or "", product.mode or ""),
                    "general_sets": list(product.general_sets or []),
                    "card_sets": list(product.card_sets or []),
                    "score": score,
                }
            )
        rows.sort(key=lambda r: (r["score"], r["name"], r["id"]))
        return rows[:limit]

    def suggest_values(self, field_name: str, query: str = "", limit: int = 40) -> list[dict]:
        """势力 / 武将牌系列 / 游戏牌拓展包这类“自由文本但希望复用已有值”的字段的候选值。

        - 武将牌系列的**权威来源是 Product.general_sets**：产品里声明过的系列都能选到，
          另外把已经在武将版本上使用的值也一并列出；
        - 游戏牌拓展包的**权威来源是 Product.card_sets**，另外把已经在卡牌上使用的
          ``Card.card_set`` 值也一并列出。
        """
        text = (query or "").strip()
        counter: dict[str, int] = {}
        if field_name == "card_set":
            for product in self.bundle.all("product").values():
                for value in (product.card_sets or []):
                    value = str(value).strip()
                    if not value or (text and text not in value):
                        continue
                    counter.setdefault(value, 0)
            for card in self.bundle.all("card").values():
                value = str(getattr(card, "card_set", "") or "").strip()
                if not value or (text and text not in value):
                    continue
                counter[value] = counter.get(value, 0) + 1
            return [
                {"value": value, "count": counter[value]}
                for value in sorted(counter, key=lambda v: (-counter[v], v))[:limit]
            ]
        if field_name == "general_set":
            for product in self.bundle.all("product").values():
                for value in (product.general_sets or []):
                    value = str(value).strip()
                    if not value or (text and text not in value):
                        continue
                    counter.setdefault(value, 0)
        # 势力 / 武将牌系列现在都属于 GeneralVariant
        for variant in self.bundle.all("general_variant").values():
            value = (
                getattr(variant, "faction", "") if field_name == "faction"
                else getattr(variant, "general_set", None) if field_name == "general_set"
                else None
            )
            if not value:
                continue
            if text and text not in str(value):
                continue
            counter[str(value)] = counter.get(str(value), 0) + 1
        return [
            {"value": value, "count": counter[value]}
            for value in sorted(counter, key=lambda v: (-counter[v], v))[:limit]
        ]

    def card_image_options(self, query: str = "", limit: int = 30) -> list[dict]:
        """``data/card_images/`` 下真实存在的卡图文件名（卡牌录入页的候选）。

        已经在卡牌 / 武将版本上使用的图排前面，并标出是谁在用；查询为空时也一样，
        这样录入时优先看到“已经被用过的图”，不容易把文件名打错。
        """
        directory = self.data_dir / "card_images"
        text = (query or "").strip().lower()
        try:
            names = sorted(path.name for path in directory.iterdir() if path.is_file())
        except OSError:  # pragma: no cover - 目录不存在时不给候选
            names = []
        if text:
            names = [name for name in names if text in name.lower()]
        used: dict[str, str] = {}
        for kind in ("card", "general_variant"):
            for entity in self.bundle.all(kind).values():
                image = getattr(entity, "image", "") or ""
                if image:
                    used.setdefault(image, self.display_name(kind, entity))
        rows = [
            {"value": name, "label": name, "sub": f"已被「{used[name]}」使用" if name in used else ""}
            for name in names
        ]
        rows.sort(key=lambda row: (not bool(row["sub"]), row["value"]))
        return rows[:limit]

    def generals_named(self, name: str, limit: int = 8) -> list[dict]:
        """同名武将提醒（同名在不同产品/版本下是合法的，所以只提醒、不阻止）。"""
        text = (name or "").strip()
        if not text:
            return []
        rows = []
        bundle = self.bundle
        for general_id, general in bundle.all("general").items():
            if general.name != text and text not in general.name:
                continue
            variants = sorted(
                (v for v in bundle.all("general_variant").values() if v.general_id == general_id),
                key=lambda v: (v.variant, v.id),
            )
            first = variants[0] if variants else None
            rows.append(
                {
                    "id": general_id,
                    "name": general.name,
                    "mode": first.mode if first else None,
                    "mode_label": MODE_LABELS.get(first.mode or "", first.mode or "")
                    if first
                    else "",
                    "product_id": first.product_id if first else None,
                    "product_name": self.name_of("product", first.product_id) if first else "",
                    "general_set": (first.general_set or "") if first else "",
                    "general_number": (first.general_number or "") if first else "",
                    "variant_labels": [v.variant for v in variants],
                    "variant_count": len(variants),
                    "exact": general.name == text,
                }
            )
        rows.sort(key=lambda r: (not r["exact"], r["name"], r["id"]))
        return rows[:limit]

    # ------------------------------------------------------------ 反向引用

    def reverse_refs(self, kind: str, entity_id: str) -> list[dict]:
        """谁在引用这条记录？（删除前的安全检查）

        用与校验器同一张声明表 + 同一个路径展开器，所以 ``skills[0].version_id``
        这种嵌套引用也会被准确列出（字段名就是那个路径）。
        """
        bundle = self.bundle
        out: list[dict] = []
        for src_kind, field_name, target_kind, _is_list in REFERENCE_FIELDS:
            if target_kind != kind:
                continue
            for src_id, src_entity in bundle.all(src_kind).items():
                for ref in iter_reference_values(src_entity, field_name):
                    if ref.value == entity_id:
                        out.append(
                            {
                                "kind": src_kind,
                                "kind_label": KIND_BY_NAME[src_kind].label,
                                "id": src_id,
                                "name": self.display_name(src_kind, src_entity),
                                "field": ref.path,
                            }
                        )
        out.sort(key=lambda r: (r["kind"], r["id"], r["field"]))
        return out

    def outgoing_refs(self, kind: str, entity: Any) -> dict[str, list[dict]]:
        """这条记录自己引用了谁（详情页把字段渲染成可点击链接）。"""
        out: dict[str, list[dict]] = {}
        for src_kind, field_name, target_kind, _is_list in REFERENCE_FIELDS:
            if src_kind != kind:
                continue
            for ref in iter_reference_values(entity, field_name):
                out.setdefault(field_name, []).append(
                    {
                        "kind": target_kind,
                        "id": ref.value,
                        "path": ref.path,
                        "name": self.name_of(target_kind, ref.value),
                        "exists": self.entity(target_kind, ref.value) is not None,
                    }
                )
        return out

    def delete_impact(self, kind: str, entity_id: str) -> dict:
        """删除前的“影响说明”（不阻止删除，但要让使用者知道后果）。"""
        impact: dict = {"lines": [], "is_current": False}
        if kind == "skill_version":
            version = self.entity("skill_version", entity_id)
            if version is not None:
                bound = [item["name"] for item in self.variants_using_version(entity_id)]
                successors = [
                    v.id
                    for v in self.bundle.all("skill_version").values()
                    if v.previous_version_id == entity_id
                ]
                impact["is_current"] = bool(bound)
                impact["bound_by"] = bound
                if bound:
                    impact["lines"].append(
                        "有武将版本显式绑定这一版：" + "、".join(bound) + "；删除后它们会失去技能文本。"
                    )
                if successors:
                    impact["lines"].append(
                        "有更新的版本把它当作前一版本：" + "、".join(successors) + "。"
                    )
                if not bound and not successors:
                    impact["lines"].append("既没有武将版本绑定它，也没有后继版本引用它，删除属于历史清理。")
        elif kind == "skill":
            versions = [v.id for v in self.bundle.all("skill_version").values() if v.skill_id == entity_id]
            impact["lines"].append(
                f"该技能本体下还有 {len(versions)} 条 SkillVersion；只要它们还在，删除会被校验拦下。"
            )
        elif kind == "product":
            bundle = self.bundle
            product = self.entity("product", entity_id)
            card_sets = {str(value) for value in (getattr(product, "card_sets", None) or [])}
            counts = {
                "武将版本": sum(
                    1 for v in bundle.all("general_variant").values() if v.product_id == entity_id
                ),
                "卡牌（按拓展包归属）": sum(
                    1 for c in bundle.all("card").values() if (c.card_set or "") in card_sets
                ) if card_sets else 0,
                "技能版本": sum(1 for v in bundle.all("skill_version").values() if v.product_id == entity_id),
            }
            detail = "、".join(f"{name} {count} 条" for name, count in counts.items() if count)
            impact["lines"].append(f"该产品关联：{detail or '暂无'}。只要还有引用，删除会被拦下。")
        return impact

    # ------------------------------------------------------------ 并发保护

    def entity_sha(self, kind: str, entity_id: str) -> str:
        """该记录的**内容哈希**：用于发现“页面打开后文件被别人改过”。"""
        path = self.file_path(kind, entity_id)
        if path is None:
            return ""
        return self._entity_sha_in_file(path, entity_id)

    @staticmethod
    def _entity_sha_in_file(path: Path, entity_id: str) -> str:
        path = Path(path)
        if not path.exists():
            return ""
        try:
            doc = yaml_writer.read_doc(path)
        except (OSError, ValueError):
            return ""
        index = doc.index_of(entity_id)
        if index is None:
            return ""
        payload = yaml_writer.dump_yaml(doc.nodes[index])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _concurrency_check(
        self, kind: str, check_path: Path, check_id: str, expected_sha: str | None
    ) -> str:
        """返回错误信息；没有冲突则返回空串。"""
        if expected_sha is None:
            return ""
        actual = self._entity_sha_in_file(check_path, check_id)
        if actual == expected_sha:
            return ""
        if expected_sha == "":
            # 新增时表示“这条记录还不该存在”。id 撞车应该由校验器报
            # “id 重复：… 已在 xxx.yaml 中定义”（带文件与行号），
            # 不要在这里误导成“文件被改动过”。
            return ""
        return (
            "这条记录在页面打开之后被改动过了（可能是另一个标签页，或者你手工编辑了 YAML）。"
            "为了避免覆盖其他人的修改，本次**没有写入任何文件**，请刷新页面确认最新内容后重试。"
        )

    # ------------------------------------------------------------ 详情视图

    def skill_view(self, skill_id: str) -> dict | None:
        entity = self.entity("skill", skill_id)
        if entity is None:
            return None
        bundle = self.bundle
        versions = [
            v for v in bundle.all("skill_version").values() if v.skill_id == skill_id
        ]
        versions.sort(key=lambda v: (mode_sort_key(v.mode), v.version_label or "", v.id))

        rows = []
        for version in versions:
            rows.append(
                {
                    "entity": version,
                    "id": version.id,
                    "product_name": self.name_of("product", version.product_id),
                    "previous_id": version.previous_version_id,
                    "successor_ids": [
                        v.id for v in versions if v.previous_version_id == version.id
                    ],
                    # 技能文本里的技能名 / 关键词名 / 卡牌名自动变成链接
                    "segments": self.annotate(version.text),
                }
            )

        return {
            "kind": "skill",
            "skill": entity,
            "versions": rows,
            "modes": [
                {
                    "mode": mode,
                    "label": MODE_LABELS.get(mode, mode),
                    "count": len([v for v in versions if v.mode == mode]),
                    "version_ids": [v.id for v in versions if v.mode == mode],
                }
                for mode in sorted({v.mode for v in versions}, key=mode_sort_key)
            ],
            "chains": _history_chains(rows),
        }

    def general_view(self, general_id: str) -> dict | None:
        entity = self.entity("general", general_id)
        if entity is None:
            return None
        bundle = self.bundle

        variants = []
        for variant in sorted(
            (v for v in bundle.all("general_variant").values() if v.general_id == general_id),
            key=lambda v: (v.variant, v.id),
        ):
            skills = []
            for position, ref in enumerate(variant.skills):
                all_versions = sorted(
                    (
                        v
                        for v in bundle.all("skill_version").values()
                        if v.skill_id == ref.skill_id
                    ),
                    key=lambda v: (mode_sort_key(v.mode), v.version_label or "", v.id),
                )
                pinned = bundle.get("skill_version", ref.version_id) if ref.version_id else None
                chosen = pinned or self._fallback_version(all_versions, variant.mode)
                skills.append(
                    {
                        "position": position,
                        "skill_id": ref.skill_id,
                        "skill": bundle.get("skill", ref.skill_id),
                        "version_id": ref.version_id,
                        "pinned": pinned,
                        "current": chosen,
                        "pinned_missing": bool(ref.version_id and pinned is None),
                        "all_versions": all_versions,
                        "segments": self.annotate(chosen.text) if chosen is not None else [],
                    }
                )
            product = bundle.get("product", variant.product_id)
            variants.append(
                {
                    "entity": variant,
                    "id": variant.id,
                    "variant": variant.variant,
                    "skills": skills,
                    "product_name": self.name_of("product", variant.product_id),
                    "product_mode": getattr(product, "mode", None),
                    "set_display": self.set_display(variant.general_set),
                    "edit_url": f"/edit/general/{general_id}?variant={variant.id}",
                }
            )

        return {
            "kind": "general",
            "general": entity,
            "variants": variants,
        }

    @staticmethod
    def _fallback_version(versions: list[Any], mode: str | None) -> Any | None:
        """技能版本回退规则（与 schema 约定一致）。

        没有显式 ``version_id`` 时，按该武将版本的 mode 匹配**唯一**一条版本作为回退；
        匹配不到（一条都没有，或有多条并列有歧义）就表示这个技能在这个版本上没有文本，
        不静默挑一个。
        """
        seen: set[str] = set()
        for wanted in (mode, "common"):
            if not wanted or wanted in seen:
                continue
            seen.add(wanted)
            hits = [v for v in versions if v.mode == wanted]
            if len(hits) == 1:
                return hits[0]
        return None

    def card_view(self, entity_id: str) -> dict | None:
        """卡牌详情：**一张卡**（单层）——分类 / 卡名 / 拓展包 / 效果文本 / 关联技能。"""
        entity = self.entity("card", entity_id)
        if entity is None:
            return None
        bundle = self.bundle
        effect = entity.effect
        skills: list[dict] = []
        for position, ref in enumerate(effect.skills):
            all_versions = sorted(
                (v for v in bundle.all("skill_version").values() if v.skill_id == ref.skill_id),
                key=lambda v: (mode_sort_key(v.mode), v.version_label or "", v.id),
            )
            pinned = bundle.get("skill_version", ref.version_id) if ref.version_id else None
            # 卡牌没有 mode：回退时只认 common（跨模式通用）那一条唯一版本
            chosen = pinned or self._fallback_version(all_versions, None)
            skills.append(
                {
                    "position": position,
                    "skill_id": ref.skill_id,
                    "skill": bundle.get("skill", ref.skill_id),
                    "version_id": ref.version_id,
                    "pinned": pinned,
                    "current": chosen,
                    "pinned_missing": bool(ref.version_id and pinned is None),
                    "all_versions": all_versions,
                    "segments": self.annotate(chosen.text) if chosen is not None else [],
                }
            )
        return {
            "kind": "card",
            "card": entity,
            "effect_text": self.annotate(effect.text) if (effect.text or "").strip() else [],
            "skills": skills,
        }

    def product_view(self, product_id: str) -> dict | None:
        entity = self.entity("product", product_id)
        if entity is None:
            return None
        bundle = self.bundle
        variants = [v for v in bundle.all("general_variant").values() if v.product_id == product_id]
        for variant in variants:
            general = bundle.get("general", variant.general_id)
            variant_name = getattr(general, "name", variant.general_id)
            variant._display_name = f"{variant_name}（{variant.variant}）"  # type: ignore[attr-defined]
        versions = [v for v in bundle.all("skill_version").values() if v.product_id == product_id]
        return {
            "kind": "product",
            "product": entity,
            "variants": sorted(variants, key=lambda v: (v.general_id, v.variant, v.id)),
            "skill_versions": sorted(versions, key=lambda v: (v.skill_id, mode_sort_key(v.mode), v.id)),
            "general_set_chips": [{"name": str(name)} for name in (entity.general_sets or [])],
            "card_set_chips": [{"name": str(name)} for name in (entity.card_sets or [])],
        }

    # ------------------------------------------------------------ 写入

    def _candidate_origin(self, kind: str, entity_id: str, old_id: str | None) -> Origin:
        if old_id:
            origin = self.origin(kind, old_id)
            if origin is not None:
                return Origin(origin.path, 0, "")
        spec = KIND_BY_NAME[kind]
        return Origin(f"{spec.dirname}/{entity_id}.yaml", 0, "")

    def validate_candidate(
        self, kind: str, candidate: dict, *, old_id: str | None = None
    ) -> ValidationResult:
        """在内存里把“候选改动”合进整个数据集并跑完整校验，不碰磁盘。

        ``old_id`` 为 ``None`` 表示**新增**：此时不移除任何已有记录，
        于是“id 已被占用”会被校验器报成 ``id 重复``，不会被当成覆盖保存。
        传入 ``old_id`` 表示**修改**（可以等于新 id，也可以是改名）。
        """
        raws, load_issues = load_data_dir(self.data_dir)
        if old_id is None:
            kept = list(raws)
        else:
            kept = [
                raw
                for raw in raws
                if not (raw.kind == kind and raw.data.get("id") == old_id)
            ]
        entity_id = str(candidate.get("id") or "")
        kept.append(
            RawEntity(kind=kind, data=candidate, origin=self._candidate_origin(kind, entity_id, old_id))
        )
        return validate_entities(self.data_dir, kept, load_issues)

    def validate_changes(self, changes: list[Change]) -> ValidationResult:
        """只把一批候选改动合进内存跑校验，**不写盘**。

        失败页用它把“文件 / 字段 / 原因”也显示出来（字段级错误之外再给一次完整校验说明）。
        """
        with self._lock:
            raws, load_issues = load_data_dir(self.data_dir)
            removed = {
                (change.kind, change.old_id)
                for change in changes
                if change.old_id is not None
            }
            kept = [raw for raw in raws if (raw.kind, raw.data.get("id")) not in removed]
            for change in changes:
                kept.append(
                    RawEntity(
                        kind=change.kind,
                        data=change.candidate,
                        origin=self._candidate_origin(
                            change.kind, change.entity_id, change.old_id
                        ),
                    )
                )
            return validate_entities(self.data_dir, kept, load_issues)

    def _resolve_target_file(
        self, kind: str, entity_id: str, old_id: str | None
    ) -> tuple[Path, Path | None]:
        """返回 (写入目标文件, 需要清理的旧文件)。

        - **新增**（``old_id is None``）永远落 ``data/<目录>/<id>.yaml``：
          即使这个 id 已经被别的文件占用，也交给校验器报“id 重复”，
          而不是让并发检查在这里变出一个莫名其妙的“文件被改动过”。
        - **修改**沿用原文件；改了 id 则写到新文件并删掉旧文件里的那一条。
        """
        if old_id is None:
            return yaml_writer.entity_file_path(self.data_dir, kind, entity_id), None
        if old_id != entity_id:
            old_path = self.file_path(kind, old_id)
            return yaml_writer.entity_file_path(self.data_dir, kind, entity_id), old_path
        origin = self.origin(kind, old_id)
        if origin is not None:
            return self.data_dir / origin.path, None
        return yaml_writer.entity_file_path(self.data_dir, kind, entity_id), None

    def _build_atomic(self) -> BuildResult:
        """全量重建到临时库，成功后再原子替换 build/sgs.db。

        这样“重建中途失败”不会把原来可用的数据库删成半成品。
        """
        target = Path(self.db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".rebuild")
        if tmp.exists():
            tmp.unlink()
        try:
            build = build_database(self.data_dir, tmp)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        os.replace(tmp, target)
        build.db_path = target
        return build

    def save(
        self,
        kind: str,
        candidate: dict,
        *,
        old_id: str | None = None,
        expected_sha: str | None = None,
    ) -> SaveOutcome:
        """单条保存（``save_batch`` 的薄包装）。"""
        return self.save_batch(
            [Change(kind=kind, candidate=candidate, old_id=old_id, expected_sha=expected_sha)]
        )

    def save_batch(self, changes: list[Change]) -> SaveOutcome:
        """**一次事务**保存多条改动（武将录入页会同时产生 General + Skill + SkillVersion）。

        流程（与单条保存完全一致，只是把“一条”换成“一批”）：

        1. 并发检查（``_base_sha``）与目标文件解析；
        2. 把**全部**候选变化合进内存里的 DataBundle，跑一次完整 ``validate_entities``；
           任何一条不合法 → 一个文件都不写；
        3. 备份所有要改的文件，全部写入（临时文件 + fsync + os.replace）；
        4. **只 rebuild 一次** SQLite；
        5. 任何一步失败 → 回滚全部文件（被外部改动过的文件跳过回滚），旧数据库保持可用。
        """
        with self._lock:
            changes = [change for change in changes if change.candidate is not None]
            if not changes:
                return SaveOutcome(ok=True, kind="", entity_id="")

            primary = changes[0]
            entity_id = primary.entity_id
            kind = primary.kind if len(changes) == 1 else ""

            # 0. 批内不能有两条改动命中同一条记录（否则合并语义不明确）
            seen_keys: set[tuple[str, str]] = set()
            for change in changes:
                key = (change.kind, change.old_id or change.entity_id)
                if key in seen_keys:
                    return SaveOutcome(
                        ok=False, kind=kind, entity_id=entity_id,
                        error=(
                            "同一批改动里出现了重复目标："
                            f"{change.kind} {change.old_id or change.entity_id}"
                        ),
                    )
                seen_keys.add(key)

            # 1. 并发检查 + 目标文件解析
            targets: list[tuple[Change, Path, Path | None]] = []
            #: 顺带记下「改之前」的卡图名，写盘后用来把图片文件一起改名
            image_moves: list[tuple[str, str]] = []
            for change in changes:
                target, stale_path = self._resolve_target_file(
                    change.kind, change.entity_id, change.old_id
                )
                check_path = (
                    stale_path if (stale_path is not None and stale_path != target) else target
                )
                check_id = change.old_id or change.entity_id
                conflict = self._concurrency_check(
                    change.kind, check_path, check_id, change.expected_sha
                )
                if conflict:
                    return SaveOutcome(
                        ok=False, kind=kind, entity_id=entity_id, conflict=True, error=conflict
                    )
                targets.append((change, target, stale_path))

                # 卡图名由卡面编号派生：编号改了，图片文件也要跟着改名
                old_entity = self.entity(change.kind, change.old_id) if change.old_id else None
                old_image = (getattr(old_entity, "image", "") or "").strip()
                new_image = (change.candidate.get("image") or "").strip()
                if old_image and new_image and old_image != new_image:
                    image_moves.append((old_image, new_image))
                # 新建记录：磁盘上的文件是用户刚上传的（名字随意），
                # 需要把它改成编号派生的名字
                uploaded = (change.uploaded_image or "").strip()
                if uploaded and new_image and uploaded != new_image:
                    image_moves.append((uploaded, new_image))

            # 2. 一次性合并进内存候选集合并完整校验
            raws, load_issues = load_data_dir(self.data_dir)
            removed = {
                (change.kind, change.old_id)
                for change in changes
                if change.old_id is not None
            }
            kept = [
                raw for raw in raws if (raw.kind, raw.data.get("id")) not in removed
            ]
            for change in changes:
                kept.append(
                    RawEntity(
                        kind=change.kind,
                        data=change.candidate,
                        origin=self._candidate_origin(
                            change.kind, change.entity_id, change.old_id
                        ),
                    )
                )
            pre = validate_entities(self.data_dir, kept, load_issues)
            if not pre.ok:
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, issues=pre.errors,
                    issues_by_entity=_group_issues(pre.errors),
                    error="校验未通过：没有写入任何文件，数据库也没有变化",
                )

            # 3. 备份 + 全部写盘
            backup = FileBackup()
            written: list[str] = []
            moved_images: list[str] = []
            try:
                for change, target, stale_path in targets:
                    entity = pre.bundle.get(change.kind, change.entity_id)
                    if entity is None:  # pragma: no cover - 预校验通过时不可能发生
                        raise ValueError(
                            f"内部错误：候选实体 {change.kind} {change.entity_id} 没被校验器接受"
                        )
                    backup.stash(target)
                    yaml_writer.upsert_entity(
                        target, change.kind, entity, old_id=change.old_id
                    )
                    backup.note_written(target)
                    written.append(str(target))
                    if stale_path is not None and stale_path != target:
                        backup.stash(stale_path)
                        yaml_writer.remove_entity(stale_path, change.old_id or "")
                        backup.note_written(stale_path)
                # 卡图改名：编号变了就把图片文件也改成派生名（YAML 已指向新名字）
                moved_images = self._move_renamed_images(image_moves)
            except (OSError, ValueError) as exc:
                backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id,
                    error=f"写入 YAML 失败：{exc}", rolled_back=True,
                )

            # 4. 只 rebuild 一次
            self.invalidate()
            try:
                build = self._build_atomic()
            except DataValidationError as exc:
                skipped = backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, issues=exc.result.errors,
                    issues_by_entity=_group_issues(exc.result.errors),
                    error=_rollback_note("写盘后校验仍然失败，已回滚 YAML 文件", skipped),
                    rolled_back=True,
                )
            except Exception as exc:  # pragma: no cover - 磁盘/权限等意外
                skipped = backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id,
                    error=_rollback_note(f"重建数据库失败，已回滚 YAML：{exc}", skipped),
                    rolled_back=True,
                )

            return SaveOutcome(
                ok=True, kind=kind, entity_id=entity_id,
                path=str(targets[0][1]) if targets else "",
                build=build, warnings=list(build.warnings), issues=[],
                written=written, moved_images=moved_images,
            )

    def _move_renamed_images(self, moves: list[tuple[str, str]]) -> list[str]:
        """把卡图文件改成新的派生名（编号改了 → 卡图名跟着改）。

        ``moves`` 是 ``(旧文件名, 新文件名)``。只在旧文件存在、新文件不存在时移动，
        并把结果记进 ``SaveOutcome.moved_images`` 供页面提示。
        图片改名失败不算致命：YAML 已经指向新名字，validate 会把它报成「卡图不存在」，
        用户能立刻看到，所以这里不抛异常、不触发回滚。
        """
        images_dir = self.data_dir / "card_images"
        done: list[str] = []
        for old, new in moves:
            if not old or not new or old == new:
                continue
            src, dst = images_dir / old, images_dir / new
            if not src.exists() or dst.exists():
                continue
            try:
                src.rename(dst)
            except OSError:  # pragma: no cover - 权限/占用
                continue
            done.append(new)
        return done

    def delete(
        self, kind: str, entity_id: str, *, expected_sha: str | None = None
    ) -> SaveOutcome:
        """删除前先查引用；被引用则拒绝，并列出引用方。"""
        with self._lock:
            entity = self.entity(kind, entity_id)
            if entity is None:
                return SaveOutcome(ok=False, kind=kind, entity_id=entity_id, error="记录不存在")

            references = self.reverse_refs(kind, entity_id)
            if references:
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, blocked=True,
                    references=references,
                    error="该记录正在被其他数据引用，删除会破坏引用关系",
                )

            path = self.file_path(kind, entity_id)
            if path is None:
                return SaveOutcome(ok=False, kind=kind, entity_id=entity_id, error="找不到该记录所在的 YAML 文件")

            conflict = self._concurrency_check(kind, path, entity_id, expected_sha)
            if conflict:
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, conflict=True, error=conflict
                )

            raws, load_issues = load_data_dir(self.data_dir)
            kept = [
                raw
                for raw in raws
                if not (raw.kind == kind and raw.data.get("id") == entity_id)
            ]
            pre = validate_entities(self.data_dir, kept, load_issues)
            if not pre.ok:
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, issues=pre.errors,
                    error="删除后数据集不再合法（可能触发其它校验规则），已阻止",
                )

            backup = FileBackup()
            backup.stash(path)
            try:
                yaml_writer.remove_entity(path, entity_id)
                backup.note_written(path)
            except (OSError, ValueError) as exc:
                backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id,
                    error=f"删除 YAML 记录失败：{exc}", rolled_back=True,
                )

            self.invalidate()
            try:
                build = self._build_atomic()
            except DataValidationError as exc:
                skipped = backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, issues=exc.result.errors,
                    error=_rollback_note("删除后校验失败，已回滚", skipped), rolled_back=True,
                )
            except Exception as exc:  # pragma: no cover
                skipped = backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id,
                    error=_rollback_note(f"重建数据库失败，已回滚：{exc}", skipped), rolled_back=True,
                )

            return SaveOutcome(
                ok=True, kind=kind, entity_id=entity_id, path=str(path),
                build=build, warnings=list(build.warnings),
            )

    def rebuild(self) -> SaveOutcome:
        """手动把当前 YAML 全量重建为 SQLite。"""
        with self._lock:
            self.invalidate()
            try:
                build = self._build_atomic()
            except DataValidationError as exc:
                return SaveOutcome(
                    ok=False, kind="", error="数据校验未通过，未生成数据库",
                    issues=exc.result.errors,
                )
            except Exception as exc:  # pragma: no cover
                return SaveOutcome(ok=False, kind="", error=f"重建数据库失败：{exc}")
            return SaveOutcome(ok=True, kind="", build=build, warnings=list(build.warnings))

    # ------------------------------------------------------------ 数据库状态

    def db_state(self) -> dict:
        """YAML 与生成物是否同步（页面顶部横幅用）。"""
        snapshot = self.snapshot()  # 顺带保证指纹是最新的
        state: dict[str, Any] = {
            "path": str(self.db_path),
            "exists": self.db_path.exists(),
            "stale": True,
            "built_at": "",
            "git": "",
            "meta": {},
            "counts": {},
        }
        if not state["exists"]:
            return state
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
            finally:
                conn.close()
        except sqlite3.Error as exc:  # pragma: no cover
            state["error"] = str(exc)
            return state

        state["meta"] = meta
        state["built_at"] = meta.get("built_at", "")
        state["counts"] = {k: int(v) for k, v in meta.items() if k.startswith("count.")}
        try:
            # 内容指纹会读全部 YAML，按当前文件指纹缓存一次即可
            if self._digest_fingerprint != self._fingerprint:
                self._digest = manifest_digest(self.data_dir)
                self._digest_fingerprint = self._fingerprint
            state["stale"] = meta.get("yaml_manifest_sha256") != self._digest
        except OSError:  # pragma: no cover
            state["stale"] = True
        return state

    def file_text(self, kind: str, entity_id: str) -> str:
        path = self.file_path(kind, entity_id)
        if path is None or not path.exists():
            return ""
        return path.read_text(encoding="utf-8")


def _group_issues(issues: list[Issue]) -> dict[str, list[Issue]]:
    grouped: dict[str, list[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.entity_id or "", []).append(issue)
    return grouped


# ---------------------------------------------------------------- 内部 ID 生成


def _slug_token(name: str) -> str:
    """把名称变成一个合法的 id 片段：ASCII 取字母数字，中文等用稳定短哈希。

    ``ascii_part`` 必须**至少 2 个字符且含字母**才算有意义：像「群狼环鼎 2026」
    这种名字转写出来只剩 ``2026``，用它当 id 反而比哈希更难认。
    """
    ascii_part = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if len(ascii_part) >= 2 and re.search(r"[a-z]", ascii_part):
        return ascii_part[:40]
    digest = hashlib.sha1((name or "").strip().encode("utf-8")).hexdigest()[:8]
    return f"n{digest}"


def make_entity_id(prefix: str, name: str, existing: set[str]) -> str:
    """由名称生成**唯一合法**内部 id（普通录入者不需要关心 id）。

    中文名没法直接音译，就退回“稳定短哈希”，保证同样的名字总得到同样的 id 片段，
    冲突时再追加 ``_2`` / ``_3`` …。
    """
    token = _slug_token(name)
    candidate = f"{prefix}{token}"
    if len(candidate) < 2:  # pragma: no cover - 前缀保证了长度
        candidate = f"{prefix}n"
    index = 2
    while candidate in existing:
        candidate = f"{prefix}{token}_{index}"
        index += 1
    return candidate


def slug_card_number(number: str | None) -> str:
    """卡面编号 → id / 卡图名的片段。

    规则：小写，**非字母数字一律换成下划线**，首尾下划线去掉。
    例：``G.WEI 001`` → ``g_wei_001``；``G.WEI/SHU 079`` → ``g_wei_shu_079``；
    ``G-JIN 047`` → ``g_jin_047``（连字符与点号结果一致）。

    卡面编号在现有数据里**唯一**，所以它同时决定：
    武将版本的内部 id（``variant_<slug>``）与卡图文件名（``<slug>.jpg``）。
    """
    return re.sub(r"[^a-z0-9]+", "_", (number or "").lower()).strip("_")


def make_variant_id(number: str | None, existing: set[str]) -> str:
    """由卡面编号生成武将版本的内部 id：``variant_<slug>``。

    编号为空时退回 ``variant_n<hash>``，保证仍然能建记录。
    """
    slug = slug_card_number(number)
    if not slug:
        token = hashlib.sha1((number or "").encode("utf-8")).hexdigest()[:8]
        slug = f"n{token}"
    candidate = f"variant_{slug}"
    if len(candidate) < 2:  # pragma: no cover
        candidate = "variant_n"
    index = 2
    while candidate in existing:
        candidate = f"variant_{slug}_{index}"
        index += 1
    return candidate


def make_version_id(skill_id: str, mode: str | None, existing: set[str]) -> str:
    """生成 SkillVersion 的 id：``<skill_id>_<mode>_vN``，N 取第一个没被占用的序号。"""
    base = f"{skill_id}_{mode or 'common'}"
    index = 1
    while f"{base}_v{index}" in existing:
        index += 1
    return f"{base}_v{index}"


def _rollback_note(message: str, skipped: list[Path]) -> str:
    """回滚结果说明；如果某些文件因为被外部改动而没有回滚，必须说清楚。"""
    if not skipped:
        return message
    names = "、".join(str(path) for path in skipped)
    return (
        f"{message}；但 {names} 在本次写入之后又被外部改动过，为避免覆盖别人的修改，"
        "这些文件**没有回滚**，请手工检查内容"
    )


def _iter_yaml(data_dir: Path):
    """按目录约定枚举 data/ 下的 YAML 文件（复用 loader 的规则）。"""
    from ..loader import iter_yaml_files

    return iter_yaml_files(Path(data_dir))
