"""本地 Wiki 页面（`/wiki/*`）。

这是任务书里 **Phase 2–5** 的落地：

- 五个一级入口：武将 / 技能 / 游戏牌 / 指示物 / FAQ（`规则` 只预留入口）；
- 详情页之间的**互相跳转**（武将 ↔ 技能、卡牌 → 技能、各实体 → 相关 FAQ、FAQ → 关联实体）；
- **编辑模式**开关：默认浏览；开启后列表页有「+新增」、详情页有「编辑 / 复制 / 删除」；
- 图片上传接口（写 ``data/card_images/``，实体里只存**文件名**）。

设计上刻意**不重写**写入逻辑：新增 / 编辑 / 删除继续复用现成的
``/new/{kind}``、``/edit/{kind}/{id}``、``/delete/{kind}/{id}``（它们已经带
「内存全量预校验 → 原子落盘 → 回滚 → 并发保护 → 一次 rebuild」），
Wiki 只负责用一个更好用的入口把它们组织起来。

分类（``category_1/2/3``）与来源（``card_set`` / ``product_id``）是**两套独立信息**：
游戏牌与指示物两个入口直接按 ``category_1`` 筛选。
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..constants import (
    CARD_CATEGORY_1_FOR_WIKI,
    GENDER_LABELS,
    KIND_BY_NAME,
    MODE_LABELS,
    SOURCE_LABELS,
    SOURCE_TITLE_BY_REFERENCE,
)
from .store import AdminStore, truncate

#: Wiki 一级入口。``rules`` 只预留，不渲染内容。
WIKI_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("generals", "武将", "general"),
    ("skills", "技能", "skill"),
    ("cards", "游戏牌", "card"),
    ("markers", "指示物", "card"),
    ("faq", "FAQ", "faq"),
)

#: 每个入口的一段说明（列表页副标题）
SECTION_LEAD: dict[str, str] = {
    "home": "浏览、检索并就地维护资料；数据以 data/ 下的 YAML 为唯一权威源",
    "generals": "同一个武将的不同玩法是不同「版本」，技能可点进详情",
    "skills": "技能本体与各版本文本；可回跳所属武将",
    "cards": "按「这张卡是什么」的三级分类归集，与来自哪个拓展包相互独立",
    "markers": "军令 / 鏖战事件 / 技能指示物 / 衍生技能牌 / 通用标记",
    "faq": "规则专题与裁定；正文里的武将 / 技能 / 卡牌名可点击跳转",
    "rules": "规则入口已预留，本阶段不做内容",
}

#: 详情页「相关 FAQ」每个实体最多显示几条
FAQ_LIMIT = 6

#: 列表页每页条数
PER_PAGE = 48

#: 图片上传白名单（与 validate._check_images 的口径保持一致）
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
IMAGE_MAX_BYTES = 8 * 1024 * 1024


def entity_url(kind: str, entity_id: str) -> str:
    """实体 → 可点击 URL（找不到对应 Wiki 入口就退回管理页详情）。"""
    if kind == "card":
        # 指示物与游戏牌共用 card，靠 category_1 区分；详情统一走 /wiki/card/{id}
        return f"/wiki/card/{entity_id}"
    if kind in ("general", "skill", "faq"):
        return f"/wiki/{kind}s/{entity_id}" if kind != "faq" else f"/wiki/faq/{entity_id}"
    return f"/view/{kind}/{entity_id}"


def _basename(value: str | None) -> str:
    """把可能的相对路径（data/card_images/x.jpg）归一成文件名。"""
    if not value:
        return ""
    return os.path.basename(str(value).replace("\\", "/"))


def image_url(value: str | None) -> str:
    """图片文件名 → 可访问 URL（实体里只存文件名，这里是唯一拼接处）。"""
    name = _basename(value)
    if not name:
        return ""
    return f"/card-images/{name}"


class WikiRoutes:
    """把 Wiki 的页面逻辑集中在一处，``create_app`` 只负责挂载。"""

    def __init__(self, templates: Any) -> None:
        self.templates = templates
        self.router = APIRouter()
        self._register()

    # ------------------------------------------------------------ 上下文

    def _ctx(
        self,
        request: Request,
        *,
        title: str = "",
        section: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        live: AdminStore = request.app.state.store
        counts = live.counts()

        tabs = []
        for key, label, kind in WIKI_SECTIONS:
            if kind == "card":
                category_1 = CARD_CATEGORY_1_FOR_WIKI.get(key)
                count = sum(
                    1
                    for card in live.bundle.all("card").values()
                    if (getattr(card, "category_1", "") or "") == category_1
                )
            else:
                count = counts.get(kind, 0)
            tabs.append({"key": key, "label": label, "url": f"/wiki/{key}", "count": count})
        tabs.append({"key": "home", "label": "资料检索", "url": "/wiki", "count": None})

        edit_mode = request.cookies.get("sgs_edit") == "1"
        payload: dict[str, Any] = {
            "request": request,
            "title": title,
            "csrf": request.app.state.csrf,
            "counts": counts,
            "total": live.bundle.total,
            "db_state": live.db_state(),
            "flash": _flash(request),
            "edit_mode": edit_mode,
            # 模板里同时用 section（当前入口 key）和 wiki_section（同值，语义更明确）
            "section": section,
            "wiki_section": section,
            "wiki_tabs": tabs,
            "q": "",
            "url_for_entity": entity_url,
        }
        payload.update(extra)
        return payload

    def _render(self, req: Request, template: str, **extra: Any) -> HTMLResponse:
        # 参数名刻意不叫 request：_ctx() 返回的字典里已经有一个 "request" 键，
        # 用同名形参会报 “got multiple values for argument 'request'”。
        return self.templates.TemplateResponse(req, template, extra)

    def _require_kind(self, kind: str) -> str:
        if kind not in KIND_BY_NAME:
            raise HTTPException(status_code=404, detail=f"未知的资料类型：{kind}")
        return kind

    # ------------------------------------------------------------ 列表行

    def _list_row(self, live: AdminStore, kind: str, row: dict) -> dict:
        """把 ``store.filter_rows`` 的一行转成 Wiki 卡片需要的字段。"""
        entity = row["entity"]
        tags: list[dict] = []
        badge = ""
        badge_class = ""
        excerpt = ""
        if kind == "general":
            variants = live.variants_of(row["id"])
            if variants:
                first = variants[0]
                if getattr(first, "faction", ""):
                    tags.append({"text": first.faction, "class": "wiki-tag--faction"})
                if getattr(first, "hp", None) is not None:
                    tags.append({"text": f"{_hp(first.hp)} 体力", "class": ""})
            if len(variants) > 1:
                tags.append({"text": f"{len(variants)} 个版本", "class": ""})
            subtitle = "、".join(sorted({v.variant for v in variants})) if variants else "暂无版本"
            if variants and getattr(variants[0], "title", ""):
                excerpt = f"称号：{variants[0].title}"

        elif kind == "skill":
            versions = sorted(
                (
                    v
                    for v in live.bundle.all("skill_version").values()
                    if v.skill_id == row["id"]
                ),
                key=lambda v: (v.mode, v.id),
            )
            subtitle = f"{len(versions)} 个技能版本"
            if versions:
                subtitle += " · " + "、".join(
                    sorted({MODE_LABELS.get(v.mode, v.mode) for v in versions})
                )
                excerpt = truncate(versions[0].text, 110)

        elif kind == "card":
            subtitle = f'{row["category_2"]} / {row["category_3"]}'
            if row.get("card_set"):
                subtitle += f' · 拓展包 {row["card_set"]}'
            excerpt = truncate(
                str(getattr(getattr(entity, "effect", None), "text", "") or ""), 110
            )

        elif kind == "faq":
            subtitle = MODE_LABELS.get(row.get("mode") or "", "") or "通用"
            excerpt = truncate(str(getattr(entity, "answer", "") or ""), 110)

        else:
            subtitle = row.get("subtitle", "")

        return {
            "id": row["id"],
            "name": row["name"],
            "subtitle": subtitle,
            "excerpt": excerpt,
            "image_url": image_url(row.get("image")),
            "badge": badge,
            "badge_class": badge_class,
            "tags": tags,
            "detail_url": entity_url(kind, row["id"]),
            "faq_count": 0,
        }

    # ------------------------------------------------------------ 注册路由

    def _register(self) -> None:
        router = self.router

        # ---------------------------------------------------- 首页
        @router.get("/wiki", response_class=HTMLResponse)
        def wiki_home(request: Request) -> HTMLResponse:
            live: AdminStore = request.app.state.store
            counts = live.counts()
            card_counts: dict[str, int] = {}
            for card in live.bundle.all("card").values():
                c1 = getattr(card, "category_1", "") or ""
                card_counts[c1] = card_counts.get(c1, 0) + 1

            overview = [
                {
                    "label": "武将",
                    "count": counts.get("general", 0),
                    "hint": f'武将版本 {counts.get("general_variant", 0)} 个',
                    "url": "/wiki/generals",
                },
                {
                    "label": "技能",
                    "count": counts.get("skill", 0),
                    "hint": f'技能版本 {counts.get("skill_version", 0)} 条',
                    "url": "/wiki/skills",
                },
                {
                    "label": "游戏牌",
                    "count": card_counts.get("游戏牌", 0),
                    "hint": "基本牌 / 锦囊牌 / 装备牌",
                    "url": "/wiki/cards",
                },
                {
                    "label": "指示物",
                    "count": card_counts.get("指示物", 0),
                    "hint": "军令 / 鏖战事件 / 技能指示物…",
                    "url": "/wiki/markers",
                },
                {
                    "label": "FAQ",
                    "count": counts.get("faq", 0),
                    "hint": "规则专题与裁定",
                    "url": "/wiki/faq",
                },
            ]
            return self._render(
                request,
                "wiki/home.html",
                **self._ctx(
                    request,
                    title="资料检索",
                    section="home",
                    overview=overview,
                    rules_reserved={
                        "keywords": counts.get("keyword", 0),
                        "timings": counts.get("timing", 0),
                    },
                ),
            )

        # ---------------------------------------------------- 列表（五个入口）
        @router.get("/wiki/generals", response_class=HTMLResponse)
        def wiki_generals(request: Request) -> HTMLResponse:
            return self._list(request, "generals")

        @router.get("/wiki/skills", response_class=HTMLResponse)
        def wiki_skills(request: Request) -> HTMLResponse:
            return self._list(request, "skills")

        @router.get("/wiki/cards", response_class=HTMLResponse)
        def wiki_cards(request: Request) -> HTMLResponse:
            return self._list(request, "cards")

        @router.get("/wiki/markers", response_class=HTMLResponse)
        def wiki_markers(request: Request) -> HTMLResponse:
            return self._list(request, "markers")

        @router.get("/wiki/faq", response_class=HTMLResponse)
        def wiki_faq(request: Request) -> HTMLResponse:
            return self._list(request, "faq")

        @router.get("/wiki/rules", response_class=HTMLResponse)
        def wiki_rules(request: Request) -> HTMLResponse:
            live: AdminStore = request.app.state.store
            counts = live.counts()
            return self._render(
                request,
                "wiki/rules.html",
                **self._ctx(
                    request,
                    title="规则",
                    section="rules",
                    keywords=counts.get("keyword", 0),
                    timings=counts.get("timing", 0),
                ),
            )

        # ---------------------------------------------------- 全库搜索
        @router.get("/wiki/search", response_class=HTMLResponse)
        def wiki_search(request: Request) -> HTMLResponse:
            live: AdminStore = request.app.state.store
            q = (request.query_params.get("q") or "").strip()
            groups: list[dict] = []
            total_results = 0
            if q:
                # 搜索复用 store.search（名称精确 > 前缀 > 包含 > 正文包含）
                hits = live.search(q, limit=400)
                by_kind: dict[str, list[dict]] = {}
                for row in hits:
                    kind = row["kind"]
                    if kind in ("general_variant", "skill_version", "product", "keyword", "timing"):
                        continue  # Wiki 五个入口只收这几类
                    by_kind.setdefault(kind, []).append(row)

                order = (
                    ("general", "武将"),
                    ("skill", "技能"),
                    ("card", "游戏牌 / 指示物"),
                    ("faq", "FAQ"),
                )
                for kind, label in order:
                    rows = by_kind.get(kind) or []
                    if not rows:
                        continue
                    total_results += len(rows)
                    shown = rows[:PER_PAGE]
                    groups.append(
                        {
                            "label": label,
                            "rows": [self._list_row(live, kind, r) for r in shown],
                            "more": len(rows) > len(shown),
                        }
                    )

            return self._render(
                request,
                "wiki/search.html",
                **self._ctx(
                    request,
                    title=f"搜索 {q}" if q else "搜索",
                    section="search",
                    q=q,
                    groups=groups,
                    total_results=total_results,
                ),
            )

        # ---------------------------------------------------- 卡牌详情（游戏牌 / 指示物共用）
        @router.get("/wiki/card/{entity_id}", response_class=HTMLResponse)
        def wiki_card_detail(request: Request, entity_id: str) -> HTMLResponse:
            live: AdminStore = request.app.state.store
            view = live.card_view(entity_id)
            if view is None:
                raise HTTPException(status_code=404, detail=f"卡牌 {entity_id} 不存在")
            card = view["card"]
            return self._render(
                request,
                "wiki/detail.html",
                **self._ctx(
                    request,
                    title=card.name,
                    section="markers" if card.category_1 == "指示物" else "cards",
                    detail=self._card_detail(live, view),
                ),
            )

        # ---------------------------------------------------- 武将 / 技能 / FAQ 详情
        @router.get("/wiki/generals/{entity_id}", response_class=HTMLResponse)
        def wiki_general_detail(request: Request, entity_id: str) -> HTMLResponse:
            live: AdminStore = request.app.state.store
            view = live.general_view(entity_id)
            if view is None:
                raise HTTPException(status_code=404, detail=f"武将 {entity_id} 不存在")
            return self._render(
                request,
                "wiki/detail.html",
                **self._ctx(
                    request,
                    title=view["general"].name,
                    section="generals",
                    detail=self._general_detail(live, view),
                ),
            )

        @router.get("/wiki/skills/{entity_id}", response_class=HTMLResponse)
        def wiki_skill_detail(request: Request, entity_id: str) -> HTMLResponse:
            live: AdminStore = request.app.state.store
            view = live.skill_view(entity_id)
            if view is None:
                raise HTTPException(status_code=404, detail=f"技能 {entity_id} 不存在")
            return self._render(
                request,
                "wiki/detail.html",
                **self._ctx(
                    request,
                    title=view["skill"].name,
                    section="skills",
                    detail=self._skill_detail(live, view),
                ),
            )

        @router.get("/wiki/faq/{entity_id}", response_class=HTMLResponse)
        def wiki_faq_detail(request: Request, entity_id: str) -> HTMLResponse:
            live: AdminStore = request.app.state.store
            faq = live.entity("faq", entity_id)
            if faq is None:
                raise HTTPException(status_code=404, detail=f"FAQ {entity_id} 不存在")
            return self._render(
                request,
                "wiki/detail.html",
                **self._ctx(
                    request,
                    title=truncate(faq.question, 40),
                    section="faq",
                    detail=self._faq_detail(live, faq),
                ),
            )

        # ---------------------------------------------------- 编辑模式开关
        @router.get("/wiki/edit-mode", response_class=HTMLResponse)
        def wiki_edit_mode_get(request: Request) -> HTMLResponse:
            """允许用链接切换（列表空状态里的「开启编辑模式并新增」，以及只带 ?on=1 的入口）。"""
            on = request.query_params.get("on") == "1"
            nxt = request.query_params.get("next") or "/wiki"
            return _redirect_with_edit_mode(nxt, on)

        @router.post("/wiki/edit-mode", response_class=HTMLResponse)
        async def wiki_edit_mode_post(request: Request) -> HTMLResponse:
            form = await request.form()
            token = str(form.get("_csrf", ""))
            if not token or not secrets.compare_digest(token, request.app.state.csrf):
                raise HTTPException(status_code=403, detail="表单已失效（CSRF token 不匹配）")
            on = str(form.get("on", "")) == "1"
            nxt = str(form.get("next") or "/wiki")
            return _redirect_with_edit_mode(nxt, on)

        # ---------------------------------------------------- 图片上传
        @router.post("/api/upload-image")
        async def upload_image(request: Request, file: UploadFile = File(...)) -> JSONResponse:
            """上传卡图到 ``data/card_images/``，返回**文件名**（实体里只存相对名）。

            与 ``validate._check_images`` 的口径保持一致：只接受
            jpg / jpeg / png / webp，文件名做安全化处理并避免覆盖同名文件。
            """
            live: AdminStore = request.app.state.store
            original = os.path.basename((file.filename or "").replace("\\", "/"))
            suffix = Path(original).suffix.lower()
            if suffix not in IMAGE_SUFFIXES:
                raise HTTPException(
                    status_code=400,
                    detail=f"只支持 {'/'.join(IMAGE_SUFFIXES)}，收到的是 “{suffix or '无扩展名'}”",
                )

            data = await file.read()
            if not data:
                raise HTTPException(status_code=400, detail="文件是空的")
            if len(data) > IMAGE_MAX_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"文件太大（{len(data) // 1024} KB），"
                        f"上限 {IMAGE_MAX_BYTES // 1024 // 1024} MB"
                    ),
                )

            stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", Path(original).stem).strip("_") or "upload"
            images_dir = live.data_dir / "card_images"
            images_dir.mkdir(parents=True, exist_ok=True)

            name = f"{stem}{suffix}"
            counter = 1
            while (images_dir / name).exists():
                name = f"{stem}_{counter}{suffix}"
                counter += 1

            target = images_dir / name
            target.write_bytes(data)
            return JSONResponse(
                {
                    "ok": True,
                    "file": name,
                    "url": f"/card-images/{name}",
                    "bytes": len(data),
                }
            )

    # ------------------------------------------------------------ 列表实现

    def _list(self, request: Request, section: str) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        params = request.query_params
        kind = next(k for key, _label, k in WIKI_SECTIONS if key == section)
        label = next(lbl for key, lbl, _k in WIKI_SECTIONS if key == section)

        q = (params.get("q") or "").strip()
        mode = params.get("mode") or ""
        status = params.get("status") or ""
        set_name = params.get("set_name") or ""
        category_1 = CARD_CATEGORY_1_FOR_WIKI.get(section, "")
        category_2 = params.get("category_2") or ""
        category_3 = params.get("category_3") or ""
        try:
            page = max(1, int(params.get("page") or 1))
        except ValueError:
            page = 1

        rows = live.filter_rows(
            kind,
            q=q,
            mode=mode,
            set_name=set_name,
            category_1=category_1,
            category_2=category_2,
        )
        # category_3 在 store 层没有筛选参数，这里补一刀（三级分类粒度各分支不同）
        if category_3:
            rows = [r for r in rows if r.get("category_3") == category_3]

        total_rows = len(rows)
        total_pages = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
        page = min(page, total_pages)
        page_rows = rows[(page - 1) * PER_PAGE : page * PER_PAGE]

        items = [self._list_row(live, kind, r) for r in page_rows]
        # 相关 FAQ 条数（一次算完，避免逐行扫描）
        faq_counts = live.related_faq_counts(kind, [r["id"] for r in page_rows])
        for item in items:
            item["faq_count"] = faq_counts.get(item["id"], 0)

        base = f"/wiki/{section}"
        # 注意：_make_filter_url / _make_page_url 在闭包外先把基础条件固化成 dict，
        # 避免 lambda 形参遮蔽闭包变量（形参名与下面的键同名时读到的是形参而不是当前值）。
        common = {
            "q": q,
            "mode": mode,
            "set_name": set_name,
            "category_2": category_2,
            "category_3": category_3,
        }

        return self._render(
            request,
            "wiki/list.html",
            **self._ctx(
                request,
                title=label,
                section=section,
                kind=kind,
                rows=items,
                total_rows=total_rows,
                page=page,
                total_pages=total_pages,
                q=q,
                mode=mode,
                set_name=set_name,
                set_label="拓展包" if kind == "card" else "武将牌系列",
                category_1=category_1,
                category_2=category_2,
                category_3=category_3,
                filters=live.filter_options_scoped(kind, category_1=category_1),
                nav_tree=live.wiki_nav_tree(category_1) if category_1 else None,
                section_title=label,
                section_lead=SECTION_LEAD.get(section, ""),
                search_placeholder=_placeholder(section),
                section_url=base,
                new_url=_new_url(kind, section),
                empty_title=_empty_title(section),
                empty_text=_empty_text(section),
                empty_hint=_empty_hint(section),
                filter_url=_make_filter_url(base, **common),
                page_url=_make_page_url(base, **common),
            ),
        )

    # ------------------------------------------------------------ 详情实现

    def _common_detail(self, live: AdminStore, kind: str, entity_id: str) -> dict:
        origin = live.origin(kind, entity_id)
        return {
            "file": f"data/{origin.path}" if origin else "",
            "line": origin.line if origin else 0,
            "raw_url": f"/raw/{kind}/{entity_id}",
            "admin_url": f"/view/{kind}/{entity_id}",
        }

    def _general_detail(self, live: AdminStore, view: dict) -> dict:
        general = view["general"]
        variants = view["variants"]
        first = variants[0] if variants else None
        variant = first["entity"] if first else None

        fields = [
            {"k": "称号", "v": getattr(variant, "title", "") if variant else ""},
            {"k": "势力", "v": getattr(variant, "faction", "") if variant else ""},
            {"k": "体力", "v": _hp(getattr(variant, "hp", None)) if variant else ""},
            {
                "k": "性别",
                "v": GENDER_LABELS.get(getattr(variant, "gender", ""), "") if variant else "",
            },
            {"k": "模式", "v": MODE_LABELS.get(getattr(variant, "mode", ""), "") if variant else ""},
            {"k": "编号", "v": (getattr(variant, "general_number", "") or "") if variant else ""},
            {"k": "武将牌系列", "v": first["set_display"] if first else ""},
            {"k": "产品", "v": first["product_name"] if first else ""},
            {"k": "画师", "v": getattr(variant, "illustrator", "") if variant else ""},
            {
                "k": "珠联璧合",
                "v": "、".join(getattr(variant, "zhulianbihe", []) or []) if variant else "",
            },
        ]

        skill_rows = []
        if first:
            for row in first["skills"]:
                chosen = row["current"]
                skill_rows.append(
                    {
                        "skill_id": row["skill_id"],
                        "name": live.display_name("skill", row["skill"]),
                        "text": truncate(chosen.text, 220) if chosen else "",
                    }
                )

        refs = []
        if variant is not None and getattr(variant, "product_id", None):
            product = live.entity("product", variant.product_id)
            if product is not None:
                refs.append(
                    {
                        "label": "产品 / 拓展包",
                        "links": [
                            {
                                "name": f"{product.name}（在管理页打开）",
                                "url": f"/view/product/{product.id}",
                            }
                        ],
                    }
                )

        return {
            "eyebrow": "武将",
            "title": general.name,
            "subtitle": _variant_summary(first) if first else "暂无版本",
            "image_url": image_url(getattr(variant, "image", "") if variant else ""),
            "tags": _variant_tags(variant),
            "edit_url": f"/edit/general/{general.id}",
            "copy_url": f"/new/general?from={general.id}",
            "delete_url": f"/delete/general/{general.id}",
            "back_url": "/wiki/generals",
            "back_label": "武将",
            "fields": fields,
            "prose_blocks": [],
            "skill_rows": skill_rows,
            "version_rows": [],
            "owners": [],
            "variant_rows": [
                {"variant": v["variant"], "summary": _variant_summary(v)} for v in variants
            ],
            "refs": refs,
            "effect_skill_rows": [],
            "faqs": live.related_faqs("general", general.id, limit=FAQ_LIMIT),
            **self._common_detail(live, "general", general.id),
        }

    def _skill_detail(self, live: AdminStore, view: dict) -> dict:
        skill = view["skill"]
        versions = view["versions"]

        rows = []
        for row in versions:
            version = row["entity"]
            previous = live.entity("skill_version", version.previous_version_id)
            rows.append(
                {
                    "label": version.version_label or version.id,
                    "mode_label": MODE_LABELS.get(version.mode, version.mode),
                    "product": row["product_name"],
                    "source": _source_label(version),
                    "previous_label": (
                        (previous.version_label or previous.id) if previous is not None else ""
                    ),
                    "segments": row["segments"],
                }
            )

        owners = []
        for variant in live.bundle.all("general_variant").values():
            if any(ref.skill_id == skill.id for ref in (variant.skills or [])):
                general = live.entity("general", variant.general_id)
                owners.append(
                    {
                        "name": f'{getattr(general, "name", variant.general_id)}（{variant.variant}）',
                        "url": entity_url("general", variant.general_id),
                        "subtitle": _variant_summary(
                            {
                                "entity": variant,
                                "set_display": live.set_display(variant.general_set),
                                "product_name": live.name_of("product", variant.product_id),
                            }
                        ),
                    }
                )

        return {
            "eyebrow": "技能",
            "title": skill.name,
            "subtitle": f"{len(versions)} 个技能版本",
            "image_url": "",
            "tags": [],
            "edit_url": f"/edit/skill/{skill.id}",
            "copy_url": f"/new/skill?from={skill.id}",
            "delete_url": f"/delete/skill/{skill.id}",
            "back_url": "/wiki/skills",
            "back_label": "技能",
            "fields": [
                {"k": "技能名", "v": skill.name},
                {"k": "版本数", "v": str(len(versions))},
                {"k": "复核备注", "v": getattr(skill, "review_note", "")},
            ],
            "prose_blocks": [],
            "skill_rows": [],
            "version_rows": rows,
            "owners": owners,
            "variant_rows": [],
            "refs": [],
            "effect_skill_rows": [],
            "faqs": live.related_faqs("skill", skill.id, limit=FAQ_LIMIT),
            **self._common_detail(live, "skill", skill.id),
        }

    def _card_detail(self, live: AdminStore, view: dict) -> dict:
        card = view["card"]
        effect = card.effect
        is_marker = card.category_1 == "指示物"

        fields = [
            {"k": "一级分类", "v": card.category_1},
            {"k": "二级分类", "v": card.category_2},
            {"k": "三级分类", "v": card.category_3},
            {"k": "拓展包", "v": card.card_set or ""},
            {"k": "卡名", "v": card.name},
            {"k": "复核备注", "v": getattr(card, "review_note", "")},
        ]

        prose_blocks = []
        if effect.text:
            prose_blocks.append(
                {
                    "title": "效果文本",
                    "segments": live.annotate(effect.text),
                    "meta": "",
                    "note": "照抄卡面原文；技能规则正文由「技能」维护，不抄进这里。",
                }
            )

        effect_skill_rows = []
        for row in view["skills"]:
            chosen = row["current"]
            effect_skill_rows.append(
                {
                    "skill_id": row["skill_id"],
                    "name": live.display_name("skill", row["skill"]),
                    "text": truncate(chosen.text, 220) if chosen else "",
                }
            )

        return {
            "eyebrow": "指示物" if is_marker else "游戏牌",
            "title": card.name,
            "subtitle": f"{card.category_2} / {card.category_3}",
            "image_url": image_url(card.image),
            "tags": [
                {"text": card.category_2, "class": "wiki-tag--faction"},
                {"text": card.category_3, "class": ""},
            ]
            + ([{"text": f"拓展包 {card.card_set}", "class": ""}] if card.card_set else []),
            "edit_url": f"/edit/card/{card.id}",
            "copy_url": f"/new/card?from={card.id}",
            "delete_url": f"/delete/card/{card.id}",
            "back_url": "/wiki/markers" if is_marker else "/wiki/cards",
            "back_label": "指示物" if is_marker else "游戏牌",
            "fields": fields,
            "prose_blocks": prose_blocks,
            "skill_rows": [],
            "version_rows": [],
            "owners": [],
            "variant_rows": [],
            "refs": [],
            "effect_skill_rows": effect_skill_rows,
            "faqs": live.related_faqs("card", card.id, limit=FAQ_LIMIT),
            **self._common_detail(live, "card", card.id),
        }

    def _faq_detail(self, live: AdminStore, faq: Any) -> dict:
        # FAQ 正文里的武将 / 技能 / 卡牌 / 关键词 / 时点名自动变成可点击链接
        segments = live.annotate(faq.answer)
        mentions = live.mentions(faq.answer, limit=40)

        groups: list[dict] = []
        buckets: dict[str, list[dict]] = {}
        for hit in mentions:
            buckets.setdefault(str(hit["kind"]), []).append(hit)
        for kind, label in (
            ("general", "相关武将"),
            ("skill", "相关技能"),
            ("card", "相关卡牌 / 指示物"),
            ("keyword", "相关关键词"),
            ("timing", "相关时点"),
        ):
            hits = buckets.get(kind) or []
            if not hits:
                continue
            groups.append(
                {
                    "label": label,
                    # 键名不能叫 items / keys / values：Jinja2 取属性优先于取键，
                    # `group.items` 会命中 dict.items 方法，报 “not iterable”。
                    "links": [
                        {
                            "name": live.display_name(kind, live.entity(kind, h["id"]))
                            or h["name"],
                            "url": entity_url(kind, str(h["id"])),
                        }
                        for h in hits
                    ],
                }
            )

        return {
            "eyebrow": "FAQ / 裁定",
            "title": faq.question,
            "subtitle": "",
            "image_url": "",
            "tags": [
                {"text": MODE_LABELS.get(faq.mode, faq.mode), "class": ""},
            ],
            "edit_url": f"/edit/faq/{faq.id}",
            "copy_url": f"/new/faq?from={faq.id}",
            "delete_url": f"/delete/faq/{faq.id}",
            "back_url": "/wiki/faq",
            "back_label": "FAQ",
            "fields": [
                {"k": "模式", "v": MODE_LABELS.get(faq.mode, faq.mode)},
                {"k": "来源", "v": _source_label(faq)},
            ],
            "prose_blocks": [
                {
                    "title": "问题",
                    "segments": [{"text": faq.question}],
                    "meta": "",
                    "note": "",
                },
                {
                    "title": "答案",
                    "segments": segments,
                    "meta": f"{len(mentions)} 个关联实体",
                    "note": "正文里的资料名是自动识别的，可以直接点进去。",
                },
            ],
            "skill_rows": [],
            "version_rows": [],
            "owners": [],
            "variant_rows": [],
            "refs": groups,
            "effect_skill_rows": [],
            "faqs": None,  # FAQ 详情页不需要「相关 FAQ」（自己就是 FAQ）
            **self._common_detail(live, "faq", faq.id),
        }


# ---------------------------------------------------------------- 辅助


def _redirect_with_edit_mode(next_url: str, on: bool) -> RedirectResponse:
    """只允许跳回本站相对路径，避免开放重定向。"""
    target = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/wiki"
    response = RedirectResponse(target, status_code=303)
    if on:
        response.set_cookie(
            "sgs_edit", "1", max_age=60 * 60 * 24 * 30, samesite="lax", httponly=True
        )
    else:
        response.delete_cookie("sgs_edit")
    return response


def _flash(request: Request) -> list[dict]:
    level = request.query_params.get("ok")
    if not level:
        return []
    text = request.query_params.get("msg") or ""
    return [{"level": level, "text": text}] if text else []


def _hp(value: Any) -> str:
    if value is None:
        return ""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def _variant_summary(row: dict) -> str:
    variant = row["entity"]
    bits = [
        MODE_LABELS.get(getattr(variant, "mode", "") or "", getattr(variant, "mode", "") or ""),
        getattr(variant, "faction", "") or "",
        (
            f"{_hp(getattr(variant, 'hp', None))} 体力"
            if getattr(variant, "hp", None) is not None
            else ""
        ),
        getattr(variant, "title", "") or "",
        row.get("set_display") or "",
        row.get("product_name") or "",
    ]
    return " · ".join(b for b in bits if b)


def _variant_tags(variant: Any) -> list[dict]:
    """武将详情头部的标签。

    核验状态已从数据模型删除，这里不再产生任何标签（保留函数便于以后加别的标签）。
    """
    return []


def _source_label(entity: Any) -> str:
    source = getattr(entity, "source", None)
    if source is None:
        return ""
    reference = getattr(source, "reference", "") or ""
    if reference in SOURCE_TITLE_BY_REFERENCE:
        return SOURCE_TITLE_BY_REFERENCE[reference]
    base = SOURCE_LABELS.get(getattr(source, "type", ""), getattr(source, "type", ""))
    return f"{base}（{reference}）" if reference else base


def _placeholder(section: str) -> str:
    return {
        "generals": "搜索武将名 / 称号 / 编号 / 技能名",
        "skills": "搜索技能名 / 技能描述",
        "cards": "搜索卡名 / 分类",
        "markers": "搜索指示物名 / 分类",
        "faq": "搜索问题 / 答案 / 关联对象",
    }.get(section, "搜索…")


def _new_url(kind: str, section: str) -> str:
    """新增入口。卡牌与指示物都走卡牌录入页，并预填一级分类。"""
    if kind == "card":
        category_1 = CARD_CATEGORY_1_FOR_WIKI.get(section, "")
        return f"/new/card?category_1={category_1}" if category_1 else "/new/card"
    return f"/new/{kind}"


def _empty_title(section: str) -> str:
    if section == "markers":
        return "暂无指示物资料"
    return "暂无资料"


def _empty_text(section: str) -> str:
    if section == "markers":
        return (
            "当前数据库里还没有任何 category_1 为「指示物」的记录 —— 这是正常状态"
            "（原有指示物数据已删除）。开启编辑模式后可以在这里逐条录入。"
        )
    return "当前分类下还没有资料。"


def _empty_hint(section: str) -> bool:
    """空状态里是否显示「怎么新增第一条指示物」的引导（内容写在模板里，避免转义）。"""
    return section == "markers"


def _query_url(base: str, **params: Any) -> str:
    from urllib.parse import urlencode

    clean = {k: v for k, v in params.items() if v not in (None, "", 0)}
    return f"{base}?{urlencode(clean)}" if clean else base


def _make_filter_url(base: str, **base_params: Any) -> Callable[..., str]:
    """构造「保留当前筛选 + 覆盖若干条件」的 URL 生成器。

    在**闭包外**先把基础条件固化成 dict，避免 lambda 形参遮蔽闭包变量
    （形参名与下面的键同名时，函数体里读到的是形参而不是当前值）。
    """

    def build(**overrides: Any) -> str:
        merged = dict(base_params)
        merged.update(overrides)
        return _query_url(base, **merged)

    return build


def _make_page_url(base: str, **base_params: Any) -> Callable[[int], str]:
    """分页 URL 生成器（同样先把基础条件固化）。"""

    def build(page: int) -> str:
        merged = dict(base_params)
        merged["page"] = page
        return _query_url(base, **merged)

    return build
