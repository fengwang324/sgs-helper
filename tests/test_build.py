"""SQLite 构建测试。

单层化之后建库与旧模型的差别：

- 没有 ``card_printings`` 表（也没有它的索引），卡牌只有 ``cards`` 一张表；
- ``cards`` 的效果文本 / 技能引用落成 ``effect_text`` / ``effect_skills``(JSON)；
- ``Card`` 不再有 ``product_id`` / ``mode`` / 花色点数，这些字段不会出现在任何列里；
- 检索正文（原 ``normalized_text``）在构建期生成，并把识别到的实体名拼进
  ``search_index.body``。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import replace_in

from sgs_helper.db import DataValidationError, build_database
from sgs_helper.loader import manifest_digest
from sgs_helper.repo import get_meta
from sgs_helper.validate import validate_data_dir

EXPECTED_TABLES = {
    "products",
    "generals",
    "general_variants",
    "variant_skills",
    "skills",
    "skill_versions",
    "cards",
    "faqs",
    "keywords",
    "sources",
    "search_index",
    "meta",
}

#: 模型精简后已删除的表
REMOVED_TABLES = {"general_sets", "card_printings", "timings"}

#: 卡牌单层化后删掉的列
REMOVED_CARD_COLUMNS = (
    "card_type",
    "subtype",
    "name_printed",
    "card_id",
    "product_id",
    "suit",
    "rank",
    "quantity",
    "printed_text",
    "normalized_text",
    "illustrator",
    "bottom_right_mark",
    "card_number",
    "related_keywords",
)


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        return {row[0] for row in rows}
    finally:
        conn.close()


def _indexes(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        return {row[0] for row in rows}
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_all_expected_tables_created(synth_db: Path) -> None:
    assert EXPECTED_TABLES <= _tables(synth_db)


def test_removed_tables_and_indexes_are_gone(synth_db: Path) -> None:
    assert not REMOVED_TABLES & _tables(synth_db)
    indexes = _indexes(synth_db)
    assert "idx_skill_versions_window" not in indexes
    assert not any(name.startswith("idx_card_printings") for name in indexes)
    # 单层卡牌的新索引在，旧印刷索引不在
    assert {"idx_cards_category", "idx_cards_card_set"} <= indexes


def test_removed_columns_are_gone(synth_db: Path) -> None:
    card_columns = _columns(synth_db, "cards")
    for removed in REMOVED_CARD_COLUMNS:
        assert removed not in card_columns, f"cards 残留旧列 {removed}"
    # Card 没有 mode / source：mode 只存在于 skill_versions / general_variants
    assert "mode" not in card_columns
    assert "source_key" not in card_columns
    assert "notes" not in _columns(synth_db, "skill_versions")
    assert "change_note" not in _columns(synth_db, "skill_versions")
    assert "aliases" not in _columns(synth_db, "search_index")


def test_cards_table_matches_single_layer_model(synth_db: Path) -> None:
    assert _columns(synth_db, "cards") == [
        "id",
        "category_1",
        "category_2",
        "category_3",
        "name",
        "image",
        "card_set",
        "effect_text",
        "effect_skills",
        "rules",
        "yaml_file",
        "yaml_line",
    ]


def test_card_effect_skills_are_stored_as_json(synth_db: Path) -> None:
    conn = sqlite3.connect(synth_db)
    try:
        raw = conn.execute(
            "SELECT effect_text, effect_skills FROM cards WHERE id = 'card_demo_trick'"
        ).fetchone()
    finally:
        conn.close()
    text, skills = raw
    assert text == "演示效果：与演示技能相关。"
    assert isinstance(skills, str)  # 落库的是 JSON 文本
    assert json.loads(skills) == [
        {"skill_id": "skill_demo", "version_id": "skill_demo_guozhan_v1"}
    ]


def test_build_creates_db_file(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "sgs.db"
    result = build_database(synth, db_path)
    assert db_path.exists()
    assert result.total == 18
    assert result.db_path == db_path


def test_counts_match_bundle(synth: Path, tmp_path: Path) -> None:
    result = build_database(synth, tmp_path / "sgs.db")
    assert result.counts["product"] == 2
    assert result.counts["general"] == 2
    assert result.counts["general_variant"] == 3
    assert result.counts["skill"] == 2
    assert result.counts["skill_version"] == 4
    assert result.counts["card"] == 2
    assert result.counts["faq"] == 1
    assert result.counts["keyword"] == 2
    assert result.total == 18


def test_rebuild_is_idempotent(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    first = build_database(synth, db_path)
    conn = sqlite3.connect(db_path)
    rows_first = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(EXPECTED_TABLES)]
    conn.close()

    second = build_database(synth, db_path)
    conn = sqlite3.connect(db_path)
    rows_second = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(EXPECTED_TABLES)]
    conn.close()

    assert first.counts == second.counts
    assert rows_first == rows_second
    assert rows_second[sorted(EXPECTED_TABLES).index("skill_versions")] == 4


def test_build_refuses_invalid_data(synth: Path, tmp_path: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "skill_id: skill_demo",
        "skill_id: skill_missing",
    )
    with pytest.raises(DataValidationError) as excinfo:
        build_database(synth, tmp_path / "sgs.db")
    assert "skill_missing" in excinfo.value.result.format_report()


def test_build_refuses_broken_card_effect(synth: Path, tmp_path: Path) -> None:
    """卡牌效果里的技能引用也是硬引用：不存在就拒绝建库。"""
    replace_in(
        synth / "cards" / "demo_trick.yaml",
        "skill_id: skill_demo",
        "skill_id: skill_missing",
    )
    with pytest.raises(DataValidationError) as excinfo:
        build_database(synth, tmp_path / "sgs.db")
    assert "skill_missing" in excinfo.value.result.format_report()


def test_build_can_force_invalid_data(synth: Path, tmp_path: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "skill_id: skill_demo",
        "skill_id: skill_missing",
    )
    db_path = tmp_path / "sgs.db"
    build_database(synth, db_path, allow_invalid=True)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM generals").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM general_variants").fetchone()[0] == 3
    finally:
        conn.close()


def test_sources_are_deduplicated(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path)
    assert result.source_count == 1  # 全部指向同一份 source（type + reference）
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        linked = conn.execute(
            "SELECT COUNT(*) FROM skill_versions WHERE source_key IS NOT NULL"
        ).fetchone()[0]
        assert linked == 4
        # 实体卡类没有 source_key 列
        assert "source_key" not in _columns(db_path, "cards")
        assert "source_key" not in _columns(db_path, "products")
    finally:
        conn.close()


def test_meta_records_traceability(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    build_database(synth, db_path)
    meta = get_meta(sqlite3.connect(db_path))
    assert meta["schema_version"] == "6"
    assert meta["yaml_manifest_sha256"] == manifest_digest(synth)
    assert meta["count.skill_version"] == "4"
    assert meta["count.card"] == "2"
    assert "count.card_printing" not in meta
    assert meta["entity_total"] == "18"
    assert meta["fts5"] in {"0", "1"}
    assert "built_at" in meta


def test_variant_skills_join_table(synth_db: Path) -> None:
    """技能关系挂在**武将版本**上；version_id 只存显式绑定，回退在查询期做。"""
    conn = sqlite3.connect(synth_db)
    try:
        rows = conn.execute(
            "SELECT variant_id, general_id, skill_id, version_id, position "
            "FROM variant_skills ORDER BY variant_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("variant_demo_alpha_guozhan", "general_demo_alpha", "skill_demo", None, 0),
        (
            "variant_demo_alpha_identity",
            "general_demo_alpha",
            "skill_demo",
            "skill_demo_identity_v1",
            0,
        ),
        ("variant_demo_beta_identity", "general_demo_beta", "skill_demo", None, 0),
    ]


def test_search_index_covers_every_entity(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path)
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
        assert count == result.total
        # 技能版本的可读名称里带上了技能名，便于检索结果显示
        name = conn.execute(
            "SELECT name FROM search_index WHERE kind = 'skill_version' "
            "AND entity_id = 'skill_demo_identity_v2'"
        ).fetchone()[0]
        assert "演示技能" in name
    finally:
        conn.close()


def test_search_body_includes_detected_entity_names(synth_db: Path) -> None:
    """related_* 删除后，关联名靠构建期识别并写进 body，所以按关联名仍能搜到。"""
    conn = sqlite3.connect(synth_db)
    try:
        body = conn.execute(
            "SELECT body FROM search_index WHERE kind = 'faq' AND entity_id = 'faq_demo_1'"
        ).fetchone()[0]
        assert "演示技能" in body
        assert "演示卡牌" in body

        # 技能版本正文里识别出的关键词 / 技能名也会进 body
        version_body = conn.execute(
            "SELECT body FROM search_index WHERE kind = 'skill_version' "
            "AND entity_id = 'skill_demo_identity_v2'"
        ).fetchone()[0]
        assert "身份局二代演示技能文本" in version_body
    finally:
        conn.close()


def test_fts_index_created_when_available(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path, with_fts=True)
    conn = sqlite3.connect(db_path)
    try:
        meta = get_meta(conn)
        if result.with_fts:
            assert meta["fts5"] == "1"
            assert conn.execute("SELECT COUNT(*) FROM fts_search").fetchone()[0] == result.total
        else:  # pragma: no cover - 本环境有 FTS5
            assert meta["fts5"] == "0"
    finally:
        conn.close()


def test_fts_can_be_disabled(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path, with_fts=False)
    assert result.with_fts is False
    conn = sqlite3.connect(db_path)
    try:
        assert get_meta(conn)["fts5"] == "0"
        assert "fts_search" not in _tables(db_path)
    finally:
        conn.close()


def test_trace_columns_are_filled(synth_db: Path) -> None:
    conn = sqlite3.connect(synth_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT yaml_file, yaml_line FROM skill_versions WHERE id = 'skill_demo_identity_v2'"
        ).fetchone()
    finally:
        conn.close()
    assert row["yaml_file"] == "data/skill_versions/demo_identity_v2.yaml"
    assert row["yaml_line"] >= 1


def test_real_data_builds_and_has_rows(data_dir: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "real.db"
    result = build_database(data_dir, db_path)
    expected = validate_data_dir(data_dir).counts()
    assert result.counts == expected
    assert result.total == sum(result.counts.values())
    assert result.total > 1000
    assert result.counts["skill_version"] >= 1
    assert result.counts["card"] >= 81
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == result.total
    finally:
        conn.close()
    assert _tables(db_path) >= EXPECTED_TABLES
    assert "card_printings" not in _tables(db_path)
