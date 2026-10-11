"""游戏牌规则展示的共用字段顺序（管理页 / Wiki / CLI 共用）。"""

from __future__ import annotations

from .schema import Card


def rule_sections(card: Card) -> list[dict]:
    """把牌堆资料、武器范围、使用方法和备注转换成展示区块。"""
    sections: list[dict] = []
    if card.recast:
        sections.append({"title": "重铸", "fields": [{"label": "可重铸", "text": "是"}]})
    attributes = [
        {"label": label, "text": value}
        for label, value in (
            ("攻击范围", card.attack_range),
            ("牌堆", card.deck_info),
        )
        if value
    ]
    if attributes:
        sections.append({"title": "卡牌资料", "fields": attributes})

    if card.rules is not None:
        for index, method in enumerate(card.rules.use_methods, 1):
            fields = [
                {"label": label, "text": getattr(method, name)}
                for name, label in (
                    ("timing", "出牌时机"),
                    ("target", "使用目标"),
                    ("effect", "作用效果"),
                )
                if getattr(method, name)
            ]
            if fields:
                sections.append({"title": method.label or f"使用方法 {index}", "fields": fields})

    if card.note:
        sections.append({"title": "补充说明", "fields": [{"label": "备注", "text": card.note}]})
    return sections
