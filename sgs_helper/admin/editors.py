"""产品 / 武将 / 卡牌**专用录入页**的表单解析与批量改动构造。

为什么不塞进 ``forms.py``：这些页面的目标是「拿着一张卡从上到下一路录完」，
字段顺序、联动（产品 → 模式/系列）和“一次提交产生多条实体”的语义，
跟 generic 的“按 schema 平铺字段”完全不是一回事。放这里，generic 表单继续保持简单。

关键约定：

- **只有武将名字必填**；其余都可以先空着，以后再补。
- 内部 ``id`` 由后端生成（用户不填也能保存），已存在的记录绝不无故重生 id。
- 页面没展示的字段（image / review_note…）在编辑时**保留原值**：
  解析时以“已存在实体的 dump”为底，只覆盖表单里真的出现的字段。
- 一次提交可以同时产生 General + 0..N Skill + 0..N SkillVersion，交给
  ``AdminStore.save_batch`` 一次性事务保存。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..constants import (
    CARD_CATEGORY_TREE,
    GENDER_LABELS,
    MODE_LABELS,
    Gender,
    Mode,
    SourceType,
)
from ..schema import MODEL_BY_KIND
from .forms import FieldError
from .store import (
    Change,
    make_entity_id,
    make_variant_id,
    make_version_id,
    slug_card_number,
)

#: 勾玉（体力）下拉的取值
HP_CHOICES: tuple[str, ...] = ("1", "1.5", "2", "2.5", "3", "4", "5")

#: 新技能版本的默认来源（从实体卡录入，通常就是这个）；Source 只有 type + reference
DEFAULT_SOURCE: dict[str, str] = {
    "type": "physical_card",
    "reference": "",
}

PRODUCT_DEFAULTS: dict[str, Any] = {
    "id": "",
    "name": "",
    "release_year": None,
    "mode": None,
    "general_sets": [],
    "card_sets": [],
}

#: 武将个体（General）：只保存 id + 官方武将名
GENERAL_DEFAULTS: dict[str, Any] = {
    "id": "",
    "name": "",
}

#: 武将版本（GeneralVariant）：模式/势力/体力/称号/技能等版本属性都在这里。
#: 实体类记录（武将版本 / 卡牌）没有 source；需要人工复核的异常写进 review_note。
VARIANT_DEFAULTS: dict[str, Any] = {
    "id": "",
    "general_id": "",
    "variant": "标准版",
    "title": "",
    "general_number": None,
    "illustrator": "",
    "image": "",
    "mode": None,
    "faction": "",
    "hp": None,
    "gender": "unknown",
    "product_id": None,
    "general_set": None,
    "zhulianbihe": [],
    "skills": [],
    "review_note": "",
}

#: 版本标签的常用取值（下拉里可以直接选，也可以自己输）
VARIANT_LABELS: tuple[str, ...] = (
    "标准版", "国战版", "界限突破", "OL", "手卡（手杀）", "其他",
)

#: 卡牌（Card）：单层——分类 + 卡名 + 卡图 + 拓展包 + 效果。
#: ``effect`` 是嵌套映射（text + skills），卡牌**没有** source 字段；分类字段必填。
CARD_DEFAULTS: dict[str, Any] = {
    "id": "",
    "category_1": "",
    "category_2": "",
    "category_3": "",
    "name": "",
    "image": "",
    "card_set": None,
    "effect": {"text": "", "skills": []},
    "review_note": "",
}


@dataclass
class EditorResult:
    """专用录入页一次提交的解析结果。"""

    changes: list[Change] = field(default_factory=list)
    errors: list[FieldError] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    record_id: str = ""
    #: 本次提交对应的武将版本 id（GeneralVariant）
    variant_id: str = ""
    values: dict[str, str] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------- 表单取值


#: 浏览器表单有时会带上来这些“空的写法”，一律当空处理
_NULLISH = {"", "none", "null", "nil", "~", "undefined"}


def _optional_text(form: Mapping[str, Any], name: str) -> str | None:
    """可空文本字段：空串 / None / null / ~ 都算“没填”。"""
    raw = form.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return None if text.lower() in _NULLISH else text


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


def _hp(form: Mapping[str, Any], name: str, errors: list[FieldError]) -> float | None:
    raw = _text(form, name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        errors.append(FieldError(name, f"勾玉要写数字（例如 1.5）：{raw!r}"))
        return None


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
    """产品录入/编辑：只用 name / release_year / mode / general_sets / card_sets 就能保存。"""
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
    if "mode" in form:
        _apply_enum(data, form, "mode", Mode, errors)
    if "general_sets" in form:
        data["general_sets"] = _multi(form, "general_sets")
    if "card_sets" in form:
        data["card_sets"] = _multi(form, "card_sets")

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
    #: 就地修改某条已存在 SkillVersion 的文本
    edit_version_id: str = ""
    edit_text: str = ""

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
            "edit_version_id": self.edit_version_id,
            "edit_text": self.edit_text,
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
                edit_version_id=_text(form, f"{prefix}edit_version_id"),
                edit_text=_text(form, f"{prefix}edit_text"),
            )
        )
    return rows


def rows_from_form(form: Mapping[str, Any]) -> list[dict]:
    return [row.as_dict() for row in parse_skill_rows(form)]


def rows_from_variant(store, variant: Any | None) -> list[dict]:
    """编辑页初始行：把已存在的技能关系渲染成一行行表单。"""
    if variant is None:
        return []
    rows: list[dict] = []
    for index, ref in enumerate(variant.skills):
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
                "version_display": store.version_display_label(version) if version else "",
                "edit_version_id": ref.version_id or "",
                "version_text": version.text if version else "",
                "version_options": store.skill_version_options(
                    ref.skill_id, mode=variant.mode, product_id=variant.product_id
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
        "previous_version_id": previous_version_id or None,
        "source": dict(DEFAULT_SOURCE),
        "review_note": "",
    }
    generated.add(version_id)
    return version_id, Change(kind="skill_version", candidate=candidate)


def normalize_faction(value: str) -> str:
    """势力归一化：允许用空格 / 斜杠 / 顿号 / 逗号分隔多个（双势力），统一存成「魏/蜀」。"""
    toks = [t for t in re.split(r"[\s/、，,|]+", value or "") if t]
    out: list[str] = []
    for t in toks:
        if t not in out:
            out.append(t)
    return "/".join(out)


def _name_list(form: Mapping[str, Any], name: str) -> list[str]:
    """多值文本 -> list（珠联璧合可以写多个，也可以是「男性武将」这类类别值）。"""
    raw = _text(form, name) or ""
    toks = [t for t in re.split(r"[\s、,，/|]+", raw) if t]
    out: list[str] = []
    for t in toks:
        if t not in out:
            out.append(t)
    return out


def parse_general_form(
    form: Mapping[str, Any],
    *,
    store,
    existing: Any | None = None,
    variant: Any | None = None,
) -> EditorResult:
    """武将录入/编辑：一次提交产生 General + GeneralVariant（+ 可选的新 Skill / SkillVersion）。

    - ``existing``：已有的武将个体（General），改的是名字；
    - ``variant``：已有的武将版本（GeneralVariant），改的是模式/势力/体力/称号/技能等；
      为 None 时表示给这个武将**新建**一个版本（``variant=new`` 或首次录入）。
    """
    result = EditorResult(values=echo_values(form))
    errors = result.errors
    info = result.info

    # ---------------------------------------------------------------- General
    general_data = _base(existing, GENERAL_DEFAULTS)
    if "name" in form:
        general_data["name"] = _text(form, "name")
    if not str(general_data.get("name") or "").strip():
        errors.append(FieldError("name", "武将名字必填"))

    raw_id = _text(form, "id")
    if raw_id:
        general_data["id"] = raw_id
    elif existing is None:
        general_data["id"] = make_entity_id(
            "general_", str(general_data.get("name") or "general"), store.bundle.ids("general")
        )

    # ---------------------------------------------------------------- Variant
    variant_data = _base(variant, VARIANT_DEFAULTS)
    variant_data["general_id"] = str(general_data.get("id") or "")

    if "variant" in form:
        raw_variant = _text(form, "variant") or ""
        if raw_variant.startswith("variant_") and variant is not None:
            raw_variant = ""          # 防御：误填了内部 ID，保留原标签
        variant_data["variant"] = raw_variant or (variant.variant if variant else "") or "标准版"
    if "title" in form:
        variant_data["title"] = _text(form, "title")
    if "faction" in form:
        variant_data["faction"] = normalize_faction(_text(form, "faction"))
    if "illustrator" in form:
        variant_data["illustrator"] = _text(form, "illustrator")
    if "zhulianbihe" in form:
        variant_data["zhulianbihe"] = _name_list(form, "zhulianbihe")
    if "general_number" in form:
        variant_data["general_number"] = _optional_text(form, "general_number")
    if "general_set" in form:
        variant_data["general_set"] = _optional_text(form, "general_set")
    if "product_id" in form:
        variant_data["product_id"] = _optional_text(form, "product_id")
    if "mode" in form:
        _apply_enum(variant_data, form, "mode", Mode, errors)
    if "gender" in form:
        _apply_enum(variant_data, form, "gender", Gender, errors, nullable=False)
    if "hp" in form:
        variant_data["hp"] = _hp(form, "hp", errors)

    known_products = store.bundle.ids("product")
    if variant_data.get("product_id") and variant_data["product_id"] not in known_products:
        errors.append(FieldError("product_id", f"产品不存在：{variant_data['product_id']}"))

    if "review_note" in form:
        variant_data["review_note"] = _text(form, "review_note")

    # 版本 id 与卡图名都由**卡面编号**决定：
    #   编号 G.WEI 001 → id = variant_g_wei_001，卡图 = g_wei_001.jpg
    # 这样「一个武将 = 名字 + 称号」唯一之后，内部 id 与卡图名不再需要手工维护，
    # 改编号时两者会一起跟着改。
    card_number = variant_data.get("general_number")
    number_slug = slug_card_number(card_number)
    existing_variant_ids = set(store.bundle.ids("general_variant"))
    if variant is not None:
        existing_variant_ids.discard(variant.id)  # 自己的 id 不算占用

    if number_slug:
        variant_id = make_variant_id(card_number, existing_variant_ids)
        image_name = f"{number_slug}.jpg"
        current_image = (getattr(variant, "image", "") or "").strip() if variant else ""
        # 只在编号变了、且旧图确实存在时才跟着改，避免把手工挑的图改掉
        if "image" not in form and current_image and current_image != image_name:
            if (store.data_dir / "card_images" / current_image).exists():
                variant_data["image"] = image_name
    else:
        variant_id = ""
        if variant is not None:
            variant_id = variant.id
        else:
            raw_variant_id = _text(form, "variant_id")
            variant_id = raw_variant_id or make_entity_id(
                "variant_",
                f"{general_data.get('name') or 'general'} {variant_data.get('variant') or ''}",
                existing_variant_ids,
            )
    variant_data["id"] = variant_id

    # ---------------------------------------------------------------- 技能行
    mode = variant_data.get("mode")
    product_id = variant_data.get("product_id")
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
            if row.edit_version_id and row.edit_text.strip():
                target = store.entity("skill_version", row.edit_version_id)
                if target is not None and target.text.strip() != row.edit_text.strip():
                    candidate = target.model_dump()
                    candidate["text"] = row.edit_text.strip()
                    result.changes.append(
                        Change(kind="skill_version", candidate=candidate, old_id=target.id)
                    )
                    info.append(
                        f"将就地修改技能版本 {target.id} 的文本"
                        f"（{len(target.text)} 字 → {len(candidate['text'])} 字）"
                    )
                elif target is None:
                    errors.append(
                        FieldError(f"{prefix}edit_version_id",
                                   f"要修改的技能版本不存在：{row.edit_version_id}")
                    )
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
                    "review_note": "",
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
            "武将版本没有填模式：新建的技能版本按 common（跨模式通用）保存，"
            "之后可以在技能版本页面改成具体模式。"
        )

    variant_data["skills"] = refs

    # ---------------------------------------------------------------- 组装成一批
    result.changes.insert(
        0,
        Change(kind="general_variant", candidate=variant_data, old_id=variant.id if variant else None),
    )
    result.changes.insert(
        0,
        Change(kind="general", candidate=general_data, old_id=existing.id if existing else None),
    )
    result.record_id = str(general_data.get("id") or "")
    result.variant_id = variant_id
    return result


def general_values(
    store,
    general: Any | None,
    variant: Any | None = None,
    *,
    params: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """渲染武将录入页时的初始值（不含技能行，技能行单独给）。"""
    from .forms import values_from_entity

    values: dict[str, str] = {}
    if general is not None:
        values.update(values_from_entity("general", general))
    if variant is not None:
        variant_values = values_from_entity("general_variant", variant)
        for key, value in variant_values.items():
            if key in {"id", "general_id"}:
                continue
            values[key] = value
        values["variant_id"] = variant.id
    if params:
        for key, value in params.items():
            if value not in (None, ""):
                values[key] = str(value)
    return values


# ---------------------------------------------------------------- 卡牌


def card_category_tree() -> dict[str, dict[str, list[str]]]:
    """卡牌分类树（``constants.CARD_CATEGORY_TREE`` 的副本）。

    录入页的候选 = 这棵树 ∪ 现有卡牌数据里出现过的值；两边都是可输入可选的，
    所以新增分类（例如以后补上「指示物」树）不需要改前端。
    """
    return {level_1: dict(children) for level_1, children in CARD_CATEGORY_TREE.items()}


def card_category_triples(store) -> list[list[str]]:
    """现有卡牌里出现过的 ``(一级, 二级, 三级)`` 组合，供录入页联动候选使用。"""
    triples: list[list[str]] = []
    for card in store.bundle.all("card").values():
        triple = [
            str(getattr(card, "category_1", "") or ""),
            str(getattr(card, "category_2", "") or ""),
            str(getattr(card, "category_3", "") or ""),
        ]
        if all(triple) and triple not in triples:
            triples.append(triple)
    return triples


def parse_card_form(
    form: Mapping[str, Any], *, store, existing: Any | None = None
) -> EditorResult:
    """卡牌录入/编辑：单层 ``Card``（分类 + 卡名 + 卡图 + 拓展包 + 效果）。

    - 卡名与一 / 二 / 三级分类必填（分类联动下拉允许自由输入新值）；
    - ``card_set`` / ``image`` / ``effect.text`` 都允许为空，``effect`` 里两者同时为空也合法；
    - ``effect.skills`` 是 0～N 条 ``SkillRef``（``{skill_id, version_id}``）：
      卡牌**只关联已有技能**，技能文本继续由 Skill / SkillVersion 维护。
    """
    result = EditorResult(values=echo_values(form))
    errors = result.errors
    data = _base(existing, CARD_DEFAULTS)

    if "name" in form:
        data["name"] = _text(form, "name")
    if not str(data.get("name") or "").strip():
        errors.append(FieldError("name", "卡名必填"))

    for field_name, label in (
        ("category_1", "一级分类"),
        ("category_2", "二级分类"),
        ("category_3", "三级分类"),
    ):
        if field_name in form:
            data[field_name] = _text(form, field_name)
        if not str(data.get(field_name) or "").strip():
            errors.append(
                FieldError(field_name, f"{label}必填（可以从下拉里选，也可以直接输入新值）")
            )

    raw_id = _text(form, "id")
    if raw_id:
        data["id"] = raw_id
    elif existing is None:
        data["id"] = make_entity_id(
            "card_", str(data.get("name") or "card"), store.bundle.ids("card")
        )

    if "image" in form:
        data["image"] = _text(form, "image")
    if "card_set" in form:
        data["card_set"] = _optional_text(form, "card_set")
    if "review_note" in form:
        data["review_note"] = _text(form, "review_note")

    # ---------------------------------------------------------------- 效果（嵌套映射）
    effect = dict(data.get("effect") or {})
    if "effect_text" in form:
        effect["text"] = _text(form, "effect_text")
    else:
        effect.setdefault("text", "")

    existing_skills = store.bundle.ids("skill")
    refs: list[dict] = []
    result.rows = []
    for row in parse_skill_rows(form):
        prefix = _row_prefix(row.index)
        rendered = row.as_dict()
        result.rows.append(rendered)

        if not row.skill_id:
            if row.new_name:
                errors.append(
                    FieldError(
                        f"{prefix}skill_id",
                        f"技能「{row.new_name}」还没有从下拉里选中：卡牌只关联已有技能",
                    )
                )
            continue
        # 失败页要原样回显这一行：技能名 + 可选版本一起带上，
        # 否则重新渲染时 <select> 里没有当前版本，用户一提交就把版本绑定丢了。
        rendered["skill_name"] = store.name_of("skill", row.skill_id)
        rendered["version_options"] = store.skill_version_options(row.skill_id)
        if row.skill_id not in existing_skills:
            errors.append(FieldError(f"{prefix}skill_id", f"技能不存在：{row.skill_id}"))
            # 仍然把这条引用放进候选：这样失败页也能由校验器给出“文件 / 字段 / 原因”
            refs.append({"skill_id": row.skill_id, "version_id": row.version_id or None})
            continue

        version_id = row.version_id or None
        if version_id:
            version = store.entity("skill_version", version_id)
            if version is None:
                errors.append(FieldError(f"{prefix}version_id", f"版本不存在：{version_id}"))
                refs.append({"skill_id": row.skill_id, "version_id": version_id})
                continue
            if version.skill_id != row.skill_id:
                errors.append(
                    FieldError(
                        f"{prefix}version_id",
                        f"版本 {version_id} 属于技能 {version.skill_id}，和选中的技能不一致",
                    )
                )
                refs.append({"skill_id": row.skill_id, "version_id": version_id})
                continue

        refs.append({"skill_id": row.skill_id, "version_id": version_id})
        rendered["version_label"] = (
            store.version_display_label(store.entity("skill_version", version_id))
            if version_id
            else ""
        )
        rendered["version_display"] = rendered["version_label"]

    effect["skills"] = refs
    data["effect"] = effect

    result.changes = [
        Change(kind="card", candidate=data, old_id=existing.id if existing else None)
    ]
    result.record_id = str(data.get("id") or "")
    return result


def card_values(
    store, card: Any | None, *, params: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """渲染卡牌录入页时的初始值（技能行单独给）。"""
    from .forms import values_from_entity

    values: dict[str, str] = {
        "id": "",
        "category_1": "",
        "category_2": "",
        "category_3": "",
        "name": "",
        "image": "",
        "card_set": "",
        "effect_text": "",
        "review_note": "",
    }
    if card is not None:
        values.update(values_from_entity("card", card))
        effect = getattr(card, "effect", None)
        values["effect_text"] = str(getattr(effect, "text", "") or "")
        values["card_set"] = card.card_set or ""
    if params:
        for key, value in params.items():
            if value not in (None, ""):
                values[str(key)] = str(value)
    return values


def card_skill_rows(store, card: Any | None) -> list[dict]:
    """编辑页初始技能行：把 ``effect.skills`` 渲染成一行行表单。"""
    if card is None:
        return []
    rows: list[dict] = []
    for index, ref in enumerate(card.effect.skills):
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
                "version_display": store.version_display_label(version) if version else "",
                "version_text": version.text if version else "",
                "version_options": store.skill_version_options(ref.skill_id),
            }
        )
    return rows


# ---------------------------------------------------------------- 共用小工具


def variant_choices() -> list[tuple[str, str]]:
    return [(label, label) for label in VARIANT_LABELS]


def mode_choices() -> list[tuple[str, str]]:
    return [(key, MODE_LABELS.get(key, key)) for key in Mode.__members__]


def gender_choices() -> list[tuple[str, str]]:
    return [(key, GENDER_LABELS.get(key, key)) for key in Gender.__members__]


def source_type_choices() -> list[tuple[str, str]]:
    from ..constants import SOURCE_LABELS

    return [(key, SOURCE_LABELS.get(key, key)) for key in SourceType.__members__]


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
