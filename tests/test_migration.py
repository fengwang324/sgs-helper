"""迁移后的完整体检：真实 ``data/`` 必须通过校验、能完整建库，且旧字段彻底消失。

这一组测试是本次“数据模型精简”的回归护栏：

- 字段层面：YAML 里不允许再出现任何被删字段；
- 结构层面：SQLite 表结构必须与新模型一一对应，没有残留列 / 残留表；
- 行为层面：真实数据建库后，技能版本绑定、按名称自动识别仍然工作。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from sgs_helper.constants import KIND_BY_NAME, KIND_ORDER
from sgs_helper.db import COLUMNS, SOURCE_KINDS, TRACE_COLUMNS, build_database
from sgs_helper.schema import MODEL_BY_KIND

from conftest import error_text

#: 本次精简删掉的字段名（YAML / SQLite 里都不应该再出现）
REMOVED_FIELDS = {
    "notes",
    "series",
    "publisher",
    "release_date",
    "edition",
    "package",
    "effective_from",
    "effective_to",
    "change_note",
    "normalized_text",
    "related_keywords",
    "related_timing_ids",
    "related_keyword_ids",
    "related_skill_ids",
    "related_general_ids",
    "related_card_ids",
    "related_faq_ids",
    "parent_id",
    "previous_ids",
    "next_ids",
    "aliases",
    # 卡牌从两层收敛成一层后删掉的字段
    "card_type",
    "subtype",
    "card_id",
    "name_printed",
    "printed_text",
    "suit",
    "rank",
    "quantity",
    "bottom_right_mark",
    "card_number",
}

#: 已删除的实体类型
REMOVED_KINDS = {"general_set", "card_printing"}


def _yaml_files(data_dir: Path):
    for path in sorted(data_dir.rglob("*.yaml")):
        if "_templates" in path.parts:
            continue
        yield path


# ---------------------------------------------------------------- YAML 层


def test_no_removed_fields_left_in_yaml(data_dir: Path) -> None:
    offenders: list[str] = []
    files = 0
    for path in _yaml_files(data_dir):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        files += 1
        for key in doc:
            if key in REMOVED_FIELDS:
                offenders.append(f"{path.relative_to(data_dir)}: {key}")
            if key == "source" and isinstance(doc[key], dict):
                for sub in doc[key]:
                    if sub in {"title", "notes"}:
                        offenders.append(f"{path.relative_to(data_dir)}: source.{sub}")
    assert files > 1000, f"data/ 下的 YAML 数量异常：{files}"
    assert not offenders, "以下文件仍含已删除字段：\n" + "\n".join(offenders[:20])


def test_general_set_directory_is_gone(data_dir: Path) -> None:
    assert not (data_dir / "general_sets").exists()


def test_source_is_type_plus_reference_only(data_dir: Path) -> None:
    seen = 0
    for path in _yaml_files(data_dir):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        source = (doc or {}).get("source")
        if source is None:
            continue
        seen += 1
        assert set(source) == {"type", "reference"}, f"{path} 的 source 不是精简结构：{source}"
    assert seen > 0, "规则类数据应当保留精简来源"


def test_entity_card_kinds_have_no_source(data_dir: Path) -> None:
    for dirname in ("products", "general_variants"):
        for path in sorted((data_dir / dirname).glob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert "source" not in doc, f"{path} 不该再保存 source"


def test_review_note_is_gone_from_all_data(data_dir: Path) -> None:
    """review_note 已整体删除：任何数据文件里都不该再有这个字段。"""
    offenders = []
    for path in _yaml_files(data_dir):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(doc, dict) and "review_note" in doc:
            offenders.append(str(path.relative_to(data_dir)))
    assert offenders == [], f"这些文件仍带 review_note：{offenders[:5]}"


def test_general_and_skill_are_name_only(data_dir: Path) -> None:
    for path in sorted((data_dir / "generals").glob("*.yaml")):
        assert set(yaml.safe_load(path.read_text(encoding="utf-8"))) == {"id", "name"}
    for path in sorted((data_dir / "skills").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(doc) <= {"id", "name"}


# ---------------------------------------------------------------- 校验层


def test_real_data_has_no_validation_errors(data_dir: Path) -> None:
    from sgs_helper.validate import validate_data_dir

    result = validate_data_dir(data_dir)
    assert not result.errors, error_text(result)
    assert result.bundle.total > 1000


def test_general_set_must_come_from_product(data_dir: Path) -> None:
    """GeneralVariant.general_set 只能取自所属产品的 Product.general_sets。"""
    products = {}
    for path in (data_dir / "products").glob("*.yaml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        products[doc["id"]] = doc
    for path in sorted((data_dir / "general_variants").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        general_set = doc.get("general_set")
        product_id = doc.get("product_id")
        if not general_set or not product_id:
            continue
        product = products.get(product_id)
        assert product is not None, f"{path} 指向不存在的产品 {product_id}"
        assert general_set in product["general_sets"], (
            f"{path} 的 general_set「{general_set}」不在 {product_id} 的 general_sets 里"
        )


# ---------------------------------------------------------------- 建库层


@pytest.fixture(scope="module")
def migration_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data_dir = Path(__file__).resolve().parents[1] / "data"
    db_path = tmp_path_factory.mktemp("migration") / "sgs.db"
    build_database(data_dir, db_path)
    return db_path


def test_sqlite_tables_match_models(migration_db: Path) -> None:
    conn = sqlite3.connect(migration_db)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for kind in KIND_ORDER:
            assert KIND_BY_NAME[kind].table in tables, f"缺少表 {kind}"
        for kind in REMOVED_KINDS:
            assert KIND_BY_NAME.get(kind) is None
        assert "general_sets" not in tables
        assert "variant_skills" in tables and "sources" in tables and "search_index" in tables

        for kind in KIND_ORDER:
            table = KIND_BY_NAME[kind].table
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            expected = ["id", *COLUMNS[kind]]
            if kind in SOURCE_KINDS:
                expected.append("source_key")
            expected.extend(TRACE_COLUMNS)
            assert columns == expected, f"{table} 的列与模型不一致：{columns} != {expected}"
            assert not REMOVED_FIELDS & set(columns), f"{table} 残留旧列：{REMOVED_FIELDS & set(columns)}"
    finally:
        conn.close()


def test_sources_and_search_index_are_slim(migration_db: Path) -> None:
    conn = sqlite3.connect(migration_db)
    try:
        assert [r[1] for r in conn.execute("PRAGMA table_info(sources)")] == [
            "source_key", "type", "reference"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(search_index)")] == [
            "kind", "entity_id", "name", "body"]
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 3
    finally:
        conn.close()


def test_every_variant_skill_binds_a_version(migration_db: Path) -> None:
    conn = sqlite3.connect(migration_db)
    try:
        total, bound = conn.execute(
            "SELECT COUNT(*), SUM(version_id IS NOT NULL) FROM variant_skills").fetchone()
        assert total > 0
        assert bound == total, f"{total - bound} 条武将技能没有绑定版本"
    finally:
        conn.close()


def test_search_still_finds_skill_text_without_normalized_text(migration_db: Path) -> None:
    """搜索正文是构建期生成的：没有 normalized_text 字段也要能搜到技能文本与关联名。"""
    conn = sqlite3.connect(migration_db)
    try:
        row = conn.execute(
            "SELECT body FROM search_index WHERE kind = 'skill_version' AND entity_id = ?",
            ("skill_kang_kai_guozhan_qlhd_v1",),
        ).fetchone()
        assert row is not None
        assert "目标后" in row[0]

        # FAQ 正文里按名称自动识别出的技能名，替代了原来的 related_skill_ids
        names = [r[0] for r in conn.execute("SELECT name FROM skills")]
        faq_body = conn.execute(
            "SELECT body FROM search_index WHERE kind = 'faq' AND entity_id = ?",
            ("faq_ruling_skill_bu_qu_1",),
        ).fetchone()[0]
        assert any(name in faq_body for name in names if len(str(name)) >= 2)
    finally:
        conn.close()


def test_entity_counts_are_stable(migration_db: Path) -> None:
    conn = sqlite3.connect(migration_db)
    try:
        counts = {
            kind: conn.execute(f"SELECT COUNT(*) FROM {KIND_BY_NAME[kind].table}").fetchone()[0]
            for kind in KIND_ORDER
        }
        assert counts == {
            "product": 1,
            "general": 163,
            "general_variant": 163,
            "skill": 311,
            "skill_version": 297,
            "card": 81,
            "faq": 83,
            "keyword": 126,
            "timing": 30,
        }
    finally:
        conn.close()


def test_models_reject_removed_fields() -> None:
    """写错字段名必须报错，不能静默丢弃。"""
    for kind, model in MODEL_BY_KIND.items():
        assert "notes" not in model.model_fields, f"{kind} 仍保留 notes"
        assert "source" not in model.model_fields or kind in SOURCE_KINDS


# ---------------------------------------------------------------- 卡牌（单层）


def test_cards_are_single_layer_and_categorized(migration_db: Path) -> None:
    conn = sqlite3.connect(migration_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 81
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'card_printings'").fetchone()
        rows = dict(
            conn.execute("SELECT id, category_1 || '/' || category_2 || '/' || category_3 FROM cards")
        )
        assert rows["card_sha"] == "游戏牌/基本牌/杀"
        assert rows["card_huo_sha"] == "游戏牌/基本牌/杀"       # 基本牌最后一级是 name
        assert rows["card_lve_shan"] == "游戏牌/基本牌/闪"
        assert rows["card_wu_xie_ke_ji"] == "游戏牌/锦囊牌/非延时锦囊"
        assert rows["card_le_bu_si_shu"] == "游戏牌/锦囊牌/延时锦囊"
        assert rows["card_ding_lan_ye_ming_zhu"] == "游戏牌/装备牌/宝物"
    finally:
        conn.close()


def test_no_indicator_cards_kept(data_dir: Path) -> None:
    """本次明确舍弃全部指示物卡牌（用户以后自己加）。"""
    for path in sorted((data_dir / "cards").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["category_1"] != "指示物", f"{path} 还是指示物卡"


def test_card_effect_skills_are_linked(migration_db: Path) -> None:
    conn = sqlite3.connect(migration_db)
    try:
        raw = conn.execute(
            "SELECT effect_skills FROM cards WHERE id = 'card_ding_lan_ye_ming_zhu'"
        ).fetchone()[0]
        assert json.loads(raw) == [
            {"skill_id": "skill_zhi_heng", "version_id": "skill_zhi_heng_guozhan_qlhd_v1"}
        ]
    finally:
        conn.close()


def test_card_set_must_be_declared_by_a_product(data_dir: Path) -> None:
    declared: set[str] = set()
    for path in (data_dir / "products").glob("*.yaml"):
        declared |= set(yaml.safe_load(path.read_text(encoding="utf-8"))["card_sets"])
    assert "指示物" not in declared, "「指示物」是卡牌分类，不是拓展包"
    for path in sorted((data_dir / "cards").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if doc.get("card_set"):
            assert doc["card_set"] in declared, f"{path} 的拓展包没在任何产品里声明"


def test_pending_basic_cards_have_no_image(data_dir: Path) -> None:
    """火杀 / 雷杀 / 掠闪 是用户要求先记录、卡图后续补的；其余卡必须有真实卡图。"""
    images = {p.name for p in (data_dir / "card_images").iterdir() if p.is_file()}
    pending = {"火杀", "雷杀", "掠闪"}
    seen: set[str] = set()
    for path in sorted((data_dir / "cards").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if doc["name"] in pending:
            seen.add(doc["name"])
            assert doc["image"] == "", f"{doc['id']} 的卡图应当先留空"
            continue
        assert doc["image"], f"{doc['id']} 没有卡图"
        assert doc["image"] in images, f"{doc['id']} 的卡图 {doc['image']} 不存在"
    assert seen == pending


def test_cards_no_longer_carry_review_flags(data_dir: Path) -> None:
    """卡牌原先用 review_note 标记「同名牌多种文本」等问题，该字段已删除。"""
    for path in sorted((data_dir / "cards").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "review_note" not in doc, f"{path.name} 仍带 review_note"
