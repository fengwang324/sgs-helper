"""两层结构（技能本体/版本、卡牌定义/印刷）的关联测试。

这是本项目最关键的不变量：**旧版本永远不被新版本覆盖**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sgs_helper.repo import card_report, general_report, skill_report


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_skill_definition_and_versions_are_linked(synth_db: Path) -> None:
    orphan = _rows(
        synth_db,
        "SELECT id FROM skill_versions WHERE skill_id NOT IN (SELECT id FROM skills)",
    )
    assert orphan == []
    assert _rows(synth_db, "SELECT COUNT(*) FROM skill_versions WHERE skill_id = 'skill_demo'")[0][0] == 3


def test_same_skill_multiple_modes_do_not_overwrite(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo", as_of=None)
    assert report is not None
    modes = [group["mode"] for group in report["modes"]]
    assert modes == ["guozhan", "identity"]
    texts = {v["mode"]: v["text"] for v in report["versions"]}
    assert texts["guozhan"] != texts["identity"]
    # 每个模式都保留了自己的当前有效版本
    current_by_mode = {group["mode"]: group["current_id"] for group in report["modes"]}
    assert current_by_mode["guozhan"] == "skill_demo_guozhan_v1"
    assert current_by_mode["identity"] == "skill_demo_identity_v2"


def test_old_version_is_kept_after_update(synth_db: Path) -> None:
    ids = {
        row[0]
        for row in _rows(
            synth_db, "SELECT id FROM skill_versions WHERE skill_id = 'skill_demo' AND mode = 'identity'"
        )
    }
    assert ids == {"skill_demo_identity_v1", "skill_demo_identity_v2"}


def test_version_chain_is_recorded(synth_db: Path) -> None:
    row = _rows(
        synth_db,
        "SELECT previous_version_id, change_note FROM skill_versions "
        "WHERE id = 'skill_demo_identity_v2'",
    )[0]
    assert row[0] == "skill_demo_identity_v1"
    assert row[1]


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


def test_general_references_skills_by_id(synth_conn) -> None:
    report = general_report(synth_conn, "general_demo_alpha")
    assert report is not None
    assert report["skills"][0]["skill_id"] == "skill_demo"
    assert report["skills"][0]["version_id"] is None  # 没有显式绑定 -> 详情页自动推导
    assert report["product"]["id"] == "product_demo_a"


def test_faq_relations_resolve(synth_db: Path, synth_conn) -> None:
    from sgs_helper.repo import faq_report

    report = faq_report(synth_conn, "faq_demo_1")
    assert report is not None
    assert report["skills"][0]["id"] == "skill_demo"
    assert report["cards"][0]["id"] == "card_demo"
    assert report["keywords"][0]["id"] == "keyword_demo"
    assert report["timings"][0]["id"] == "timing_demo"
    # 数据库里存的关联 id 必须都真实存在
    assert _rows(
        synth_db,
        "SELECT related_keywords FROM faqs WHERE id = 'faq_demo_1'",
    )[0][0] == '["keyword_demo"]'


def test_keyword_and_timing_relations(synth_conn) -> None:
    from sgs_helper.repo import keyword_report, timing_report

    keyword = keyword_report(synth_conn, "keyword_demo")
    assert keyword is not None
    assert [t["id"] for t in keyword["timings"]] == ["timing_demo"]
    assert [f["id"] for f in keyword["faqs"]] == ["faq_demo_1"]
    assert [v["id"] for v in keyword["skills"]] == ["skill_demo_guozhan_v1"]

    timing = timing_report(synth_conn, "timing_demo")
    assert timing is not None
    assert [t["id"] for t in timing["next"]] == ["timing_demo_next"]
    assert [t["id"] for t in timing["keywords"]] == ["keyword_demo", "keyword_demo2"]
    assert [s["id"] for s in timing["skills"]] == ["skill_demo"]
