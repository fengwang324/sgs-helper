"""卡牌规则展示的共用字段顺序（管理页 / Wiki / CLI 共用）。"""

from __future__ import annotations

from .schema import Card


def rule_sections(card: Card) -> list[dict]:
    if card.rules is None:
        return []
    rules = card.rules
    sections = []
    attributes = [
        {"label": label, "text": value}
        for label, value in (("攻击范围", rules.attack_range), ("距离修正", rules.distance_modifier))
        if value
    ]
    if attributes:
        sections.append({"title": "装备属性", "fields": attributes})
    for index, method in enumerate(rules.use_methods, 1):
        fields = [
            {"label": label, "text": getattr(method, name)}
            for name, label in (("timing", "出牌时机"), ("target", "使用目标"),
                                ("effect", "作用效果"), ("note", "note · 补充说明"))
            if getattr(method, name)
        ]
        if fields:
            sections.append({"title": method.label or f"使用方法 {index}", "fields": fields})
    if rules.note:
        sections.append({"title": "补充说明", "fields": [{"label": "note", "text": rules.note}]})
    return sections
