#!/usr/bin/env python3
"""为已有游戏牌补使用方法或武器攻击范围，原文、ID 与指示物保持原样。

默认只输出迁移计划；--write 才写盘。只拆明确的“出牌阶段，对…使用。”句式，
其余原文保留在使用方法.effect 中；未出现的规则留空，不从外部版本推导。
已有 rules 的记录跳过，重复执行安全。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgs_helper.admin.yaml_writer import entity_to_yaml_dict, read_doc, render_doc, _atomic_write
from sgs_helper.schema import Card, CardUseMethod, GameCardRules


def migrate_card(node: dict) -> dict:
    if node.get("category_1") != "游戏牌" or node.get("rules") is not None:
        return node
    card = Card.model_validate(node)
    text = card.effect
    if card.category_2 in {"基本牌", "锦囊牌"}:
        method = CardUseMethod(effect=text)
        match = re.fullmatch(r"(出牌阶段(?:限一次)?)[，。]对(.+?)使用[。；](.+)", text, flags=re.S)
        if match:
            method.timing, method.target, method.effect = match.groups()
        card.rules = GameCardRules(use_methods=[method])
    elif card.category_2 == "装备牌":
        if card.category_3 == "武器":
            if card.attack_range:
                return node
            match = re.search(r"(?:^|\n)攻击范围[：: ]+([^\s◆]+)", text)
            if match:
                card.attack_range = match.group(1)
            else:
                return node
        else:
            return node
    else:
        return node
    migrated = entity_to_yaml_dict("card", card)
    # 不覆盖原文；结构化拆分只新增明确存在的内容。
    if "effect" in node:
        migrated["effect"] = node["effect"]
    else:
        migrated.pop("effect", None)
    return migrated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    plans = []
    for path in sorted((args.data_dir / "cards").glob("*.yaml")):
        doc = read_doc(path)
        nodes = [migrate_card(node) for node in doc.nodes]
        if nodes != doc.nodes:
            plans.append((path, doc, nodes))
    # 所有记录先完成解析与校验，再写盘，避免遇到坏文件时留下半轮迁移。
    for path, doc, nodes in plans:
        doc.nodes = nodes
        if args.write:
            _atomic_write(path, render_doc(doc))
    print(f"{'已迁移' if args.write else '待迁移'} {len(plans)} 个游戏牌文件；指示物及已有规则记录跳过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
