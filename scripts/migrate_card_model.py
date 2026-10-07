#!/usr/bin/env python3
"""把旧的两层卡牌数据（Card + CardPrinting）迁移成单层 Card。

> **一次性历史迁移脚本**（2026-10-06 已在 main 上执行完毕）。保留它是为了让这次
> 字段映射可复现：`data/card_printings/` 已经不存在时脚本会直接退出，不会重复执行。

规则：
- 一张有独立规则意义的卡 = 一条 Card（同一 card_id 的多份花色/点数印刷合并成一条）；
- 印刷文本不一致时取“多数 → 最长 → 字典序”的那一版，具体有几种文本由脚本打印出来；
- 指示物卡牌（card_type=other / subtype=指示物）本次**全部舍弃**，连同印刷移入备份目录；
- 旧的花色/点数/数量/角标/编号等牌堆字段不写进新模型，但旧文件整体备份，供以后
  新增 deck_entries 使用。

用法：
    python scripts/migrate_card_model.py            # dry-run，只报告
    python scripts/migrate_card_model.py --apply    # 先备份再落盘
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import sys
import tarfile
from collections import Counter, defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
BACKUP_ROOT = REPO.parent / "_ai_work"
BACKUP_DIR = BACKUP_ROOT / "removed_card_model"
BACKUP_TGZ = BACKUP_ROOT / "data_backup_before_card_model.tgz"

#: 旧 card_set 里需要规范化的写法
CARD_SET_FIX = {"君临天下-权包": "君临天下·权"}

#: 装备牌的三级分类直接沿用旧 subtype
EQUIPMENT_SUBTYPES = {"武器", "防具", "坐骑", "宝物"}

SUIT_ORDER = {"spade": 0, "heart": 1, "club": 2, "diamond": 3, "none": 4}


# ------------------------------------------------------------------ dumper
# 与 sgs_helper/admin/yaml_writer.py 的 dump_yaml 保持一致，diff 才干净。

class BlockStr(str):
    pass


class _ReadableDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):  # noqa: D102
        return super().increase_indent(flow, False)


def _represent_str(dumper, data):
    forced = isinstance(data, BlockStr)
    text = str(data)
    if forced or "\n" in text:
        if forced and "\n" not in text and len(text) <= 30:
            return dumper.represent_scalar("tag:yaml.org,2002:str", text)
        return dumper.represent_scalar("tag:yaml.org,2002:str", text, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", text)


def _represent_date(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data.isoformat())


_ReadableDumper.add_representer(str, _represent_str)
_ReadableDumper.add_representer(BlockStr, _represent_str)
_ReadableDumper.add_representer(dt.date, _represent_date)
_ReadableDumper.add_representer(dt.datetime, _represent_date)


def dump_yaml(payload) -> str:
    text = yaml.dump(
        payload, Dumper=_ReadableDumper, allow_unicode=True, sort_keys=False,
        default_flow_style=False, indent=2, width=10 ** 6,
    )
    return text if text.endswith("\n") else text + "\n"


# ------------------------------------------------------------------ 装载


def load_all():
    cards = {}
    for path in sorted((DATA / "cards").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        cards[doc["id"]] = doc
    printings = defaultdict(list)
    for path in sorted((DATA / "card_printings").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        printings[doc["card_id"]].append(doc)
    skills = {}
    for path in sorted((DATA / "skills").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if len(doc["name"]) >= 2:
            skills.setdefault(doc["name"], doc["id"])
    versions = defaultdict(list)
    for path in sorted((DATA / "skill_versions").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        versions[doc["skill_id"]].append(doc["id"])
    return cards, printings, skills, versions


def category_of(card: dict) -> tuple[str, str, str] | None:
    card_type = card.get("card_type")
    subtype = (card.get("subtype") or "").strip()
    if card_type == "basic":
        return ("游戏牌", "基本牌", card["name"])
    if card_type == "trick":
        return ("游戏牌", "锦囊牌", "非延时锦囊")
    if card_type == "delayed_trick":
        return ("游戏牌", "锦囊牌", "延时锦囊")
    if card_type == "equipment":
        if subtype not in EQUIPMENT_SUBTYPES:
            raise ValueError(f"{card['id']}: 装备牌 subtype 不认识：{subtype!r}")
        return ("游戏牌", "装备牌", subtype)
    if card_type == "other" and subtype == "指示物":
        return None
    raise ValueError(f"{card['id']}: 不认识的 card_type/subtype：{card_type!r}/{subtype!r}")


def pick_text(printings: list[dict]) -> tuple[str, Counter]:
    counter = Counter(p.get("printed_text") or "" for p in printings)
    chosen = sorted(counter.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[0][0]
    return chosen, counter


def pick_printing(printings: list[dict], text: str) -> dict:
    same = [p for p in printings if (p.get("printed_text") or "") == text] or printings

    def key(p):
        return (SUIT_ORDER.get(str(p.get("suit")), 9), p.get("rank") or 0, p["id"])

    return sorted(same, key=key)[0]


def skill_refs(text: str, skills: dict[str, str], versions: dict[str, list[str]]):
    refs = []
    for name in re.findall(r"视为拥有[\"“]([^\"”]+)[\"”]", text):
        skill_id = skills.get(name)
        if skill_id and skill_id not in [r["skill_id"] for r in refs]:
            candidates = versions.get(skill_id) or []
            refs.append({"skill_id": skill_id, "version_id": candidates[0] if len(candidates) == 1 else None})
    return refs


#: 用户指明要"先记录进去、卡图后续再补"的基本牌：
#: 基本牌的最后一级是卡名（name），所以 category_3 是牌族（杀 / 闪）。
#: effect.text / image 先留空，不是编造规则文本。
NEW_BASIC_CARDS: tuple[dict, ...] = (
    {"id": "card_huo_sha", "name": "火杀", "category_3": "杀"},
    {"id": "card_lei_sha", "name": "雷杀", "category_3": "杀"},
    {"id": "card_lve_shan", "name": "掠闪", "category_3": "闪"},
)


def extra_basic_cards() -> list[dict]:
    """构造待补的基本牌记录（卡图与卡面文字留空）。"""
    out = []
    for item in NEW_BASIC_CARDS:
        out.append({
            "id": item["id"],
            "category_1": "游戏牌",
            "category_2": "基本牌",
            "category_3": item["category_3"],
            "name": item["name"],
            "image": "",
            "card_set": None,
            "effect": {"text": "", "skills": []},
        })
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    if not (DATA / "card_printings").is_dir():
        print("data/card_printings/ 不存在 —— 这次迁移已经执行过了，无需重复运行。")
        return 1
    cards, printings, skills, versions = load_all()

    new_cards: list[dict] = []
    kept_ids: list[str] = []
    dropped: list[dict] = []
    set_stats = Counter()
    multi_text: list[str] = []

    for cid, card in cards.items():
        category = category_of(card)
        ps = printings.get(cid) or []
        if category is None:
            dropped.append({"card": card, "printings": ps})
            continue
        text, counter = pick_text(ps) if ps else ("", Counter())
        rep = pick_printing(ps, text) if ps else {}
        sets = {CARD_SET_FIX.get(str(p.get("card_set")), p.get("card_set")) for p in ps}
        card_set = sets.pop() if len(sets) == 1 else None

        if len(counter) > 1:
            multi_text.append(f"{cid}（{len(counter)} 种文本）")

        new_cards.append({
            "id": cid,
            "category_1": category[0],
            "category_2": category[1],
            "category_3": category[2],
            "name": card["name"],
            "image": rep.get("image") or "",
            "card_set": card_set,
            "effect": {"text": text, "skills": skill_refs(text, skills, versions)},
        })
        kept_ids.append(cid)
        set_stats[card_set] += 1

    print(f"旧 Card {len(cards)} 条 / 旧 CardPrinting {sum(len(v) for v in printings.values())} 条")
    print(f"→ 新 Card {len(new_cards)} 条；舍弃指示物 {len(dropped)} 个 card_id"
          f"（{sum(len(d['printings']) for d in dropped)} 张印刷）")
    print("\n新 card_set 分布：")
    for k, v in set_stats.most_common():
        print(f"   {str(k):14s} {v}")

    print("\n=== 迁移后的 78 条卡牌 ===")
    for c in new_cards:
        skills_txt = "、".join(r["skill_id"] for r in c["effect"]["skills"]) or "-"
        print(f"   {c['category_1']}/{c['category_2']}/{c['category_3']:6s} {c['name']:8s}"
              f" set={str(c['card_set']):10s} 技能={skills_txt:24s} 文本={len(c['effect']['text'])} 字")

    print("\n=== 舍弃的指示物卡 ===")
    for d in dropped:
        print(f"   {d['card']['id']:32s} {d['card']['name']:8s} 印刷 {len(d['printings'])} 张")

    print("\n多文本卡（自动取多数版）：", "，".join(multi_text) or "无")

    extras = extra_basic_cards()
    print("\n=== 额外补录的基本牌（用户指明先记录、卡图后续补）===")
    for c in extras:
        print(f"   {c['category_1']}/{c['category_2']}/{c['category_3']} → {c['name']}（image 空）")

    if not apply:
        print("\n（dry-run，未落盘。加 --apply 执行）")
        return 0

    if not BACKUP_TGZ.exists():
        with tarfile.open(BACKUP_TGZ, "w:gz") as tar:
            tar.add(DATA, arcname="data")
        print("已备份原 data/ ->", BACKUP_TGZ)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # 旧数据整体移入备份目录
    for name in ("card_printings",):
        src = DATA / name
        if src.is_dir():
            dest = BACKUP_DIR / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))
            print("已移出", src, "->", dest)
    (BACKUP_DIR / "indicator_cards").mkdir(parents=True, exist_ok=True)
    for item in dropped:
        path = DATA / "cards" / f"{item['card']['id']}.yaml"
        if path.exists():
            shutil.move(str(path), str(BACKUP_DIR / "indicator_cards" / path.name))
    print(f"已移出 {len(dropped)} 个指示物卡定义 -> {BACKUP_DIR / 'indicator_cards'}")
    (BACKUP_DIR / "old_cards_originals").mkdir(parents=True, exist_ok=True)
    for cid in kept_ids:
        path = DATA / "cards" / f"{cid}.yaml"
        if path.exists():
            shutil.copy2(path, BACKUP_DIR / "old_cards_originals" / path.name)

    for card in new_cards + extras:
        path = DATA / "cards" / f"{card['id']}.yaml"
        path.write_text(dump_yaml(card), encoding="utf-8")
    print(f"已写入 {len(new_cards)} 个迁移后的 Card + {len(extras)} 个补录 Card")

    # 产品：去掉「指示物」这个假拓展包，补上君临天下·权
    product_path = DATA / "products" / "qlhd.yaml"
    product = yaml.safe_load(product_path.read_text(encoding="utf-8"))
    sets = [s for s in product.get("card_sets") or [] if s != "指示物"]
    for extra in ["君临天下·权"]:
        if extra not in sets:
            sets.append(extra)
    product["card_sets"] = sets
    product_path.write_text(dump_yaml(product), encoding="utf-8")
    print("产品 card_sets ->", sets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
