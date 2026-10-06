"""数据校验：模型校验 + 交叉引用 + 一致性检查。

校验器只做“能明确判定对错”的检查，不做主观可信度评分。

输出格式固定为三段：**文件路径 / 字段 / 原因**，例如::

    ✗ data/generals/kongrong.yaml:12
        general_kongrong_sample_guozhan.skills[1].skill_id
        引用的 技能(skill_not_exist) 不存在：data/skills/ 下没有该 id
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from .constants import CARD_CATEGORY_TREE, KIND_BY_NAME, KIND_ORDER, MODE_LABELS
from .loader import DataBundle, Issue, RawEntity, load_data_dir
from .schema import MODEL_BY_KIND

#: 交叉引用声明表：(来源 kind, 字段, 目标 kind, 是否列表)
#:
#: 字段支持两种写法：
#:
#: - ``product_id``               普通标量字段；
#: - ``skills[].skill_id``        嵌套结构里的字段（``[]`` 表示“逐项展开列表”）。
#:
#: 这张表同时驱动校验器的引用检查与本地管理页的删除保护，只声明一次。
#:
#: 实体卡类（Product / GeneralVariant / Card）不再有 ``source``，
#: 规则类的 ``related_*`` / ``parent_id`` / ``previous_ids`` / ``next_ids`` 也已
#: 全部删除，所以这里只剩真正的结构化引用。``GeneralVariant.general_set`` 是字符串，
#: 它到 ``Product.general_sets`` 的归属关系由 :func:`_check_general_set_consistency`
#: 单独对账（不再是实体引用，``GeneralSet`` 实体已删除）。
#:
#: ``Card.effect.skills`` 与 ``GeneralVariant.skills`` 形状相同（都是 ``SkillRef``），
#: 所以引用存在性 / 空引用 / 列表内重复检查同样声明一次即可。
REFERENCE_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("general_variant", "general_id", "general", False),
    ("general_variant", "product_id", "product", False),
    ("general_variant", "skills[].skill_id", "skill", True),
    ("general_variant", "skills[].version_id", "skill_version", True),
    ("skill_version", "skill_id", "skill", False),
    ("skill_version", "product_id", "product", False),
    ("skill_version", "previous_version_id", "skill_version", False),
    ("card", "effect.skills[].skill_id", "skill", True),
    ("card", "effect.skills[].version_id", "skill_version", True),
)


@dataclass(frozen=True)
class ReferenceValue:
    """一条具体的引用值。"""

    #: 可读字段路径，例如 ``skills[0].version_id`` 或 ``skills[2].skill_id``
    path: str
    value: Any
    #: 这个值所属的列表路径（标量字段为 None）；用于“列表内重复引用”检查
    list_path: str | None = None


def iter_reference_values(entity: Any, field: str) -> list[ReferenceValue]:
    """按声明表枚举实体上的引用值，支持 ``a.b`` 与 ``a[].b`` 两种路径。

    这样“武将 → 技能关系（``skills[].skill_id`` / ``skills[].version_id``）”
    这类嵌套结构不需要在校验器里写特例，声明一次即可同时服务于：
    引用存在性检查、空引用检查、重复引用检查、删除保护。
    """
    parts = field.split(".")
    found: list[ReferenceValue] = []

    def walk(current: Any, index: int, prefix: str, list_path: str | None) -> None:
        part = parts[index]
        expand = part.endswith("[]")
        name = part[:-2] if expand else part
        value = getattr(current, name, None)
        if value is None:
            return
        path = f"{prefix}.{name}" if prefix else name

        if expand:
            if not isinstance(value, (list, tuple)):
                return
            for position, item in enumerate(value):
                item_path = f"{path}[{position}]"
                if index == len(parts) - 1:
                    found.append(ReferenceValue(item_path, item, path))
                else:
                    # 记住这一层的列表路径：同一个列表里的重复引用要能被发现
                    walk(item, index + 1, item_path, path)
            return

        if index == len(parts) - 1:
            if isinstance(value, (list, tuple)):
                for position, item in enumerate(value):
                    found.append(ReferenceValue(f"{path}[{position}]", item, path))
            else:
                found.append(ReferenceValue(path, value, list_path))
            return

        walk(value, index + 1, path, list_path)

    walk(entity, 0, "", None)
    return found


@dataclass
class ValidationResult:
    """校验结果。``ok`` 只看 error，warning 不影响构建。"""

    data_dir: Path
    bundle: DataBundle
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def counts(self) -> dict[str, int]:
        return {kind: self.bundle.count(kind) for kind in KIND_ORDER}

    def counts_line(self) -> str:
        parts = [
            f"{KIND_BY_NAME[kind].label} {self.bundle.count(kind)}"
            for kind in KIND_ORDER
        ]
        return " / ".join(parts)

    def format_report(self, limit: int = 200) -> str:
        lines: list[str] = []
        lines.append(f"数据目录：{self.data_dir}")
        lines.append(f"实体统计：{self.counts_line()}")
        lines.append(f"校验结果：错误 {len(self.errors)} 条 / 警告 {len(self.warnings)} 条")
        if self.errors:
            lines.append("")
            lines.append(f"── 错误（{len(self.errors)}）──")
            for issue in self.errors[:limit]:
                lines.append(issue.format())
            if len(self.errors) > limit:
                lines.append(f"… 其余 {len(self.errors) - limit} 条错误已省略")
        if self.warnings:
            lines.append("")
            lines.append(f"── 警告（{len(self.warnings)}）──")
            for issue in self.warnings[:limit]:
                lines.append(issue.format())
            if len(self.warnings) > limit:
                lines.append(f"… 其余 {len(self.warnings) - limit} 条警告已省略")
        if self.ok and not self.warnings:
            lines.append("✓ 校验通过，无错误、无警告")
        elif self.ok:
            lines.append("✓ 校验通过（有警告，不影响建库）")
        else:
            lines.append("✗ 校验失败，请先修好上面的错误再执行 build_db")
        return "\n".join(lines)


# ---------------------------------------------------------------- 入口


def validate_data_dir(data_dir: Path) -> ValidationResult:
    """校验整个 ``data/`` 目录。"""
    raws, load_issues = load_data_dir(data_dir)
    return validate_entities(data_dir, raws, load_issues)


def validate_entities(
    data_dir: Path,
    raws: Iterable[RawEntity],
    load_issues: Iterable[Issue] = (),
) -> ValidationResult:
    """校验一批**已经在内存里**的原始实体，不读盘。

    这是 ``validate_data_dir`` 的底层入口，也被本地管理网页复用：
    管理端先把“候选改动”合成 RawEntity 列表在内存里跑一遍完整校验，
    通过了才写 YAML，于是磁盘上不会出现非法数据。
    """
    bundle = DataBundle(data_dir=data_dir)
    result = ValidationResult(data_dir=data_dir, bundle=bundle)
    result.errors.extend(i for i in load_issues if i.is_error)
    result.warnings.extend(i for i in load_issues if not i.is_error)

    _validate_models(list(raws), bundle, result)
    _check_references(bundle, result)
    _check_general_set_consistency(bundle, result)
    _check_kind_consistency(bundle, result)
    _check_version_consistency(bundle, result)
    _check_card_consistency(bundle, result)
    _check_images(bundle, result)

    result.errors.sort(key=_issue_sort_key)
    result.warnings.sort(key=_issue_sort_key)
    return result


def _issue_sort_key(issue: Issue) -> tuple[str, int, str]:
    return (issue.path, issue.line or 0, issue.field)


# ---------------------------------------------------------------- 模型校验


def _loc_to_path(loc: Iterable[Any]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("" if not out else ".") + str(part)
    return out or "-"


def _validate_models(
    raws: list[RawEntity], bundle: DataBundle, result: ValidationResult
) -> None:
    per_kind: dict[str, dict[str, Any]] = {}

    for raw in raws:
        model_cls = MODEL_BY_KIND[raw.kind]
        raw_id = raw.data.get("id")
        try:
            entity = model_cls.model_validate(raw.data)
        except ValidationError as exc:
            for err in exc.errors():
                message = err["msg"]
                if err["type"] == "extra_forbidden":
                    message = f"未知字段（YAML 里多了这个字段？）：{message}"
                result.errors.append(
                    Issue(
                        "error",
                        raw.origin.path,
                        _loc_to_path(err["loc"]),
                        message,
                        line=raw.origin.line,
                        entity_id=raw_id if isinstance(raw_id, str) else None,
                    )
                )
            continue

        seen = per_kind.setdefault(raw.kind, {})
        if entity.id in seen:
            first: Any = seen[entity.id]
            result.errors.append(
                Issue(
                    "error",
                    raw.origin.path,
                    "id",
                    f'id 重复："{entity.id}" 已在 data/{first.path}:{first.line} 中定义',
                    line=raw.origin.line,
                    entity_id=entity.id,
                )
            )
            continue
        seen[entity.id] = raw.origin
        bundle.add(raw.kind, entity.id, entity, raw.origin)


# ---------------------------------------------------------------- 交叉引用


def _describe_missing(bundle: DataBundle, target_kind: str, ref: str) -> str:
    label = KIND_BY_NAME[target_kind].label
    for other in KIND_ORDER:
        if other == target_kind:
            continue
        if ref in bundle.ids(other):
            return (
                f'引用的 {label}({ref}) 不存在：data/{KIND_BY_NAME[target_kind].dirname}/ 下没有该 id；'
                f"但同名的 {KIND_BY_NAME[other].label}({ref}) 存在，请检查字段是否写错"
            )
    return (
        f"引用的 {label}({ref}) 不存在：data/{KIND_BY_NAME[target_kind].dirname}/ 下没有该 id"
    )


def _check_references(bundle: DataBundle, result: ValidationResult) -> None:
    for kind, field_name, target_kind, _is_list in REFERENCE_FIELDS:
        for entity_id, entity in bundle.all(kind).items():
            origin = bundle.origin_of(kind, entity_id)
            seen_lists: dict[str, set[str]] = {}

            for ref in iter_reference_values(entity, field_name):
                value = ref.value
                field_path = ref.path

                if ref.list_path is not None:
                    seen = seen_lists.setdefault(ref.list_path, set())

                if not isinstance(value, str) or not value.strip():
                    result.errors.append(
                        Issue(
                            "error",
                            origin.path,
                            field_path,
                            "空引用：这里出现空字符串，请删除该元素或填写有效 id",
                            line=origin.line,
                            entity_id=entity_id,
                        )
                    )
                    continue

                if ref.list_path is not None:
                    if value in seen:
                        result.warnings.append(
                            Issue(
                                "warning",
                                origin.path,
                                field_path,
                                f'列表内重复引用 "{value}"',
                                line=origin.line,
                                entity_id=entity_id,
                            )
                        )
                        continue
                    seen.add(value)

                if target_kind == kind and value == entity_id:
                    result.errors.append(
                        Issue(
                            "error",
                            origin.path,
                            field_path,
                            f"自引用：{KIND_BY_NAME[kind].label} 不能引用自己",
                            line=origin.line,
                            entity_id=entity_id,
                        )
                    )
                    continue

                if value not in bundle.ids(target_kind):
                    result.errors.append(
                        Issue(
                            "error",
                            origin.path,
                            field_path,
                            _describe_missing(bundle, target_kind, value),
                            line=origin.line,
                            entity_id=entity_id,
                        )
                    )


# ---------------------------------------------------------------- 一致性


def _mode_label(mode: Any) -> str:
    """模式的中文标签；未填模式时给一个可读的占位（Product/General 的 mode 允许为空）。"""
    if not mode:
        return "（未填模式）"
    return MODE_LABELS.get(mode, str(mode))


def _check_general_set_consistency(bundle: DataBundle, result: ValidationResult) -> None:
    """武将牌系列归属：``GeneralVariant.general_set`` 必须出现在所属产品的 ``general_sets`` 里。

    ``GeneralSet`` 实体已删除：系列名只在两处维护 —— ``Product.general_sets``（候选全集）
    与 ``GeneralVariant.general_set``（具体归属），所以这里只对这两处对账，不再有系列实体。
    """
    products = bundle.all("product")
    for entity_id, variant in bundle.all("general_variant").items():
        name = (getattr(variant, "general_set", "") or "").strip()
        if not name:
            continue
        product = products.get(variant.product_id) if variant.product_id else None
        if product is None:
            # product_id 缺失 / 不存在：引用检查已经报错，这里不重复报
            continue
        if name in (product.general_sets or []):
            continue
        origin = bundle.origin_of("general_variant", entity_id)
        declared = "、".join(product.general_sets or []) or "（空）"
        result.warnings.append(
            Issue(
                "warning",
                origin.path,
                "general_set",
                f'武将牌系列 "{name}" 不在所属产品 {product.id} 的 general_sets'
                f"（{declared}）里，请确认是不是录错了产品，或该产品的 general_sets 需要补上这个系列",
                line=origin.line,
                entity_id=entity_id,
            )
        )


def _check_kind_consistency(bundle: DataBundle, result: ValidationResult) -> None:
    products = bundle.all("product")

    # 武将：体力步进 / 产品模式一致 / 技能关系（含显式版本绑定）
    for entity_id, variant in bundle.all("general_variant").items():
        origin = bundle.origin_of("general_variant", entity_id)
        if variant.hp is not None and abs(variant.hp * 2 - round(variant.hp * 2)) > 1e-9:
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "hp",
                    f"体力 {variant.hp} 不是 0.5 的整数倍；国战/身份局体力通常按 0.5 步进，请确认",
                    line=origin.line,
                    entity_id=entity_id,
                )
            )
        if variant.product_id and variant.product_id in products:
            product = products[variant.product_id]
            if (
                product.mode is not None
                and variant.mode is not None
                and product.mode != variant.mode
            ):
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "mode",
                        f"武将模式 {_mode_label(variant.mode)} 与所属产品 "
                        f"{product.id} 的模式 {_mode_label(product.mode)} 不一致",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )

        versions = bundle.all("skill_version")
        seen_skills: dict[str, int] = {}
        for position, ref in enumerate(variant.skills):
            field_path = f"skills[{position}].skill_id"
            if ref.skill_id in seen_skills:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        field_path,
                        f"同一个武将版本重复引用技能 {ref.skill_id}"
                        f"（已在 skills[{seen_skills[ref.skill_id]}] 出现）",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
            else:
                seen_skills[ref.skill_id] = position

            skill = bundle.get("skill", ref.skill_id)
            if skill is None:
                continue  # 引用不存在已在引用检查里报错

            # 显式绑定的版本必须属于同一个技能；文本由它决定，不再按日期推导
            if ref.version_id:
                version = bundle.get("skill_version", ref.version_id)
                if version is not None and version.skill_id != ref.skill_id:
                    result.errors.append(
                        Issue(
                            "error",
                            origin.path,
                            f"skills[{position}].version_id",
                            f"绑定的技能版本 {ref.version_id} 属于技能 {version.skill_id}，"
                            f"与这里的技能 {ref.skill_id} 不一致",
                            line=origin.line,
                            entity_id=entity_id,
                        )
                    )
                continue

            # version_id 为空：按模式回退到该技能唯一的一条版本；
            # 找不到与 variant.mode（或 common）匹配的版本，就表示这个武将版本上的技能没有文本
            hit = [
                sv
                for sv in versions.values()
                if sv.skill_id == ref.skill_id and sv.mode in (variant.mode, "common")
            ]
            if not hit:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        f"skills[{position}].version_id",
                        f'该武将版本的技能没有可用文本：技能 "{skill.name}"({ref.skill_id}) 在模式 '
                        f"{_mode_label(variant.mode)} 下没有任何 SkillVersion，"
                        "且 version_id 为空，查询时拿不到技能文本",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )

    # 武将个体：至少有一个版本；同一武将的版本标签不要重复
    variants_by_general: dict[str, list[str]] = {}
    for variant_id, variant in bundle.all("general_variant").items():
        variants_by_general.setdefault(variant.general_id, []).append(variant_id)
    for general_id, general in bundle.all("general").items():
        origin = bundle.origin_of("general", general_id)
        if general_id not in variants_by_general:
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "id",
                    f'武将 "{general.name}" 还没有任何版本（GeneralVariant），'
                    "录入时至少要有“标准版”这一条",
                    line=origin.line,
                    entity_id=general_id,
                )
            )
        seen_labels: dict[tuple, str] = {}
        for variant_id in variants_by_general.get(general_id, []):
            variant = bundle.get("general_variant", variant_id)
            key = (variant.variant, variant.mode)
            if key in seen_labels:
                variant_origin = bundle.origin_of("general_variant", variant_id)
                result.warnings.append(
                    Issue(
                        "warning",
                        variant_origin.path,
                        "variant",
                        f"同一武将出现重复版本：{variant.variant} / {_mode_label(variant.mode)}"
                        f"（{seen_labels[key]} 与 {variant_id}）",
                        line=variant_origin.line,
                        entity_id=variant_id,
                    )
                )
            else:
                seen_labels[key] = variant_id

    # 卡牌的校验见 :func:`_check_card_consistency`（基本牌三级分类 / card_set 归属 /
    # 技能版本绑定）与 :func:`_check_images`（卡图存在性）。

    # FAQ 的关联关系不再手工维护：构建索引 / 渲染详情时按实体名称从 question/answer 自动识别，
    # 因此这里不再校验 related_general_ids 与 FAQ 模式是否一致。

    # 技能本体：没有任何版本
    for entity_id, skill in bundle.all("skill").items():
        if not any(sv.skill_id == entity_id for sv in bundle.all("skill_version").values()):
            origin = bundle.origin_of("skill", entity_id)
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "id",
                    f'技能本体 "{skill.name}" 没有任何 SkillVersion，查询时将显示不出技能文本',
                    line=origin.line,
                    entity_id=entity_id,
                )
            )


def _check_version_consistency(bundle: DataBundle, result: ValidationResult) -> None:
    """技能版本链：``previous_version_id`` 必须同技能、不能成环。

    ``effective_from/effective_to`` 已删除，技能版本不再按日期推导“当前版本”，
    因此这里不做任何生效区间的重叠 / 顺序校验，只保留结构性的版本链检查。
    """
    versions = bundle.all("skill_version")

    # previous_version_id：必须同技能、不能成环
    for entity_id, version in versions.items():
        origin = bundle.origin_of("skill_version", entity_id)
        previous_id = version.previous_version_id
        if previous_id is None:
            continue
        previous = versions.get(previous_id)
        if previous is None:
            continue  # 不存在已由引用检查报错
        if previous.skill_id != version.skill_id:
            result.errors.append(
                Issue(
                    "error",
                    origin.path,
                    "previous_version_id",
                    f'前一版本 {previous_id} 属于技能 {previous.skill_id}，'
                    f"与当前版本技能 {version.skill_id} 不一致",
                    line=origin.line,
                    entity_id=entity_id,
                )
            )
        # 环检测
        walked = {entity_id}
        cursor = previous_id
        while cursor:
            if cursor in walked:
                result.errors.append(
                    Issue(
                        "error",
                        origin.path,
                        "previous_version_id",
                        f"版本链成环：{entity_id} -> … -> {cursor}",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
                break
            walked.add(cursor)
            node = versions.get(cursor)
            cursor = node.previous_version_id if node is not None else None


# ---------------------------------------------------------------- 卡牌


#: 「指示物」是卡牌分类（``category_1``），不是游戏牌拓展包
INDICATOR_CATEGORY = "指示物"

#: 基本牌的三级分类只能是牌族（具体是哪一张牌写在 ``name`` 里），取自分类树
BASIC_CARD_FAMILIES: tuple[str, ...] = tuple(
    CARD_CATEGORY_TREE.get("游戏牌", {}).get("基本牌", ())
)


def _check_card_consistency(bundle: DataBundle, result: ValidationResult) -> None:
    """卡牌一致性：基本牌三级分类 / ``card_set`` 归属 / 技能版本绑定。

    ``Card`` 是单层的：``category_1/2/3`` 表达“这张卡是什么”，``card_set`` 表达
    “它来自哪个拓展包”，两者完全独立。以下几条 pydantic 管不了，放在这里对账：

    - 基本牌的最后一级是 ``name``，``category_3`` 只放牌族（杀 / 闪 / 桃 / 酒）；
    - ``card_set`` 非空时必须出现在**任一产品**的 ``Product.card_sets`` 里
      （拓展包名的候选全集由产品声明，卡牌不自己发明拓展包）；
    - ``effect.skills[].version_id`` 非空时必须属于同一个 ``skill_id``
      （存在性由 :data:`REFERENCE_FIELDS` 的引用检查负责，这里只查父子关系）。
    """
    declared_card_sets: set[str] = set()
    for product in bundle.all("product").values():
        declared_card_sets.update(product.card_sets or [])
    declared_line = "、".join(sorted(declared_card_sets)) or "（没有任何产品声明 card_sets）"

    for entity_id, card in bundle.all("card").items():
        origin = bundle.origin_of("card", entity_id)

        # 基本牌：三级分类只放牌族，具体是哪一张杀 / 闪 / 桃 / 酒写在 name 里
        if card.category_2 == "基本牌" and card.category_3 not in BASIC_CARD_FAMILIES:
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "category_3",
                    f'基本牌的三级分类应为牌族（{"、".join(BASIC_CARD_FAMILIES)}），'
                    f'当前是 "{card.category_3}"；具体是哪一张牌写在 name（"{card.name}"）里',
                    line=origin.line,
                    entity_id=entity_id,
                )
            )

        # card_set 归属：只要出现在任一产品的 card_sets 里就算对上了
        card_set = (card.card_set or "").strip()
        if card_set:
            if card_set == INDICATOR_CATEGORY:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "card_set",
                        f'"{card_set}" 不是游戏牌拓展包，而是卡牌分类；'
                        "请把它写到 category_1，card_set 留空或改填真正的拓展包名",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
            elif card_set not in declared_card_sets:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "card_set",
                        f'拓展包 "{card_set}" 不在任何产品声明的 card_sets'
                        f"（{declared_line}）里，请确认拓展包名是否写错，"
                        "或需要在对应产品的 card_sets 里补上它",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )

        # 卡牌效果里的技能：显式绑定的版本必须属于同一个技能
        for position, ref in enumerate(card.effect.skills):
            if not ref.version_id:
                continue
            version = bundle.get("skill_version", ref.version_id)
            if version is None:
                continue  # 版本不存在已由引用检查报错
            if version.skill_id != ref.skill_id:
                result.errors.append(
                    Issue(
                        "error",
                        origin.path,
                        f"effect.skills[{position}].version_id",
                        f"绑定的技能版本 {ref.version_id} 属于技能 {version.skill_id}，"
                        f"与这里的技能 {ref.skill_id} 不一致",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )


# ---------------------------------------------------------------- 卡图


def _check_images(bundle: DataBundle, result: ValidationResult) -> None:
    """卡图存在性：``Card.image`` / ``GeneralVariant.image`` 必须真的在 ``data/card_images/`` 下。

    空 ``image`` 是允许的（例如待补卡图的 火杀 / 雷杀 / 掠闪），只检查非空值。
    """
    images_dir = bundle.data_dir / "card_images"
    for kind in ("card", "general_variant"):
        for entity_id, entity in bundle.all(kind).items():
            image = (getattr(entity, "image", "") or "").strip()
            if not image:
                continue
            if (images_dir / image).is_file():
                continue
            origin = bundle.origin_of(kind, entity_id)
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "image",
                    f'卡图 "{image}" 不存在：data/card_images/ 下没有这个文件'
                    "（image 填相对 data/card_images/ 的文件名），请确认文件名是否写错",
                    line=origin.line,
                    entity_id=entity_id,
                )
            )
