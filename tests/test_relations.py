"""两层结构（技能本体/版本、卡牌定义/印刷）与“按名称自动识别”的关联测试。

仍然成立的核心不变量：**旧版本永远不被新版本覆盖**。

新模型下 FAQ / 关键词 / 时点的关联不再手工维护（``related_*`` 全部删除），
而是在正文里按实体名称自动识别（``repo.mentions`` / ``db.detect_mentions``）。
"""

from __future__ import annotations

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


# ---------------------------------------------------------------- 卡牌：定义 / 印刷


def test_card_definition_and_printings_are_linked(synth_db: Path) -> None:
    orphan = _rows(
        synth_db,
        "SELECT id FROM card_printings WHERE card_id NOT IN (SELECT id FROM cards)",
    )
    assert orphan == []
    assert _rows(synth_db, "SELECT COUNT(*) FROM card_printings WHERE card_id = 'card_demo'")[0][0] == 2


def test_same_card_in_different_products_does_not_overwrite(synth_conn) -> None:
    report = card_report(synth_conn, "card_demo")
    assert report is not None
    printings = {p["id"]: p for p in report["printings"]}
    assert set(printings) == {"card_demo_a_spade_1", "card_demo_b_heart_5"}
    products = {p["product_id"] for p in report["printings"]}
    assert products == {"product_demo_a", "product_demo_b"}
    # 卡面文字、角标按印刷记录分别保存
    assert printings["card_demo_a_spade_1"]["printed_text"] == "A 产品演示卡面原文"
    assert printings["card_demo_a_spade_1"]["bottom_right_mark"] == "君"
    assert printings["card_demo_b_heart_5"]["bottom_right_mark"] == ""
    assert printings["card_demo_a_spade_1"]["card_number"] == "001"


def test_card_printing_mode_comes_from_product(synth_conn) -> None:
    """CardPrinting 不存 mode，报告里的 mode 由 product_id -> Product.mode 推导。"""
    report = card_report(synth_conn, "card_demo")
    assert report is not None
    modes = {p["id"]: p["mode"] for p in report["printings"]}
    assert modes == {"card_demo_a_spade_1": "guozhan", "card_demo_b_heart_5": "identity"}


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
