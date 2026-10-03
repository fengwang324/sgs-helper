"""查询层：只读 SQLite 生成物，不读 YAML。

这一层的函数全部返回纯 ``dict`` / ``list``，所以：

- CLI（``scripts/query.py``）直接渲染；
- 以后接微信小程序时，FastAPI 路由可以原样复用它，不用改数据层。

“当前有效版本”的推导规则（不使用易忘的 current: true）::

    1. 只看 skill_id 相同、mode 为「请求模式」或「common」的版本；
    2. 生效区间按日期闭区间判断：effective_from <= as_of <= effective_to，
       未填写的一端视为无穷；
    3. 具体模式优先于 common；
    4. 再按 effective_from 取最晚；
    5. 若仍有多个并列，则判定为“有歧义”，全部列出并提示，不静默挑一个。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    KIND_BY_NAME,
    KIND_ORDER,
    MODE_LABELS,
    MODE_SPECIFICITY,
    mode_sort_key,
)
from .db import JSON_COLUMNS, fts_query

MIN_DATE = "0001-01-01"
MAX_DATE = "9999-12-31"


class DatabaseMissing(FileNotFoundError):
    """数据库还没构建。"""


# ---------------------------------------------------------------- 连接


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    if not db_path.exists():
        raise DatabaseMissing(
            f"数据库不存在：{db_path}\n请先执行：python scripts/build_db.py"
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    # 用下标取值，兼容未设置 row_factory 的普通连接
    return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}


def row_to_dict(kind: str, row: sqlite3.Row) -> dict:
    data = dict(row)
    for column in JSON_COLUMNS:
        if column in data and isinstance(data[column], str):
            try:
                data[column] = json.loads(data[column] or "[]")
            except json.JSONDecodeError:
                data[column] = []
    data["kind"] = kind
    data["yaml_file"] = data.get("yaml_file")
    data["yaml_line"] = data.get("yaml_line")
    return data


def fetch(conn: sqlite3.Connection, kind: str, entity_id: str | None) -> dict | None:
    if not entity_id:
        return None
    table = KIND_BY_NAME[kind].table
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return None
    return with_source(conn, row_to_dict(kind, row))


def source_of(conn: sqlite3.Connection, key: str | None) -> dict | None:
    """按 source_key 取回去重后的来源记录。"""
    if not key:
        return None
    row = conn.execute(
        "SELECT type, title, reference, notes FROM sources WHERE source_key = ?", (key,)
    ).fetchone()
    return dict(row) if row is not None else None


def with_source(conn: sqlite3.Connection, data: dict) -> dict:
    data["source"] = source_of(conn, data.get("source_key"))
    return data


def _fetch_all(conn: sqlite3.Connection, kind: str, sql: str, params: Iterable = ()) -> list[dict]:
    return [
        with_source(conn, row_to_dict(kind, row)) for row in conn.execute(sql, tuple(params))
    ]


# ---------------------------------------------------------------- 检索


def _aliases_of(row) -> list[str]:
    return [a.strip() for a in (row["aliases"] or "").split(" | ") if a.strip()]


def _score(query: str, row) -> int | None:
    name = row["name"] or ""
    aliases = _aliases_of(row)
    body = row["body"] or ""
    if name == query:
        return 0
    if query in aliases:
        return 1
    if name.startswith(query):
        return 2
    if query in name:
        return 3
    if any(query in alias for alias in aliases):
        return 4
    if query in body:
        return 5
    return None


def _kind_rank(kind: str) -> int:
    try:
        return KIND_ORDER.index(kind)
    except ValueError:  # pragma: no cover
        return len(KIND_ORDER)


def search(
    conn: sqlite3.Connection,
    query: str,
    kinds: Iterable[str] | None = None,
    limit: int = 25,
) -> list[dict]:
    """简单可靠的检索：精确名称 > 别名 > 名称模糊 > 正文；FTS5 作为兜底。"""
    text = (query or "").strip()
    if not text:
        return []
    kind_list = list(kinds) if kinds else list(KIND_ORDER)
    placeholders = ", ".join("?" for _ in kind_list)
    like = f"%{text}%"
    sql = (
        "SELECT kind, entity_id, name, aliases, body FROM search_index "
        f"WHERE kind IN ({placeholders}) AND (name LIKE ? OR aliases LIKE ? OR body LIKE ?)"
    )
    rows = list(conn.execute(sql, [*kind_list, like, like, like]).fetchall())

    from_fts = False
    if not rows:
        rows = _fts_search(conn, text, kind_list)
        from_fts = True

    scored: list[tuple] = []
    for row in rows:
        score = _score(text, row)
        if score is None:
            if not from_fts:
                continue
            score = 6
        scored.append((score, _kind_rank(row["kind"]), row["name"] or "", row["kind"], row["entity_id"]))
    scored.sort(key=lambda item: item[:3])

    return [
        {"kind": kind, "id": entity_id, "name": name, "score": score}
        for score, _rank, name, kind, entity_id in scored[:limit]
    ]


def _fts_search(conn: sqlite3.Connection, text: str, kinds: list[str]) -> list[sqlite3.Row]:
    match = fts_query(text)
    if not match:
        return []
    try:
        hits = conn.execute(
            "SELECT kind, entity_id FROM fts_search WHERE fts_search MATCH ?", (match,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    rows = []
    for hit in hits:
        if hit["kind"] not in kinds:
            continue
        row = conn.execute(
            "SELECT kind, entity_id, name, aliases, body FROM search_index "
            "WHERE kind = ? AND entity_id = ?",
            (hit["kind"], hit["entity_id"]),
        ).fetchone()
        if row is not None:
            rows.append(row)
    return rows


def resolve(
    conn: sqlite3.Connection, kind: str, query: str, limit: int = 10
) -> tuple[dict | None, list[dict]]:
    """把用户输入解析成唯一实体。

    返回 ``(实体, 候选列表)``：命中唯一时候选为空；有多个可能时实体为 None。
    """
    text = (query or "").strip()
    if not text:
        return None, []

    table = KIND_BY_NAME[kind].table
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (text,)).fetchone()
    if row is not None:
        return row_to_dict(kind, row), []

    rows = list(
        conn.execute(
            "SELECT kind, entity_id, name, aliases, body FROM search_index WHERE kind = ?",
            (kind,),
        ).fetchall()
    )
    exact = [
        r
        for r in rows
        if (r["name"] or "") == text or text in _aliases_of(r)
    ]
    if len(exact) == 1:
        return fetch(conn, kind, exact[0]["entity_id"]), []
    if len(exact) > 1:
        return None, [
            {"kind": kind, "id": r["entity_id"], "name": r["name"], "score": 0} for r in exact
        ]

    fuzzy: list[tuple[int, str, str]] = []
    for r in rows:
        score = _score(text, r)
        if score is not None:
            fuzzy.append((score, r["name"] or "", r["entity_id"]))
    fuzzy.sort()
    candidates = [
        {"kind": kind, "id": entity_id, "name": name, "score": score}
        for score, name, entity_id in fuzzy[:limit]
    ]
    if len(candidates) == 1:
        return fetch(conn, kind, candidates[0]["id"]), []
    return None, candidates


# ---------------------------------------------------------------- 版本推导


def _active(version: dict, as_of: str) -> bool:
    low = version.get("effective_from") or MIN_DATE
    high = version.get("effective_to") or MAX_DATE
    return low <= as_of <= high


def pick_current(
    versions: list[dict], mode: str, as_of: str
) -> tuple[dict | None, list[dict]]:
    """推导某模式下的当前有效版本。返回 (唯一版本 | None, 并列版本列表)。"""
    candidates = [v for v in versions if v["mode"] in (mode, "common") and _active(v, as_of)]
    if not candidates:
        return None, []

    def rank(version: dict) -> tuple:
        return (
            MODE_SPECIFICITY.get(version["mode"], 0),
            version.get("effective_from") or MIN_DATE,
            version["id"],
        )

    best = max(rank(v) for v in candidates)
    winners = [v for v in candidates if rank(v) == best]
    if len(winners) == 1:
        return winners[0], []
    return None, winners


def _status(version: dict, as_of: str) -> str:
    if (version.get("effective_from") or MIN_DATE) > as_of:
        return "future"
    if (version.get("effective_to") or MAX_DATE) < as_of:
        return "expired"
    return "active"


def _history_chains(versions: list[dict]) -> list[list[str]]:
    by_id = {v["id"]: v for v in versions}
    referenced = {v["previous_version_id"] for v in versions if v.get("previous_version_id")}
    chain_starts = [v for v in versions if v["id"] not in referenced]
    chains: list[list[str]] = []
    for start in sorted(chain_starts, key=lambda v: (v["id"],)):
        chain: list[str] = []
        cursor: str | None = start["id"]
        while cursor and cursor in by_id and cursor not in chain:
            chain.append(cursor)
            cursor = by_id[cursor].get("previous_version_id")
        chains.append(chain)
    return [c for c in chains if len(c) > 1] + [c for c in chains if len(c) == 1]


def _versions_of_skill(conn: sqlite3.Connection, skill_id: str) -> list[dict]:
    return _fetch_all(
        conn,
        "skill_version",
        "SELECT * FROM skill_versions WHERE skill_id = ? "
        "ORDER BY mode, COALESCE(effective_from, '0001-01-01'), id",
        (skill_id,),
    )


def annotate_versions(
    conn: sqlite3.Connection, skill_id: str, modes: Iterable[str], as_of: dt.date
) -> list[dict]:
    """给某技能的全部版本打上状态与“在哪些模式下是当前有效版本”。"""
    as_of_iso = as_of.isoformat()
    versions = _versions_of_skill(conn, skill_id)
    for version in versions:
        version["status"] = _status(version, as_of_iso)
        version["current_for_modes"] = []
    for mode in modes:
        current, ambiguous = pick_current(versions, mode, as_of_iso)
        if current is not None:
            current["current_for_modes"].append(mode)
        for version in ambiguous:
            version.setdefault("ambiguous_for_modes", []).append(mode)
    return versions


# ---------------------------------------------------------------- 各类报告


def skill_report(
    conn: sqlite3.Connection, query: str, as_of: dt.date | None = None
) -> dict | None:
    entity, _candidates = resolve(conn, "skill", query)
    if entity is None:
        return None
    as_of = as_of or dt.date.today()
    as_of_iso = as_of.isoformat()

    versions = _fetch_all(
        conn,
        "skill_version",
        "SELECT * FROM skill_versions WHERE skill_id = ? "
        "ORDER BY mode, COALESCE(effective_from, '0001-01-01'), id",
        (entity["id"],),
    )
    modes_present = sorted(
        {v["mode"] for v in versions}, key=mode_sort_key
    )

    for version in versions:
        version["status"] = _status(version, as_of_iso)
        version["current_for_modes"] = []
        version["ambiguous_for_modes"] = []

    groups = []
    for mode in modes_present:
        current, ambiguous = pick_current(versions, mode, as_of_iso)
        if current is not None:
            current["current_for_modes"].append(mode)
        for version in ambiguous:
            version["ambiguous_for_modes"].append(mode)
        groups.append(
            {
                "mode": mode,
                "label": MODE_LABELS.get(mode, mode),
                "version_ids": [v["id"] for v in versions if v["mode"] == mode],
                "current_id": current["id"] if current else None,
                # 该模式的当前版本其实来自 common 时给出提示
                "current_mode": current["mode"] if current else None,
                "ambiguous_ids": [v["id"] for v in ambiguous],
            }
        )

    for version in versions:
        version["previous_version"] = fetch(conn, "skill_version", version.get("previous_version_id"))
        version["related_keyword_names"] = [
            (fetch(conn, "keyword", k) or {}).get("name", k) for k in version["related_keywords"]
        ]
        version["related_timing_names"] = [
            (fetch(conn, "timing", t) or {}).get("name", t)
            for t in version["related_timing_ids"]
        ]

    product_ids = {v["product_id"] for v in versions if v.get("product_id")}
    products = {pid: fetch(conn, "product", pid) for pid in product_ids}

    return {
        "kind": "skill",
        "definition": entity,
        "as_of": as_of_iso,
        "versions": versions,
        "modes": groups,
        "history_chains": _history_chains(versions),
        "products": products,
    }


def variant_skill_refs(conn: sqlite3.Connection, variant_id: str) -> list[dict]:
    """读某个武将版本的技能关系（含可选的具体版本绑定），顺序即卡面上的技能顺序。"""
    rows = conn.execute(
        "SELECT skill_id, version_id, position FROM variant_skills "
        "WHERE variant_id = ? ORDER BY position",
        (variant_id,),
    ).fetchall()
    return [{"skill_id": row[0], "version_id": row[1], "position": row[2]} for row in rows]


def general_variants(conn: sqlite3.Connection, general_id: str) -> list[dict]:
    """读某个武将个体的全部版本。"""
    return _fetch_all(
        conn,
        "general_variant",
        "SELECT * FROM general_variants WHERE general_id = ? ORDER BY variant, id",
        (general_id,),
    )


def _variant_skills(conn: sqlite3.Connection, variant: dict, as_of_iso: str) -> list[dict]:
    """给一个武将版本的技能打上“当前显示哪一版”的信息。"""
    mode = variant.get("mode")
    skills = []
    for ref in variant_skill_refs(conn, variant["id"]):
        skill_id = ref["skill_id"]
        pinned_id = ref["version_id"]
        skill = fetch(conn, "skill", skill_id)
        all_versions = _fetch_all(
            conn,
            "skill_version",
            "SELECT * FROM skill_versions WHERE skill_id = ? "
            "ORDER BY mode, COALESCE(effective_from, '0001-01-01'), id",
            (skill_id,),
        )
        for version in all_versions:
            version["status"] = _status(version, as_of_iso)

        candidates = [v for v in all_versions if v["mode"] in (mode, "common")]
        pinned = next((v for v in all_versions if v["id"] == pinned_id), None)
        derived, ambiguous = (
            pick_current(candidates, mode, as_of_iso) if candidates else (None, [])
        )
        skills.append(
            {
                "skill_id": skill_id,
                "skill": skill,
                "version_id": pinned_id,
                "pinned": pinned,
                "versions": candidates,
                "all_versions": all_versions,
                "current": pinned or derived,
                "derived": derived,
                "ambiguous_ids": [v["id"] for v in ambiguous],
            }
        )
    return skills


def general_report(
    conn: sqlite3.Connection, query: str, as_of: dt.date | None = None
) -> dict | None:
    entity, _candidates = resolve(conn, "general", query)
    if entity is None:
        return None
    as_of = as_of or dt.date.today()
    as_of_iso = as_of.isoformat()

    variants = []
    for variant in general_variants(conn, entity["id"]):
        variants.append({**variant, "skills": _variant_skills(conn, variant, as_of_iso)})

    return {
        "kind": "general",
        "general": entity,
        "variants": variants,
        "product": fetch(conn, "product", (variants[0].get("product_id") if variants else None)),
        "as_of": as_of_iso,
    }


def card_report(conn: sqlite3.Connection, query: str) -> dict | None:
    entity, _candidates = resolve(conn, "card", query)
    if entity is None:
        return None
    printings = _fetch_all(
        conn,
        "card_printing",
        "SELECT * FROM card_printings WHERE card_id = ? "
        "ORDER BY product_id, suit, COALESCE(rank, 0), id",
        (entity["id"],),
    )
    for printing in printings:
        printing["product"] = fetch(conn, "product", printing.get("product_id"))
        printing["sibling_printings"] = []
    keywords = [fetch(conn, "keyword", k) for k in entity.get("related_keywords", [])]
    return {
        "kind": "card",
        "definition": entity,
        "printings": printings,
        "keywords": [k for k in keywords if k],
    }


def faq_report(conn: sqlite3.Connection, faq_id: str) -> dict | None:
    entity = fetch(conn, "faq", faq_id)
    if entity is None:
        return None
    return {
        "kind": "faq",
        "faq": entity,
        "generals": [fetch(conn, "general", i) for i in entity["related_general_ids"]],
        "skills": [fetch(conn, "skill", i) for i in entity["related_skill_ids"]],
        "cards": [fetch(conn, "card", i) for i in entity["related_card_ids"]],
        "keywords": [fetch(conn, "keyword", i) for i in entity["related_keywords"]],
        "timings": [fetch(conn, "timing", i) for i in entity["related_timing_ids"]],
    }


def faq_search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    hits = search(conn, query, kinds=["faq"], limit=limit)
    reports = []
    for hit in hits:
        report = faq_report(conn, hit["id"])
        if report is not None:
            reports.append(report)
    return reports


def keyword_report(conn: sqlite3.Connection, query: str) -> dict | None:
    entity, _candidates = resolve(conn, "keyword", query)
    if entity is None:
        return None
    return {
        "kind": "keyword",
        "keyword": entity,
        "related_keywords": [
            fetch(conn, "keyword", i) for i in entity["related_keyword_ids"]
        ],
        "timings": [fetch(conn, "timing", i) for i in entity["related_timing_ids"]],
        "faqs": _fetch_all(
            conn, "faq", "SELECT * FROM faqs WHERE related_keywords LIKE ? ORDER BY id",
            (f'%"{entity["id"]}"%',),
        ),
        "skills": _fetch_all(
            conn, "skill_version",
            "SELECT * FROM skill_versions WHERE related_keywords LIKE ? ORDER BY skill_id",
            (f'%"{entity["id"]}"%',),
        ),
    }


def timing_report(conn: sqlite3.Connection, query: str) -> dict | None:
    entity, _candidates = resolve(conn, "timing", query)
    if entity is None:
        return None
    return {
        "kind": "timing",
        "timing": entity,
        "parent": fetch(conn, "timing", entity.get("parent_id")),
        "previous": [fetch(conn, "timing", i) for i in entity["previous_ids"]],
        "next": [fetch(conn, "timing", i) for i in entity["next_ids"]],
        "children": _fetch_all(
            conn, "timing", "SELECT * FROM timings WHERE parent_id = ? ORDER BY id", (entity["id"],)
        ),
        "keywords": [fetch(conn, "keyword", i) for i in entity["related_keywords"]],
        "skills": [fetch(conn, "skill", i) for i in entity["related_skill_ids"]],
        "faqs": [fetch(conn, "faq", i) for i in entity["related_faq_ids"]],
    }


def product_report(conn: sqlite3.Connection, query: str) -> dict | None:
    entity, _candidates = resolve(conn, "product", query)
    if entity is None:
        return None
    variants = _fetch_all(
        conn,
        "general_variant",
        "SELECT * FROM general_variants WHERE product_id = ? ORDER BY general_id, variant, id",
        (entity["id"],),
    )
    for variant in variants:
        general = fetch(conn, "general", variant.get("general_id"))
        variant["general"] = general
        variant["general_name"] = (general or {}).get("name") or variant.get("general_id")
    return {
        "kind": "product",
        "product": entity,
        "variants": variants,
        "printings": _fetch_all(
            conn, "card_printing",
            "SELECT * FROM card_printings WHERE product_id = ? ORDER BY card_set, suit, COALESCE(rank, 0), id",
            (entity["id"],),
        ),
        "skill_versions": _fetch_all(
            conn, "skill_version",
            "SELECT * FROM skill_versions WHERE product_id = ? ORDER BY skill_id, mode, id",
            (entity["id"],),
        ),
    }


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    meta = get_meta(conn)
    counts = {
        kind: int(meta.get(f"count.{kind}", "0")) for kind in KIND_ORDER
    }
    return {"meta": meta, "counts": counts}
