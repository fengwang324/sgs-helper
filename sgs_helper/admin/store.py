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
import sqlite3
import threading
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
from ..validate import REFERENCE_FIELDS, ValidationResult, validate_data_dir, validate_entities
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

    def format_issues(self) -> str:
        return "\n".join(issue.format() for issue in self.issues)


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

    def __init__(self, data_dir: Path, db_path: Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path)
        self.repo_dir = self.data_dir.parent
        self._lock = threading.RLock()
        self._snapshot: ValidationResult | None = None
        self._fingerprint: str = ""
        self._index: dict[str, dict[str, tuple[str, str, str]]] = {}

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
        """返回当前 YAML 的校验结果（含 bundle）。文件一变就自动重新装载。"""
        with self._lock:
            fingerprint = self._fingerprint_now()
            if force or self._snapshot is None or fingerprint != self._fingerprint:
                self._snapshot = validate_data_dir(self.data_dir)
                self._fingerprint = fingerprint
                self._index = {}
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None
            self._fingerprint = ""
            self._index = {}

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
                getattr(entity, "series", ""),
                getattr(entity, "publisher", ""),
                str(getattr(entity, "release_year", "") or ""),
                getattr(entity, "edition", ""),
            ]
            return " · ".join(b for b in bits if b)
        if kind == "general":
            bits = [
                getattr(entity, "faction", "") or "",
                f"{getattr(entity, 'hp', '')} 体力",
                f"包：{entity.package}" if getattr(entity, "package", None) else "",
            ]
            skills = [
                self.display_name("skill", bundle.get("skill", sid))
                for sid in getattr(entity, "skill_ids", [])
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
        """列表页筛选：q / mode / verification_status / product_id / package。"""
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
            if package and getattr(entity, "package", None) != package:
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
            package = getattr(entity, "package", None)
            if package:
                packages[package] = packages.get(package, 0) + 1

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

    # ------------------------------------------------------------ 反向引用

    def reverse_refs(self, kind: str, entity_id: str) -> list[dict]:
        """谁在引用这条记录？（删除前的安全检查）"""
        bundle = self.bundle
        out: list[dict] = []
        for src_kind, field_name, target_kind, is_list in REFERENCE_FIELDS:
            if target_kind != kind:
                continue
            for src_id, src_entity in bundle.all(src_kind).items():
                value = getattr(src_entity, field_name, None)
                hit = False
                if is_list:
                    hit = bool(value) and entity_id in value
                else:
                    hit = value == entity_id
                if hit:
                    out.append(
                        {
                            "kind": src_kind,
                            "kind_label": KIND_BY_NAME[src_kind].label,
                            "id": src_id,
                            "name": self.display_name(src_kind, src_entity),
                            "field": field_name,
                        }
                    )
        out.sort(key=lambda r: (r["kind"], r["id"], r["field"]))
        return out

    def outgoing_refs(self, kind: str, entity: Any) -> dict[str, list[dict]]:
        """这条记录自己引用了谁（详情页把字段渲染成可点击链接）。"""
        out: dict[str, list[dict]] = {}
        for src_kind, field_name, target_kind, is_list in REFERENCE_FIELDS:
            if src_kind != kind:
                continue
            value = getattr(entity, field_name, None)
            items = list(value) if (is_list and value) else ([] if is_list else ([value] if value else []))
            if not items:
                continue
            out[field_name] = [
                {
                    "kind": target_kind,
                    "id": ref,
                    "name": self.name_of(target_kind, ref),
                    "exists": self.entity(target_kind, ref) is not None,
                }
                for ref in items
            ]
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
                "武将": sum(1 for g in bundle.all("general").values() if g.product_id == entity_id),
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
        skills = []
        for skill_id in entity.skill_ids:
            skill = bundle.get("skill", skill_id)
            versions = [
                v
                for v in bundle.all("skill_version").values()
                if v.skill_id == skill_id and v.mode in (entity.mode, "common")
            ]
            versions.sort(key=lambda v: (mode_sort_key(v.mode), v.effective_from or date.min, v.id))
            payloads = [v.model_dump(mode="json") for v in versions]
            for payload in payloads:
                payload["status"] = _status(payload, as_of)
            current, ambiguous = pick_current(payloads, entity.mode, as_of)
            skills.append(
                {
                    "skill_id": skill_id,
                    "skill": skill,
                    "versions": [
                        {"entity": v, "status": _status(v.model_dump(mode="json"), as_of)}
                        for v in versions
                    ],
                    "current": current,
                    "current_entity": next(
                        (v for v in versions if current and v.id == current["id"]), None
                    ),
                    "ambiguous_ids": [v["id"] for v in ambiguous],
                }
            )
        return {
            "kind": "general",
            "general": entity,
            "product": bundle.get("product", entity.product_id) if entity.product_id else None,
            "skills": skills,
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
        generals = [g for g in bundle.all("general").values() if g.product_id == product_id]
        printings = [p for p in bundle.all("card_printing").values() if p.product_id == product_id]
        versions = [v for v in bundle.all("skill_version").values() if v.product_id == product_id]
        return {
            "kind": "product",
            "product": entity,
            "generals": sorted(generals, key=lambda g: g.id),
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

    def _resolve_target_file(
        self, kind: str, entity_id: str, old_id: str | None
    ) -> tuple[Path, Path | None]:
        """返回 (写入目标文件, 需要清理的旧文件)。"""
        if old_id and old_id != entity_id:
            old_path = self.file_path(kind, old_id)
            return yaml_writer.entity_file_path(self.data_dir, kind, entity_id), old_path
        origin = self.origin(kind, old_id or entity_id)
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
        """校验 → 写 YAML → 重建 SQLite。任何一步失败都不会留下半成品。"""
        entity_id = str(candidate.get("id") or "")
        with self._lock:
            pre = self.validate_candidate(kind, candidate, old_id=old_id)
            if not pre.ok:
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, issues=pre.errors,
                    error="校验未通过：没有写入任何文件，数据库也没有变化",
                )

            entity = pre.bundle.get(kind, entity_id)
            if entity is None:  # pragma: no cover - 预校验通过时不可能发生
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id,
                    error="内部错误：候选实体没有被校验器接受",
                )

            target, stale_path = self._resolve_target_file(kind, entity_id, old_id)
            check_path = stale_path if (stale_path is not None and stale_path != target) else target
            check_id = old_id or entity_id
            conflict = self._concurrency_check(kind, check_path, check_id, expected_sha)
            if conflict:
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, conflict=True, error=conflict
                )

            backup = FileBackup()
            backup.stash(target)
            if stale_path is not None and stale_path != target:
                backup.stash(stale_path)

            try:
                yaml_writer.upsert_entity(target, kind, entity, old_id=old_id)
                backup.note_written(target)
                if stale_path is not None and stale_path != target:
                    yaml_writer.remove_entity(stale_path, old_id or "")
                    backup.note_written(stale_path)
            except (OSError, ValueError) as exc:
                backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id,
                    error=f"写入 YAML 失败：{exc}", rolled_back=True,
                )

            self.invalidate()
            try:
                build = self._build_atomic()
            except DataValidationError as exc:
                skipped = backup.rollback()
                self.invalidate()
                return SaveOutcome(
                    ok=False, kind=kind, entity_id=entity_id, issues=exc.result.errors,
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
                ok=True, kind=kind, entity_id=entity_id, path=str(target),
                build=build, warnings=list(build.warnings),
                issues=[],
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
            digest = manifest_digest(self.data_dir)
            state["stale"] = meta.get("yaml_manifest_sha256") != digest
        except OSError:  # pragma: no cover
            state["stale"] = True
        return state

    def file_text(self, kind: str, entity_id: str) -> str:
        path = self.file_path(kind, entity_id)
        if path is None or not path.exists():
            return ""
        return path.read_text(encoding="utf-8")


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
