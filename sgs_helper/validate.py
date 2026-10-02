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
REFERENCE_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("general", "product_id", "product", False),
    ("general", "skill_ids", "skill", True),
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

#: source 允许为空的 kind（其余 kind 的 source 由模型层强制必填）
SOURCE_OPTIONAL_KINDS = ("product", "general", "skill")


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
    for kind, field_name, target_kind, is_list in REFERENCE_FIELDS:
        for entity_id, entity in bundle.all(kind).items():
            origin = bundle.origin_of(kind, entity_id)
            value = getattr(entity, field_name, None)

            if is_list:
                if not value:
                    continue
                seen: set[str] = set()
                for index, ref in enumerate(value):
                    field_path = f"{field_name}[{index}]"
                    if not isinstance(ref, str) or not ref.strip():
                        result.errors.append(
                            Issue(
                                "error",
                                origin.path,
                                field_path,
                                "空引用：列表里出现空字符串，请删除该元素或填写有效 id",
                                line=origin.line,
                                entity_id=entity_id,
                            )
                        )
                        continue
                    if ref in seen:
                        result.warnings.append(
                            Issue(
                                "warning",
                                origin.path,
                                field_path,
                                f'列表内重复引用 "{ref}"',
                                line=origin.line,
                                entity_id=entity_id,
                            )
                        )
                        continue
                    seen.add(ref)
                    if target_kind == kind and ref == entity_id:
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
                    if ref not in bundle.ids(target_kind):
                        result.errors.append(
                            Issue(
                                "error",
                                origin.path,
                                field_path,
                                _describe_missing(bundle, target_kind, ref),
                                line=origin.line,
                                entity_id=entity_id,
                            )
                        )
                continue

            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                result.errors.append(
                    Issue(
                        "error",
                        origin.path,
                        field_name,
                        "空引用：该字段不能是空字符串，请填写有效 id 或改为 null",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
                continue
            if target_kind == kind and value == entity_id:
                result.errors.append(
                    Issue(
                        "error",
                        origin.path,
                        field_name,
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
                        field_name,
                        _describe_missing(bundle, target_kind, value),
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )


# ---------------------------------------------------------------- 一致性


def _check_kind_consistency(bundle: DataBundle, result: ValidationResult) -> None:
    products = bundle.all("product")

    # 武将：体力步进 / 产品模式一致 / 技能在对应模式下必须有版本
    for entity_id, general in bundle.all("general").items():
        origin = bundle.origin_of("general", entity_id)
        if abs(general.hp * 2 - round(general.hp * 2)) > 1e-9:
            result.warnings.append(
                Issue(
                    "warning",
                    origin.path,
                    "hp",
                    f"体力 {general.hp} 不是 0.5 的整数倍；国战/身份局体力通常按 0.5 步进，请确认",
                    line=origin.line,
                    entity_id=entity_id,
                )
            )
        if general.product_id and general.product_id in products:
            product = products[general.product_id]
            if product.mode != general.mode:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "mode",
                        f"武将模式 {MODE_LABELS.get(general.mode, general.mode)} 与所属产品 "
                        f"{product.id} 的模式 {MODE_LABELS.get(product.mode, product.mode)} 不一致",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )
        versions = bundle.all("skill_version")
        for skill_id in general.skill_ids:
            skill = bundle.get("skill", skill_id)
            if skill is None:
                continue  # 引用不存在已在引用检查里报错
            hit = [
                sv
                for sv in versions.values()
                if sv.skill_id == skill_id and sv.mode in (general.mode, "common")
            ]
            if not hit:
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "skill_ids",
                        f'技能 "{skill.name}"({skill_id}) 在模式 '
                        f"{MODE_LABELS.get(general.mode, general.mode)} 下没有任何 SkillVersion，"
                        "查询时拿不到技能文本",
                        line=origin.line,
                        entity_id=entity_id,
                    )
                )

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
                        f"{product.id} 的模式 {MODE_LABELS.get(product.mode, product.mode)} 不一致",
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
            if general.mode not in (faq.mode, "common"):
                result.warnings.append(
                    Issue(
                        "warning",
                        origin.path,
                        "related_general_ids",
                        f"FAQ 模式 {MODE_LABELS.get(faq.mode, faq.mode)} 与关联武将 "
                        f"{general_id} 的模式 {MODE_LABELS.get(general.mode, general.mode)} 不一致",
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
