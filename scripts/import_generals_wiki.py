#!/usr/bin/env python3
"""从三国杀 WIKI（bilibili）导入武将：General + GeneralVariant。

数据来源：https://wiki.biligame.com/sgs/ （社区 WIKI，MediaWiki + 语义扩展）

导入什么
--------
只取每个武将页面的 **「经典版本」段**（= 身份局标准版），并按 `武将包` 前缀筛选
（默认只导入 `标准*`，也就是标准包的 26 名武将）。页面里的
「界限突破版本 / 国战版本 / 自走棋 / 皮肤 / 台词」等段落**本轮不录** ——
它们各自是以后要单独加的 GeneralVariant（国战体力按阴阳鱼记 1.5/2/2.5）。

结构
----
- ``General``（`data/generals/`）：只有 id / name / notes。
  官方名不同 = 不同武将个体（诸葛亮 / 界诸葛亮 / 神诸葛亮）。
- ``GeneralVariant``（`data/general_variants/`）：variant 标签 + 模式 / 势力 / 体力 /
  性别 / 称号 / 编号 / 武将牌系列 / 产品 / 技能。官方名相同、玩法不同 → 同一个 General 的多个版本。

技能
----
技能按名字挂到已有的 ``Skill`` 上（规则集导入的技能已经覆盖标准包）。
**不新建技能版本**：技能文本继续用规则集那份，避免同一 (技能, 模式) 出现两条重叠版本；
每个武将版本的 ``notes`` 里写明“文本与卡面用词可能不同，待按实体卡核验”。

用法（容器内、仓库根目录）
--------------------------
先按 `scripts/import_rules_book.sh` 的方式下载页面（本脚本需要 ``action=parse`` 的 JSON）::

    python scripts/fetch_wiki_generals.py /tmp/wiki/generals     # 或自行下载
    pip install pypinyin                                        # 生成拼音 id（可选）
    python scripts/import_generals_wiki.py --pages-dir /tmp/wiki/generals --apply

不加 ``--apply`` 时只做“解析 + 校验”，不会写任何 YAML。
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # 可选依赖：有它就生成拼音 id（general_zhu_ge_liang），没有就退回稳定短哈希
    from pypinyin import lazy_pinyin
except ImportError:  # pragma: no cover
    lazy_pinyin = None

WIKI_TITLE = "三国杀WIKI（bilibili）"
WIKI_BASE = "https://wiki.biligame.com/sgs/"
NOTE = (
    "技能文本暂用《三国杀官方规则集 3.0》的规范化表述：与 WIKI 的卡面原文在个别用词上"
    "可能不同（例如 仁德/突袭/无双/观星 等），待按实体卡逐条核验后再改。"
)

SECTION_SKILL_ROW = re.compile(
    r'<div class="basic-info-row-label[^"]*"[^>]*>(.*?)</div>\s*'
    r'<div[^>]*style="align-self: center;"[^>]*>(.*?)</div>',
    re.S,
)
HEADER_DIV = re.compile(
    r'<div class="middle-grey-side-transparen-bg[^"]*"[^>]*>\s*([^<]+?)\s*</div>'
)
GENDER = {"男": "male", "女": "female"}


# ---------------------------------------------------------------- 小工具


def slug(text: str) -> str:
    """中文 → 拼音 id 片段；没有 pypinyin 时退回稳定短哈希。"""
    if lazy_pinyin is not None:
        parts = [part for part in lazy_pinyin(text) if part.isalnum()]
        value = re.sub(r"[^a-z0-9_]", "", "_".join(parts).lower())
        if value:
            return value[:48]
    ascii_part = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if len(ascii_part) >= 2 and re.search(r"[a-z]", ascii_part):
        return ascii_part[:48]
    import hashlib

    return "n" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def clean(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def strip_templates(value: str) -> str:
    """{{黑幕|三个禽兽}} -> 三个禽兽。"""
    value = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}", r"\1", value)
    return re.sub(r"\{\{[^{}]*\}\}", "", value).strip()


def clean_title(raw: str) -> str:
    """称号字段常混着别的版本：白衣渡江、士别三日（界） -> 白衣渡江。"""
    value = strip_templates(raw)
    if not value:
        return ""
    match = re.match(r"^(.*?)（[^）]*标[^）]*）", value)
    if match and match.group(1).strip("、，,;； "):
        return match.group(1).strip("、，,;； ")
    parts = [part.strip() for part in re.split(r"[、；;]", value) if part.strip()]
    for part in parts:
        if "（" not in part:
            return part
    return re.sub(r"（[^）]*）", "", parts[0]).strip()


def _legacy_clean_aliases(raw: str) -> list[str]:
    value = strip_templates(raw)
    if not value:
        return []
    return [
        part.strip()
        for part in re.split(r"[、，,；;/\s]+", value)
        if part.strip()
    ]


# ---------------------------------------------------------------- 页面解析


def section_body(page_html: str, prefix: str) -> str | None:
    sections = []
    for match in re.finditer(r"<h2\b[^>]*>(.*?)</h2>", page_html, re.S):
        name = html_module.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
        sections.append((match.end(), name))
    for index, (end, name) in enumerate(sections):
        if name.startswith(prefix):
            stop = sections[index + 1][0] if index + 1 < len(sections) else len(page_html)
            body = page_html[end:stop]
            return re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.S)
    return None


def skills_of(section: str) -> list[tuple[str, str]]:
    """技能表：技能名（<a title=…>）+ 紧跟的说明，只取「技能」标题到下一个标题之间。"""
    headers = [(m.start(), m.group(1).strip()) for m in HEADER_DIV.finditer(section)]
    start = None
    for position, name in headers:
        if name == "技能":
            start = position
            break
    if start is None:
        return []
    stop = len(section)
    for position, name in headers:
        if position > start and name != "技能":
            stop = position
            break

    out: list[tuple[str, str]] = []
    for label, body in SECTION_SKILL_ROW.findall(section[start:stop]):
        name = clean(re.sub(r"<a[^>]*>(.*?)</a>", r"\1", label, flags=re.S))
        name = name.replace("☆", "").strip()
        text = re.sub(r"\s*\n\s*", "", clean(body))
        if name and text and len(name) <= 12:
            out.append((name, text))
    return out


def template_fields(wikitext: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key in ("武将名", "势力", "性别", "别称", "称号", "武将包", "经典体力", "出现模式"):
        match = re.search(rf"\|\s*{key}\s*=\s*([^\n]*)", wikitext)
        if match:
            fields[key] = match.group(1).strip()
    return fields


def collect(pages_dir: Path, pack_prefix: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(pages_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        wikitext = data.get("wikitext") or ""
        if isinstance(wikitext, dict):
            wikitext = wikitext.get("*", "")
        fields = template_fields(wikitext)
        pack = fields.get("武将包", "")
        if not pack.startswith(pack_prefix):
            continue
        page_html = data["text"]["*"]
        if isinstance(page_html, dict):  # pragma: no cover - API 形状差异
            page_html = page_html.get("*", "")
        section = section_body(page_html, "经典版本")
        if section is None:
            continue
        rows.append(
            {
                "page": data["title"],
                "name": fields.get("武将名") or data["title"],
                "faction": strip_templates(fields.get("势力", "")),
                "gender": GENDER.get(fields.get("性别", ""), "unknown"),
                "title": clean_title(fields.get("称号", "")),
                "hp": fields.get("经典体力", ""),
                "pack": pack,
                "skills": skills_of(section),
            }
        )
    return rows


# ---------------------------------------------------------------- 写库


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages-dir", default="/tmp/wiki/generals")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--pack-prefix", default="标准", help="只导入武将包以此开头的武将")
    parser.add_argument("--variant-label", default="标准版")
    parser.add_argument("--apply", action="store_true", help="真的写 YAML（默认只解析试跑）")
    args = parser.parse_args()

    from sgs_helper.db import build_database
    from sgs_helper.loader import load_data_dir
    from sgs_helper.schema import MODEL_BY_KIND
    from sgs_helper.validate import validate_entities

    data_dir = Path(args.data_dir)
    rows = collect(Path(args.pages_dir), args.pack_prefix)
    print(f"解析到 {len(rows)} 名武将（武将包前缀 {args.pack_prefix!r}）")

    skill_by_name = {}
    for path in sorted((data_dir / "skills").glob("*.yaml")):
        skill = yaml.safe_load(path.read_text(encoding="utf-8"))
        skill_by_name[skill["name"]] = skill["id"]

    if args.apply:
        from sgs_helper.admin import yaml_writer

        for path in sorted((data_dir / "generals").glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if "SAMPLE" in str(data.get("name", "")):
                path.unlink()
                print(f"  删除 SAMPLE 武将 {path.name}")

    missing: list[str] = []
    written = 0
    for row in rows:
        general_id = f"general_{slug(row['name'])}"
        variant_id = f"variant_{slug(row['name'])}_{slug(args.variant_label)}"
        refs = []
        for skill_name, _text in row["skills"]:
            skill_id = skill_by_name.get(skill_name)
            if skill_id is None:
                missing.append(skill_name)
                continue
            refs.append({"skill_id": skill_id, "version_id": None})

        hp = None
        try:
            hp = float(row["hp"]) if row["hp"] else None
        except ValueError:
            hp = None

        general = MODEL_BY_KIND["general"].model_validate(
            {"id": general_id, "name": row["name"], "notes": ""}
        )
        variant = MODEL_BY_KIND["general_variant"].model_validate(
            {
                "id": variant_id,
                "general_id": general_id,
                "variant": args.variant_label,
                "title": row["title"],
                "general_number": None,
                "mode": "identity",
                "faction": row["faction"],
                "hp": hp,
                "gender": row["gender"],
                "product_id": None,
                "general_set": row["pack"],
                "skills": refs,
                "source": {
                    "type": "wiki",
                    "title": WIKI_TITLE,
                    "reference": f"{WIKI_BASE}{row['page']}",
                    "notes": (
                        f"武将页「{row['page']}」的经典版本（身份局标准版）；"
                        "社区 WIKI，未按实体卡核验；名单另见萌娘百科《三国杀》条目"
                    ),
                },
                "verification_status": "community",
                "notes": NOTE,
            }
        )
        print(
            f"   {row['name']:6s} {row['faction']} hp={hp} {row['title']:10s} "
            f"技能={[name for name, _ in row['skills']]}"
        )
        if args.apply:
            yaml_writer.upsert_entity(
                data_dir / "generals" / f"{general_id}.yaml", "general", general
            )
            yaml_writer.upsert_entity(
                data_dir / "general_variants" / f"{variant_id}.yaml", "general_variant", variant
            )
        written += 1

    print(f"{'写入' if args.apply else '试跑'}武将 {written} 个")
    if missing:
        print("⚠ 数据里没有这些技能（需要先补 Skill）:", sorted(set(missing)))
    if not args.apply:
        print("（没有 --apply，未写任何文件）")
        return 0

    raws, issues = load_data_dir(data_dir)
    result = validate_entities(data_dir, raws, issues)
    print(f"校验：错误 {len(result.errors)} / 警告 {len(result.warnings)}")
    for issue in (result.errors + result.warnings)[:15]:
        print(issue.format())
    if result.ok:
        print(build_database(data_dir, ROOT / "build" / "sgs.db").summary())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
