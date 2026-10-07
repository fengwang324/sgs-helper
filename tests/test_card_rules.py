"""游戏牌规则从迁移、录入到 SQLite / Wiki 的完整往返。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scripts.migrate_card_rules import migrate_card
from sgs_helper.admin.app import create_app
from sgs_helper.admin.yaml_writer import entity_to_yaml_dict
from sgs_helper.repo import card_report, connect, search
from sgs_helper.schema import Card


def card_node(**overrides):
    return dict(id="card_rules_test", name="规则测试牌", category_1="游戏牌",
                category_2="锦囊牌", category_3="非延时锦囊",
                effect={"text": "", "skills": []}, **overrides)


def test_text_attributes_allow_x_and_reject_numbers():
    card = Card.model_validate(card_node(rules={"attack_range": "X+1", "distance_modifier": "你到其他角色：-X"}))
    assert card.rules.attack_range == "X+1"
    for field in ("attack_range", "distance_modifier", "note"):
        with pytest.raises(ValidationError):
            Card.model_validate(card_node(rules={field: 2}))


def test_markers_stay_free_and_have_no_game_rules():
    node = card_node()
    node.update(category_1="指示物", category_2="自由标签", category_3="")
    assert migrate_card(node) is node
    assert "rules" not in entity_to_yaml_dict("card", Card.model_validate(node))
    with pytest.raises(ValidationError):
        Card.model_validate({**node, "rules": {"attack_range": "X"}})


def test_migration_keeps_original_and_only_splits_explicit_usage():
    node = card_node()
    node["effect"]["text"] = "出牌阶段，对一名其他角色使用。令其执行测试效果。"
    migrated = migrate_card(node)
    assert migrated["effect"] == node["effect"]
    assert migrated["rules"]["use_methods"] == [dict(label="", timing="出牌阶段", target="一名其他角色", effect="令其执行测试效果。", note="")]
    assert migrate_card(migrated) is migrated
    node["effect"]["text"] = "没有明确时机与目标的原文。"
    method = migrate_card(node)["rules"]["use_methods"][0]
    assert method["timing"] == method["target"] == ""
    assert method["effect"] == node["effect"]["text"]
    node["effect"]["skills"] = [{"skill_id": "skill_demo"}]
    assert migrate_card(node)["effect"] == node["effect"]


@pytest.mark.parametrize("subtype,text,field,value", [
    ("武器", "攻击范围 X ◆装备能力", "attack_range", "X"),
    ("坐骑", "-1", "distance_modifier", "-1"),
    ("武器", "只有装备能力", "attack_range", ""),
])
def test_equipment_migration_does_not_invent_attributes(subtype, text, field, value):
    node = card_node()
    node.update(category_2="装备牌", category_3=subtype)
    node["effect"]["text"] = text
    migrated = migrate_card(node)
    assert migrated["rules"][field] == value
    assert migrated["effect"] == node["effect"]


def test_rules_survive_form_database_search_and_details(synth: Path, tmp_path: Path):
    db = tmp_path / "sgs.db"
    app = create_app(synth, db)
    with TestClient(app) as client:
        response = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "id": "card_rules_test",
            "name": "规则测试牌", "category_1": "游戏牌", "category_2": "基本牌",
            "category_3": "自由牌族", "effect_text": "卡面原文 <保留>", "skill_count": "0",
            "use_count": "2", "use_0_label": "方法一", "use_0_timing": "时机甲",
            "use_0_target": "X 名角色", "use_0_effect": "效果甲", "use_0_note": "定义X",
            "use_1_label": "方法二", "use_1_timing": "时机乙", "use_1_effect": "效果乙",
            "rules_note": "检索专用备注：演示技能", "rules_attack_range": "", "rules_distance_modifier": "",
        }, follow_redirects=False)
        assert response.status_code == 303, response.text
        entity = app.state.store.entity("card", "card_rules_test")
        assert len(entity.rules.use_methods) == 2
        sha = app.state.store.entity_sha("card", entity.id)
        # 旧客户端 / 部分字段编辑也不能擦掉新的规则块。
        saved = client.post(f"/edit/card/{entity.id}", data={
            "_csrf": app.state.csrf, "_base_sha": sha, "name": "新名称", "skill_count": "0",
        }, follow_redirects=False)
        assert saved.status_code == 303
        doc = yaml.safe_load((synth / "cards" / "card_rules_test.yaml").read_text(encoding="utf-8"))
        assert len(doc["rules"]["use_methods"]) == 2
        assert doc["effect"]["text"] == "卡面原文 <保留>"
        for url in (f"/wiki/card/{entity.id}", f"/view/card/{entity.id}"):
            body = client.get(url).text
            for text in ("时机甲", "X 名角色", "效果乙", "定义X", "检索专用备注"):
                assert text in body
            assert "&lt;保留&gt;" in body
        edit = client.get(f"/edit/card/{entity.id}").text
        assert 'name="use_1_effect"' in edit and "效果乙" in edit
        assert app.state.store.search("检索专用备注", kinds=["card"])
    with connect(db) as conn:
        report = card_report(conn, "card_rules_test")
        assert report["card"]["rules"]["use_methods"][1]["effect"] == "效果乙"
        raw = conn.execute("SELECT rules FROM cards WHERE id = ?", ("card_rules_test",)).fetchone()[0]
        assert json.loads(raw)["use_methods"][0]["target"] == "X 名角色"
        assert search(conn, "检索专用备注", kinds=["card"])


def test_marker_editor_omits_rules_and_game_rule_fields(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "markers.db")
    with TestClient(app) as client:
        response = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "id": "card_marker_test",
            "name": "自由指示物", "category_1": "指示物", "category_2": "千奇百怪",
            "effect_text": "自由效果", "skill_count": "0", "use_count": "0",
            "rules_note": "", "rules_attack_range": "", "rules_distance_modifier": "",
        }, follow_redirects=False)
        assert response.status_code == 303
        doc = yaml.safe_load((synth / "cards" / "card_marker_test.yaml").read_text(encoding="utf-8"))
        assert "rules" not in doc
        body = client.get("/wiki/card/card_marker_test").text
        assert "自由效果" in body and "出牌时机" not in body and "攻击范围" not in body


@pytest.mark.parametrize("subtype,field,value", [
    ("武器", "attack_range", "X+1"),
    ("坐骑", "distance_modifier", "其他角色到你：+X"),
    ("防具", "note", "防具补充说明"),
    ("宝物", "note", "宝物补充说明"),
])
def test_equipment_form_round_trip(synth: Path, tmp_path: Path, subtype, field, value):
    app = create_app(synth, tmp_path / "equipment.db")
    with TestClient(app) as client:
        result = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "id": "card_equipment_test",
            "name": "装备测试", "category_1": "游戏牌", "category_2": "装备牌", "category_3": subtype,
            "effect_text": "装备能力", "skill_count": "0", "use_count": "0",
            f"rules_{field}": value,
        }, follow_redirects=False)
        assert result.status_code == 303, result.text
        saved = app.state.store.entity("card", "card_equipment_test")
        assert getattr(saved.rules, field) == value
        assert value in client.get("/wiki/card/card_equipment_test").text
        # 有规则内容的类型切换不得静默擦除。
        failed = client.post("/edit/card/card_equipment_test", data={
            "_csrf": app.state.csrf, "_base_sha": app.state.store.entity_sha("card", saved.id),
            "category_1": "指示物", "skill_count": "0",
        }, follow_redirects=False)
        assert failed.status_code == 400
        assert "请清空游戏牌专用规则" in failed.text and value in failed.text
        assert app.state.store.entity("card", saved.id).category_1 == "游戏牌"


def test_failure_echo_keeps_multiple_methods(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "failure.db")
    with TestClient(app) as client:
        result = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "name": "",
            "category_1": "游戏牌", "category_2": "锦囊牌", "skill_count": "0",
            "use_count": "2", "use_0_effect": "失败保留甲", "use_1_note": "失败保留乙",
        })
        assert result.status_code == 400
        assert "失败保留甲" in result.text and "失败保留乙" in result.text
        assert 'name="use_1_note"' in result.text
