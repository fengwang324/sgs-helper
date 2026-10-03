"""管理端的数据访问层：**直接面向 YAML 权威源**，SQLite 只在保存成功后重建。

为什么读也走 YAML 而不是 SQLite：

- YAML 是唯一权威源，永远是最新的；SQLite 可能落后（比如刚手工改过 YAML 没重建）；
- 管理端“看到的”和“编辑的”必须是同一份数据，否则会出现看着旧值改新值的事故。

写入路径严格固定为：

    浏览器表单 → 候选实体（内存） → 预校验（模型 + 交叉引用 + 一致性）
      ├─ 失败：一个字都不落盘，返回 文件/字段/原因
      └─ 通过：写 YAML → build_database() 全量重建 SQLite
                 └─ 若重建意外失败：用备份内容回滚 YAML，磁盘不残留半成品

搜索复用 ``db._search_row`` 与 ``repo._score``，不另写一套匹配规则。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
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
from ..db import BuildResult, DataValidationError, build_database, _search_row
from ..loader import (
    DataBundle,
    Issue,
    Origin,
    RawEntity,
    load_data_dir,
    manifest_digest,
)
from ..repo import _score, pick_current, _status
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


# ---------------------------------------------------------------- 展示辅助


def rank_label(rank: int | None) -> str:
    """1..13 -> A/2..10/J/Q/K。"""
    if rank is None:
        return ""
    mapping = {1: "A", 11: "J", 12: "Q", 13: "K"}
    return mapping.get(rank, str(rank))


def source_dict(entity: Any) -> dict | None:
    source = getattr(entity, "source", None)
    if source is None:
        return None
    return {
        "type": getattr(source, "type", ""),
        "type_label": SOURCE_LABELS.get(str(getattr(source, "type", "")), str(getattr(source, "type", ""))),
        "title": getattr(source, "title", "") or "",
        "reference": getattr(source, "reference", "") or "",
        "notes": getattr(source, "notes", "") or "",
    }


def truncate(text: str, limit: int = 60) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


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
        self._index: dict[str, dict[str, tuple[str, str, str]]] = {}
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
                getattr(entity, "publisher", ""),
            ]
            return " · ".join(b for b in bits if b)
        if kind == "general":
            variants = [
                v for v in bundle.all("general_variant").values()
                if v.general_id == entity.id
            ]
            bits = []
            if getattr(entity, "aliases", None):
                bits.append("别名：" + "、".join(entity.aliases))
            bits.append(f"{len(variants)} 个版本")
            if variants:
                bits.append("、".join(sorted(v.variant for v in variants)))
            return " · ".join(bits)
        if kind == "general_variant":
            bits = [
                getattr(entity, "faction", "") or "",
                f"{entity.hp} 体力" if getattr(entity, "hp", None) is not None else "",
                getattr(entity, "title", "") or "",
                f"系列：{entity.general_set}" if getattr(entity, "general_set", None) else "",
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
            start = getattr(entity, "effective_from", None) or "不限"
            end = getattr(entity, "effective_to", None) or "至今"
            label = getattr(entity, "version_label", "") or ""
            return f"{mode} · {label} · {start} ~ {end}".strip(" ·")
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
            bits = [product, f"{suit}{rank}", f"角标 {mark}" if mark else ""]
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

    def _product_id_of_variant(self, variant: Any) -> str | None:
        return getattr(variant, "product_id", None)

    def describe(self, kind: str, entity: Any) -> dict:
        """列表/搜索结果的一行。"""
        origin = self.origin(kind, entity.id)
        status = getattr(entity, "verification_status", None)
        mode = getattr(entity, "mode", None)
        return {
            "kind": kind,
            "kind_label": KIND_BY_NAME[kind].label,
            "id": entity.id,
            "name": self.display_name(kind, entity),
            "subtitle": self.subtitle(kind, entity),
            "title": getattr(entity, "title", "") or "",
            "general_number": getattr(entity, "general_number", None),
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

    # ------------------------------------------------------------ 检索

    def _build_index(self) -> dict[str, dict[str, tuple[str, str, str]]]:
        if self._index:
            return self._index
        bundle = self.bundle
        index: dict[str, dict[str, tuple[str, str, str]]] = {}
        for kind in KIND_ORDER:
            rows: dict[str, tuple[str, str, str]] = {}
            for entity_id, entity in bundle.all(kind).items():
                rows[entity_id] = _search_row(bundle, kind, entity)
            index[kind] = rows
        self._index = index
        return index

    def search(
        self, query: str, kinds: Iterable[str] | None = None, limit: int = 50
    ) -> list[dict]:
        """跨类型检索。复用 ``repo._score`` 的排序规则，另外支持按 id 命中。"""
        text = (query or "").strip()
        if not text:
            return []
        index = self._build_index()
        kind_list = list(kinds) if kinds else list(KIND_ORDER)
        lowered = text.lower()

        scored: list[tuple] = []
        for kind in kind_list:
            for entity_id, (name, aliases, body) in index.get(kind, {}).items():
                score = _score(text, {"name": name, "aliases": aliases, "body": body})
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
    ) -> list[dict]:
        """列表页筛选：q / mode / verification_status / product_id / general_set（package）。

        ``package`` 现在只对武将表示「武将牌系列」（``general_set``）；
        card_printing / skill_version 仍然用它们自己的 ``package`` 字段。
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
            if mode and getattr(entity, "mode", None) != mode:
                continue
            if status and getattr(entity, "verification_status", None) != status:
                continue
            if product and getattr(entity, "product_id", None) != product:
                continue
            if package:
                if kind in {"general", "general_variant"}:
                    if getattr(entity, "general_set", None) != package:
                        continue
                elif getattr(entity, "package", None) != package:
                    continue
            rows.append(self.describe(kind, entity))

        if kind == "skill_version":
            rows.sort(key=lambda r: (self.name_of("skill", getattr(r["entity"], "skill_id", "")),
                                     mode_sort_key(getattr(r["entity"], "mode", "common")), r["id"]))
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
            mode = getattr(entity, "mode", None)
            if mode:
                modes[mode] = modes.get(mode, 0) + 1
            status = getattr(entity, "verification_status", None)
            if status:
                statuses[status] = statuses.get(status, 0) + 1
            product = getattr(entity, "product_id", None)
            if product:
                products[product] = products.get(product, 0) + 1
            if kind in {"general", "general_variant"}:
                value = getattr(entity, "general_set", None)
            else:
                value = getattr(entity, "package", None)
            if value:
                packages[value] = packages.get(value, 0) + 1

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
            "package_label": "武将牌系列" if kind in {"general", "general_variant"} else "包/堆",
        }

    def review_groups(self) -> list[dict]:
        """``/review``：按 verification_status 汇总需要核验的记录。"""
        bundle = self.bundle
        groups: list[dict] = []
        for status in REVIEW_STATUSES:
            rows: list[dict] = []
            for kind in KIND_ORDER:
                for _eid, entity in bundle.all(kind).items():
                    if getattr(entity, "verification_status", None) == status:
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
        """技能搜索式组合框：按 名称 / aliases / id 匹配，名称命中优先。"""
        text = (query or "").strip()
        bundle = self.bundle
        lowered = text.lower()
        scored: list[tuple] = []
        for skill_id, skill in bundle.all("skill").items():
            name = skill.name or ""
            aliases = list(skill.aliases or [])
            version_count = sum(
                1 for v in bundle.all("skill_version").values() if v.skill_id == skill_id
            )
            if not text:
                score = (5, 0)
            elif name == text:
                score = (0, 0)
            elif text in aliases:
                score = (1, 0)
            elif name.startswith(text):
                score = (2, 0)
            elif lowered in skill_id.lower():
                score = (3, 0)
            elif text in name:
                score = (4, 0)
            else:
                hit = next((a for a in aliases if text in a), None)
                if hit is None:
                    continue
                score = (5, 0)
            scored.append((score[0], name, skill_id, name, aliases, version_count))
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return [
            {
                "id": skill_id,
                "name": name,
                "aliases": aliases,
                "version_count": version_count,
                "exact": bool(text) and name == text,
            }
            for _score, _n, skill_id, name, aliases, version_count in scored[:limit]
        ]

    def skill_version_options(
        self, skill_id: str, *, mode: str | None = None, product_id: str | None = None
    ) -> list[dict]:
        """某技能的可选版本，按“产品匹配 > 模式匹配 > common > 其它”排序。

        每项带上足够人判断的信息（版本标签/模式/产品/生效时间/文本摘要/文本哈希），
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
                    "mode": version.mode,
                    "mode_label": MODE_LABELS.get(version.mode, version.mode),
                    "product_id": version.product_id,
                    "product_name": self.name_of("product", version.product_id),
                    "effective_from": version.effective_from.isoformat() if version.effective_from else "",
                    "effective_to": version.effective_to.isoformat() if version.effective_to else "",
                    "previous_version_id": version.previous_version_id,
                    "verification_status": version.verification_status,
                    "text": text,
                    "text_excerpt": truncate(text, 60),
                    "text_sha": hashlib.sha1(text.strip().encode("utf-8")).hexdigest(),
                    "rank": rank,
                    "same_product": bool(product_id and version.product_id == product_id),
                    "same_mode": bool(mode and version.mode == mode),
                }
            )
        rows.sort(key=lambda r: (r["rank"], r["effective_from"] or "", r["id"]))
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
        """势力 / 武将牌系列这类“自由文本但希望复用已有值”的字段的候选值。"""
        text = (query or "").strip()
        counter: dict[str, int] = {}
        # 势力 / 武将牌系列现在属于 GeneralVariant
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
                payload = version.model_dump(mode="json")
                status = _status(payload, date.today().isoformat())
                impact["status"] = status
                view = self.skill_view(version.skill_id) or {}
                for row in view.get("versions", []):
                    if row["id"] == entity_id:
                        impact["is_current"] = bool(row["current_for_modes"])
                        impact["current_for_modes"] = row["current_for_modes"]
                label = {"active": "当前生效中", "expired": "已过期", "future": "尚未生效"}.get(status, status)
                impact["lines"].append(f"该版本状态：{label}。")
                if impact["is_current"]:
                    impact["lines"].append(
                        "它当前仍是某些模式下推导出的“当前有效版本”，删除后这些模式会失去技能文本。"
                    )
                if not impact["is_current"] and not any(
                    v.previous_version_id == entity_id for v in self.bundle.all("skill_version").values()
                ):
                    impact["lines"].append("它既不是当前有效版本，也没有后继版本引用它，删除属于历史清理。")
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
        as_of = date.today().isoformat()
        versions = [
            v for v in bundle.all("skill_version").values() if v.skill_id == skill_id
        ]
        versions.sort(
            key=lambda v: (mode_sort_key(v.mode), v.effective_from or date.min, v.id)
        )
        as_dict = {v.id: v.model_dump(mode="json") for v in versions}
        for version_id, payload in as_dict.items():
            payload["status"] = _status(payload, as_of)
            payload["current_for_modes"] = []
            payload["ambiguous_for_modes"] = []

        modes_present = sorted({v.mode for v in versions}, key=mode_sort_key)
        mode_groups: list[dict] = []
        for mode in modes_present:
            current, ambiguous = pick_current(list(as_dict.values()), mode, as_of)
            if current is not None:
                as_dict[current["id"]]["current_for_modes"].append(mode)
            for version in ambiguous:
                as_dict[version["id"]]["ambiguous_for_modes"].append(mode)
            mode_groups.append(
                {
                    "mode": mode,
                    "label": MODE_LABELS.get(mode, mode),
                    "current_id": current["id"] if current else None,
                    "current_mode": current["mode"] if current else None,
                    "ambiguous_ids": [v["id"] for v in ambiguous],
                }
            )

        rows = []
        for version in versions:
            payload = as_dict[version.id]
            rows.append(
                {
                    "entity": version,
                    "id": version.id,
                    "status": payload["status"],
                    "current_for_modes": [MODE_LABELS.get(m, m) for m in payload["current_for_modes"]],
                    "ambiguous_for_modes": [MODE_LABELS.get(m, m) for m in payload["ambiguous_for_modes"]],
                    "previous_id": version.previous_version_id,
                    "next_ids": [
                        v.id for v in versions if v.previous_version_id == version.id
                    ],
                    "product_name": self.name_of("product", version.product_id),
                    "keyword_names": [self.name_of("keyword", k) for k in version.related_keywords],
                    "timing_names": [self.name_of("timing", t) for t in version.related_timing_ids],
                }
            )

        return {
            "kind": "skill",
            "skill": entity,
            "versions": rows,
            "modes": mode_groups,
            "chains": [
                [self.name_of("skill_version", vid) + f"（{vid}）" for vid in chain]
                for chain in _history_chains(list(as_dict.values()))
            ],
            "as_of": as_of,
        }

    def general_view(self, general_id: str) -> dict | None:
        entity = self.entity("general", general_id)
        if entity is None:
            return None
        bundle = self.bundle
        as_of = date.today().isoformat()

        variants = []
        for variant in sorted(
            (v for v in bundle.all("general_variant").values() if v.general_id == general_id),
            key=lambda v: (v.variant, v.id),
        ):
            mode = variant.mode
            payloads: dict[str, dict] = {}
            skills = []
            for position, ref in enumerate(variant.skills):
                versions = [
                    v
                    for v in bundle.all("skill_version").values()
                    if v.skill_id == ref.skill_id
                ]
                versions.sort(
                    key=lambda v: (mode_sort_key(v.mode), v.effective_from or date.min, v.id)
                )
                for version in versions:
                    if version.id not in payloads:
                        payload = version.model_dump(mode="json")
                        payload["status"] = _status(payload, as_of)
                        payloads[version.id] = payload

                candidates = [
                    payloads[v.id]
                    for v in versions
                    if payloads[v.id]["mode"] in (mode, "common")
                ]
                pinned = payloads.get(ref.version_id) if ref.version_id else None
                derived, ambiguous = (
                    pick_current(candidates, mode, as_of) if candidates else (None, [])
                )
                skills.append(
                    {
                        "position": position,
                        "skill_id": ref.skill_id,
                        "skill": bundle.get("skill", ref.skill_id),
                        "version_id": ref.version_id,
                        "pinned": pinned,
                        "derived": derived,
                        "current": pinned or derived,
                        "pinned_missing": bool(ref.version_id and pinned is None),
                        "all_versions": [payloads[v.id] for v in versions],
                        "ambiguous_ids": [p["id"] for p in ambiguous],
                    }
                )
            variants.append(
                {
                    "entity": variant,
                    "id": variant.id,
                    "variant": variant.variant,
                    "skills": skills,
                    "product_name": self.name_of("product", variant.product_id),
                    "source": source_dict(variant),
                    "edit_url": f"/edit/general/{general_id}?variant={variant.id}",
                }
            )

        return {
            "kind": "general",
            "general": entity,
            "variants": variants,
            "as_of": as_of,
        }

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
                }
            )
        return {
            "kind": "card",
            "card": entity,
            "printings": rows,
            "keywords": [
                bundle.get("keyword", k) for k in entity.related_keywords if bundle.get("keyword", k)
            ],
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
        printings = [p for p in bundle.all("card_printing").values() if p.product_id == product_id]
        versions = [v for v in bundle.all("skill_version").values() if v.product_id == product_id]
        return {
            "kind": "product",
            "product": entity,
            "variants": sorted(variants, key=lambda v: (v.general_id, v.variant, v.id)),
            "printings": sorted(printings, key=lambda p: (p.card_set or "", p.suit, p.rank or 0, p.id)),
            "skill_versions": sorted(versions, key=lambda v: (v.skill_id, mode_sort_key(v.mode), v.id)),
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


def _history_chains(versions: list[dict]) -> list[list[str]]:
    from ..repo import _history_chains as impl

    return impl(versions)
