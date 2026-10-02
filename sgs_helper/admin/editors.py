"""产品 / 武将**专用录入页**的表单解析与批量改动构造。

为什么不塞进 ``forms.py``：这两个页面的目标是「拿着实体卡从上到下一路录完」，
字段顺序、联动（产品 → 模式/系列）和“一次提交产生多条实体”的语义，
跟 generic 的“按 schema 平铺字段”完全不是一回事。放这里，generic 表单继续保持简单。

关键约定：

- **只有武将名字必填**；其余都可以先空着，以后再补。
- 内部 ``id`` 由后端生成（用户不填也能保存），已存在的记录绝不无故重生 id。
- 页面没展示的字段（aliases / source / verification_status / notes…）在编辑时**保留原值**：
  解析时以“已存在实体的 dump”为底，只覆盖表单里真的出现的字段。
- 一次提交可以同时产生 General + 0..N Skill + 0..N SkillVersion，交给
  ``AdminStore.save_batch`` 一次性事务保存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from ..constants import GENDER_LABELS, MODE_LABELS, Gender, Mode, SourceType, VerificationStatus
from ..schema import MODEL_BY_KIND
from .forms import FieldError
from .store import Change, make_entity_id, make_version_id

#: 勾玉（体力）下拉的取值
HP_CHOICES: tuple[str, ...] = ("1", "1.5", "2", "2.5", "3", "4", "5")

#: 新技能 / 新版本的默认来源（从实体卡录入，通常就是这个）
DEFAULT_SOURCE: dict[str, str] = {
    "type": "physical_card",
    "title": "本人实体卡",
    "reference": "",
    "notes": "",
}

PRODUCT_DEFAULTS: dict[str, Any] = {
    "id": "",
    "name": "",
    "series": "",
    "mode": None,
    "publisher": "",
    "release_year": None,
    "release_date": None,
    "edition": "",
    "general_sets": [],
    "card_sets": [],
    "source": None,
    "verification_status": "unverified",
    "notes": "",
}

GENERAL_DEFAULTS: dict[str, Any] = {
    "id": "",
    "name": "",
    "title": "",
    "general_number": None,
    "mode": None,
    "faction": "",
    "hp": None,
    "gender": "unknown",
    "product_id": None,
    "general_set": None,
    "skills": [],
    "aliases": [],
    "source": None,
    "verification_status": "unverified",
    "notes": "",
}


@dataclass
class EditorResult:
    """专用录入页一次提交的解析结果。"""

    changes: list[Change] = field(default_factory=list)
    errors: list[FieldError] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    record_id: str = ""
    values: dict[str, str] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------- 表单取值


def _text(form: Mapping[str, Any], name: str) -> str:
    value = form.get(name)
    return str(value).strip() if value is not None else ""


def _multi(form: Mapping[str, Any], name: str) -> list[str]:
    """标签输入：支持重复字段（getlist）与换行分隔两种提交方式，保序去重。"""
    raw: list[str] = []
    if hasattr(form, "getlist"):
        raw = [str(item) for item in form.getlist(name)]
    else:
        value = form.get(name)
        if value is not None:
            raw = [str(value)]
    out: list[str] = []
    for chunk in raw:
        for line in chunk.splitlines():
            item = line.strip()
            if item and item not in out:
                out.append(item)
    return out


def _int(form: Mapping[str, Any], name: str, errors: list[FieldError]) -> int | None:
    raw = _text(form, name)
    if not raw:
        return None
    if not raw.lstrip("-").isdigit():
        errors.append(FieldError(name, f"必须是整数年份/数字：{raw!r}"))
        return None
    return int(raw)


def _date(form: Mapping[str, Any], name: str, errors: list[FieldError]) -> date | None:
    raw = _text(form, name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        errors.append(FieldError(name, f"日期格式应为 YYYY-MM-DD：{raw!r}"))
        return None


def _hp(form: Mapping[str, Any], name: str, errors: list[FieldError]) -> float | None:
    raw = _text(form, name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        errors.append(FieldError(name, f"勾玉要写数字（例如 1.5）：{raw!r}"))
        return None


def _source_from_form(form: Mapping[str, Any], *, allow_empty: bool = True):
    """source 是可选的（Product/General/Skill 都允许没有来源）。"""
    if not any(f"source.{key}" in form for key in ("type", "title", "reference", "notes")):
        return "keep"
    payload = {
        "type": _text(form, "source.type"),
        "title": _text(form, "source.title"),
        "reference": _text(form, "source.reference"),
        "notes": _text(form, "source.notes"),
    }
    if not any(payload.values()):
        return None if allow_empty else payload
    return payload


def echo_values(form: Mapping[str, Any]) -> dict[str, str]:
    """把提交内容原样回显（失败时不能丢用户已经敲进去的东西）。"""
    out: dict[str, str] = {}
    keys = form.keys() if hasattr(form, "keys") else []
    for key in keys:
        if hasattr(form, "getlist"):
            out[key] = "\n".join(str(item) for item in form.getlist(key))
        else:
            out[key] = str(form.get(key, ""))
    return out


def _base(existing: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    if existing is not None:
        data = existing.model_dump(mode="json")
        for key, value in defaults.items():
            data.setdefault(key, value)
        return data
    return dict(defaults)


def _apply_enum(
    data: dict[str, Any],
    form: Mapping[str, Any],
    name: str,
    allowed: type,
    errors: list[FieldError],
    *,
    nullable: bool = True,
):
    if name not in form:
        return
    raw = _text(form, name)
    if not raw:
        data[name] = None if nullable else data.get(name)
        return
    if raw not in allowed.__members__:
        errors.append(FieldError(name, f"取值不合法：{raw!r}（可选：{'、'.join(allowed.__members__)}）"))
        return
    data[name] = raw


# ---------------------------------------------------------------- 产品


def parse_product_form(
    form: Mapping[str, Any], *, store, existing: Any | None = None
) -> EditorResult:
    """产品录入/编辑：只用 name / release_year / mode / general_sets / card_sets 也能保存。"""
    result = EditorResult(values=echo_values(form))
    errors = result.errors
    data = _base(existing, PRODUCT_DEFAULTS)

    if "name" in form:
        data["name"] = _text(form, "name")
    if not str(data.get("name") or "").strip():
        errors.append(FieldError("name", "产品名称必填"))

    raw_id = _text(form, "id")
    if raw_id:
        data["id"] = raw_id
    elif existing is None:
        data["id"] = make_entity_id(
            "product_", str(data.get("name") or "product"), store.bundle.ids("product")
        )

    if "release_year" in form:
        data["release_year"] = _int(form, "release_year", errors)
    if "release_date" in form:
        data["release_date"] = _date(form, "release_date", errors)
    if "mode" in form:
        _apply_enum(data, form, "mode", Mode, errors)
    if "general_sets" in form:
        data["general_sets"] = _multi(form, "general_sets")
    if "card_sets" in form:
        data["card_sets"] = _multi(form, "card_sets")

    for name in ("series", "publisher", "edition", "notes"):
        if name in form:
            data[name] = _text(form, name)
    if "verification_status" in form:
        _apply_enum(data, form, "verification_status", VerificationStatus, errors, nullable=False)
    source = _source_from_form(form)
    if source != "keep":
        data["source"] = source

    result.changes = [
        Change(kind="product", candidate=data, old_id=existing.id if existing else None)
    ]
    result.record_id = str(data.get("id") or "")
    return result


def product_values(store, product: Any | None) -> dict[str, str]:
    """渲染产品录入页时的初始值。"""
    from .forms import values_from_entity

    if product is None:
        return {}
    values = values_from_entity("product", product)
    # 标签输入用换行分隔的文本提交
    values["general_sets"] = "\n".join(product.general_sets or [])
    values["card_sets"] = "\n".join(product.card_sets or [])
    return values


# ---------------------------------------------------------------- 武将：技能行


@dataclass
class SkillRow:
    """武将录入页上的一行技能。"""

    index: int = 0
    #: 选中的已有技能 id（为空表示新建）
    skill_id: str = ""
    #: 显式绑定的版本 id（可为空 → 详情页自动推导）
    version_id: str = ""
    #: 新建技能时的名称 / 内部 id
    new_name: str = ""
    new_id: str = ""
    #: 新建技能时同时创建的版本：描述 / 版本名 / 内部 id
    new_text: str = ""
    new_version_label: str = ""
    new_version_id: str = ""
    #: 给“已有技能”新增版本：描述 / 版本名 / 前一版本
    add_version_text: str = ""
    add_version_label: str = ""
    add_version_from: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "skill_id": self.skill_id,
            "version_id": self.version_id,
            "new_name": self.new_name,
            "new_id": self.new_id,
            "new_text": self.new_text,
            "new_version_label": self.new_version_label,
            "new_version_id": self.new_version_id,
            "add_version_text": self.add_version_text,
            "add_version_label": self.add_version_label,
            "add_version_from": self.add_version_from,
        }


def _row_prefix(index: int) -> str:
    return f"skill_{index}_"


def parse_skill_rows(form: Mapping[str, Any]) -> list[SkillRow]:
    raw_count = _text(form, "skill_count")
    try:
        count = int(raw_count) if raw_count else 0
    except ValueError:
        count = 0
    rows: list[SkillRow] = []
    for index in range(max(0, min(count, 200))):
        prefix = _row_prefix(index)
        rows.append(
            SkillRow(
                index=index,
                skill_id=_text(form, f"{prefix}skill_id"),
                version_id=_text(form, f"{prefix}version_id"),
                new_name=_text(form, f"{prefix}new_name"),
                new_id=_text(form, f"{prefix}new_id"),
                new_text=_text(form, f"{prefix}new_text"),
                new_version_label=_text(form, f"{prefix}new_version_label"),
                new_version_id=_text(form, f"{prefix}new_version_id"),
                add_version_text=_text(form, f"{prefix}add_version_text"),
                add_version_label=_text(form, f"{prefix}add_version_label"),
                add_version_from=_text(form, f"{prefix}add_version_from"),
            )
        )
    return rows


def rows_from_form(form: Mapping[str, Any]) -> list[dict]:
    return [row.as_dict() for row in parse_skill_rows(form)]


def rows_from_general(store, general: Any | None) -> list[dict]:
    """编辑页初始行：把已存在的技能关系渲染成一行行表单。"""
    if general is None:
        return []
    rows: list[dict] = []
    for index, ref in enumerate(general.skills):
        skill = store.entity("skill", ref.skill_id)
        version = store.entity("skill_version", ref.version_id) if ref.version_id else None
        rows.append(
            SkillRow(
                index=index,
                skill_id=ref.skill_id,
                version_id=ref.version_id or "",
            ).as_dict()
            | {
                "skill_name": skill.name if skill else ref.skill_id,
                "version_label": (version.version_label or version.id) if version else "",
                "version_options": store.skill_version_options(
                    ref.skill_id, mode=general.mode, product_id=general.product_id
                ),
            }
        )
    return rows


# ---------------------------------------------------------------- 武将


def _new_version_change(
    store,
    *,
    skill_id: str,
    mode: str | None,
    product_id: str | None,
    text: str,
    label: str,
    version_id_hint: str,
    previous_version_id: str | None,
    generated: set[str],
    info: list[str],
) -> tuple[str | None, Change | None]:
    """构造一条新 SkillVersion；如果内容与已有版本完全相同则直接复用（不重复创建）。"""
    body = (text or "").strip()
    if not body:
        return None, None

    same = store.version_by_content(skill_id, mode, product_id, body)
    if same is not None:
        info.append(
            f'技能 {skill_id} 下已有内容完全相同的版本 {same["id"]}，已直接复用它（没有新建重复版本）。'
        )
        return same["id"], None

    version_id = version_id_hint or make_version_id(
        skill_id, mode, store.bundle.ids("skill_version") | generated
    )
    candidate = {
        "id": version_id,
        "skill_id": skill_id,
        "mode": mode or "common",
        "text": body,
        "version_label": label,
        "product_id": product_id,
        "package": None,
        "effective_from": None,
        "effective_to": None,
        "previous_version_id": previous_version_id or None,
        "change_note": "",
        "related_keywords": [],
        "related_timing_ids": [],
        "source": dict(DEFAULT_SOURCE),
        "verification_status": "unverified",
        "notes": "",
    }
    generated.add(version_id)
    return version_id, Change(kind="skill_version", candidate=candidate)


def parse_general_form(
    form: Mapping[str, Any], *, store, existing: Any | None = None
) -> EditorResult:
    """武将录入/编辑：一次提交可能同时产生 General + Skill + SkillVersion。"""
    result = EditorResult(values=echo_values(form))
    errors = result.errors
    info = result.info
    data = _base(existing, GENERAL_DEFAULTS)

    # ---- 基本字段（只有 name 必填）
    if "name" in form:
        data["name"] = _text(form, "name")
    if not str(data.get("name") or "").strip():
        errors.append(FieldError("name", "武将名字必填"))

    raw_id = _text(form, "id")
    if raw_id:
        data["id"] = raw_id
    elif existing is None:
        data["id"] = make_entity_id(
            "general_", str(data.get("name") or "general"), store.bundle.ids("general")
        )

    for name in ("title", "faction"):
        if name in form:
            data[name] = _text(form, name)
    if "general_number" in form:
        data["general_number"] = _text(form, "general_number") or None
    if "general_set" in form:
        data["general_set"] = _text(form, "general_set") or None
    if "product_id" in form:
        data["product_id"] = _text(form, "product_id") or None
    if "mode" in form:
        _apply_enum(data, form, "mode", Mode, errors)
    if "gender" in form:
        _apply_enum(data, form, "gender", Gender, errors, nullable=False)
    if "hp" in form:
        data["hp"] = _hp(form, "hp", errors)

    known_products = store.bundle.ids("product")
    if data.get("product_id") and data["product_id"] not in known_products:
        errors.append(FieldError("product_id", f"产品不存在：{data['product_id']}"))

    for name in ("aliases",):
        if name in form:
            data[name] = _multi(form, name)
    if "notes" in form:
        data["notes"] = _text(form, "notes")
    if "verification_status" in form:
        _apply_enum(data, form, "verification_status", VerificationStatus, errors, nullable=False)
    source = _source_from_form(form)
    if source != "keep":
        data["source"] = source

    # ---- 技能行
    mode = data.get("mode")
    product_id = data.get("product_id")
    existing_skills = store.bundle.ids("skill")
    generated_ids: set[str] = set()
    refs: list[dict] = []
    result.rows = []

    for row in parse_skill_rows(form):
        prefix = _row_prefix(row.index)
        rendered = row.as_dict()
        result.rows.append(rendered)

        if not row.skill_id and not row.new_name:
            continue  # 空行，忽略

        if row.skill_id:
            # ---- 绑定已有技能
            if row.skill_id not in existing_skills:
                errors.append(FieldError(f"{prefix}skill_id", f"技能不存在：{row.skill_id}"))
                # 仍然把这条关系放进候选：这样失败页还能由校验器给出
                # “文件 / 字段 / 原因”完整说明（否则用户只知道行号，不知道写到哪个文件）
                refs.append({"skill_id": row.skill_id, "version_id": row.version_id or None})
                continue
            skill_id = row.skill_id
            version_id = row.version_id or None
            if version_id:
                version = store.entity("skill_version", version_id)
                if version is None:
                    errors.append(FieldError(f"{prefix}version_id", f"版本不存在：{version_id}"))
                    refs.append({"skill_id": skill_id, "version_id": version_id})
                    continue
                if version.skill_id != skill_id:
                    errors.append(
                        FieldError(
                            f"{prefix}version_id",
                            f"版本 {version_id} 属于技能 {version.skill_id}，和选中的技能不一致",
                        )
                    )
                    refs.append({"skill_id": skill_id, "version_id": version_id})
                    continue
            if row.add_version_text:
                new_version_id, change = _new_version_change(
                    store,
                    skill_id=skill_id,
                    mode=mode,
                    product_id=product_id,
                    text=row.add_version_text,
                    label=row.add_version_label,
                    version_id_hint="",
                    previous_version_id=row.add_version_from or version_id,
                    generated=generated_ids,
                    info=info,
                )
                if change is not None:
                    result.changes.append(change)
                    info.append(f"将为技能 {skill_id} 新增版本 {new_version_id}")
                if new_version_id:
                    version_id = new_version_id
            refs.append({"skill_id": skill_id, "version_id": version_id})
            rendered["skill_name"] = store.name_of("skill", skill_id)
            rendered["version_label"] = store.name_of("skill_version", version_id) if version_id else ""
            continue

        # ---- 新建技能（可选同时新建版本）
        skill_id = row.new_id or make_entity_id(
            "skill_", row.new_name, existing_skills | generated_ids
        )
        if skill_id in existing_skills or skill_id in generated_ids:
            errors.append(FieldError(f"{prefix}new_id", f"技能 id 已被占用：{skill_id}"))
            continue
        generated_ids.add(skill_id)
        existing_name = [
            s for s in store.bundle.all("skill").values() if s.name == row.new_name
        ]
        if existing_name:
            info.append(
                f"注意：已经存在同名技能「{row.new_name}」({existing_name[0].id})，"
                "本次仍会新建一个独立技能，请确认这是你要的。"
            )
        result.changes.append(
            Change(
                kind="skill",
                candidate={
                    "id": skill_id,
                    "name": row.new_name,
                    "aliases": [],
                    "notes": "",
                    "source": dict(DEFAULT_SOURCE),
                },
            )
        )
        info.append(f"将新建技能「{row.new_name}」({skill_id})")

        version_id = None
        if row.new_text:
            version_id, change = _new_version_change(
                store,
                skill_id=skill_id,
                mode=mode,
                product_id=product_id,
                text=row.new_text,
                label=row.new_version_label,
                version_id_hint=row.new_version_id,
                previous_version_id=None,
                generated=generated_ids,
                info=info,
            )
            if change is not None:
                result.changes.append(change)
                info.append(f"将新建技能版本 {version_id}（模式 {MODE_LABELS.get(mode or 'common', mode or '')}）")
        elif row.new_version_label:
            info.append("只填了技能名称、没填技能描述：只会创建 SkillDefinition，不创建 SkillVersion。")
        refs.append({"skill_id": skill_id, "version_id": version_id})
        rendered["skill_name"] = row.new_name
        rendered["version_label"] = row.new_version_label

    if mode is None and any(ref["version_id"] for ref in refs):
        info.append(
            "武将没有填模式：新建的技能版本按 common（跨模式通用）保存，之后可以在技能版本页面改成具体模式。"
        )

    data["skills"] = refs

    # ---- 把武将本体也放进这一批（放在最前面，方便页面/日志先看到它）
    result.changes.insert(
        0,
        Change(kind="general", candidate=data, old_id=existing.id if existing else None),
    )
    result.record_id = str(data.get("id") or "")
    return result


def general_values(store, general: Any | None, *, params: Mapping[str, Any] | None = None) -> dict[str, str]:
    """渲染武将录入页时的初始值（不含技能行，技能行单独给）。"""
    if general is None:
        values: dict[str, str] = {}
    else:
        from .forms import values_from_entity

        values = values_from_entity("general", general)
        values["aliases"] = "\n".join(general.aliases or [])
    if params:
        for key, value in params.items():
            if value not in (None, ""):
                values[key] = str(value)
    return values


# ---------------------------------------------------------------- 共用小工具


def mode_choices() -> list[tuple[str, str]]:
    return [(key, MODE_LABELS.get(key, key)) for key in Mode.__members__]


def gender_choices() -> list[tuple[str, str]]:
    return [(key, GENDER_LABELS.get(key, key)) for key in Gender.__members__]


def source_type_choices() -> list[tuple[str, str]]:
    from ..constants import SOURCE_LABELS

    return [(key, SOURCE_LABELS.get(key, key)) for key in SourceType.__members__]


def verification_choices() -> list[tuple[str, str]]:
    from ..constants import VERIFICATION_LABELS

    return [(key, VERIFICATION_LABELS.get(key, key)) for key in VerificationStatus.__members__]


def model_field_errors(kind: str, payload: dict, prefix: str = "") -> list[FieldError]:
    """用 Pydantic 先做一次字段级校验，好把错误定位到具体输入框。"""
    from pydantic import ValidationError

    try:
        MODEL_BY_KIND[kind].model_validate(payload)
    except ValidationError as exc:
        out: list[FieldError] = []
        for error in exc.errors():
            loc = ".".join(str(part) for part in error["loc"]) or "-"
            message = error["msg"]
            if error["type"] == "extra_forbidden":
                message = f"未知字段：{message}"
            out.append(FieldError(f"{prefix}{loc}", message))
        return out
    return []
