#!/usr/bin/env python3
"""从 gltjk.com 的《三国杀官方规则集 3.0》网页版导入规则数据（一次性/可重跑）。

数据来源：https://gltjk.com/sanguosha/rules/
内容为凌天翼规则集 3.0，网页版由 gltjk.com 制作（社区整理，非游卡官方发布物），
导入后统一写成 ``source.type=official_rule``（社区整理，非官方发布物）。

导入什么：

- ``--stage skills``  武将页（card/hero/*）的「技能名——技能描述」→ SkillDefinition + SkillVersion
                      （每个技能按模式取一个印次的文本；其它印次暂不单独录入版本）
- ``--stage all``     目前等同 skills

FAQ / 规则关键词 / 时点三类数据已不再来自本脚本：
FAQ 与关键词改由《群狼环鼎规则资料包》的人工核对稿录入（见 docs/updates.md），
「时点」这个概念整体舍弃，因此这里删掉了对应的三个 stage。

用法（在容器里、仓库根目录执行）::

    pip install pypinyin              # 可选：装了就会生成拼音 id（skill_ji_zhi）
    bash scripts/import_rules_book.sh # 或手动 curl 下面这些页面到 --pages-dir
    python scripts/import_rules_book.py --pages-dir /tmp/rules --stage all

需要的页面（文件名 = URL 路径把 "/" 换成 "_" 再加 ".html"）::

    card/hero{,/wei,/shu,/wu,/neutral,/legend,/west,/nongame}
    glossary/{card,cardinfo,zone,value,description,flow,event,gamecard,herocard,guo}
    flow/{game,use,play,rankcompare,move,statechange,judge,damage,loselife,decreaselife,
          recoverlife,decreaselifemax,increaselifemax,neardeath,death,condition,virtual}
    rule/{principle,element,forced,classification}
    appendix/mizhimie

注意：这个脚本是**导入工具**，不属于运行时依赖；它只写 ``data/`` 下的 YAML，
写完请照常跑 ``validate_data.py`` / ``build_db.py`` / ``pytest``。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sgs_helper.admin import yaml_writer  # noqa: E402
from sgs_helper.db import build_database  # noqa: E402
from sgs_helper.schema import MODEL_BY_KIND  # noqa: E402
from sgs_helper.validate import validate_entities  # noqa: E402
from sgs_helper.loader import RawEntity, Origin, load_data_dir  # noqa: E402

RULES_DIR = Path("/tmp/rules")  # 可被 --pages-dir 覆盖
DATA_DIR = ROOT / "data"  # 可被 --data-dir 覆盖
SITE_URL = "https://gltjk.com/sanguosha/rules/"
BOOK_TITLE = "三国杀官方规则集 3.0"

SOURCE = {
    "type": "official_rule",
    "reference": SITE_URL,
}

HERO_PAGES = [
    "card_hero_wei", "card_hero_shu", "card_hero_wu", "card_hero_neutral",
    "card_hero_legend", "card_hero_west", "card_hero_nongame",
]

RULE_PAGES = {
    "glossary_card": ("牌", "术语"),
    "glossary_cardinfo": ("牌面信息", "术语"),
    "glossary_zone": ("区域", "术语"),
    "glossary_value": ("数值", "术语"),
    "glossary_description": ("技能描述", "术语"),
    "glossary_flow": ("游戏流程", "术语"),
    "glossary_event": ("事件、响应与结算", "术语"),
    "glossary_gamecard": ("游戏牌的操作", "术语"),
    "glossary_herocard": ("武将牌的操作", "术语"),
    "glossary_guo": ("国战", "术语"),
    "flow_game": ("游戏的流程", "流程"),
    "flow_use": ("使用事件的结算流程", "流程"),
    "flow_play": ("打出事件的结算流程", "流程"),
    "flow_rankcompare": ("拼点事件的结算流程", "流程"),
    "flow_move": ("牌移动的结算流程", "流程"),
    "flow_statechange": ("牌状态改变能发动的技能", "流程"),
    "flow_judge": ("判定事件的结算流程", "流程"),
    "flow_damage": ("伤害事件的结算流程", "流程"),
    "flow_loselife": ("失去体力事件的结算流程", "流程"),
    "flow_decreaselife": ("扣减体力事件的结算流程", "流程"),
    "flow_recoverlife": ("回复体力事件的结算流程", "流程"),
    "flow_decreaselifemax": ("减体力上限事件的结算流程", "流程"),
    "flow_increaselifemax": ("加体力上限事件的结算流程", "流程"),
    "flow_neardeath": ("濒死事件的结算流程", "流程"),
    "flow_death": ("死亡事件的结算流程", "流程"),
    "flow_condition": ("一张牌能被使用的条件", "专题"),
    "flow_virtual": ("虚拟牌及与之对应的实体牌", "专题"),
    "rule_principle": ("结算的原则", "规则"),
    "rule_element": ("技能的要素", "规则"),
    "rule_forced": ("锁定技与发动技能/执行效果顺序的优先级的关系", "专题"),
    "rule_classification": ("技能、技能/牌的效果的分类对各种情况下技能/牌的效果是否继续执行的重要性", "专题"),
}

#: 身份局印次的优先顺序（取一个作为该技能的身份局文本）
IDENTITY_PRIORITY = [
    "标", "界限突破", "风", "火", "林", "山", "旧标", "旧风", "将", "旧将",
    "SP", "台湾", "铜雀台", "2013三国杀王者之战纪念卡牌", "狄仁杰之通天帝国", "国-标",
]
#: 国战印次的优先顺序
GUOZHAN_PRIORITY = ["国-标", "国-阵", "国-势"]

VARIANT_LABEL = {
    "?": "未标注印次",
    "标": "标准版", "旧标": "标准版（旧）", "界限突破": "界限突破",
    "风": "神话再临·风", "火": "神话再临·火", "林": "神话再临·林", "山": "神话再临·山",
    "旧风": "神话再临·风（旧）", "将": "一将成名", "旧将": "一将成名（旧）",
    "国-标": "国战标准版", "国-阵": "国战·阵", "国-势": "国战·势",
    "台湾": "台湾版", "铜雀台": "铜雀台", "SP": "SP",
}

HEAD = re.compile(r"^【(.+?)】(.*)$")
SKILL = re.compile(r"^([\u4e00-\u9fff·・]{1,12})——(.+)$")
VARIANT = re.compile(r"体力上限[\d.]+（([^）]+)）")

#: 技能行之前可能出现这些噪声行
NOISE = re.compile(r"^(上一页|下一页|三国杀官方规则集|Generated using GitBook)")


# ---------------------------------------------------------------- HTML → 文本


def page_inner(name: str) -> str:
    html = (RULES_DIR / f"{name}.html").read_text(encoding="utf-8")
    match = re.search(r'<div class="page-inner">(.*?)</div>\s*</div>', html, re.S)
    return match.group(1) if match else html


def block_text(html: str) -> list[str]:
    """把一段 HTML 变成“一行一个块”的纯文本（<p>/<h*>/<li>/<blockquote> 各成一行）。"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|h[1-6]|li|blockquote|tr|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if line and not NOISE.match(line):
            lines.append(line)
    return lines


def page_blocks(name: str) -> list[str]:
    return block_text(page_inner(name))


# ---------------------------------------------------------------- 内部 id


try:  # 有 pypinyin 就生成可读 id；没有就退回稳定短哈希
    from pypinyin import lazy_pinyin  # type: ignore
except Exception:  # pragma: no cover
    lazy_pinyin = None


def slug_of(name: str) -> str:
    if lazy_pinyin is not None:
        parts = [p for p in lazy_pinyin(name) if p.isalnum()]
        text = "_".join(parts)
        text = re.sub(r"[^a-z0-9_]", "", text.lower())
        if text:
            return text[:48]
    ascii_part = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if len(ascii_part) >= 2 and re.search(r"[a-z]", ascii_part):
        return ascii_part[:48]
    import hashlib

    return "n" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


class IdAllocator:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.used: set[str] = set()

    def make(self, name: str) -> str:
        base = f"{self.prefix}{slug_of(name)}"
        candidate, index = base, 2
        while candidate in self.used:
            candidate = f"{base}_{index}"
            index += 1
        self.used.add(candidate)
        return candidate

    def reserve(self, entity_id: str) -> None:
        self.used.add(entity_id)


# ---------------------------------------------------------------- 武将页解析


@dataclass
class SkillEntry:
    name: str
    text: str


@dataclass
class CardGroup:
    general: str
    variants: list[str] = field(default_factory=list)
    skills: list[SkillEntry] = field(default_factory=list)
    rulings: list[list] = field(default_factory=list)  # [技能名, 原文]


def parse_hero_pages() -> tuple[list[CardGroup], dict[str, str]]:
    """返回 (武将条目分组, 技能名 -> 出现过的技能名写法)。"""
    groups: list[CardGroup] = []
    for page in HERO_PAGES:
        pending: list[tuple[str, str]] = []  # (武将名, 印次)
        current: CardGroup | None = None
        last_skill: str | None = None
        for line in page_blocks(page):
            head = HEAD.match(line)
            if head:
                variant_match = VARIANT.search(line)
                variant = variant_match.group(1) if variant_match else "?"
                pending.append((head.group(1), variant))
                current = None
                last_skill = None
                continue
            skill_parts = re.split(r"(?<=。)(?=[\u4e00-\u9fff·・]{1,12}——)", line)
            skill = SKILL.match(skill_parts[0]) if len(skill_parts) == 1 else None
            if len(skill_parts) > 1 and (pending or current is not None):
                if current is None:
                    current = CardGroup(general=pending[-1][0], variants=[v for _, v in pending])
                    groups.append(current)
                    pending = []
                for part in skill_parts:
                    piece = SKILL.match(part)
                    if piece:
                        current.skills.append(SkillEntry(piece.group(1), piece.group(2).strip()))
                        last_skill = piece.group(1)
                continue
            if skill and (pending or current is not None):
                if current is None:
                    current = CardGroup(general=pending[-1][0], variants=[v for _, v in pending])
                    groups.append(current)
                    pending = []
                current.skills.append(SkillEntry(skill.group(1), skill.group(2).strip()))
                last_skill = skill.group(1)
                continue
            if line.startswith("◆") and current is not None:
                current.rulings.append([last_skill, line.lstrip("◆").strip()])
                continue
            if current is not None and current.rulings and line:
                # ◆ 裁定后面的续行（(1)/(2)、例如…）属于同一条裁定
                skill_name, text = current.rulings[-1]
                if len(text) < 4000:
                    current.rulings[-1][1] = f"{text}\n{line}"
                continue
    return groups, {}


def mode_of(variant: str) -> str | None:
    if variant in {"1V1", "3V3"}:
        return None  # 这些是独立的对战模式，本次不录入
    if variant.startswith("国"):
        return "guozhan"
    if variant == "?":
        return "identity"
    return "identity"


def variant_rank(variant: str, mode: str) -> int:
    order = GUOZHAN_PRIORITY if mode == "guozhan" else IDENTITY_PRIORITY
    try:
        return order.index(variant)
    except ValueError:
        return len(order)


# ---------------------------------------------------------------- 技能


def import_skills() -> dict:
    groups, _ = parse_hero_pages()
    id_alloc = IdAllocator("skill_")
    version_alloc = IdAllocator("skill_version_")

    # (技能名, mode) -> 候选列表
    candidates: dict[tuple[str, str], list[dict]] = {}
    general_variants: dict[str, set[str]] = {}
    for group in groups:
        general_variants.setdefault(group.general, set()).update(group.variants)
        for variant in group.variants:
            mode = mode_of(variant)
            if mode is None:
                continue
            # 同一组里的技能属于该组所有印次（网页把共享文本放在最后一个印次后面）
            for skill in group.skills:
                key = (skill.name, mode)
                candidates.setdefault(key, []).append(
                    {
                        "variant": variant,
                        "text": skill.text,
                        "general": group.general,
                    }
                )

    chosen: dict[str, dict] = {}
    skipped_texts: dict[str, set[str]] = {}
    for (name, mode), options in sorted(candidates.items()):
        options = sorted(options, key=lambda item: (variant_rank(item["variant"], mode), item["general"]))
        best = options[0]
        chosen.setdefault(name, {})[mode] = best
        variants = {option["variant"] for option in options}
        if len(variants) > 1:
            skipped_texts.setdefault(name, set()).update(
                variant_label(option["variant"]) for option in options[1:]
            )

    written_skills: list[str] = []
    written_versions: list[str] = []
    name_to_id: dict[str, str] = {}
    for name, modes in sorted(chosen.items()):
        skill_id = id_alloc.make(name)
        name_to_id[name] = skill_id
        skill = MODEL_BY_KIND["skill"].model_validate(
            {
                "id": skill_id,
                "name": name,
            }
        )
        write_entity("skill", skill)
        written_skills.append(skill_id)

        for mode, best in sorted(modes.items()):
            version_id = f"{skill_id}_{mode}_v1"
            version_alloc.reserve(version_id)
            version = MODEL_BY_KIND["skill_version"].model_validate(
                {
                    "id": version_id,
                    "skill_id": skill_id,
                    "mode": mode,
                    "text": best["text"],
                    "version_label": variant_label(best["variant"]),
                    "product_id": None,
                    "previous_version_id": None,
                    "source": SOURCE,
                }
            )
            write_entity("skill_version", version)
            written_versions.append(version_id)

    return {
        "skills": written_skills,
        "versions": written_versions,
        "candidates": candidates,
        "groups": groups,
        "general_variants": general_variants,
        "chosen": chosen,
        "name_to_id": name_to_id,
    }


def variant_label(variant: str) -> str:
    return VARIANT_LABEL.get(variant, variant)


# ---------------------------------------------------------------- 写盘


def write_entity(kind: str, entity) -> Path:
    path = yaml_writer.entity_file_path(DATA_DIR, kind, entity.id)
    yaml_writer.upsert_entity(path, kind, entity)
    return path


def write_entity_by_id(kind: str, entity_id: str, entity) -> Path:
    path = yaml_writer.entity_file_path(DATA_DIR, kind, entity_id)
    yaml_writer.upsert_entity(path, kind, entity)
    return path


# ---------------------------------------------------------------- 主流程


def validate_all() -> bool:
    raws, issues = load_data_dir(DATA_DIR)
    result = validate_entities(DATA_DIR, raws, issues)
    print(f"校验：错误 {len(result.errors)} / 警告 {len(result.warnings)}")
    for issue in result.errors[:15]:
        print(issue.format())
    if result.warnings:
        print("--- 警告（前 10 条）---")
        for issue in result.warnings[:10]:
            print(issue.format())
    return result.ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default="all")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--pages-dir", default=None)
    args = parser.parse_args()
    global DATA_DIR, RULES_DIR
    if args.data_dir:
        DATA_DIR = Path(args.data_dir).resolve()
    if args.pages_dir:
        RULES_DIR = Path(args.pages_dir).resolve()

    if args.stage not in {"all", "skills"}:
        print(f"未知的 stage：{args.stage}（现在只支持 skills / all）")
        return 2

    report = import_skills()
    print(
        f"技能：{len(report['skills'])} 个 SkillDefinition，"
        f"{len(report['versions'])} 个 SkillVersion"
    )
    modes = {}
    for (_name, mode) in report["candidates"]:
        modes[mode] = modes.get(mode, 0) + 1
    print("  (技能名, 模式) 候选数：", modes)

    ok = validate_all()
    if ok:
        result = build_database(DATA_DIR, DATA_DIR.parent / "build" / "sgs.db")
        print(result.summary())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
