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
    SUIT_LABELS,
    VERIFICATION_LABELS,
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

#: 待核验页面关心的三种状态
REVIEW_STATUSES: tuple[str, ...] = ("unverified", "community", "conflict")

#: 新模型里**自带** ``verification_status`` 的实体
#: （Product / General / Skill / Card 没有核验状态）
STATUS_KINDS: tuple[str, ...] = (
    "general_variant",
    "skill_version",
    "card_printing",
    "faq",
    "keyword",
    "timing",
)

#: 正文里自动识别的实体类型（与 repo.mentions 的默认值一致）
MENTION_KINDS: tuple[str, ...] = ("general", "skill", "card", "keyword", "timing")


# ---------------------------------------------------------------- 展示辅助


def rank_label(rank: int | None) -> str:
    """1..13 -> A/2..10/J/Q/K。"""
    if rank is None:
        return ""
    mapping = {1: "A", 11: "J", 12: "Q", 13: "K"}
    return mapping.get(rank, str(rank))


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
        """
        with self._lock:
            now = time.monotonic()
            needs_check = (
                force
                or self._snapshot is None
                or (now - self._fingerprint_at) > self.FINGERPRINT_TTL
            )
            if not needs_check:
                return self._snapshot  # type: ignore[return-value]

            fingerprint = self._fingerprint_now()
            self._fingerprint_at = now
            if force or self._snapshot is None or fingerprint != self._fingerprint:
                self._snapshot = validate_data_dir(self.data_dir)
                self._fingerprint = fingerprint
                self._index = {}
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None
            self._fingerprint = ""
            self._fingerprint_at = 0.0
            self._index = {}
            self._digest = ""
            self._digest_fingerprint = ""

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
        if kind == "card_printing":
            return getattr(entity, "name_printed", "") or entity.id
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
            from ..constants import CARD_TYPE_LABELS

            count = sum(
                1 for p in bundle.all("card_printing").values() if p.card_id == entity.id
            )
            bits = [CARD_TYPE_LABELS.get(getattr(entity, "card_type", ""), ""), getattr(entity, "subtype", "")]
            return " · ".join(b for b in bits if b) + f"　{count} 种印刷"
        if kind == "card_printing":
            product = self.display_name("product", bundle.get("product", entity.product_id))
            suit = SUIT_LABELS.get(getattr(entity, "suit", ""), getattr(entity, "suit", ""))
            rank = rank_label(getattr(entity, "rank", None))
            mark = getattr(entity, "bottom_right_mark", "") or ""
            cs = getattr(entity, "card_set", "") or ""
            mode = MODE_LABELS.get(self.mode_of(kind, entity) or "", self.mode_of(kind, entity) or "")
            bits = [product, mode, f"{suit}{rank}", f"牌堆 {cs}" if cs else "",
                    f"角标 {mark}" if mark else ""]
            return " · ".join(b for b in bits if b)
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

    #: 核验状态的优先级（合成「武将」这一层的状态时取最保守的那个）
    STATUS_ORDER = ("unverified", "conflict", "community", "official", "physical_verified")

    def set_display(self, name: str | None) -> str:
        """武将牌系列的展示文本：系列层级（GeneralSet 实体）已删除，名字本身就是全部信息。"""
        return name or ""

    def status_of(self, kind: str, entity: Any) -> str:
        """核验状态的来源。

        - 自带 ``verification_status`` 的实体用自己的值；
        - ``General`` / ``Skill`` / ``SkillVersion`` 自己没有卡：状态跟着**引用它的武将版本**走，
          于是「武将核验过 => 它的技能也算核验」，武将列表与版本列表显示一致；
        - ``Product`` / ``Card`` 没有核验状态，返回空串（不进待核验清单）。
        """
        own = getattr(entity, "verification_status", None)
        bundle = self.bundle
        order = {name: i for i, name in enumerate(self.STATUS_ORDER)}
        if kind in ("skill", "skill_version"):
            refs = []
            for v in bundle.all("general_variant").values():
                for ref in (v.skills or []):
                    hit = (ref.version_id == entity.id if kind == "skill_version"
                           else ref.skill_id == entity.id)
                    if hit:
                        refs.append(v)
                        break
            if not refs:
                return own or "unverified"
            return min((getattr(v, "verification_status", None) or "unverified" for v in refs),
                       key=lambda x: order.get(x, 99))
        if kind == "general":
            statuses = [
                getattr(v, "verification_status", None) or "unverified"
                for v in bundle.all("general_variant").values()
                if v.general_id == entity.id
            ]
            if not statuses:
                return own or "unverified"
            return min(statuses, key=lambda x: order.get(x, 99))
        if kind not in STATUS_KINDS:
            return ""
        return own or "unverified"

    def mode_of(self, kind: str, entity: Any) -> str | None:
        """实体的模式。

        ``CardPrinting`` 已经没有 ``mode`` 字段：需要时一律由 ``product_id -> Product.mode`` 推导。
        """
        if kind == "card_printing":
            product = self.bundle.get("product", getattr(entity, "product_id", None))
            return getattr(product, "mode", None) if product is not None else None
        return getattr(entity, "mode", None)

    def describe(self, kind: str, entity: Any) -> dict:
        """列表/搜索结果的一行。"""
        origin = self.origin(kind, entity.id)
        status = self.status_of(kind, entity)
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
            "mode": mode,
            "mode_label": MODE_LABELS.get(mode, mode) if mode else "",
            "verification_status": status,
            "status_label": VERIFICATION_LABELS.get(status, status) if status else "",
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
        status: str = "",
        product: str = "",
        package: str = "",
        faction: str = "",
    ) -> list[dict]:
        """列表页筛选：q / mode / verification_status / product_id / 系列。

        ``package`` 在武将（版本）上表示「武将牌系列」（``general_set``），
        在实体卡上表示「牌堆 / 套牌」（``card_set``）。
        ``CardPrinting`` 已经没有 ``mode`` 字段：mode 一律由产品推导后再比较。
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
            if status and self.status_of(kind, entity) != status:
                continue
            if product and getattr(entity, "product_id", None) != product:
                continue
            if package:
                if kind in {"general", "general_variant"}:
                    if getattr(entity, "general_set", None) != package:
                        continue
                elif kind == "card_printing":
                    if getattr(entity, "card_set", None) != package:
                        continue
                else:
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
        else:
            rows.sort(key=lambda r: (r["name"], r["id"]))
        return rows

    def filter_options(self, kind: str) -> dict[str, list[dict]]:
        """当前数据里实际出现过的筛选值，避免下拉框给一堆空选项。"""
        bundle = self.bundle
        modes: dict[str, int] = {}
        statuses: dict[str, int] = {}
        products: dict[str, int] = {}
        packages: dict[str, int] = {}
        for _eid, entity in bundle.all(kind).items():
            mode = self.mode_of(kind, entity)
            if mode:
                modes[mode] = modes.get(mode, 0) + 1
            status = self.status_of(kind, entity)
            if status:
                statuses[status] = statuses.get(status, 0) + 1
            product = getattr(entity, "product_id", None)
            if product:
                products[product] = products.get(product, 0) + 1
            if kind in {"general", "general_variant"}:
                values = [getattr(entity, "general_set", None)]
            elif kind == "card_printing":
                values = [getattr(entity, "card_set", None)]
            else:
                values = []
            for value in values:
                if value:
                    packages[value] = packages.get(value, 0) + 1

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
            "statuses": options(statuses, lambda k: VERIFICATION_LABELS.get(k, k)),
            "products": options(products, lambda k: self.name_of("product", k)),
            "packages": options(packages, lambda k: k),
            "factions": options(factions, lambda k: k),
            "package_label": (
                "武将牌系列" if kind in {"general", "general_variant"}
                else "牌堆" if kind == "card_printing"
                else "包/堆"
            ),
        }

    def review_groups(self) -> list[dict]:
        """``/review``：按 verification_status 汇总需要核验的记录。"""
        bundle = self.bundle
        groups: list[dict] = []
        for status in REVIEW_STATUSES:
            rows: list[dict] = []
            for kind in KIND_ORDER:
                for _eid, entity in bundle.all(kind).items():
                    if self.status_of(kind, entity) == status:
                        rows.append(self.describe(kind, entity))
            rows.sort(key=lambda r: (KIND_ORDER.index(r["kind"]), r["name"], r["id"]))
            groups.append(
                {
                    "status": status,
                    "label": VERIFICATION_LABELS.get(status, status),
                    "rows": rows,
                    "count": len(rows),
                }
            )
        return groups

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
                    "verification_status": version.verification_status,
                    "review_note": version.review_note,
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
        """势力 / 武将牌系列这类“自由文本但希望复用已有值”的字段的候选值。

        武将牌系列的**权威来源是 Product.general_sets**：产品里声明过的系列都能选到，
        另外把已经在武将版本上使用的值也一并列出。
        """
        text = (query or "").strip()
        counter: dict[str, int] = {}
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
            counts = {
                "武将版本": sum(
                    1 for v in bundle.all("general_variant").values() if v.product_id == entity_id
                ),
                "实体卡": sum(1 for p in bundle.all("card_printing").values() if p.product_id == entity_id),
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

    def card_view(self, card_id: str) -> dict | None:
        entity = self.entity("card", card_id)
        if entity is None:
            return None
        bundle = self.bundle
        printings = [p for p in bundle.all("card_printing").values() if p.card_id == card_id]
        printings.sort(key=lambda p: (p.product_id, p.card_set or "", p.suit, p.rank or 0, p.id))
        rows = []
        for printing in printings:
            rows.append(
                {
                    "entity": printing,
                    "product": bundle.get("product", printing.product_id),
                    "product_name": self.name_of("product", printing.product_id),
                    "suit_label": SUIT_LABELS.get(printing.suit, printing.suit),
                    "rank_label": rank_label(printing.rank),
                    # CardPrinting 自己没有 mode：一律由产品推导
                    "mode": self.mode_of("card_printing", printing),
                }
            )
        return {"kind": "card", "card": entity, "printings": rows}

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
        printings = [p for p in bundle.all("card_printing").values() if p.product_id == product_id]
        versions = [v for v in bundle.all("skill_version").values() if v.product_id == product_id]
        return {
            "kind": "product",
            "product": entity,
            "variants": sorted(variants, key=lambda v: (v.general_id, v.variant, v.id)),
            "printings": sorted(printings, key=lambda p: (p.card_set or "", p.suit, p.rank or 0, p.id)),
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
                written=written,
            )

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
