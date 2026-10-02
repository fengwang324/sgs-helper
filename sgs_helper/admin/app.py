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

import math
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..constants import (
    CARD_TYPE_LABELS,
    GENDER_LABELS,
    KIND_BY_NAME,
    KIND_ORDER,
    MODE_LABELS,
    SOURCE_LABELS,
    SUIT_LABELS,
    VERIFICATION_LABELS,
)
from ..validate import REFERENCE_FIELDS
from . import forms
from .store import (
    REVIEW_STATUSES,
    AdminStore,
    SaveOutcome,
    rank_label,
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


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(
        directory=str(ADMIN_DIR / "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env = templates.env
    env.filters.update(
        {
            "rank_label": rank_label,
            "mode_label": lambda v: MODE_LABELS.get(v, v or ""),
            "status_label": lambda v: VERIFICATION_LABELS.get(v, v or ""),
            "source_label": lambda v: SOURCE_LABELS.get(v, v or ""),
            "suit_label": lambda v: SUIT_LABELS.get(v, v or ""),
            "card_type_label": lambda v: CARD_TYPE_LABELS.get(v, v or ""),
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
            "VERIFICATION_LABELS": VERIFICATION_LABELS,
            "SOURCE_LABELS": SOURCE_LABELS,
            "REVIEW_STATUSES": REVIEW_STATUSES,
            "REVIEW_STATUS_SET": set(REVIEW_STATUSES),
            "reference_fields": REFERENCE_FIELDS,
        }
    )
    return templates


templates = build_templates()


# ---------------------------------------------------------------- 应用


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

    # ------------------------------------------------------------ 上下文

    def ctx(request: Request, *, title: str = "", **extra: Any) -> dict:
        live: AdminStore = request.app.state.store
        counts = live.counts()
        bundle = live.bundle
        review_count = sum(
            1
            for kind in KIND_ORDER
            for entity in bundle.all(kind).values()
            if getattr(entity, "verification_status", None) in REVIEW_STATUSES
        )
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
            "review_count": review_count,
            "flash": _flash_from_query(request),
            "current_kind": "",
            "q": "",
            "name_of": live.name_of,
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

    # ------------------------------------------------------------ 首页

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        snap = live.snapshot()
        status_counts = [
            {
                "status": status,
                "label": VERIFICATION_LABELS.get(status, status),
                "count": sum(
                    1
                    for kind in KIND_ORDER
                    for entity in snap.bundle.all(kind).values()
                    if getattr(entity, "verification_status", None) == status
                ),
            }
            for status in VERIFICATION_LABELS
        ]
        return render(
            request,
            "index.html",
            **ctx(
                request,
                title="首页",
                snap=snap,
                status_counts=status_counts,
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
        status: str = "",
        mode: str = "",
    ) -> HTMLResponse:
        live: AdminStore = request.app.state.store
        kinds = [kind] if kind in KIND_BY_NAME else None
        rows = live.search(q, kinds=kinds, limit=500) if q.strip() else []
        if status:
            rows = [row for row in rows if row["verification_status"] == status]
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
                status=status,
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
        status: str = "",
        product: str = "",
        package: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        rows = live.filter_rows(kind, q=q, mode=mode, status=status, product=product, package=package)
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
                status=status,
                product=product,
                package=package,
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
            ),
        )

    @app.get("/raw/{kind}/{entity_id}", response_class=PlainTextResponse)
    def raw_page(request: Request, kind: str, entity_id: str) -> PlainTextResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        if live.entity(kind, entity_id) is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return PlainTextResponse(live.file_text(kind, entity_id), media_type="text/plain; charset=utf-8")

    # ------------------------------------------------------------ 新增

    @app.get("/new/{kind}", response_class=HTMLResponse)
    def new_page(request: Request, kind: str) -> HTMLResponse:
        require_kind(kind)
        live: AdminStore = request.app.state.store
        values = forms.values_from_defaults(kind)
        params = {key: value for key, value in request.query_params.items() if key in values}
        if kind == "skill_version" and params.get("previous_version_id"):
            previous = live.entity("skill_version", params["previous_version_id"])
            if previous is not None:
                for field_name in ("skill_id", "mode", "product_id", "package"):
                    value = getattr(previous, field_name, None)
                    if value and not params.get(field_name):
                        params[field_name] = value
                if previous.source is not None:
                    params.setdefault("source.type", previous.source.type)
                    params.setdefault("source.title", previous.source.title)
                    params.setdefault("source.reference", previous.source.reference)
        values = forms.prefill(values, params)
        if kind == "skill_version" and not values.get("effective_from"):
            from datetime import date

            values["effective_from"] = date.today().isoformat()
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
                source_choices=forms.source_choices(),
                action=f"/new/{kind}",
                base_sha="",
            ),
        )

    @app.post("/new/{kind}", response_class=HTMLResponse)
    async def create_entity(request: Request, kind: str) -> HTMLResponse:
        require_kind(kind)
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
                source_choices=forms.source_choices(),
                action=f"/edit/{kind}/{entity_id}",
                references=live.reverse_refs(kind, entity_id),
                entity=entity,
                base_sha=live.entity_sha(kind, entity_id),
            ),
        )

    @app.post("/edit/{kind}/{entity_id}", response_class=HTMLResponse)
    async def update_entity(request: Request, kind: str, entity_id: str) -> HTMLResponse:
        require_kind(kind)
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

    # ------------------------------------------------------------ 待核验 / 重建

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, status: str = "") -> HTMLResponse:
        live: AdminStore = request.app.state.store
        groups = live.review_groups()
        if status in REVIEW_STATUSES:
            groups = [group for group in groups if group["status"] == status]
        return render(
            request,
            "review.html",
            **ctx(request, title="待核验清单", groups=groups, status=status),
        )

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
    """把校验器里的 ``skill_ids[1]`` / ``source.title`` 归并到表单字段名。"""
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
            "review_count": 0,
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
            "source_choices": forms.source_choices(),
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
    """详情页的字段表：引用字段渲染成可点击链接。"""
    outgoing = live.outgoing_refs(kind, entity)
    out: list[dict] = []
    for spec in forms.form_specs(kind):
        if spec.name.startswith("source."):
            continue
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
