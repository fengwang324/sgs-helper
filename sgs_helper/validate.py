"""数据校验：模型校验 + 交叉引用 + 一致性检查。

校验器只做“能明确判定对错”的检查，不做主观可信度评分。

输出格式固定为三段：**文件路径 / 字段 / 原因**，例如::

    ✗ data/generals/kongrong.yaml:12
        general_kongrong_sample_guozhan.skill_ids[1]
        引用的 技能(skill_not_exist) 不存在：data/skills/ 下没有该 id
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from .constants import KIND_BY_NAME, KIND_ORDER, MODE_LABELS
from .loader import DataBundle, Issue, RawEntity, load_data_dir
from .schema import MODEL_BY_KIND

#: 交叉引用声明表：(来源 kind, 字段, 目标 kind, 是否列表)
#:
#: 字段支持两种写法：
#:
#: - ``product_id``               普通标量字段；
#: - ``related_keyword_ids``      普通列表字段；
#: - ``skills[].skill_id``        嵌套结构里的字段（``[]`` 表示“逐项展开列表”）。
#:
#: 这张表同时驱动校验器的引用检查与本地管理页的删除保护，只声明一次。
REFERENCE_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("general_variant", "general_id", "general", False),
    ("general_variant", "product_id", "product", False),
    ("general_variant", "skills[].skill_id", "skill", True),
    ("general_variant", "skills[].version_id", "skill_version", True),
    ("skill_version", "skill_id", "skill", False),
    ("skill_version", "product_id", "product", False),
    ("skill_version", "previous_version_id", "skill_version", False),
    ("skill_version", "related_keywords", "keyword", True),
    ("skill_version", "related_timing_ids", "timing", True),
    ("card", "related_keywords", "keyword", True),
    ("card_printing", "card_id", "card", False),
    ("card_printing", "product_id", "product", False),
    ("faq", "related_general_ids", "general", True),
    ("faq", "related_skill_ids", "skill", True),
    ("faq", "related_card_ids", "card", True),
    ("faq", "related_keywords", "keyword", True),
    ("faq", "related_timing_ids", "timing", True),
    ("keyword", "related_keyword_ids", "keyword", True),
    ("keyword", "related_timing_ids", "timing", True),
    ("timing", "parent_id", "timing", False),
    ("timing", "previous_ids", "timing", True),
    ("timing", "next_ids", "timing", True),
    ("timing", "related_keywords", "keyword", True),
    ("timing", "related_skill_ids", "skill", True),
    ("timing", "related_faq_ids", "faq", True),
)


@dataclass(frozen=True)
class ReferenceValue:
    """一条具体的引用值。"""

    #: 可读字段路径，例如 ``skills[0].version_id`` 或 ``related_skill_ids[2]``
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

#: source 允许为空的 kind（其余 kind 的 source 由模型层强制必填）
SOURCE_OPTIONAL_KINDS = ("product", "general_variant", "skill")


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
    _check_kind_consistency(bundle, result)
    _check_version_consistency(bundle, result)
    _check_timing_consistency(bundle, result)

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

    # 来源缺失提示（source 本身是可选的 kind 才需要提示）
    for kind in SOURCE_OPTIONAL_KINDS:
        for entity_id, entity in bundle.all(kind).items():
            if getattr(entity, "source", None) is None:
                origin = bundle.origin_of(kind, entity_id)
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "source",
                        "未记录来源(source)；资料来源是长期维护的关键，建议补上",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )


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
            if product.general_sets and variant.general_set:
                if variant.general_set not in product.general_sets:
                    result.warnings.append(
                        Issue(
                            "warning",
                            origin.path,
                            "general_set",
                            f'武将牌系列 "{variant.general_set}" 不在所属产品 {product.id} 的 '
                            f"general_sets（{'、'.join(product.general_sets)}）里，请确认是不是录错了产品",
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

            # 显式绑定的版本必须属于同一个技能
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
                        field_path,
                        f'技能 "{skill.name}"({ref.skill_id}) 在模式 '
                        f"{_mode_label(variant.mode)} 下没有任何 SkillVersion，"
                        "查询时拿不到技能文本",
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

    # 卡牌印刷：产品模式一致 / 花色点数组合 / 同产品重复
    combos: dict[tuple, list[str]] = {}
    for entity_id, printing in bundle.all("card_printing").items():
        origin = bundle.origin_of("card_printing", entity_id)
        if printing.product_id in products:
            product = products[printing.product_id]
            if product.mode != printing.mode:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "mode",
                        f"实体卡模式 {MODE_LABELS.get(printing.mode, printing.mode)} 与所属产品 "
                        f"{product.id} 的模式 {_mode_label(product.mode)} 不一致",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
        if printing.suit == "none" and printing.rank is not None:
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "rank",
                    "suit 为 none 却填写了 rank，请确认是否漏填花色",
                    line=origin.line,
                    entity_id=entity_id,
                )
            )
        if printing.suit != "none" and printing.rank is None:
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "rank",
                    "填写了花色却没有点数，请确认",
                    line=origin.line,
                    entity_id=entity_id,
                )
            )
        key = (
            printing.product_id,
            printing.card_set or "",
            printing.suit,
            printing.rank,
        )
        combos.setdefault(key, []).append(entity_id)

    for key, ids in combos.items():
        if len(ids) < 2:
            continue
        product_id, card_set, suit, rank = key
        origin = bundle.origin_of("card_printing", ids[0])
        result.warnings.append(
            Issue(
                "warning",
                origin.path,
                "suit/rank",
                f"同一产品 {product_id}"
                + (f" / 牌堆 {card_set}" if card_set else "")
                + f" 下存在多张 {suit} {rank} 的实体卡：{', '.join(sorted(ids))}",
                line=origin.line,
                entity_id=ids[0],
            )
        )

    # FAQ 模式与关联武将模式
    for entity_id, faq in bundle.all("faq").items():
        origin = bundle.origin_of("faq", entity_id)
        if faq.mode == "common":
            continue
        for general_id in faq.related_general_ids:
            general = bundle.get("general", general_id)
            if general is None:
                continue
            modes = {
                variant.mode
                for variant in bundle.all("general_variant").values()
                if variant.general_id == general_id
            }
            if modes and faq.mode not in modes and "common" not in modes:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "related_general_ids",
                        f"FAQ 模式 {MODE_LABELS.get(faq.mode, faq.mode)} 与关联武将 "
                        f"{general_id} 的版本模式（{'、'.join(sorted(m or '未填' for m in modes))}）"
                        "都不一致",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )

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


#: 未填写的生效区间端点按“无穷”处理（日期闭区间）
_MIN_DATE = date.min
_MAX_DATE = date.max


def _interval(value_from, value_to) -> tuple[Any, Any]:
    """把 None 端点替换为可比较的边界；日期闭区间。"""
    low = value_from or _MIN_DATE
    high = value_to or _MAX_DATE
    return low, high


def _overlaps(a_from, a_to, b_from, b_to) -> bool:
    a_low, a_high = _interval(a_from, a_to)
    b_low, b_high = _interval(b_from, b_to)
    return max(a_low, b_low) <= min(a_high, b_high)


def _check_version_consistency(bundle: DataBundle, result: ValidationResult) -> None:
    versions = bundle.all("skill_version")

    # previous_version_id：必须同技能、不能成环、时间上不能倒退
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
        if (
            previous.effective_from is not None
            and version.effective_from is not None
            and previous.effective_from > version.effective_from
        ):
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "previous_version_id",
                    f"前一版本 {previous_id} 的 effective_from({previous.effective_from}) "
                    f"晚于当前版本({version.effective_from})，请确认版本顺序",
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

    # 同一技能 + 同一模式 + 同一产品范围：生效区间不应重叠，业务键不应重复
    groups: dict[tuple, list[Any]] = {}
    for version in versions.values():
        key = (version.skill_id, version.mode, version.product_id or "*")
        groups.setdefault(key, []).append(version)

    for key, group in groups.items():
        skill_id, mode, product_scope = key
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                origin = bundle.origin_of("skill_version", right.id)
                same_key = (left.effective_from, left.effective_to) == (
                    right.effective_from,
                    right.effective_to,
                )
                if same_key:
                    result.warnings.append(
                        Issue(
                            "warning",
                            origin.path,
                            "effective_from/effective_to",
                            f"{left.id} 与 {right.id} 的生效区间完全相同，疑似重复版本"
                            "（同一技能同一模式同一产品范围只应有一条同时有效的版本）",
                            line=origin.line,
                            entity_id=right.id,
                        )
                    )
                elif _overlaps(
                    left.effective_from, left.effective_to, right.effective_from, right.effective_to
                ):
                    result.warnings.append(
                        Issue(
                            "warning",
                            origin.path,
                            "effective_from/effective_to",
                            f"{left.id} 与 {right.id} 的生效区间重叠"
                            f"（技能 {skill_id} / 模式 {MODE_LABELS.get(mode, mode)} / "
                            f"产品范围 {product_scope}），"
                            "推导“当前有效版本”时会出现歧义",
                            line=origin.line,
                            entity_id=right.id,
                        )
                    )


def _check_timing_consistency(bundle: DataBundle, result: ValidationResult) -> None:
    timings = bundle.all("timing")

    # parent 链成环
    for entity_id, timing in timings.items():
        origin = bundle.origin_of("timing", entity_id)
        walked = {entity_id}
        cursor = timing.parent_id
        while cursor:
            if cursor in walked:
                result.errors.append(
                    Issue(
                        "error",
                        origin.path,
                        "parent_id",
                        f"parent 链成环：{entity_id} -> … -> {cursor}",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
                break
            walked.add(cursor)
            node = timings.get(cursor)
            cursor = node.parent_id if node is not None else None

    # previous_ids / next_ids 双向一致
    for entity_id, timing in timings.items():
        origin = bundle.origin_of("timing", entity_id)
        for next_id in timing.next_ids:
            node = timings.get(next_id)
            if node is not None and entity_id not in node.previous_ids:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "next_ids",
                        f"{entity_id}.next_ids 含 {next_id}，"
                        f"但 {next_id}.previous_ids 未反向包含 {entity_id}",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
        for previous_id in timing.previous_ids:
            node = timings.get(previous_id)
            if node is not None and entity_id not in node.next_ids:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "previous_ids",
                        f"{entity_id}.previous_ids 含 {previous_id}，"
                        f"但 {previous_id}.next_ids 未反向包含 {entity_id}",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
