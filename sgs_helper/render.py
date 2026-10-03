"""把 repo 层返回的 dict 渲染成人类可读文本。

CLI 使用这一层；未来接微信小程序 / FastAPI 时直接返回 repo 的 dict 即可，
不需要这一层（可用 ``--json`` 查看同样的结构）。
"""

from __future__ import annotations

from typing import Any, Iterable

from .constants import (
    CARD_TYPE_LABELS,
    GENDER_LABELS,
    KIND_BY_NAME,
    MODE_LABELS,
    SOURCE_LABELS,
    SUIT_LABELS,
    VERIFICATION_LABELS,
)

RANK_LABELS = {1: "A", 11: "J", 12: "Q", 13: "K"}
STATUS_LABELS = {"active": "生效中", "expired": "已过期", "future": "尚未生效"}


def mode_label(mode: str | None) -> str:
    return MODE_LABELS.get(mode or "", mode or "未填")


def source_label(value: str | None) -> str:
    return SOURCE_LABELS.get(value or "", value or "未填")


def verification_label(value: str | None) -> str:
    return VERIFICATION_LABELS.get(value or "", value or "未核验")


def suit_label(value: str | None) -> str:
    return SUIT_LABELS.get(value or "", value or "")


def card_type_label(value: str | None) -> str:
    return CARD_TYPE_LABELS.get(value or "", value or "")


def gender_label(value: str | None) -> str:
    return GENDER_LABELS.get(value or "", value or "")


def rank_label(rank: Any) -> str:
    if rank is None:
        return "无点数"
    return RANK_LABELS.get(int(rank), str(rank))


def fmt_source(source: dict | None) -> str:
    if not source:
        return "（未记录来源）"
    bits = [source_label(source.get("type"))]
    if source.get("title"):
        bits.append(str(source["title"]))
    if source.get("reference"):
        bits.append(str(source["reference"]))
    text = " / ".join(bits)
    if source.get("notes"):
        text += f"（{source['notes']}）"
    return text


def fmt_range(entity: dict) -> str:
    start = entity.get("effective_from") or "（起始未填）"
    end = entity.get("effective_to") or "至今"
    return f"{start} ~ {end}"


def fmt_status(entity: dict) -> str:
    parts = [STATUS_LABELS.get(entity.get("status") or "", entity.get("status") or "")]
    current_for = entity.get("current_for_modes") or []
    if current_for:
        parts.append("当前有效：" + "、".join(mode_label(m) for m in current_for))
    ambiguous = entity.get("ambiguous_for_modes") or []
    if ambiguous:
        parts.append("存在歧义：" + "、".join(mode_label(m) for m in ambiguous))
    return "，".join(p for p in parts if p)


def fmt_location(entity: dict | None) -> str:
    if not entity:
        return ""
    file = entity.get("yaml_file")
    line = entity.get("yaml_line")
    return f"{file}:{line}" if file else ""


def _block(title: str, text: str, indent: str = "      ") -> list[str]:
    """输出多行文本，保持缩进。"""
    if not text:
        return [f"{indent}{title}：（空）"]
    rows = str(text).splitlines() or [""]
    lines = [f"{indent}{title}：{rows[0].strip()}"]
    lines += [f"{indent}{' ' * (len(title) + 1)}{row.strip()}" for row in rows[1:]]
    return lines


def _names(entities: Iterable[dict | None]) -> str:
    names = [str(e["name"]) for e in entities if e and e.get("name")]
    return "、".join(names)


def _section(title: str) -> str:
    return f"── {title} " + "─" * max(4, 60 - len(title) * 2)


# ---------------------------------------------------------------- 通用条目


def render_search(query: str, results: list[dict]) -> str:
    if not results:
        return f'没有检索到与 "{query}" 相关的内容。'
    lines = [f'检索 "{query}"，命中 {len(results)} 条：', ""]
    for item in results:
        label = KIND_BY_NAME[item["kind"]].label if item["kind"] in KIND_BY_NAME else item["kind"]
        lines.append(f"  [{label}] {item['id']}")
        lines.append(f"        {item['name']}")
    lines.append("")
    lines.append("提示：用 query.py skill/general/card/faq/keyword/timing/product 查看详情。")
    return "\n".join(lines)


def render_candidates(kind: str, query: str, candidates: list[dict]) -> str:
    label = KIND_BY_NAME[kind].label if kind in KIND_BY_NAME else kind
    lines = [f'没有唯一匹配的「{label}」：{query}', "", "可能是："]
    for item in candidates:
        lines.append(f"  - {item['id']}   {item['name']}")
    lines.append("")
    lines.append("请改用更精确的名称或直接使用 id 查询。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 技能


def render_skill(report: dict) -> str:
    definition = report["definition"]
    versions = report["versions"]
    by_id = {v["id"]: v for v in versions}

    lines = ["=" * 72, f"技能：{definition['name']}    （id：{definition['id']}）"]
    if definition.get("aliases"):
        lines.append(f"  别名：{'、'.join(definition['aliases'])}")
    lines.append(f"  技能本体来源：{fmt_source(definition.get('source'))}")
    lines.append(f"  源文件：{fmt_location(definition)}")
    if definition.get("notes"):
        lines.append(f"  备注：{definition['notes']}")

    if not versions:
        lines.append("  ⚠ 该技能没有任何 SkillVersion，尚无规则文本。")
        return "\n".join(lines)

    if len(report["modes"]) > 1:
        lines.append(
            "  同名技能共有 "
            + str(len(report["modes"]))
            + " 个模式："
            + "、".join(group["label"] for group in report["modes"])
            + "（分别保留，互不覆盖）"
        )
    lines.append(f"  “当前有效版本”按 as_of={report['as_of']} 推导，可用 --as-of 查历史：")
    lines.append("")

    for group in report["modes"]:
        lines.append(_section(group["label"]))
        if group["current_id"]:
            current = by_id.get(group["current_id"])
            suffix = ""
            if current and group.get("current_mode") == "common" and group["mode"] != "common":
                suffix = "（来自通用 common 版本）"
            lines.append(
                f"  当前有效版本：{group['current_id']}{suffix}"
                + (f"  [{current['version_label']}]" if current and current.get("version_label") else "")
            )
        elif group["ambiguous_ids"]:
            lines.append(
                "  ⚠ 当前有效版本存在歧义（同一优先级并列），候选："
                + "、".join(group["ambiguous_ids"])
            )
            lines.append("     请检查 data/skill_versions/ 下这些版本的生效区间是否重叠。")
        else:
            lines.append("  （该模式下当前没有生效中的版本）")

        for version_id in group["version_ids"]:
            version = by_id[version_id]
            mark = "▶" if version_id == group["current_id"] else "·"
            head = (
                f"  {mark} {version['id']}    {mode_label(version['mode'])}    "
                f"{fmt_range(version)}    {fmt_status(version)}"
            )
            lines.append(head)
            if version.get("version_label"):
                lines.append(f"      版本标签：{version['version_label']}")
            lines += _block("技能文本", version.get("text"))
            lines.append(f"      来源：{fmt_source(version.get('source'))}")
            lines.append(f"      校验状态：{verification_label(version.get('verification_status'))}")
            if version.get("change_note"):
                lines += _block("变更说明", version["change_note"])
            if version.get("product_id"):
                product = report.get("products", {}).get(version["product_id"]) or {}
                lines.append(
                    f"      所属产品：{version['product_id']} / {product.get('name', '（未找到）')}"
                )
            if version.get("package"):
                lines.append(f"      所属包：{version['package']}")
            if version.get("previous_version_id"):
                lines.append(f"      前一版本：{version['previous_version_id']}")
            if version.get("related_keyword_names") or version.get("related_timing_names"):
                lines.append(
                    "      关联关键词："
                    + ("、".join(version.get("related_keyword_names") or []) or "无")
                    + "    关联时点："
                    + ("、".join(version.get("related_timing_names") or []) or "无")
                )
            if version.get("notes"):
                lines.append(f"      备注：{version['notes']}")
            lines.append("")

    chains = [c for c in report["history_chains"] if len(c) > 1]
    if chains:
        lines.append(_section("版本演进（previous_version_id）"))
        for chain in chains:
            lines.append("  " + "  →  ".join(chain))
    return "\n".join(lines)


# ---------------------------------------------------------------- 武将


def fmt_hp(value: Any) -> str:
    """体力显示：3 而不是 3.0，国战半勾玉仍显示 1.5。"""
    if value is None:
        return "未填"
    number = float(value)
    return str(int(number)) if number == int(number) else str(number)


def render_general(report: dict) -> str:
    general = report["general"]
    lines = [
        "=" * 72,
        f"武将：{general['name']}    （id：{general['id']}；共 {len(report['variants'])} 个版本）",
    ]
    if general.get("aliases"):
        lines.append(f"  别名：{'、'.join(general['aliases'])}")
    if general.get("notes"):
        lines.append(f"  备注：{general['notes']}")
    lines.append(f"  源文件：{fmt_location(general)}")
    if not report["variants"]:
        lines.append("")
        lines.append("  ⚠ 这个武将还没有任何版本（GeneralVariant）")

    for variant in report["variants"]:
        lines.append("")
        lines.append(
            f"── 版本：{variant['variant']}（{variant['id']}）"
            f"    模式：{mode_label(variant.get('mode'))}"
            "  " + "-" * 20
        )
        lines.append(
            f"  称号：{variant.get('title') or '未填'}    "
            f"势力：{variant.get('faction') or '未填'}    "
            f"体力：{fmt_hp(variant.get('hp'))}    "
            f"性别：{gender_label(variant.get('gender'))}"
        )
        lines.append(
            f"  编号：{variant.get('general_number') or '未填'}    "
            f"武将牌系列：{variant.get('general_set') or '未填'}    "
            f"所属产品：{variant.get('product_id') or '（未关联）'}"
        )
        lines.append(f"  来源：{fmt_source(variant.get('source'))}")
        lines.append(
            f"  校验状态：{verification_label(variant.get('verification_status'))}"
        )
        lines.append(f"  源文件：{fmt_location(variant)}")
        if variant.get("notes"):
            lines.append(f"  备注：{variant['notes']}")

        lines.append("")
        lines.append(f"  技能（{len(variant['skills'])} 个；显式绑定的版本优先）")
        if not variant["skills"]:
            lines.append("    （该版本未关联任何技能）")
        for entry in variant["skills"]:
            skill = entry.get("skill") or {}
            lines.append(
                f"    ● {skill.get('name', entry['skill_id'])}    （{entry['skill_id']}）"
            )
            if entry.get("version_id"):
                lines.append(f"        武将上显式绑定的版本：{entry['version_id']}")
                if entry.get("pinned") is None:
                    lines.append("        ⚠ 绑定的版本不存在（可能被删掉了），下面显示推导出的版本")
            if not entry["versions"]:
                lines.append("        ⚠ 该技能没有任何 SkillVersion，拿不到技能文本")
                continue
            current = entry.get("current")
            if current is not None:
                how = "显式绑定" if entry.get("pinned") else "按模式+生效区间推导"
                lines.append(
                    f"        当前显示版本（{how}）：{current['id']}"
                    + (f"  [{current['version_label']}]" if current.get("version_label") else "")
                    + f"    {fmt_range(current)}"
                )
                lines += _block("技能文本", current.get("text"), indent="          ")
                lines.append(f"        来源：{fmt_source(current.get('source'))}")
                lines.append(
                    f"        校验状态：{verification_label(current.get('verification_status'))}"
                )
                if current.get("change_note"):
                    lines += _block("变更说明", current["change_note"], indent="          ")
            else:
                lines.append(
                    "        ⚠ 该模式当前没有唯一有效版本；候选："
                    + "、".join(entry["ambiguous_ids"] or ["无"])
                )
    return "\n".join(lines)


# ---------------------------------------------------------------- 卡牌


def render_card(report: dict) -> str:
    definition = report["definition"]
    lines = ["=" * 72, f"卡牌：{definition['name']}    （id：{definition['id']}）"]
    lines.append(
        f"  类型：{card_type_label(definition.get('card_type'))}"
        + (f" / {definition['subtype']}" if definition.get("subtype") else "")
    )
    if definition.get("aliases"):
        lines.append(f"  别名：{'、'.join(definition['aliases'])}")
    keywords = report.get("keywords") or []
    if keywords:
        lines.append(f"  关联关键词：{_names(keywords)}")
    if definition.get("notes"):
        lines.append(f"  备注：{definition['notes']}")
    lines.append(f"  源文件：{fmt_location(definition)}")

    printings = report["printings"]
    lines.append("")
    lines.append(_section(f"实体卡印刷（{len(printings)} 条，同名不同产品分别保留）"))
    if not printings:
        lines.append("  （尚未登记任何实体卡）")
    for printing in printings:
        product = printing.get("product") or {}
        lines.append(
            f"  ▪ {printing['id']}    {printing['name_printed']}"
            + (f"    ×{printing['quantity']}" if printing.get("quantity", 1) > 1 else "")
        )
        lines.append(
            f"      来源产品：{printing['product_id']}"
            + (f" / {product.get('name')}" if product else "")
            + f"    （{mode_label(product.get('mode') or printing.get('mode'))}）"
        )
        lines.append(
            f"      模式：{mode_label(printing.get('mode'))}    "
            f"牌堆/卡组：{printing.get('card_set') or '未填'}    "
            f"包：{printing.get('package') or '未填'}"
        )
        lines.append(
            f"      花色点数：{suit_label(printing.get('suit'))} {rank_label(printing.get('rank'))}    "
            f"右下角角标：{printing.get('bottom_right_mark') or '（无）'}    "
            f"卡牌编号：{printing.get('card_number') or '（无）'}"
        )
        lines += _block("卡面原文", printing.get("printed_text"), indent="      ")
        if printing.get("normalized_text"):
            lines += _block("检索用规范化文本", printing["normalized_text"], indent="      ")
        lines.append(f"      来源：{fmt_source(printing.get('source'))}")
        lines.append(f"      校验状态：{verification_label(printing.get('verification_status'))}")
        lines.append(f"      源文件：{fmt_location(printing)}")
        if printing.get("notes"):
            lines.append(f"      备注：{printing['notes']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- FAQ / 关键词 / 时点 / 产品


def render_faq(report: dict) -> str:
    faq = report["faq"]
    lines = ["=" * 72, f"FAQ：{faq['id']}    （模式：{mode_label(faq.get('mode'))}）"]
    lines += _block("问题", faq.get("question"), indent="  ")
    lines += _block("回答", faq.get("answer"), indent="  ")
    related = []
    if report.get("skills"):
        related.append("技能 " + _names(report["skills"]))
    if report.get("generals"):
        related.append("武将 " + _names(report["generals"]))
    if report.get("cards"):
        related.append("卡牌 " + _names(report["cards"]))
    if report.get("keywords"):
        related.append("关键词 " + _names(report["keywords"]))
    if report.get("timings"):
        related.append("时点 " + _names(report["timings"]))
    if related:
        lines.append("  关联：" + "；".join(related))
    lines.append(f"  来源：{fmt_source(faq.get('source'))}")
    lines.append(f"  校验状态：{verification_label(faq.get('verification_status'))}")
    lines.append(f"  源文件：{fmt_location(faq)}")
    if faq.get("notes"):
        lines += _block("备注", faq["notes"], indent="  ")
    return "\n".join(lines)


def render_keyword(report: dict) -> str:
    keyword = report["keyword"]
    lines = ["=" * 72, f"关键词：{keyword['name']}    （id：{keyword['id']}）"]
    lines.append(f"  模式：{mode_label(keyword.get('mode'))}")
    if keyword.get("aliases"):
        lines.append(f"  别名：{'、'.join(keyword['aliases'])}")
    lines += _block("规则定义", keyword.get("definition"), indent="  ")
    if report.get("related_keywords"):
        lines.append(f"  相关关键词：{_names(report['related_keywords'])}")
    if report.get("timings"):
        lines.append(f"  相关时点：{_names(report['timings'])}")
    if report.get("faqs"):
        lines.append(f"  相关 FAQ：{'、'.join(f['id'] for f in report['faqs'])}")
    if report.get("skills"):
        lines.append(
            "  关联技能版本：" + "、".join(v["id"] for v in report["skills"])
        )
    lines.append(f"  来源：{fmt_source(keyword.get('source'))}")
    lines.append(f"  校验状态：{verification_label(keyword.get('verification_status'))}")
    lines.append(f"  源文件：{fmt_location(keyword)}")
    if keyword.get("notes"):
        lines += _block("备注", keyword["notes"], indent="  ")
    return "\n".join(lines)


def render_timing(report: dict) -> str:
    timing = report["timing"]
    lines = ["=" * 72, f"时点：{timing['name']}    （id：{timing['id']}）"]
    lines.append(f"  模式：{mode_label(timing.get('mode'))}")
    if timing.get("description"):
        lines += _block("说明", timing["description"], indent="  ")
    parent = report.get("parent")
    lines.append(f"  上级时点：{parent['name'] if parent else '（无）'}")
    if report.get("previous"):
        lines.append("  前序：" + _names(report["previous"]))
    if report.get("next"):
        lines.append("  后续：" + _names(report["next"]))
    if report.get("children"):
        lines.append("  下级：" + _names(report["children"]))
    if report.get("keywords"):
        lines.append(f"  相关关键词：{_names(report['keywords'])}")
    if report.get("skills"):
        lines.append(f"  相关技能：{_names(report['skills'])}")
    if report.get("faqs"):
        lines.append(f"  相关 FAQ：{'、'.join(f['id'] for f in report['faqs'])}")
    lines.append(f"  来源：{fmt_source(timing.get('source'))}")
    lines.append(f"  校验状态：{verification_label(timing.get('verification_status'))}")
    lines.append(f"  源文件：{fmt_location(timing)}")
    if timing.get("notes"):
        lines += _block("备注", timing["notes"], indent="  ")
    return "\n".join(lines)


def render_product(report: dict) -> str:
    product = report["product"]
    lines = ["=" * 72, f"实体产品：{product['name']}    （id：{product['id']}）"]
    lines.append(f"  系列：{product.get('series') or '未填'}    模式：{mode_label(product.get('mode'))}")
    lines.append(
        f"  发行：{product.get('publisher') or '未填'}    "
        f"{product.get('release_date') or product.get('release_year') or '未填'}    "
        f"版本：{product.get('edition') or '未填'}"
    )
    if product.get("general_sets"):
        lines.append(f"  武将牌系列：{'、'.join(product['general_sets'])}")
    if product.get("card_sets"):
        lines.append(f"  游戏牌牌堆：{'、'.join(product['card_sets'])}")
    lines.append(f"  来源：{fmt_source(product.get('source'))}")
    lines.append(f"  校验状态：{verification_label(product.get('verification_status'))}")
    lines.append(f"  源文件：{fmt_location(product)}")
    if product.get("notes"):
        lines += _block("备注", product["notes"], indent="  ")
    lines.append("")
    variant_names = [f"{v.get('general_name')}（{v['variant']}）" for v in report["variants"]]
    lines.append(f"  该产品收录武将版本 {len(variant_names)} 条："
                 + ("、".join(variant_names) or "（无）"))
    lines.append(f"  该产品实体卡 {len(report['printings'])} 张："
                 + ("、".join(p["id"] for p in report["printings"]) or "（无）"))
    lines.append(f"  该产品技能版本 {len(report['skill_versions'])} 条："
                 + ("、".join(v["id"] for v in report["skill_versions"]) or "（无）"))
    marks: dict[str, int] = {}
    for printing in report["printings"]:
        mark = printing.get("bottom_right_mark") or "（无角标）"
        marks[mark] = marks.get(mark, 0) + 1
    if marks:
        lines.append(
            "  右下角角标分布：" + "、".join(f"{k} × {v}" for k, v in sorted(marks.items()))
        )
    return "\n".join(lines)
