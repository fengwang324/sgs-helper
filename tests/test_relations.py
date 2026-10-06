"""两层结构（技能本体 / 版本）与单层卡牌的关联测试，以及“按名称自动识别”。

仍然成立的核心不变量：**旧版本永远不被新版本覆盖**；卡与产品之间没有外键，
只能按 ``Card.card_set ∈ Product.card_sets`` 关联。

新模型下 FAQ / 关键词 / 时点的关联不再手工维护（``related_*`` 全部删除），
而是在正文里按实体名称自动识别（``repo.mentions`` / ``db.detect_mentions``）；
``CardPrinting`` 层整体删除，卡牌只有 ``cards`` 一张表。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import replace_in

from sgs_helper.db import build_database
from sgs_helper.repo import (
    card_report,
    connect,
    faq_report,
    general_report,
    keyword_report,
    product_report,
    skill_report,
    timing_report,
)


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> list[str]:
    return [row[1] for row in _rows(db_path, f"PRAGMA table_info({table})")]


def _tables(db_path: Path) -> set[str]:
    return {row[0] for row in _rows(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


# ---------------------------------------------------------------- 技能：本体 / 版本


def test_skill_definition_and_versions_are_linked(synth_db: Path) -> None:
    orphan = _rows(
        synth_db,
        "SELECT id FROM skill_versions WHERE skill_id NOT IN (SELECT id FROM skills)",
    )
    assert orphan == []
    assert _rows(synth_db, "SELECT COUNT(*) FROM skill_versions WHERE skill_id = 'skill_demo'")[0][0] == 3


def test_same_skill_multiple_modes_do_not_overwrite(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo")
    assert report is not None
    groups = {group["mode"]: group for group in report["modes"]}
    assert list(groups) == ["guozhan", "identity"]
    # 每个模式都保留自己的全部版本
    assert groups["guozhan"]["version_ids"] == ["skill_demo_guozhan_v1"]
    assert groups["identity"]["version_ids"] == [
        "skill_demo_identity_v1",
        "skill_demo_identity_v2",
    ]
    texts = {v["mode"]: v["text"] for v in report["versions"]}
    assert texts["guozhan"] != texts["identity"]


def test_old_version_is_kept_after_update(synth_db: Path) -> None:
    ids = {
        row[0]
        for row in _rows(
            synth_db, "SELECT id FROM skill_versions WHERE skill_id = 'skill_demo' AND mode = 'identity'"
        )
    }
    assert ids == {"skill_demo_identity_v1", "skill_demo_identity_v2"}


def test_version_chain_and_review_note_are_recorded(synth_db: Path) -> None:
    previous, review_note = _rows(
        synth_db,
        "SELECT previous_version_id, review_note FROM skill_versions "
        "WHERE id = 'skill_demo_identity_v2'",
    )[0]
    assert previous == "skill_demo_identity_v1"
    assert review_note == "演示用待复核说明"


def test_skill_version_columns_drop_dates_and_lists(synth_db: Path) -> None:
    columns = _columns(synth_db, "skill_versions")
    for removed in (
        "package",
        "effective_from",
        "effective_to",
        "change_note",
        "related_keywords",
        "related_timing_ids",
        "notes",
    ):
        assert removed not in columns
    assert "review_note" in columns
    assert "source_key" in columns


# ---------------------------------------------------------------- 卡牌（单层）


def test_card_printing_layer_is_gone(synth_db: Path) -> None:
    """CardPrinting 层整体删除：没有表，也没有旧卡面列。"""
    assert "card_printings" not in _tables(synth_db)
    columns = _columns(synth_db, "cards")
    for removed in (
        "card_id",
        "name_printed",
        "product_id",
        "card_type",
        "subtype",
        "suit",
        "rank",
        "quantity",
        "printed_text",
        "normalized_text",
        "illustrator",
        "bottom_right_mark",
        "card_number",
        "related_keywords",
    ):
        assert removed not in columns
    assert {"category_1", "category_2", "category_3", "card_set", "effect_text"} <= set(columns)


def test_card_effect_skills_are_linked(synth_db: Path) -> None:
    raw = _rows(synth_db, "SELECT effect_skills FROM cards WHERE id = 'card_demo_trick'")[0][0]
    assert json.loads(raw) == [
        {"skill_id": "skill_demo", "version_id": "skill_demo_guozhan_v1"}
    ]
    # 引用不到技能的卡牌行仍然保留原始 id（不在构建期过滤）
    assert _rows(
        synth_db, "SELECT effect_skills FROM cards WHERE id = 'card_demo'"
    )[0][0] == "[]"


def test_card_report_has_no_printings(synth_conn) -> None:
    report = card_report(synth_conn, "card_demo")
    assert report is not None
    assert "printings" not in report
    assert report["card"]["id"] == "card_demo"
    assert report["effect"] == {"text": "", "skills": []}


def test_same_card_set_shows_up_in_every_declaring_product(synth_conn) -> None:
    """卡与产品没有外键：两个产品都声明了「演示牌堆」，因此都会列出这张卡。"""
    a = product_report(synth_conn, "product_demo_a")
    b = product_report(synth_conn, "product_demo_b")
    assert [c["id"] for c in a["cards"]] == ["card_demo_trick"]
    assert [c["id"] for c in b["cards"]] == ["card_demo_trick"]
    assert a["cards"][0]["card_set"] == b["cards"][0]["card_set"] == "演示牌堆"


# ---------------------------------------------------------------- 武将 → 技能版本绑定


def test_general_references_skills_by_id(synth_conn) -> None:
    report = general_report(synth_conn, "general_demo_alpha")
    assert report is not None
    variants = {v["id"]: v for v in report["variants"]}
    assert set(variants) == {"variant_demo_alpha_guozhan", "variant_demo_alpha_identity"}

    guozhan = variants["variant_demo_alpha_guozhan"]
    assert guozhan["skills"][0]["skill_id"] == "skill_demo"
    assert guozhan["skills"][0]["version_id"] is None  # 没有显式绑定 -> 按 mode 回退
    assert guozhan["skills"][0]["pinned"] is False
    assert guozhan["skills"][0]["version"]["id"] == "skill_demo_guozhan_v1"
    assert guozhan["product_id"] == "product_demo_a"

    identity = variants["variant_demo_alpha_identity"]
    assert identity["skills"][0]["version_id"] == "skill_demo_identity_v1"
    assert identity["skills"][0]["pinned"] is True
    assert identity["skills"][0]["version"]["id"] == "skill_demo_identity_v1"


def test_variant_skill_binding_is_persisted_not_inferred(synth_db: Path) -> None:
    """落库的只有显式绑定；回退匹配发生在查询期，不会写回 variant_skills。"""
    rows = _rows(
        synth_db,
        "SELECT variant_id, skill_id, version_id FROM variant_skills ORDER BY variant_id",
    )
    assert rows == [
        ("variant_demo_alpha_guozhan", "skill_demo", None),
        ("variant_demo_alpha_identity", "skill_demo", "skill_demo_identity_v1"),
        ("variant_demo_beta_identity", "skill_demo", None),
    ]


def test_ambiguous_mode_fallback_has_no_text(synth_conn) -> None:
    """身份局有 v1 / v2 两条版本时，空的 version_id 无法唯一回退 -> 没有文本。"""
    report = general_report(synth_conn, "general_demo_beta")
    assert report is not None
    entry = report["variants"][0]["skills"][0]
    assert entry["version_id"] is None
    assert entry["version"] is None


# ---------------------------------------------------------------- 按名称自动识别


def test_faq_relations_are_detected_by_name(synth_db: Path, synth_conn) -> None:
    report = faq_report(synth_conn, "faq_demo_1")
    assert report is not None
    assert [e["id"] for e in report["skills"]] == ["skill_demo"]
    assert [e["id"] for e in report["cards"]] == ["card_demo"]
    assert report["generals"] == []
    assert report["keywords"] == []
    assert report["timings"] == []
    # 关联不再落库
    assert not any(column.startswith("related_") for column in _columns(synth_db, "faqs"))


def test_keyword_and_timing_mentions_are_detected(synth: Path, tmp_path: Path) -> None:
    """正文里写到其它实体名称，就会被识别成“相关”，并且不把自己算进去。"""
    replace_in(
        synth / "keywords" / "demo.yaml",
        "definition: 演示用规则关键词定义",
        "definition: 演示关键词不再手工关联：这里的演示技能与演示时点、演示卡牌都按名称识别",
    )
    replace_in(
        synth / "timings" / "demo.yaml",
        "description: 演示用时点说明",
        "description: 演示时点会提到演示关键词与演示技能",
    )

    db_path = tmp_path / "mentions.db"
    build_database(synth, db_path)
    conn = connect(db_path)
    try:
        keyword = keyword_report(conn, "keyword_demo")
        assert keyword is not None
        assert [e["id"] for e in keyword["skills"]] == ["skill_demo"]
        assert [e["id"] for e in keyword["timings"]] == ["timing_demo"]
        assert [e["id"] for e in keyword["cards"]] == ["card_demo"]
        assert keyword["keywords"] == []  # 自己的名字写在定义里，也不算“相关”
        assert keyword["generals"] == []

        timing = timing_report(conn, "timing_demo")
        assert timing is not None
        assert [e["id"] for e in timing["keywords"]] == ["keyword_demo"]
        assert [e["id"] for e in timing["skills"]] == ["skill_demo"]
        assert timing["timings"] == []  # 时点自身被排除
    finally:
        conn.close()
