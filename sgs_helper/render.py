"""把 repo 层返回的 dict 渲染成人类可读文本。

CLI 使用这一层；未来接微信小程序 / FastAPI 时直接返回 repo 的 dict 即可，
不需要这一层（可用 ``--json`` 查看同样的结构）。
"""

from __future__ import annotations

from typing import Any, Iterable

from .constants import (
    GENDER_LABELS,
    KIND_BY_NAME,
    MODE_LABELS,
    SOURCE_LABELS,
    SOURCE_TITLE_BY_REFERENCE,
)

#: 自动识别出来的相关实体：报告键 -> kind（顺序即展示顺序）
_LINK_GROUPS: tuple[tuple[str, str], ...] = (
    ("skills", "skill"),
    ("generals", "general"),
    ("cards", "card"),
    ("keywords", "keyword"),
)


def mode_label(mode: str | None) -> str:
    return MODE_LABELS.get(mode or "", mode or "未填")


def source_label(value: str | None) -> str:
    return SOURCE_LABELS.get(value or "", value or "未填")


def gender_label(value: str | None) -> str:
    return GENDER_LABELS.get(value or "", value or "")


def fmt_source(source: dict | None) -> str:
    """来源只有 type + reference：展示名按 reference 推导，认不出就退化成 type 标签。"""
    if not source:
        return "（未记录来源）"
    reference = str(source.get("reference") or "").strip()
    label = SOURCE_TITLE_BY_REFERENCE.get(reference) or source_label(source.get("type"))
    if reference and reference != label:
        return f"{label} / {reference}"
    return label


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


def _link_text(report: dict) -> str:
    """把自动识别出的相关实体渲染成「标签 名字1、名字2；…」。"""
    parts = []
    for key, kind in _LINK_GROUPS:
        entities = report.get(key) or []
        if not entities:
            continue
        label = KIND_BY_NAME[kind].label if kind in KIND_BY_NAME else kind
        parts.append(f"{label} {_names(entities)}")
    return "；".join(parts)


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
    lines.append("提示：用 query.py skill/general/card/faq/keyword/product 查看详情。")
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
    lines.append(f"  源文件：{fmt_location(definition)}")

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
    lines.append("")

    for group in report["modes"]:
        lines.append(_section(group["label"]))

        for version_id in group["version_ids"]:
            version = by_id[version_id]
            head = f"  · {version['id']}    {mode_label(version['mode'])}"
            if version.get("version_label"):
                head += f"    [{version['version_label']}]"
            lines.append(head)
            lines += _block("技能文本", version.get("text"))
            lines.append(f"      来源：{fmt_source(version.get('source'))}")
            if version.get("product_id"):
                product = report.get("products", {}).get(version["product_id"]) or {}
                lines.append(
                    f"      所属产品：{version['product_id']} / {product.get('name', '（未找到）')}"
                )
            if version.get("previous_version_id"):
                lines.append(f"      前一版本：{version['previous_version_id']}")
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
        lines.append(f"  源文件：{fmt_location(variant)}")

        lines.append("")
        lines.append(f"  技能（{len(variant['skills'])} 个；显式绑定的版本优先）")
        if not variant["skills"]:
            lines.append("    （该版本未关联任何技能）")
        for entry in variant["skills"]:
            skill = entry.get("skill") or {}
            lines.append(
                f"    ● {skill.get('name', entry['skill_id'])}    （{entry['skill_id']}）"
            )
            version = entry.get("version")
            if entry.get("version_id"):
                lines.append(f"        显式绑定的版本：{entry['version_id']}")
                if version is None:
                    lines.append(
                        "        ⚠ 绑定的版本不存在（可能被删掉了），这个技能没有文本"
                    )
                    continue
                how = "显式绑定"
            elif version is not None:
                how = "按模式匹配"
            else:
                lines.append(
                    "        （没有显式绑定，且按模式匹配不到唯一版本：无技能文本）"
                )
                continue
            lines.append(
                f"        当前显示版本（{how}）：{version['id']}"
                + (f"  [{version['version_label']}]" if version.get("version_label") else "")
            )
            lines += _block("技能文本", version.get("text"), indent="          ")
            lines.append(f"        来源：{fmt_source(version.get('source'))}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 卡牌


def render_card(report: dict) -> str:
    card = report["card"]
    effect = report.get("effect") or {}

    lines = ["=" * 72, f"卡牌：{card['name']}    （id：{card['id']}）"]
    lines.append(
        f"  分类：{card.get('category_1') or '未填'}"
        f" / {card.get('category_2') or '未填'}"
        f" / {card.get('category_3') or '未填'}"
    )
    lines.append(f"  拓展包：{card.get('card_set') or '未填'}")
    lines.append(f"  卡图：{card.get('image') or '未填'}")
    lines.append(f"  源文件：{fmt_location(card)}")

    effect_text = effect.get("text") or ""
    effect_text = effect_text if effect_text.strip() else "（卡面无独立效果文本）"
    lines += _block("效果文本", effect_text, indent="  ")

    skills = effect.get("skills") or []
    lines.append(f"  关联技能（{len(skills)} 个）：")
    if not skills:
        lines.append("    （无）")
    for entry in skills:
        skill = entry.get("skill") or {}
        skill_name = str(skill.get("name") or "").strip()
        lines.append(
            f"    ● {skill_name}（{entry['skill_id']}）" if skill_name
            else f"    ● {entry['skill_id']}    ⚠ 这个技能不存在"
        )
        version = entry.get("version")
        if version is None:
            if entry.get("pinned"):
                lines.append(
                    f"        绑定版本：{entry['version_id']}"
                    "    ⚠ 绑定的版本不存在（可能被删掉了），这个技能没有文本"
                )
            else:
                lines.append(
                    "        绑定版本：（无）"
                    "    ⚠ 没有显式绑定，且按模式匹配不到唯一版本：无技能文本"
                )
            continue
        how = "显式绑定" if entry.get("pinned") else "按模式匹配"
        lines.append(
            f"        绑定版本（{how}）：{version['id']}"
            + (f"  [{version['version_label']}]" if version.get("version_label") else "")
        )
        lines += _block("技能文本", version.get("text"), indent="        ")

    related = _link_text(report)
    if related:
        lines.append("  相关：" + related)
    return "\n".join(lines)


# ---------------------------------------------------------------- FAQ / 关键词 / 产品


def render_faq(report: dict) -> str:
    faq = report["faq"]
    lines = ["=" * 72, f"FAQ：{faq['id']}    （变体：{faq.get('variant') or '未填'}）"]
    lines += _block("问题", faq.get("question"), indent="  ")
    lines += _block("回答", faq.get("answer"), indent="  ")
    related = _link_text(report)
    if related:
        lines.append("  关联：" + related)
    # 来源是一段多行文本（逐条列出来源属性），原样缩进输出，不折行
    source_lines = str(faq.get("source") or "").splitlines()
    lines.append("  来源：")
    lines += [f"    {row}" for row in source_lines] or ["    （空）"]
    lines.append(f"  源文件：{fmt_location(faq)}")
    return "\n".join(lines)


def render_keyword(report: dict) -> str:
    keyword = report["keyword"]
    lines = ["=" * 72, f"关键词：{keyword['name']}    （id：{keyword['id']}）"]
    lines.append(f"  变体：{keyword.get('variant') or '未填'}")
    lines += _block("规则定义", keyword.get("definition"), indent="  ")
    related = _link_text(report)
    if related:
        lines.append("  相关：" + related)
    lines.append(f"  源文件：{fmt_location(keyword)}")
    return "\n".join(lines)


def render_product(report: dict) -> str:
    product = report["product"]
    lines = ["=" * 72, f"实体产品：{product['name']}    （id：{product['id']}）"]
    lines.append(
        f"  模式：{mode_label(product.get('mode'))}    "
        f"发行年份：{product.get('release_year') or '未填'}"
    )
    if product.get("general_sets"):
        lines.append(f"  武将牌系列：{'、'.join(product['general_sets'])}")
    if product.get("card_sets"):
        lines.append(f"  游戏牌牌堆：{'、'.join(product['card_sets'])}")
    lines.append(f"  源文件：{fmt_location(product)}")
    lines.append("")
    variant_names = [f"{v.get('general_name')}（{v['variant']}）" for v in report["variants"]]
    lines.append(f"  该产品收录武将版本 {len(variant_names)} 条："
                 + ("、".join(variant_names) or "（无）"))
    cards = report.get("cards") or []
    # 卡与产品没有外键，只能按“产品声明的拓展包名”关联；同名拓展包被多个产品共用会重复计数
    lines.append(
        f"  该产品游戏牌（{len(cards)} 条；按 card_set 关联，同名拓展包共用时会重复计数）"
    )
    if not cards:
        lines.append("    （无）")
    for card in cards:
        category = "/".join(
            str(card.get(key) or "未填")
            for key in ("category_1", "category_2", "category_3")
        )
        lines.append(
            f"    ▪ {card.get('card_set') or '未填'} / {category}    {card['name']}"
        )
    lines.append(f"  该产品技能版本 {len(report['skill_versions'])} 条："
                 + ("、".join(v["id"] for v in report["skill_versions"]) or "（无）"))
    return "\n".join(lines)
