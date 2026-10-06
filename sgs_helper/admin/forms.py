"""表单：**从 Pydantic 模型半自动生成**字段，并把提交值转回实体字典。

不做复杂动态表单系统：字段类型由 ``model_json_schema()`` 推导，
枚举渲染成下拉框、日期渲染成 date 输入、列表渲染成“一行一项”的 textarea、
内嵌 ``Source`` 渲染成 ``type`` + ``reference`` 两个字段。

解析阶段只做“格式级”转换与友好报错；语义校验（值域、交叉引用、一致性）一律交给
``validate_entities``，避免两套规则各说各话。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Mapping

from ..constants import (
    GENDER_LABELS,
    ID_PATTERN,
    MODE_LABELS,
    SOURCE_LABELS,
    VERIFICATION_LABELS,
)
from ..schema import MODEL_BY_KIND
from ..validate import REFERENCE_FIELDS
from .yaml_writer import SOURCE_KEY_ORDER

#: 字段 -> 中文标签（按新模型：只保留真实存在的字段）
FIELD_LABELS: dict[str, str] = {
    "id": "记录 ID",
    "name": "名称",
    "review_note": "复核备注",
    "mode": "模式",
    "verification_status": "核验状态",
    "release_year": "发行年份",
    "general_sets": "武将牌系列",
    "card_sets": "游戏牌拓展包",
    "faction": "势力",
    "hp": "勾玉（体力）",
    "gender": "性别",
    "product_id": "所属产品",
    "general_set": "武将牌系列",
    "title": "称号",
    "general_number": "编号",
    "variant": "版本标签",
    "general_id": "所属武将",
    "skills": "技能",
    "skill_id": "所属技能",
    "text": "规则文本",
    "version_label": "版本标签",
    "previous_version_id": "前一版本",
    "category_1": "一级分类",
    "category_2": "二级分类",
    "category_3": "三级分类",
    "image": "卡图",
    "card_set": "所属拓展包",
    "effect": "效果",
    "question": "问题",
    "answer": "回答/裁定",
    "definition": "定义",
    "description": "说明",
    "source": "资料来源",
    "source.type": "来源类型",
    "source.reference": "来源引用（URL / 页码）",
}

#: 字段 -> 说明（放在输入框下方）
FIELD_HELP: dict[str, str] = {
    "id": "小写字母开头，只能包含小写字母、数字、下划线；建议带业务前缀，如 general_xxx。修改 ID 会同时移动 YAML 文件。",
    "mode": "common 表示跨模式通用。",
    "verification_status": "unverified 未核验 / community 社区资料 / official 官方依据 / physical_verified 实体卡已核验 / conflict 来源冲突。",
    "review_note": "需要人工对照卡图 / 实物复核的异常（OCR 误读、疑似错字…）。正常记录留空。",
    "category_1": "一级分类（游戏牌 / 指示物…）；用卡牌录入页的联动下拉选择，也可以自己输入新值。",
    "category_2": "二级分类（基本牌 / 锦囊牌 / 装备牌…）。",
    "category_3": "三级分类；基本牌这里只写牌族（杀 / 闪 / 桃 / 酒），具体是哪一张写在 name。",
    "image": "data/card_images/ 下的文件名；暂时没有卡图就留空。",
    "card_set": "所属拓展包（九变篇、君临天下·权…）；候选来自所有产品的 card_sets，允许留空。",
    "text": "技能规则原文。更新技能请新增一条 SkillVersion，不要改旧版本。",
    "source.reference": "如果是 URL，页面上可以直接点击打开。",
    "previous_version_id": "指向被本版本取代的旧版本；旧版本保持原样。",
    "product_id": "填 null 或不填表示不绑定具体产品。",
    "general_set": "武将牌系列 / 武将牌组名称；候选来自所选产品的 general_sets，也可以直接输入新值。",
    "general_sets": "一行一项：这个产品里的武将牌系列（国战标准、势备篇…）。",
    "card_sets": "一行一项：这个产品里的游戏牌拓展包（九变篇、君主游戏牌…）；它同时是卡牌 card_set 的候选来源。",
    "title": "武将卡上的称号，例如“独眼的罗刹”。",
    "general_number": "实体卡上的编号，例如 WEI001 / shu-01；和内部 ID 是两回事。",
    "variant": "这个版本叫什么：标准版 / 国战版 / 界限突破 / OL / 手杀 / 其他。",
    "general_id": "指向武将个体（General）；一个武将可以有多个版本。",
    "hp": "勾玉数量，支持 1 / 1.5 / 2 / 2.5 / 3 / 4 / 5；允许留空以后再补。",
    "skills": "武将的技能关系（含可选的具体版本绑定）；日常录入请用“武将录入页”。",
}

#: 需要多行输入的字段
TEXTAREA_FIELDS: frozenset[str] = frozenset(
    {
        "text",
        "question",
        "answer",
        "definition",
        "description",
        "review_note",
    }
)

#: 枚举字段 -> 中文标签表
ENUM_LABELS: dict[str, dict[str, str]] = {
    "mode": MODE_LABELS,
    "verification_status": VERIFICATION_LABELS,
    "gender": GENDER_LABELS,
    "source.type": SOURCE_LABELS,
}

#: 引用字段 -> 目标 kind（给表单加 datalist 候选项）
REFERENCE_TARGETS: dict[tuple[str, str], str] = {
    (kind, field_name): target_kind
    for kind, field_name, target_kind, _is_list in REFERENCE_FIELDS
}
#: 同一字段在不同 kind 下语义相同，补一份按字段名的兜底映射
REFERENCE_TARGET_BY_FIELD: dict[str, str] = {
    "product_id": "product",
    "skill_id": "skill",
    "previous_version_id": "skill_version",
}

#: 带 0.5 步进的数值字段
STEP_FIELDS: dict[str, str] = {"hp": "0.5"}


@dataclass
class FieldSpec:
    name: str
    label: str
    widget: str = "text"          # text | textarea | number | date | select | list | source | object
    required: bool = False
    choices: list[tuple[str, str]] = field(default_factory=list)
    default: Any = None
    help: str = ""
    group: str = "basic"          # basic | source
    ref_kind: str | None = None
    step: str = "any"
    readonly: bool = False
    #: 模型里这个字段是否允许 null（``X | None``）。留空时用来区分「写 null」和「写空串」。
    nullable: bool = False


@dataclass
class FieldError:
    field: str
    message: str


# ---------------------------------------------------------------- 生成规格


def _deref(prop: dict, defs: dict) -> dict:
    if "$ref" in prop:
        return defs.get(prop["$ref"].split("/")[-1], {})
    for branch in prop.get("anyOf", []):
        if branch.get("type") != "null" and "$ref" not in branch:
            return branch
        if "$ref" in branch:
            return defs.get(branch["$ref"].split("/")[-1], {})
    return prop


def _is_optional(prop: dict) -> bool:
    if prop.get("type") == "null":
        return True
    return any(branch.get("type") == "null" for branch in prop.get("anyOf", []))


def _default_of(model: Any, name: str) -> Any:
    info = model.model_fields.get(name)
    if info is None:
        return None
    if info.default_factory is not None:  # type: ignore[call-arg]
        try:
            return info.default_factory()  # type: ignore[misc]
        except Exception:  # pragma: no cover
            return None
    default = info.default
    if default is None or default.__class__.__name__ in {"PydanticUndefinedType", "Undefined"}:
        return None
    return default


def form_specs(kind: str, *, include_id: bool = True) -> list[FieldSpec]:
    """按模型定义生成字段规格（顺序 = 模型字段顺序）。

    字段集合完全由 ``schema.py`` 决定：模型里删掉的字段这里自然就消失了，
    不需要（也不允许）在模板里留隐藏 input。
    """
    model = MODEL_BY_KIND[kind]
    schema = model.model_json_schema()
    props: dict = schema.get("properties", {})
    defs: dict = schema.get("$defs", {})
    required: set[str] = set(schema.get("required", []))

    specs: list[FieldSpec] = []
    for name in model.model_fields:
        if name == "id" and not include_id:
            continue
        prop = props.get(name, {})
        resolved = _deref(prop, defs)
        spec = FieldSpec(
            name=name,
            label=FIELD_LABELS.get(name, name),
            required=name in required,
            default=_default_of(model, name),
            help=FIELD_HELP.get(name, ""),
            nullable=_is_optional(prop),
        )
        spec.ref_kind = REFERENCE_TARGETS.get((kind, name)) or REFERENCE_TARGET_BY_FIELD.get(name)

        if name == "source":
            # source 是内嵌对象：本身不渲染输入框，由 source.* 子字段负责
            spec.widget = "source"
            spec.required = False
            specs.append(spec)
            for sub in SOURCE_KEY_ORDER:
                key = f"source.{sub}"
                specs.append(
                    FieldSpec(
                        name=key,
                        label=FIELD_LABELS.get(key, sub),
                        widget=(
                            "select"
                            if key == "source.type"
                            else ("textarea" if key in TEXTAREA_FIELDS else "text")
                        ),
                        required=sub == "type",
                        choices=(
                            [(value, SOURCE_LABELS.get(value, value)) for value in SOURCE_LABELS]
                            if key == "source.type"
                            else []
                        ),
                        default="physical_card" if key == "source.type" else "",
                        help=FIELD_HELP.get(key, ""),
                        group="source",
                    )
                )
            continue

        type_name = resolved.get("type")
        if "enum" in resolved or "enum" in prop:
            enum_values = resolved.get("enum") or prop.get("enum") or []
            labels = ENUM_LABELS.get(name, {})
            spec.widget = "select"
            spec.choices = [(str(v), labels.get(str(v), str(v))) for v in enum_values]
        elif type_name == "array":
            item = resolved.get("items") or {}
            item_type = item.get("type") or ("object" if "$ref" in item else "")
            if item_type == "object":
                # 数组里的内嵌对象（例如武将的 skills 关系列表）：generic 表单渲染不了，
                # 交给武将专用录入页；generic 页面直接跳过这个字段。
                spec.widget = "object"
            else:
                spec.widget = "list"
                spec.help = spec.help or "一行一项；留空表示没有。"
        elif type_name == "object":
            # 内嵌对象：由专用录入页负责，generic 表单既渲染不了也不该瞎猜，直接跳过。
            spec.widget = "object"
        elif type_name == "integer":
            spec.widget = "number"
            spec.step = STEP_FIELDS.get(name, "1")
        elif type_name == "number":
            spec.widget = "number"
            spec.step = STEP_FIELDS.get(name, "any")
        elif resolved.get("format") == "date":
            spec.widget = "date"
            spec.help = spec.help or "格式 YYYY-MM-DD；留空表示不限制。"
        else:
            spec.widget = "textarea" if name in TEXTAREA_FIELDS else "text"

        if name == "id":
            spec.help = FIELD_HELP["id"]
        specs.append(spec)
    return specs


def source_choices() -> list[tuple[str, str]]:
    return [(value, SOURCE_LABELS.get(value, value)) for value in SOURCE_LABELS]


def spec_map(kind: str, *, include_id: bool = True) -> dict[str, FieldSpec]:
    return {spec.name: spec for spec in form_specs(kind, include_id=include_id)}


# ---------------------------------------------------------------- 值 <-> 文本


def value_to_text(spec: FieldSpec, value: Any) -> str:
    if value is None:
        return ""
    if spec.widget in {"object", "source"}:
        return ""
    if spec.widget == "list":
        return "\n".join(str(item) for item in value)
    if isinstance(value, Enum):
        # use_enum_values 只影响校验结果；模型默认值可能仍是枚举成员
        return str(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def values_from_entity(kind: str, entity: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    data = entity.model_dump(mode="json") if hasattr(entity, "model_dump") else dict(entity)
    source = data.pop("source", None) or {}
    for spec in form_specs(kind):
        if spec.name.startswith("source."):
            values[spec.name] = str(source.get(spec.name.split(".", 1)[1], "") or "")
        else:
            values[spec.name] = value_to_text(spec, data.get(spec.name))
    return values


def values_from_defaults(kind: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for spec in form_specs(kind):
        if spec.name.startswith("source."):
            default = "physical_card" if spec.name == "source.type" else ""
            values[spec.name] = default
        else:
            default = spec.default
            if spec.name == "id":
                default = ""
            if spec.name == "verification_status" and not default:
                default = "unverified"
            values[spec.name] = value_to_text(spec, default)
    return values


def values_from_form(kind: str, form: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for spec in form_specs(kind):
        values[spec.name] = str(form.get(spec.name, "") or "").strip()
    return values


def prefill(values: dict[str, str], params: Mapping[str, Any]) -> dict[str, str]:
    """新增技能版本之类的场景：把 URL 参数带进表单默认值。"""
    for spec_name, value in params.items():
        if spec_name in values and value not in (None, ""):
            values[spec_name] = str(value)
    return values


# ---------------------------------------------------------------- 解析提交


def _convert_scalar(spec: FieldSpec, raw: str, errors: list[FieldError]) -> Any:
    if raw == "":
        if spec.required:
            errors.append(FieldError(spec.name, "必填字段不能为空"))
            return None
        if spec.widget == "select":
            # 枚举字段 schema 里不允许 null：留空就用模型默认值
            return spec.default if spec.default is not None else None
        if spec.widget == "number" or spec.nullable:
            return None
        # 普通的非 Optional 字符串字段（version_label / subtitle / definition …）：留空就是空串
        return ""
    if spec.widget == "number":
        try:
            return int(raw) if spec.step in {"1", "-1"} else float(raw)
        except ValueError:
            errors.append(FieldError(spec.name, f"必须是数字：{raw!r}"))
            return None
    return raw


def parse_form(kind: str, form: Mapping[str, Any]) -> tuple[dict, list[FieldError]]:
    """把表单值转成候选实体 dict（不做语义校验）。"""
    errors: list[FieldError] = []
    candidate: dict[str, Any] = {}
    source: dict[str, Any] = {}
    model = MODEL_BY_KIND[kind]

    for spec in form_specs(kind):
        raw = str(form.get(spec.name, "") or "").strip()
        if spec.name.startswith("source."):
            source[spec.name.split(".", 1)[1]] = raw
            continue
        if spec.name in {"source", "skills"} or spec.widget in {"source", "object"}:
            continue  # 内嵌结构由专用录入页 / source.* 子字段处理
        if spec.widget == "list":
            # 一行一项。**不做逗号切分**：系列名里完全可能带逗号。
            items = [line.strip() for line in raw.splitlines()]
            candidate[spec.name] = [item for item in items if item]
            continue
        candidate[spec.name] = _convert_scalar(spec, raw, errors)

    entity_id = str(candidate.get("id") or "")
    if entity_id and not re.fullmatch(ID_PATTERN, entity_id):
        errors.append(
            FieldError("id", "ID 只能使用小写字母、数字、下划线，且必须以小写字母开头")
        )

    if "source" in model.model_fields:
        # 只有规则类实体（SkillVersion / FAQ / Keyword / Timing）才有 source 字段
        source_required = "source" in set(model.model_json_schema().get("required", []))
        if any(source.values()) or source_required:
            if not source.get("type"):
                errors.append(FieldError("source.type", "来源类型必填（资料来源是长期维护的关键）"))
            candidate["source"] = {
                "type": source.get("type", ""),
                "reference": source.get("reference", ""),
            }
        else:
            candidate["source"] = None

    return candidate, errors


def candidate_for_render(kind: str, form: Mapping[str, Any]) -> dict[str, str]:
    """保存失败时把用户填的内容原样回显（不要用默认值覆盖用户输入）。"""
    return values_from_form(kind, form)
