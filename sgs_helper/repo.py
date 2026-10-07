"""查询层：只读 SQLite 生成物，不读 YAML。

这一层的函数全部返回纯 ``dict`` / ``list``，所以：

- CLI（``scripts/query.py``）直接渲染；
- 以后接微信小程序时，FastAPI 路由可以原样复用它，不用改数据层。

技能文本的取法（新模型，**不按日期推导**）::

    1. ``SkillRef.version_id`` 显式绑定：填了就按它取文本；
    2. 为空时，按该实体的 ``mode`` 去匹配该技能**唯一一条**同模式版本作为回退；
    3. 匹配不到（0 条，或多条并列）就是“这个技能在此实体上没有文本”。

FAQ / 关键词的关联不再人工维护，而是在正文里按实体名称自动识别
（见 :func:`mentions`）。FAQ 的来源是一段多行文本，直接原样展示。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    KIND_BY_NAME,
    KIND_ORDER,
    MODE_LABELS,
    mode_sort_key,
)
from .db import JSON_COLUMNS, SOURCE_KINDS, fts_query

#: 参与“按名称自动识别”的实体类型（与构建期 ``db.NAME_KINDS`` 保持一致）
NAME_KINDS: tuple[str, ...] = ("general", "skill", "card", "keyword")

#: 自动识别的 kind -> 报告里的分组键名
MENTION_GROUPS: dict[str, str] = {
    "general": "generals",
    "skill": "skills",
    "card": "cards",
    "keyword": "keywords",
}


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
    if kind == "card" and isinstance(data.get("rules"), str):
        data["rules"] = json.loads(data["rules"])
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


def source_of(conn: sqlite3.Connection, key: str | None) -> dict | None:
    """按 source_key 取回去重后的来源记录（只有 type + reference）。"""
    if not key:
        return None
    row = conn.execute(
        "SELECT type, reference FROM sources WHERE source_key = ?", (key,)
    ).fetchone()
    return dict(row) if row is not None else None


def with_source(conn: sqlite3.Connection, data: dict, kind: str | None = None) -> dict:
    """只给仍有**结构化来源**的实体（skill_version）补 source 字段。

    FAQ 的 source 是普通文本列，随行一起取回；关键词没有来源字段。
    """
    if (kind or data.get("kind")) in SOURCE_KINDS:
        data["source"] = source_of(conn, data.get("source_key"))
    return data


def fetch(conn: sqlite3.Connection, kind: str, entity_id: str | None) -> dict | None:
    if not entity_id:
        return None
    table = KIND_BY_NAME[kind].table
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return None
    return with_source(conn, row_to_dict(kind, row), kind)


def _fetch_all(conn: sqlite3.Connection, kind: str, sql: str, params: Iterable = ()) -> list[dict]:
    return [
        with_source(conn, row_to_dict(kind, row), kind)
        for row in conn.execute(sql, tuple(params))
    ]


# ---------------------------------------------------------------- 按名称自动识别


def name_index(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """``name -> (kind, id)``，覆盖 general / skill / card / keyword。

    规则与构建期 ``db.build_name_index`` 一致：单字名称不参与（避免整段规则文本
    被刷成链接）；同名时按 :data:`NAME_KINDS` 的顺序取第一个。
    """
    index: dict[str, tuple[str, str]] = {}
    for kind in NAME_KINDS:
        table = KIND_BY_NAME[kind].table
        for entity_id, name in conn.execute(f"SELECT id, name FROM {table} ORDER BY id"):
            text = (name or "").strip()
            if len(text) < 2 or text in index:
                continue
            index[text] = (kind, entity_id)
    return index


def mentions(
    conn: sqlite3.Connection,
    text: str,
    kinds: tuple[str, ...] = ("general", "skill", "card", "keyword"),
    limit: int = 12,
) -> list[dict]:
    """在 ``text`` 里按实体名称做最长优先匹配。

    返回 ``[{"kind", "id", "name"}]``：去重、名称长的在前。名称长度 < 2 的不参与。
    """
    if not text:
        return []
    selected = set(kinds)
    hits = [
        (name, kind, entity_id)
        for name, (kind, entity_id) in name_index(conn).items()
        if kind in selected and name in text
    ]
    hits.sort(key=lambda hit: (-len(hit[0]), hit[0]))
    return [
        {"kind": kind, "id": entity_id, "name": name}
        for name, kind, entity_id in hits[:limit]
    ]


def _mentioned_entities(
    conn: sqlite3.Connection,
    text: str,
    *,
    exclude: tuple[str, str] | None = None,
    limit: int = 12,
) -> dict[str, list[dict]]:
    """把 ``mentions()`` 的结果按 kind 分组并取回实体。

    返回的键固定为 ``generals / skills / cards / keywords``，
    与渲染层和 admin 模板约定一致（替代原来的手工关联字段）。
    """
    grouped: dict[str, list[dict]] = {key: [] for key in MENTION_GROUPS.values()}
    for hit in mentions(conn, text, limit=limit):
        if exclude is not None and (hit["kind"], hit["id"]) == exclude:
            continue
        entity = fetch(conn, hit["kind"], hit["id"])
        if entity is not None:
            grouped[MENTION_GROUPS[hit["kind"]]].append(entity)
    return grouped


# ---------------------------------------------------------------- 检索


def _score(query: str, row) -> int | None:
    name = row["name"] or ""
    body = row["body"] or ""
    if name == query:
        return 0
    if name.startswith(query):
        return 1
    if query in name:
        return 2
    if query in body:
        return 3
    return None


def _kind_order(kind: str) -> int:
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
    """简单可靠的检索：精确名称 > 名称前缀 > 名称模糊 > 正文；FTS5 作为兜底。"""
    text = (query or "").strip()
    if not text:
        return []
    kind_list = list(kinds) if kinds else list(KIND_ORDER)
    placeholders = ", ".join("?" for _ in kind_list)
    like = f"%{text}%"
    sql = (
        "SELECT kind, entity_id, name, body FROM search_index "
        f"WHERE kind IN ({placeholders}) AND (name LIKE ? OR body LIKE ?)"
    )
    rows = list(conn.execute(sql, [*kind_list, like, like]).fetchall())

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
            score = 4
        scored.append(
            (
                score,
                _kind_order(row["kind"]),
                row["name"] or "",
                row["kind"],
                row["entity_id"],
            )
        )
    scored.sort(key=lambda item: item[:3])

    return [
        {"kind": kind, "id": entity_id, "name": name, "score": score}
        for score, _order, name, kind, entity_id in scored[:limit]
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
            "SELECT kind, entity_id, name, body FROM search_index "
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
        return with_source(conn, row_to_dict(kind, row), kind), []

    rows = list(
        conn.execute(
            "SELECT kind, entity_id, name, body FROM search_index WHERE kind = ?",
            (kind,),
        ).fetchall()
    )
    exact = [r for r in rows if (r["name"] or "") == text]
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


# ---------------------------------------------------------------- 技能版本


def _history_chains(versions: list[dict]) -> list[list[str]]:
    """按 previous_version_id 串出的版本演进链。"""
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
        "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY mode, id",
        (skill_id,),
    )


def _match_version(
    versions: list[dict], mode: str | None, version_id: str | None
) -> tuple[dict | None, bool]:
    """选定某条技能引用上要显示的技能文本，返回 ``(版本 | None, 是否显式绑定)``。

    - ``version_id`` 非空：按它取；取不到就是 ``(None, True)``（绑定失效）。
    - ``version_id`` 为空：先按 ``mode`` 找**唯一一条**同模式版本，再退到 ``common``
      的**唯一一条**；两边都是 0 条或多条并列都算无文本（绝不静默挑一个）。

    这套回退规则与 ``validate.py`` 的「该武将版本的技能没有可用文本」警告、
    以及管理页 ``AdminStore._fallback_version`` 完全一致。
    """
    if version_id:
        pinned_version = next((v for v in versions if v["id"] == version_id), None)
        return pinned_version, True
    seen: set[str] = set()
    for wanted in (mode, "common"):
        if not wanted or wanted in seen:
            continue
        seen.add(wanted)
        candidates = [v for v in versions if v.get("mode") == wanted]
        if len(candidates) == 1:
            return candidates[0], False
    return None, False


def skill_report(conn: sqlite3.Connection, query: str) -> dict | None:
    entity, _candidates = resolve(conn, "skill", query)
    if entity is None:
        return None

    versions = _versions_of_skill(conn, entity["id"])
    modes_present = sorted({v["mode"] for v in versions}, key=mode_sort_key)

    groups = [
        {
            "mode": mode,
            "label": MODE_LABELS.get(mode, mode),
            "version_ids": [v["id"] for v in versions if v["mode"] == mode],
        }
        for mode in modes_present
    ]

    for version in versions:
        version["previous_version"] = fetch(
            conn, "skill_version", version.get("previous_version_id")
        )

    product_ids = {v["product_id"] for v in versions if v.get("product_id")}
    products = {pid: fetch(conn, "product", pid) for pid in product_ids}

    return {
        "kind": "skill",
        "definition": entity,
        "versions": versions,
        "modes": groups,
        "history_chains": _history_chains(versions),
        "products": products,
    }


def variant_skill_refs(conn: sqlite3.Connection, variant_id: str) -> list[dict]:
    """读某个武将版本的技能关系原始行，顺序即卡面上的技能顺序。"""
    rows = conn.execute(
        "SELECT skill_id, version_id, position FROM variant_skills "
        "WHERE variant_id = ? ORDER BY position",
        (variant_id,),
    ).fetchall()
    return [{"skill_id": row[0], "version_id": row[1], "position": row[2]} for row in rows]


def variant_skills(conn: sqlite3.Connection, variant: dict) -> list[dict]:
    """武将版本 → 技能条目，补上“显示哪一版文本”。

    每条形如 ``{"skill_id", "version_id", "position", "version", "pinned", "skill"}``：
    ``version`` 是匹配到的版本 dict（可能为 None），``pinned`` 表示是否显式绑定。
    """
    mode = variant.get("mode")
    entries: list[dict] = []
    for ref in variant_skill_refs(conn, variant["id"]):
        versions = _versions_of_skill(conn, ref["skill_id"])
        version, pinned = _match_version(versions, mode, ref["version_id"])
        entries.append(
            {
                "skill_id": ref["skill_id"],
                "version_id": ref["version_id"],
                "position": ref["position"],
                "version": version,
                "pinned": pinned,
                "skill": fetch(conn, "skill", ref["skill_id"]),
            }
        )
    return entries


def general_variants(conn: sqlite3.Connection, general_id: str) -> list[dict]:
    """读某个武将个体的全部版本。"""
    return _fetch_all(
        conn,
        "general_variant",
        "SELECT * FROM general_variants WHERE general_id = ? ORDER BY variant, id",
        (general_id,),
    )


def general_report(conn: sqlite3.Connection, query: str) -> dict | None:
    entity, _candidates = resolve(conn, "general", query)
    if entity is None:
        return None

    variants = []
    for variant in general_variants(conn, entity["id"]):
        variants.append({**variant, "skills": variant_skills(conn, variant)})

    return {
        "kind": "general",
        "general": entity,
        "variants": variants,
        "product": fetch(conn, "product", (variants[0].get("product_id") if variants else None)),
    }


def _card_effect_skills(conn: sqlite3.Connection, card: dict) -> list[dict]:
    """把 ``cards.effect_skills``（JSON 字符串）解析成卡牌效果里的技能条目。

    每条形如 ``{"skill_id", "version_id", "version", "pinned", "skill"}``：

    - ``skills`` / ``skill_versions`` 里解析不出的引用保留原样、实体为 ``None``；
    - 没有显式 ``version_id`` 时按 ``common`` 选**唯一一条**版本，退不到就再看该技能
      是否只有唯一一条版本；仍然不唯一就算“没有文本”（绝不静默挑一个）。
    """
    raw = card.get("effect_skills")
    refs: list[dict] = []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            refs = [ref for ref in parsed if isinstance(ref, dict)]
    elif isinstance(raw, list):  # 已经是 JSON 列（row_to_dict 解析过的场景）
        refs = [ref for ref in raw if isinstance(ref, dict)]

    entries = []
    for ref in refs:
        skill_id = str(ref.get("skill_id") or "").strip()
        version_id = str(ref.get("version_id") or "").strip() or None
        if not skill_id:
            continue
        versions = _versions_of_skill(conn, skill_id)
        version, pinned = _match_version(versions, "common", version_id)
        if version is None and not pinned and len(versions) == 1:
            version = versions[0]
        entries.append(
            {
                "skill_id": skill_id,
                "version_id": version_id,
                "version": version,
                "pinned": pinned,
                "skill": fetch(conn, "skill", skill_id),
            }
        )
    return entries


def card_report(conn: sqlite3.Connection, query: str) -> dict | None:
    entity, _candidates = resolve(conn, "card", query)
    if entity is None:
        return None
    text = entity.get("effect_text") or ""
    rules = entity.get("rules") or {}
    related_text = "\n".join(
        [text, rules.get("attack_range", ""), rules.get("distance_modifier", ""), rules.get("note", "")]
        + [value for method in rules.get("use_methods", []) for value in method.values()]
    )
    return {
        "kind": "card",
        "card": entity,
        "effect": {
            "text": text,
            "skills": _card_effect_skills(conn, entity),
        },
        # 卡牌效果文本里出现的技能名 / 关键词按名称自动识别（不含卡自己）
        **_mentioned_entities(conn, related_text, exclude=("card", entity["id"])),
    }


def faq_report(conn: sqlite3.Connection, faq_id: str) -> dict | None:
    entity = fetch(conn, "faq", faq_id)
    if entity is None:
        return None
    text = f"{entity.get('question') or ''}\n{entity.get('answer') or ''}"
    return {
        "kind": "faq",
        "faq": entity,
        **_mentioned_entities(conn, text),
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
        # 目录里自己的名字可能就写在定义里，别把自身当成“相关”
        **_mentioned_entities(
            conn, entity.get("definition") or "", exclude=("keyword", entity["id"])
        ),
    }


def _product_cards(conn: sqlite3.Connection, card_sets: Iterable[str]) -> list[dict]:
    """产品声明过的 ``card_sets`` 展开出的游戏牌，按分类 + 卡名排序。

    新模型下卡与产品**没有外键**，两者的唯一关系是 ``Card.card_set ∈ Product.card_sets``；
    同名拓展包可能被多个产品共用，所以同一条卡会在多个产品页出现（这里不去重）。
    返回的每一行额外带一个 ``card_set``（就是它命中产品声明的那个拓展包名）。
    """
    wanted = sorted({str(name).strip() for name in card_sets if str(name).strip()})
    if not wanted:
        return []
    placeholders = ", ".join("?" for _ in wanted)
    table = KIND_BY_NAME["card"].table
    return _fetch_all(
        conn,
        "card",
        f"SELECT * FROM {table} WHERE card_set IN ({placeholders}) "
        "ORDER BY category_1, category_2, category_3, name, id",
        wanted,
    )


def product_report(conn: sqlite3.Connection, query: str) -> dict | None:
    """产品详情：``kind`` / ``product`` / ``variants`` / ``cards`` / ``skill_versions``。

    - ``variants``：该产品的武将版本（``GeneralVariant.product_id`` 外键）；
    - ``cards``：该产品 ``card_sets`` 里声明过的拓展包下的游戏牌，按分类 + 卡名排序
      （新模型没有卡-产品外键，只能按 ``Card.card_set`` 关联；同名拓展包共用会重复计数）；
    - ``skill_versions``：挂在该产品上的技能版本（``SkillVersion.product_id`` 外键）。
    """
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
        "cards": _product_cards(conn, entity.get("card_sets") or []),
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
