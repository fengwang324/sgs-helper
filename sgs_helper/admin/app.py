"""本地资料管理网页（FastAPI + Jinja2）。

启动::

    uvicorn sgs_helper.admin.app:app --host 0.0.0.0 --port 8000
    # 或
    python scripts/admin.py --port 8000

只服务于**本地资料维护**：无登录、无用户体系、无并发写入设计目标。
数据流向永远只有一条：

    浏览器编辑 → 写回 YAML → validate → build_db → SQLite 更新

网页**不会**直接改 SQLite；所有 SQLite 变化都由 ``build_database()`` 全量重建产生。
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..constants import (
    CARD_CATEGORY_TREE,
    GENDER_LABELS,
    KIND_BY_NAME,
    KIND_ORDER,
    MODE_LABELS,
)
from ..validate import REFERENCE_FIELDS
from . import editors, forms, ocr_test, wiki
from .store import (
    AdminStore,
    SaveOutcome,
    truncate,
)

ADMIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = ADMIN_DIR.parents[1]
PER_PAGE = 50


def default_data_dir() -> Path:
    env = os.environ.get("SGS_DATA_DIR")
    if env:
        return Path(env)
    if (Path.cwd() / "data").is_dir():
        return Path.cwd() / "data"
    return REPO_ROOT / "data"


def default_db_path() -> Path:
    env = os.environ.get("SGS_DB")
    if env:
        return Path(env)
    if (Path.cwd() / "data").is_dir():
        return Path.cwd() / "build" / "sgs.db"
    return REPO_ROOT / "build" / "sgs.db"


# ---------------------------------------------------------------- 模板


def static_asset_url(name: str) -> str:
    """内容变化时换 URL，让浏览器加载与当前模板配套的脚本 / 样式。"""
    digest = hashlib.sha256((ADMIN_DIR / "static" / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"


def build_templates() -> Jinja2Templates:
    # starlette 1.0 起 Jinja2Templates 不再接受 autoescape/trim_blocks 等
    # 环境参数，改为自行构造 jinja2.Environment 传进去（旧版本同样支持 env=）。
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ADMIN_DIR / "templates")),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    templates = Jinja2Templates(env=env)
    env.filters.update(
        {
            "mode_label": lambda v: MODE_LABELS.get(v, v or ""),
            "gender_label": lambda v: GENDER_LABELS.get(v, v or ""),
            "short": truncate,
            "is_url": lambda v: isinstance(v, str) and v.startswith(("http://", "https://")),
        }
    )
    env.globals.update(
        {
            "KIND_ORDER": KIND_ORDER,
            "KIND_BY_NAME": KIND_BY_NAME,
            "MODE_LABELS": MODE_LABELS,
            "reference_fields": REFERENCE_FIELDS,
            "static_asset_url": static_asset_url,
        }
    )
    return templates


templates = build_templates()


# ---------------------------------------------------------------- 应用


def _ocr_image_index(live: AdminStore) -> list[dict]:
    """data/card_images 下的卡图，配上可读标签，方便在 OCR 页里直接挑一张试。"""
    out: list[dict] = []
    for kind, prefix in (("general_variant", "武将"), ("card", "游戏牌")):
        for entity in live.bundle.all(kind).values():
            image = getattr(entity, "image", "") or ""
            if not image:
                continue
            if kind == "general_variant":
                general = live.bundle.get("general", getattr(entity, "general_id", ""))
                name = getattr(general, "name", "") or entity.id
                product_name = live.name_of("product", getattr(entity, "product_id", None))
                mode = MODE_LABELS.get(getattr(entity, "mode", "") or "", getattr(entity, "mode", "") or "")
                label = f"{prefix}：{name}（{product_name or mode or entity.id}）"
            else:
                label = (
                    f"{prefix}：{getattr(entity, 'name', '') or entity.id}"
                    f"（{getattr(entity, 'category_2', '')}/{getattr(entity, 'category_3', '')}）"
                )
            out.append({"file": image, "label": label, "id": entity.id})
    out.sort(key=lambda r: (r["label"], r["file"]))
    return out

def create_app(data_dir: Path | None = None, db_path: Path | None = None) -> FastAPI:
    store = AdminStore(data_dir or default_data_dir(), db_path or default_db_path())

    app = FastAPI(
        title="sgs-helper 本地资料管理",
        description="YAML 是唯一权威数据源；本页面只负责编辑 YAML 并重建 SQLite。",
        version="0.1.0",
    )
    app.state.store = store
    #: 极简 CSRF 防护：本地工具不做登录，但一个随机 token 就能挡住
    #: “恶意网页诱导浏览器向 127.0.0.1:8000 提交表单”这类跨站写入。
    app.state.csrf = secrets.token_urlsafe(24)
    static_dir = ADMIN_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 卡图：data/card_images/ 下的单卡图片，供详情页对照实体卡
    images_dir = store.data_dir / "card_images"
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover
        pass
    if images_dir.is_dir():
        app.mount("/card-images", StaticFiles(directory=str(images_dir)), name="card-images")

    # ------------------------------------------------------------ 上下文

    def ctx(request: Request, *, title: str = "", **extra: Any) -> dict:
        live: AdminStore = request.app.state.store
        counts = live.counts()
        bundle = live.bundle
        payload: dict[str, Any] = {
            "request": request,
            "title": title,
            "csrf": request.app.state.csrf,
            "kinds": [
                {"kind": kind, "label": KIND_BY_NAME[kind].label, "count": counts.get(kind, 0)}
                for kind in KIND_ORDER
            ],
            "counts": counts,
            "total": bundle.total,
            "validation": live.snapshot(),
            "db_state": live.db_state(),
            "flash": _flash_from_query(request),
            "current_kind": "",
            "q": "",
            "name_of": live.name_of,
            "hp_label": hp_label,
        }
        payload.update(extra)
        return payload

    def require_kind(kind: str) -> str:
        if kind not in KIND_BY_NAME:
            raise HTTPException(status_code=404, detail=f"未知的数据类型：{kind}")
        return kind

    async def verified_form(request: Request):
        """读取表单并校验 CSRF token。"""
        form = await request.form()
        token = str(form.get("_csrf", ""))
        if not token or not secrets.compare_digest(token, request.app.state.csrf):
            raise HTTPException(
                status_code=403,
                detail="表单已失效（CSRF token 不匹配）：本页面只接受自己页面提交的表单。请回到页面刷新后重试。",
            )
        return form

    def render(req: Request, template: str, **extra: Any) -> HTMLResponse:
        # ctx() 里本来就有 "request"，这里去掉，改由 Starlette 注入，避免重复传参
        extra.pop("request", None)
        return templates.TemplateResponse(req, template, extra)

    # ------------------------------------------------------------ 本地 Wiki
    # /wiki/* 的页面逻辑集中在 sgs_helper/admin/wiki.py；写入仍然走下面的
    # /new|/edit|/delete，避免第二套写入实现。
    app.include_router(wiki.WikiRoutes(templates).router)

    # ------------------------------------------------------------ 首页

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        snap = live.snapshot()
        return render(
            request,
            "index.html",
            **ctx(
                request,
                title="首页",
                snap=snap,
                errors=snap.errors[:20],
                warnings=snap.warnings[:20],
            ),
        )

    # ------------------------------------------------------------ 搜索

    @app.get("/search", response_class=HTMLResponse)
    def search_page(
        request: Request,
        q: str = "",
        kind: str = "",
        mode: str = "",
    ) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        kinds = [kind] if kind in KIND_BY_NAME else None
        rows = live.search(q, kinds=kinds, limit=500) if q.strip() else []
        if mode:
            rows = [row for row in rows if row["mode"] == mode]
        return render(
            request,
            "search.html",
            **ctx(
                request,
                title=f"搜索：{q}" if q else "搜索",
                rows=rows,
                q=q,
                kind=kind,
                mode=mode,
            ),
        )

    # ------------------------------------------------------------ 列表

    @app.get("/list/{kind}", response_class=HTMLResponse)
    def list_page(
        request: Request,
        kind: str,
        q: str = "",
        mode: str = "",
        product: str = "",
        set_name: str = "",
        faction: str = "",
        category_1: str = "",
        category_2: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        rows = live.filter_rows(kind, q=q, mode=mode, product=product,
                                set_name=set_name, faction=faction,
                                category_1=category_1, category_2=category_2)
        total = len(rows)
        pages = max(1, math.ceil(total / PER_PAGE))
        page = min(max(1, page), pages)
        start = (page - 1) * PER_PAGE
        return render(
            request,
            "list.html",
            **ctx(
                request,
                title=f"{KIND_BY_NAME[kind].label}列表",
                current_kind=kind,
                kind=kind,
                rows=rows[start : start + PER_PAGE],
                total=total,
                page=page,
                pages=pages,
                q=q,
                mode=mode,
                product=product,
                set_name=set_name,
                faction=faction,
                category_1=category_1,
                category_2=category_2,
                options=live.filter_options(kind),
                spec=KIND_BY_NAME[kind],
            ),
        )

    # ------------------------------------------------------------ 详情

    @app.get("/view/{kind}/{entity_id}", response_class=HTMLResponse)
    def view_page(
        request: Request,
        kind: str,
        entity_id: str,
        action_error: str = "",
    ) -> HTMLResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        entity = live.entity(kind, entity_id)
        if entity is None:
            raise HTTPException(
                status_code=404,
                detail=f"{KIND_BY_NAME[kind].label} {entity_id} 不存在（数据库页面显示的是 YAML 权威源）",
            )
        row = live.describe(kind, entity)
        detail: dict[str, Any] = {}
        if kind == "skill":
            detail = live.skill_view(entity_id) or {}
        elif kind == "general":
            detail = live.general_view(entity_id) or {}
        elif kind == "card":
            detail = live.card_view(entity_id) or {}
        elif kind == "product":
            detail = live.product_view(entity_id) or {}
        elif kind == "skill_version":
            detail = {
                # 技能文本里的技能名 / 关键词名 / 卡牌名自动变成链接
                "segments": live.annotate(entity.text),
                "bound_variants": live.variants_using_version(entity_id),
            }
        elif kind == "faq":
            # FAQ 的手工关联字段已删除：关联关系按实体名称从正文里自动识别
            detail = {
                "mentions": live.mentions(
                    f"{entity.question}\n{entity.answer}"
                )
            }
        return render(
            request,
            "detail.html",
            **ctx(
                request,
                title=row["name"] or entity_id,
                current_kind=kind,
                kind=kind,
                row=row,
                entity=entity,
                detail=detail,
                fields=_detail_fields(live, kind, entity),
                references=live.reverse_refs(kind, entity_id),
                yaml_text=live.file_text(kind, entity_id),
                yaml_path=row["file"],
                action_error=action_error,
                source=row["source"],
                source_text=row["source_text"],
            ),
        )


    @app.get("/raw/{kind}/{entity_id}", response_class=PlainTextResponse)
    def raw_page(request: Request, kind: str, entity_id: str) -> PlainTextResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        if live.entity(kind, entity_id) is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return PlainTextResponse(live.file_text(kind, entity_id), media_type="text/plain; charset=utf-8")

    # ------------------------------------------------------------ OCR 试跑

    @app.get("/ocr", response_class=HTMLResponse)
    def ocr_page(request: Request) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        return render(
            request,
            "ocr.html",
            **ctx(
                request,
                title="OCR 试跑",
                current_kind="_ocr",
                models=list(ocr_test.MODELS),
                default_model=ocr_test.DEFAULT_MODEL,
                token_tail=(ocr_test.token() or "")[-6:],
                token_from_env=bool(os.environ.get("PADDLEOCR_ACCESS_TOKEN")),
                images=_ocr_image_index(live),
            ),
        )

    @app.post("/ocr/token")
    async def ocr_save_token(request: Request):
        form = await request.form()
        if form.get("_csrf") != request.app.state.csrf:
            raise HTTPException(status_code=403, detail="CSRF token 不匹配")
        value = str(form.get("token") or "").strip()
        if value:
            ocr_test.save_token(value)
        return RedirectResponse("/ocr?saved=1", status_code=303)

    @app.post("/api/ocr-test")
    async def ocr_api(request: Request):
        if request.headers.get("x-csrf-token") != request.app.state.csrf:
            raise HTTPException(status_code=403, detail="CSRF token 不匹配")
        form = await request.form()
        model = str(form.get("model") or ocr_test.DEFAULT_MODEL)
        if model not in ocr_test.MODELS:
            return JSONResponse({"ok": False, "error": f"不支持的模型：{model}"})
        token_override = str(form.get("token") or "").strip()
        if token_override:
            os.environ["PADDLEOCR_ACCESS_TOKEN"] = token_override
        upload = form.get("file")
        name = str(form.get("image_name") or "").strip()
        live: AdminStore = request.app.state.store
        if upload is not None and getattr(upload, "filename", ""):
            data = await upload.read()
            fname = upload.filename or "image.png"
        elif name:
            safe = os.path.basename(name)
            path = live.data_dir / "card_images" / safe
            if not path.is_file():
                return JSONResponse({"ok": False, "error": f"找不到卡图：{safe}"})
            data = path.read_bytes()
            fname = safe
        else:
            return JSONResponse({"ok": False, "error": "没有收到图片（可以粘贴截图或选一张卡图）"})
        # 调 PaddleOCR 是阻塞 IO（提交 + 轮询），必须丢到线程池，
        # 否则会卡住 uvicorn 的事件循环、整站无响应。
        try:
            result = await run_in_threadpool(ocr_test.run, data, fname, model)
        except Exception as exc:  # pragma: no cover
            result = {"ok": False, "error": f"识别异常：{exc}", "model": model}
        result["image"] = name or fname
        return JSONResponse(result)

    # ------------------------------------------------------------ 专用录入页：实体产品

    def hp_label(value: Any) -> str:
        """体力显示：3 而不是 3.0；1.5 照常。"""
        if value in (None, ""):
            return "未填"
        number = float(value)
        return str(int(number)) if number == int(number) else str(number)

    def _editor_ctx(request: Request, kind: str, **extra: Any) -> dict:
        """产品 / 武将录入页共用的上下文（含下拉候选与高级字段选项）。"""
        live: AdminStore = request.app.state.store
        payload = ctx(request, current_kind=kind, kind=kind, spec=KIND_BY_NAME[kind], **extra)
        payload.update(
            {
                "error_map": {},
                "issue_list": [],
                "outcome_error": "",
                "info": [],
                "base_sha": "",
                "mode_choices": editors.mode_choices(),
                "gender_choices": editors.gender_choices(),
                "hp_choices": editors.HP_CHOICES,
                "csrf": request.app.state.csrf,
            }
        )
        payload.update(extra)
        return payload

    def _editor_failure(
        request: Request,
        template: str,
        kind: str,
        result: editors.EditorResult,
        action: str,
        editor_mode: str,
        *,
        target_id: str = "",
        outcome: SaveOutcome | None = None,
        extra: dict | None = None,
    ) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        error_map: dict[str, list[str]] = {}
        for error in result.errors:
            error_map.setdefault(error.field, []).append(error.message)

        # 额外跑一次“只校验不写盘”，把 文件 / 字段 / 原因 也展示出来
        issues: list = list(outcome.issues) if outcome is not None else []
        if outcome is None and result.changes:
            try:
                probe = live.validate_changes(result.changes)
                issues = list(probe.errors)
            except Exception:  # pragma: no cover - 展示用，失败不影响主流程
                issues = []
        for issue in issues:
            error_map.setdefault(issue_field(issue.field), []).append(issue.message)

        response = render(
            request,
            template,
            **_editor_ctx(
                request,
                kind,
                title=f"{'新增' if editor_mode == 'create' else '编辑'}{KIND_BY_NAME[kind].label}",
                values=result.values,
                rows=result.rows,
                mode=editor_mode,
                target_id=target_id,
                action=action,
                error_map=error_map,
                issue_list=issues,
                outcome_error=outcome.error if outcome else "",
                info=result.info,
                **(extra or {}),
            ),
        )
        response.status_code = 400
        return response

    def _product_editor_extra(request: Request) -> dict:
        live: AdminStore = request.app.state.store
        return {
            "all_general_sets": live.suggest_values("general_set", limit=60),
            "all_card_sets": sorted(
                {
                    value
                    for product in live.bundle.all("product").values()
                    for value in (product.card_sets or [])
                }
            ),
            "products": [
                {"id": pid, "name": product.name}
                for pid, product in live.entries("product")
            ],
        }

    @app.get("/new/product", response_class=HTMLResponse)
    def new_product(request: Request) -> HTMLResponse:
        require_kind("product")
        return render(
            request,
            "product_edit.html",
            **_editor_ctx(
                request,
                "product",
                title="新增实体产品",
                values={},
                mode="create",
                target_id="",
                action="/new/product",
                product=None,
                **_product_editor_extra(request),
            ),
        )

    @app.post("/new/product", response_class=HTMLResponse)
    async def create_product(request: Request) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        form = await verified_form(request)
        base_sha = require_base_sha(form)
        result = editors.parse_product_form(form, store=live)
        if result.errors:
            return _editor_failure(
                request, "product_edit.html", "product", result, "/new/product", "create",
                extra=_product_editor_extra(request),
            )
        result.changes[0].expected_sha = base_sha
        outcome = live.save_batch(result.changes)
        if not outcome.ok:
            return _editor_failure(
                request, "product_edit.html", "product", result, "/new/product", "create",
                outcome=outcome, extra=_product_editor_extra(request),
            )
        return RedirectResponse(
            f"/view/product/{result.record_id}?created=1&warn={len(outcome.warnings)}",
            status_code=303,
        )

    @app.get("/edit/product/{entity_id}", response_class=HTMLResponse)
    def edit_product(request: Request, entity_id: str) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        product = live.entity("product", entity_id)
        if product is None:
            raise HTTPException(status_code=404, detail="产品不存在")
        return render(
            request,
            "product_edit.html",
            **_editor_ctx(
                request,
                "product",
                title=f"编辑实体产品：{product.name}",
                values=editors.product_values(live, product),
                mode="edit",
                target_id=entity_id,
                action=f"/edit/product/{entity_id}",
                product=product,
                base_sha=live.entity_sha("product", entity_id),
                references=live.reverse_refs("product", entity_id),
                **_product_editor_extra(request),
            ),
        )

    @app.post("/edit/product/{entity_id}", response_class=HTMLResponse)
    async def update_product(request: Request, entity_id: str) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        product = live.entity("product", entity_id)
        if product is None:
            raise HTTPException(status_code=404, detail="产品不存在")
        form = await verified_form(request)
        base_sha = require_base_sha(form)
        result = editors.parse_product_form(form, store=live, existing=product)
        if result.errors:
            return _editor_failure(
                request, "product_edit.html", "product", result,
                f"/edit/product/{entity_id}", "edit", target_id=entity_id,
                extra=_product_editor_extra(request),
            )
        result.changes[0].expected_sha = base_sha
        outcome = live.save_batch(result.changes)
        if not outcome.ok:
            return _editor_failure(
                request, "product_edit.html", "product", result,
                f"/edit/product/{entity_id}", "edit", target_id=entity_id,
                outcome=outcome, extra=_product_editor_extra(request),
            )
        return RedirectResponse(
            f"/view/product/{result.record_id}?saved=1&warn={len(outcome.warnings)}",
            status_code=303,
        )

    # ------------------------------------------------------------ 专用录入页：武将

    def _general_editor_extra(request: Request) -> dict:
        live: AdminStore = request.app.state.store
        return {
            "faction_suggestions": live.suggest_values("faction", limit=40),
            "general_set_suggestions": live.suggest_values("general_set", limit=60),
        }

    def _general_params(request: Request) -> dict[str, str]:
        # 注意：这里**不能**包含 "variant" —— URL 上的 ?variant=<版本ID> 是用来选中
        # 哪个版本的，如果当成初值会把它覆盖到「版本标签」输入框里（保存后标签变成 ID）。
        allowed = {
            "name", "title", "general_number", "mode", "faction", "hp", "gender",
            "product_id", "general_set", "id",
        }
        return {
            key: value
            for key, value in request.query_params.items()
            if key in allowed
        }

    @app.get("/new/general", response_class=HTMLResponse)
    def new_general(request: Request) -> HTMLResponse:
        require_kind("general")
        live: AdminStore = request.app.state.store
        values = editors.general_values(live, None, None, params=_general_params(request))
        values["product_name"] = live.name_of("product", values.get("product_id"))

        rows: list[dict] = []
        template_source = None
        inherit_source = None
        source_id = request.query_params.get("from", "")
        copy_attrs = request.query_params.get("copy") == "attrs"
        if source_id:
            entity = live.entity("general", source_id)
            variants = (
                live.variants_of(source_id) if entity is not None else []
            )
            first = variants[0] if variants else None
            if entity is not None:
                # 无论哪种入口：产品 / 模式 / 武将牌系列都带入（它们现在属于版本）
                if first is not None:
                    values["product_id"] = values.get("product_id") or (first.product_id or "")
                    values["mode"] = values.get("mode") or (first.mode or "")
                    values["general_set"] = values.get("general_set") or (first.general_set or "")
                if copy_attrs:
                    # 「以此武将为模板新增」：势力 / 体力 / 性别可选择复制（默认带上）
                    template_source = entity
                    if first is not None:
                        for field_name in ("faction", "hp", "gender"):
                            value = getattr(first, field_name, None)
                            if value not in (None, ""):
                                values.setdefault(
                                    field_name, "" if value is None else str(value)
                                )
                else:
                    # 「保存并新增下一位」：只继承产品信息
                    inherit_source = entity
        return render(
            request,
            "general_edit.html",
            **_editor_ctx(
                request,
                "general",
                title="新增武将",
                values=values,
                rows=rows,
                mode="create",
                target_id="",
                variant_id="",
                variant_label=values.get("product_name") or values.get("mode") or "武将版本",
                action="/new/general",
                general=None,
                template_source=template_source,
                inherit_source=inherit_source,
                **_general_editor_extra(request),
            ),
        )

    @app.post("/new/general", response_class=HTMLResponse)
    async def create_general(request: Request) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        form = await verified_form(request)
        base_sha = require_base_sha(form)
        result = editors.parse_general_form(form, store=live)
        if result.errors:
            return _editor_failure(
                request, "general_edit.html", "general", result, "/new/general", "create",
                extra=_general_editor_extra(request),
            )
        # 新增：General 一定不存在（_base_sha 为空串表示“这条记录还不该存在”）
        result.changes[0].expected_sha = base_sha
        outcome = live.save_batch(result.changes)
        if not outcome.ok:
            return _editor_failure(
                request, "general_edit.html", "general", result, "/new/general", "create",
                outcome=outcome, extra=_general_editor_extra(request),
            )
        action = str(form.get("_action", "save"))
        if action == "save_next":
            inherit = str(form.get("_inherit", "")) == "1"
            target = (
                f"/new/general?from={result.record_id}&created=1"
                if inherit
                else "/new/general?created=1"
            )
            return RedirectResponse(target, status_code=303)
        return RedirectResponse(
            f"/view/general/{result.record_id}?created=1&warn={len(outcome.warnings)}"
            f"&info={len(result.info)}",
            status_code=303,
        )

    @app.get("/edit/general/{entity_id}", response_class=HTMLResponse)
    def edit_general(request: Request, entity_id: str) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        general = live.entity("general", entity_id)
        if general is None:
            raise HTTPException(status_code=404, detail="武将不存在")

        variants = live.variants_of(entity_id)
        wanted = request.query_params.get("variant", "")
        adding_variant = wanted == "new"
        variant = None
        if not adding_variant:
            if wanted:
                variant = live.entity("general_variant", wanted)
                if variant is None or variant.general_id != entity_id:
                    raise HTTPException(status_code=404, detail="武将版本不存在")
            elif variants:
                variant = variants[0]

        values = editors.general_values(
            live, general, variant, params=_general_params(request)
        )
        if adding_variant:
            values["product_name"] = live.name_of("product", values.get("product_id"))
            values["variant_id"] = ""
            values["title"] = ""
            values["general_number"] = ""
            if variants:
                first = variants[0]
                values.setdefault("mode", first.mode or "")
                values.setdefault("product_id", first.product_id or "")
        return render(
            request,
            "general_edit.html",
            **_editor_ctx(
                request,
                "general",
                title=f"编辑武将：{general.name}",
                values=values,
                rows=editors.rows_from_variant(live, variant),
                mode="edit",
                target_id=entity_id,
                variant_id=(variant.id if variant else ""),
                variant_label=(values.get("product_name") or values.get("mode") or "武将版本"),
                action=f"/edit/general/{entity_id}",
                general=general,
                variant=variant,
                all_variants=variants,
                adding_variant=adding_variant,
                base_sha=live.entity_sha("general", entity_id),
                variant_sha=(live.entity_sha("general_variant", variant.id) if variant else ""),
                references=live.reverse_refs("general", entity_id),
                template_source=None,
                **_general_editor_extra(request),
            ),
        )

    @app.post("/edit/general/{entity_id}", response_class=HTMLResponse)
    async def update_general(request: Request, entity_id: str) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        general = live.entity("general", entity_id)
        if general is None:
            raise HTTPException(status_code=404, detail="武将不存在")
        form = await verified_form(request)
        base_sha = require_base_sha(form)
        variant_sha = str(form.get("_variant_sha", "") or "")
        variant_id = str(form.get("_variant_id", "") or "")
        variant = live.entity("general_variant", variant_id) if variant_id else None
        if variant is not None and variant.general_id != entity_id:
            raise HTTPException(status_code=404, detail="武将版本不属于这个武将")

        result = editors.parse_general_form(
            form, store=live, existing=general, variant=variant
        )
        if result.errors:
            return _editor_failure(
                request, "general_edit.html", "general", result,
                f"/edit/general/{entity_id}", "edit", target_id=entity_id,
                extra=_general_editor_extra(request),
            )
        result.changes[0].expected_sha = base_sha
        # 版本这一条：新建时要求“还不存在”，编辑时要求内容哈希没变
        for change in result.changes:
            if change.kind == "general_variant":
                change.expected_sha = variant_sha if variant is not None else ""
        outcome = live.save_batch(result.changes)
        if not outcome.ok:
            return _editor_failure(
                request, "general_edit.html", "general", result,
                f"/edit/general/{entity_id}", "edit", target_id=entity_id,
                outcome=outcome, extra=_general_editor_extra(request),
            )
        action = str(form.get("_action", "save"))
        if action == "save_next":
            return RedirectResponse(
                f"/new/general?from={result.record_id}&saved=1", status_code=303
            )
        return RedirectResponse(
            f"/view/general/{result.record_id}?saved=1&warn={len(outcome.warnings)}"
            f"&info={len(result.info)}&variant={result.variant_id}",
            status_code=303,
        )

    # ------------------------------------------------------------ 专用录入页：卡牌

    def _card_editor_extra(request: Request) -> dict:
        live: AdminStore = request.app.state.store
        return {
            # 联动下拉的候选 = constants 的分类树 ∪ 现有卡牌里出现过的值
            "category_tree": editors.card_category_tree(),
            "category_triples": editors.card_category_triples(live),
        }

    def _card_params(request: Request) -> dict[str, str]:
        # 只接受卡牌自己的字段，避免 URL 上的别的参数被当成初值写进表单
        allowed = {"name", "category_1", "category_2", "category_3", "image", "card_set"}
        return {
            key: value
            for key, value in request.query_params.items()
            if key in allowed and value
        }

    @app.get("/new/card", response_class=HTMLResponse)
    def new_card(request: Request) -> HTMLResponse:
        require_kind("card")
        live: AdminStore = request.app.state.store
        return render(
            request,
            "card_edit.html",
            **_editor_ctx(
                request,
                "card",
                title="新增卡牌",
                values=editors.card_values(live, None, params=_card_params(request)),
                rows=[],
                mode="create",
                target_id="",
                action="/new/card",
                card=None,
                **_card_editor_extra(request),
            ),
        )

    @app.post("/new/card", response_class=HTMLResponse)
    async def create_card(request: Request) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        form = await verified_form(request)
        base_sha = require_base_sha(form)
        result = editors.parse_card_form(form, store=live)
        if result.errors:
            return _editor_failure(
                request, "card_edit.html", "card", result, "/new/card", "create",
                extra=_card_editor_extra(request),
            )
        # 新增：卡牌一定还不存在（_base_sha 为空串表示“这条记录还不该存在”）
        result.changes[0].expected_sha = base_sha
        outcome = live.save_batch(result.changes)
        if not outcome.ok:
            return _editor_failure(
                request, "card_edit.html", "card", result, "/new/card", "create",
                outcome=outcome, extra=_card_editor_extra(request),
            )
        if str(form.get("_action", "save")) == "save_next":
            return RedirectResponse("/new/card?created=1", status_code=303)
        return RedirectResponse(
            f"/view/card/{result.record_id}?created=1&warn={len(outcome.warnings)}",
            status_code=303,
        )

    @app.get("/edit/card/{entity_id}", response_class=HTMLResponse)
    def edit_card(request: Request, entity_id: str) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        card = live.entity("card", entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="卡牌不存在")
        return render(
            request,
            "card_edit.html",
            **_editor_ctx(
                request,
                "card",
                title=f"编辑卡牌：{card.name}",
                values=editors.card_values(live, card),
                rows=editors.card_skill_rows(live, card),
                mode="edit",
                target_id=entity_id,
                action=f"/edit/card/{entity_id}",
                card=card,
                base_sha=live.entity_sha("card", entity_id),
                references=live.reverse_refs("card", entity_id),
                **_card_editor_extra(request),
            ),
        )

    @app.post("/edit/card/{entity_id}", response_class=HTMLResponse)
    async def update_card(request: Request, entity_id: str) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        card = live.entity("card", entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="卡牌不存在")
        form = await verified_form(request)
        base_sha = require_base_sha(form)
        result = editors.parse_card_form(form, store=live, existing=card)
        retry_extra = {**_card_editor_extra(request), "base_sha": base_sha}
        if result.errors:
            return _editor_failure(
                request, "card_edit.html", "card", result,
                f"/edit/card/{entity_id}", "edit", target_id=entity_id,
                extra=retry_extra,
            )
        result.changes[0].expected_sha = base_sha
        outcome = live.save_batch(result.changes)
        if not outcome.ok:
            return _editor_failure(
                request, "card_edit.html", "card", result,
                f"/edit/card/{entity_id}", "edit", target_id=entity_id,
                outcome=outcome, extra=retry_extra,
            )
        if str(form.get("_action", "save")) == "save_next":
            return RedirectResponse("/new/card?saved=1", status_code=303)
        return RedirectResponse(
            f"/view/card/{result.record_id}?saved=1&warn={len(outcome.warnings)}",
            status_code=303,
        )

    # ------------------------------------------------------------ 录入辅助 JSON API

    @app.get("/api/skills")
    def api_skills(request: Request, q: str = "", limit: int = 20) -> list[dict]:
        live: AdminStore = request.app.state.store
        return live.skill_search(q, limit=max(1, min(limit, 50)))

    @app.get("/api/skills/{skill_id}/versions")
    def api_skill_versions(
        request: Request, skill_id: str, mode: str = "", product_id: str = ""
    ) -> dict:
        live: AdminStore = request.app.state.store
        return {
            "skill_id": skill_id,
            "skill_name": live.name_of("skill", skill_id),
            "versions": live.skill_version_options(
                skill_id, mode=mode or None, product_id=product_id or None
            ),
        }

    @app.get("/api/products")
    def api_products(request: Request, q: str = "", limit: int = 20) -> list[dict]:
        live: AdminStore = request.app.state.store
        return live.product_search(q, limit=max(1, min(limit, 50)))

    @app.get("/api/products/{product_id}")
    def api_product(request: Request, product_id: str) -> dict:
        """单个产品：武将编辑页用它取 ``Product.general_sets`` 作为 general_set 的候选。"""
        live: AdminStore = request.app.state.store
        product = live.entity("product", product_id)
        if product is None:
            raise HTTPException(status_code=404, detail=f"产品不存在：{product_id}")
        return {
            "id": product.id,
            "name": product.name,
            "release_year": product.release_year,
            "mode": product.mode,
            "mode_label": MODE_LABELS.get(product.mode or "", product.mode or ""),
            "general_sets": live.product_general_sets(product_id),
            "card_sets": list(product.card_sets or []),
        }

    @app.get("/api/suggest")
    def api_suggest(request: Request, field: str = "faction", q: str = "", limit: int = 40) -> list[dict]:
        if field not in {"faction", "general_set", "card_set"}:
            raise HTTPException(
                status_code=400, detail="field 只支持 faction / general_set / card_set"
            )
        live: AdminStore = request.app.state.store
        return live.suggest_values(field, q, limit=max(1, min(limit, 100)))

    @app.get("/api/card-images")
    def api_card_images(request: Request, q: str = "", limit: int = 30) -> list[dict]:
        """卡牌录入页的卡图候选：data/card_images/ 下真实存在的文件名。"""
        live: AdminStore = request.app.state.store
        return live.card_image_options(q, limit=max(1, min(limit, 100)))

    @app.get("/api/generals")
    def api_generals(request: Request, name: str = "", limit: int = 8) -> list[dict]:
        live: AdminStore = request.app.state.store
        return live.generals_named(name, limit=max(1, min(limit, 20)))

    @app.get("/api/version-exists")
    def api_version_exists(
        request: Request, skill_id: str, mode: str = "", product_id: str = "", text: str = ""
    ) -> dict:
        live: AdminStore = request.app.state.store
        found = live.version_by_content(skill_id, mode or None, product_id or None, text)
        return {"exists": found is not None, "version": found}

    # ------------------------------------------------------------ 新增（generic：低频实体）

    @app.get("/new/{kind}", response_class=HTMLResponse)
    def new_page(request: Request, kind: str) -> HTMLResponse:
        require_kind(kind)
        if kind in {"product", "general", "general_variant", "card"}:
            # 产品 / 武将 / 武将版本 / 卡牌都走专用录入页
            # （generic 表单漏掉技能行会清空技能，卡牌的 effect 是嵌套结构也渲染不了）
            raise HTTPException(
                status_code=404, detail=f"{KIND_BY_NAME[kind].label}请使用专用录入页"
            )
        live: AdminStore = request.app.state.store
        values = forms.values_from_defaults(kind)
        params = {key: value for key, value in request.query_params.items() if key in values}
        values = forms.prefill(values, params)
        return render(
            request,
            "edit.html",
            **ctx(
                request,
                title=f"新增{KIND_BY_NAME[kind].label}",
                current_kind=kind,
                kind=kind,
                spec=KIND_BY_NAME[kind],
                specs=forms.form_specs(kind),
                values=values,
                mode="create",
                target_id="",
                errors=[],
                error_map={},
                issue_list=[],
                datalists=_datalists(live, kind),
                action=f"/new/{kind}",
                base_sha="",
            ),
        )

    @app.post("/new/{kind}", response_class=HTMLResponse)
    async def create_entity(request: Request, kind: str) -> HTMLResponse:
        require_kind(kind)
        if kind in {"product", "general", "general_variant", "card"}:
            # 卡牌是嵌套的 effect（text + skills），generic 表单处理不了；走专用页
            raise HTTPException(status_code=404, detail="请使用专用录入页")
        live: AdminStore = request.app.state.store
        form = await verified_form(request)
        candidate, errors = forms.parse_form(kind, form)
        values = forms.values_from_form(kind, form)
        base_sha = require_base_sha(form)
        if errors:
            return _form_error(
                request, render, live, kind, values,
                [(e.field, e.message) for e in errors],
                f"/new/{kind}", action_kind="create", base_sha=base_sha or "",
            )
        outcome = live.save(kind, candidate, expected_sha=base_sha)
        if not outcome.ok:
            return _form_error(
                request, render, live, kind, values,
                [(issue_field(issue.field), issue.message) for issue in outcome.issues],
                f"/new/{kind}", action_kind="create", outcome=outcome,
                base_sha=base_sha or "",
            )
        return RedirectResponse(
            f"/view/{kind}/{outcome.entity_id}?created=1&warn={len(outcome.warnings)}",
            status_code=303,
        )

    # ------------------------------------------------------------ 编辑

    @app.get("/edit/{kind}/{entity_id}", response_class=HTMLResponse)
    def edit_page(request: Request, kind: str, entity_id: str) -> HTMLResponse:
        require_kind(kind)
        if kind in {"product", "general", "general_variant", "card"}:
            # 卡牌是嵌套的 effect（text + skills），generic 表单处理不了；走专用页
            raise HTTPException(status_code=404, detail="请使用专用录入页")
        live: AdminStore = request.app.state.store
        entity = live.entity(kind, entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return render(
            request,
            "edit.html",
            **ctx(
                request,
                title=f"编辑 {live.display_name(kind, entity)}",
                current_kind=kind,
                kind=kind,
                spec=KIND_BY_NAME[kind],
                specs=forms.form_specs(kind),
                values=forms.values_from_entity(kind, entity),
                mode="edit",
                target_id=entity_id,
                errors=[],
                error_map={},
                issue_list=[],
                datalists=_datalists(live, kind),
                action=f"/edit/{kind}/{entity_id}",
                references=live.reverse_refs(kind, entity_id),
                entity=entity,
                base_sha=live.entity_sha(kind, entity_id),
            ),
        )

    @app.post("/edit/{kind}/{entity_id}", response_class=HTMLResponse)
    async def update_entity(request: Request, kind: str, entity_id: str) -> HTMLResponse:
        require_kind(kind)
        if kind in {"product", "general", "general_variant", "card"}:
            # 卡牌是嵌套的 effect（text + skills），generic 表单处理不了；走专用页
            raise HTTPException(status_code=404, detail="请使用专用录入页")
        live: AdminStore = request.app.state.store
        form = await verified_form(request)
        candidate, errors = forms.parse_form(kind, form)
        values = forms.values_from_form(kind, form)
        base_sha = require_base_sha(form)
        if errors:
            return _form_error(
                request, render, live, kind, values,
                [(e.field, e.message) for e in errors],
                f"/edit/{kind}/{entity_id}", action_kind="edit", target_id=entity_id,
                base_sha=base_sha or "",
            )
        outcome = live.save(kind, candidate, old_id=entity_id, expected_sha=base_sha)
        if not outcome.ok:
            return _form_error(
                request, render, live, kind, values,
                [(issue_field(issue.field), issue.message) for issue in outcome.issues],
                f"/edit/{kind}/{entity_id}", action_kind="edit", target_id=entity_id,
                outcome=outcome, base_sha=base_sha or "",
            )
        return RedirectResponse(
            f"/view/{kind}/{outcome.entity_id}?saved=1&warn={len(outcome.warnings)}",
            status_code=303,
        )

    # ------------------------------------------------------------ 删除

    @app.get("/delete/{kind}/{entity_id}", response_class=HTMLResponse)
    def delete_confirm(request: Request, kind: str, entity_id: str) -> HTMLResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        entity = live.entity(kind, entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return render(
            request,
            "delete.html",
            **ctx(
                request,
                title=f"删除 {live.display_name(kind, entity)}",
                current_kind=kind,
                kind=kind,
                entity=entity,
                row=live.describe(kind, entity),
                references=live.reverse_refs(kind, entity_id),
                impact=live.delete_impact(kind, entity_id),
                base_sha=live.entity_sha(kind, entity_id),
            ),
        )

    @app.post("/delete/{kind}/{entity_id}", response_class=HTMLResponse)
    async def delete_entity(request: Request, kind: str, entity_id: str) -> HTMLResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        form = await verified_form(request)
        base_sha = require_base_sha(form)
        outcome = live.delete(kind, entity_id, expected_sha=base_sha)
        if outcome.ok:
            return RedirectResponse(
                f"/list/{kind}?deleted=1&warn={len(outcome.warnings)}", status_code=303
            )
        remaining = live.entity(kind, entity_id)
        row = live.describe(kind, remaining) if remaining is not None else {}
        if outcome.blocked:
            return render(
                request,
                "delete.html",
                **ctx(
                    request,
                    title="无法删除",
                    current_kind=kind,
                    kind=kind,
                    entity=remaining,
                    row=row,
                    references=outcome.references,
                    impact=live.delete_impact(kind, entity_id),
                    base_sha=live.entity_sha(kind, entity_id),
                    action_error=outcome.error,
                ),
            )
        return render(
            request,
            "delete.html",
            **ctx(
                request,
                title="删除失败",
                current_kind=kind,
                kind=kind,
                entity=remaining,
                row=row,
                references=live.reverse_refs(kind, entity_id),
                impact=live.delete_impact(kind, entity_id),
                base_sha=live.entity_sha(kind, entity_id),
                action_error=outcome.error,
                issues=outcome.issues,
            ),
        )

    # ------------------------------------------------------------ 重建

    @app.post("/rebuild")
    async def rebuild(request: Request) -> RedirectResponse:
        live: AdminStore = request.app.state.store
        await verified_form(request)
        outcome = live.rebuild()
        if outcome.ok:
            return RedirectResponse(f"/?rebuilt=1&warn={len(outcome.warnings)}", status_code=303)
        return RedirectResponse(
            f"/?build_failed=1&errors={len(outcome.issues)}", status_code=303
        )

    # ------------------------------------------------------------ 其它

    @app.get("/healthz")
    def healthz(request: Request) -> dict:
        live: AdminStore = request.app.state.store
        snap = live.snapshot()
        return {
            "ok": snap.ok,
            "data_dir": str(live.data_dir),
            "db": str(live.db_path),
            "db_exists": live.db_path.exists(),
            "db_stale": live.db_state().get("stale"),
            "errors": len(snap.errors),
            "warnings": len(snap.warnings),
            "counts": live.counts(),
        }

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "help.html",
            **ctx(request, title="使用说明", specs=[KIND_BY_NAME[k] for k in KIND_ORDER]),
        )

    return app


# ---------------------------------------------------------------- 工具


def base_sha_of(form: Any) -> str | None:
    """表单里的 ``_base_sha``：``None`` 表示这次提交不做并发检查（仅给程序化调用用）。"""
    value = form.get("_base_sha") if hasattr(form, "get") else None
    return None if value is None else str(value)


def require_base_sha(form: Any) -> str:
    """网页表单必须带 ``_base_sha``。

    如果允许“不带就跳过并发检查”，任何人（或任何脚本）只要漏掉这个字段就能覆盖
    页面打开之后发生的修改。所以 HTTP 层强制要求它存在；``AdminStore.save``
    仍然保留 ``expected_sha=None`` 的宽松入口给程序化调用。
    """
    value = base_sha_of(form)
    if value is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "表单缺少并发校验字段 _base_sha：请回到页面刷新后重新提交。"
                "本次**没有写入任何文件**，数据库也没有变化。"
            ),
        )
    return value


def issue_field(field_name: str) -> str:
    """把校验器里的 ``skill_ids[1]`` / ``source.reference`` 归并到表单字段名。"""
    base = field_name.split("[", 1)[0]
    return base or field_name


def _flash_from_query(request: Request) -> list[dict]:
    query = request.query_params
    flashes: list[dict] = []
    if query.get("created"):
        flashes.append({"level": "ok", "text": "已新增：YAML 已写入，并已由 YAML 重建 SQLite。"})
    if query.get("saved"):
        flashes.append({"level": "ok", "text": "已保存：YAML 已写入，并已由 YAML 重建 SQLite。"})
    if query.get("deleted"):
        flashes.append({"level": "ok", "text": "已删除：YAML 已更新，并已由 YAML 重建 SQLite。"})
    if query.get("rebuilt"):
        flashes.append({"level": "ok", "text": "已按当前 YAML 全量重建 SQLite。"})
    if query.get("info") and query.get("info") != "0":
        flashes.append(
            {
                "level": "warn",
                "text": f"有 {query.get('info')} 条录入提示（例如复用了内容完全相同的技能版本、或新建了同名技能），详见下方技能区块。",
            }
        )
    if query.get("warn") and query.get("warn") != "0":
        flashes.append(
            {
                "level": "warn",
                "text": f"本次构建有 {query.get('warn')} 条校验警告（不影响构建，可用 python scripts/validate_data.py 查看）。",
            }
        )
    if query.get("build_failed"):
        flashes.append(
            {
                "level": "error",
                "text": f"重建失败：YAML 校验有 {query.get('errors', '若干')} 条错误，未生成数据库。",
            }
        )
    return flashes


def _form_error(
    request: Request,
    render,
    live: AdminStore,
    kind: str,
    values: dict[str, str],
    errors: list[tuple[str, str]],
    action: str,
    *,
    action_kind: str = "save",
    target_id: str = "",
    outcome: SaveOutcome | None = None,
    base_sha: str = "",
) -> HTMLResponse:
    error_map: dict[str, list[str]] = {}
    for field_name, message in errors:
        error_map.setdefault(field_name, []).append(message)
    response = render(
        request,
        "edit.html",
        **{
            "request": request,
            "title": f"{'新增' if action_kind == 'create' else '编辑'}{KIND_BY_NAME[kind].label}",
            "kinds": [
                {"kind": k, "label": KIND_BY_NAME[k].label, "count": live.counts().get(k, 0)}
                for k in KIND_ORDER
            ],
            "counts": live.counts(),
            "total": live.bundle.total,
            "validation": live.snapshot(),
            "db_state": live.db_state(),
            "flash": [],
            "current_kind": kind,
            "q": "",
            "kind": kind,
            "spec": KIND_BY_NAME[kind],
            "specs": forms.form_specs(kind),
            "values": values,
            "mode": action_kind,
            "target_id": target_id,
            "errors": errors,
            "error_map": error_map,
            "issue_list": outcome.issues if outcome else [],
            "outcome_error": outcome.error if outcome else "",
            "datalists": _datalists(live, kind),
            "action": action,
            "csrf": request.app.state.csrf,
            "base_sha": base_sha,
            "name_of": live.name_of,
        },
    )
    # 表单/校验没通过一律用 400，方便脚本与测试区分“渲染成功”和“保存失败”
    response.status_code = 400
    return response


def _detail_fields(live: AdminStore, kind: str, entity: Any) -> list[dict]:
    """详情页的字段表：引用字段渲染成可点击链接；嵌套对象由专门区块展示。"""
    outgoing = live.outgoing_refs(kind, entity)
    out: list[dict] = []
    for spec in forms.form_specs(kind):
        if spec.name == "source" or spec.name.startswith("source.") or spec.widget in {"source", "object"}:
            continue  # source 单独一个区块；skills 之类由品类专属区块展示
        value = getattr(entity, spec.name, None)
        if spec.widget == "list":
            text = "\n".join(forms.value_to_text(spec, item) for item in (value or []))
        else:
            text = forms.value_to_text(spec, value)
        out.append(
            {
                "name": spec.name,
                "label": spec.label,
                "text": text,
                "empty": not text,
                "links": outgoing.get(spec.name, []),
            }
        )
    return out


def _datalists(live: AdminStore, kind: str) -> dict[str, list[dict]]:
    """给引用型字段提供候选下拉（datalist），减少手打 id 出错。"""
    lists: dict[str, list[dict]] = {}
    for spec in forms.form_specs(kind):
        target = spec.ref_kind
        if not target:
            continue
        options = [
            {"value": entity_id, "label": f"{live.display_name(target, entity)}（{entity_id}）"}
            for entity_id, entity in live.entries(target)
        ]
        if options:
            lists[spec.name] = options
    return lists


app = create_app()
