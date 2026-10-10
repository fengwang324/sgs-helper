"""游戏牌规则、编辑器和 YAML 往返。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scripts.migrate_card_rules import migrate_card
from sgs_helper.admin.app import create_app
from sgs_helper.admin.yaml_writer import entity_to_yaml_dict
from sgs_helper.schema import Card


def test_asset_versions_follow_file_contents(tmp_path: Path, monkeypatch):
    from sgs_helper.admin import app

    static = tmp_path / "static"
    static.mkdir()
    script = static / "editor.js"
    script.write_text("first version", encoding="utf-8")
    monkeypatch.setattr(app, "ADMIN_DIR", tmp_path)
    first = app.static_asset_url("editor.js")
    assert first.startswith("/static/editor.js?v=")
    assert app.static_asset_url("editor.js") == first
    script.write_text("updated version", encoding="utf-8")
    assert app.static_asset_url("editor.js") != first


def card_node(**overrides):
    node = dict(
        id="card_rules_test", name="规则测试牌", category_1="游戏牌",
        category_2="锦囊牌", category_3="非延时锦囊", effect="", deck_info="",
    )
    node.update(overrides)
    return node


def test_card_contract_keeps_text_fields_outside_rules():
    card = Card.model_validate(card_node(
        attack_range="X+1", deck_info="♠7、♥8", effect="使用原文",
        rules={"use_methods": [{"label": "使用", "timing": "出牌阶段", "target": "一名角色", "effect": "造成伤害"}]},
    ))
    assert card.attack_range == "X+1"
    assert card.deck_info == "♠7、♥8"
    assert card.rules.use_methods[0].effect == "造成伤害"
    assert "note" not in card.rules.use_methods[0].model_dump()
    with pytest.raises(ValidationError):
        Card.model_validate(card_node(rules={"distance_modifier": "-1"}))
    with pytest.raises(ValidationError):
        Card.model_validate(card_node(attack_range=2))


def test_card_yaml_writer_keeps_blank_deck_and_text_effect():
    card = Card.model_validate(card_node(effect="原文效果", deck_info=""))
    payload = entity_to_yaml_dict("card", card)
    assert payload["deck_info"] == ""
    assert payload["effect"] == "原文效果"
    assert not isinstance(payload["effect"], dict)
    assert "skills" not in payload
    assert "rules" not in payload
    assert "category_2" not in payload or payload["category_2"] == "锦囊牌"


def test_markers_stay_free_and_have_no_game_fields():
    node = card_node(category_1="指示物", category_2="自由标签", category_3="", deck_info=None)
    assert migrate_card(node) is node
    payload = entity_to_yaml_dict("card", Card.model_validate(node))
    assert "rules" not in payload
    assert "deck_info" not in payload
    with pytest.raises(ValidationError):
        Card.model_validate({**node, "deck_info": "♠7"})
    with pytest.raises(ValidationError):
        Card.model_validate({**node, "card_set": "九变篇"})


def test_migration_keeps_original_and_splits_explicit_usage():
    node = card_node(effect="出牌阶段，对一名其他角色使用。令其执行测试效果。")
    migrated = migrate_card(node)
    assert migrated["effect"] == node["effect"]
    method = migrated["rules"]["use_methods"][0]
    assert method == {"timing": "出牌阶段", "target": "一名其他角色", "effect": "令其执行测试效果。"}
    assert migrate_card(migrated) is migrated


def test_weapon_migration_does_not_invent_attributes():
    node = card_node(category_2="装备牌", category_3="武器", effect="攻击范围 X ◆装备能力")
    migrated = migrate_card(node)
    assert migrated["attack_range"] == "X"
    assert migrated["effect"] == node["effect"]
    horse = card_node(category_2="装备牌", category_3="坐骑", effect="-1")
    assert migrate_card(horse) is horse


def test_game_card_round_trip_preserves_deck_info_and_removed_skill_fields(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "sgs.db")
    with TestClient(app) as client:
        response = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "id": "card_rules_test",
            "name": "规则测试牌", "category_1": "游戏牌", "category_2": "基本牌",
            "category_3": "自由牌族", "effect_text": "卡面原文 <保留>",
            "attack_range": "", "deck_info": "♠7、♥8", "note": "资料备注",
            "use_count": "2", "use_0_label": "方法一", "use_0_timing": "时机甲",
            "use_0_target": "X 名角色", "use_0_effect": "效果甲",
            "use_1_label": "方法二", "use_1_timing": "时机乙", "use_1_effect": "效果乙",
        }, follow_redirects=False)
        assert response.status_code == 303, response.text
        entity = app.state.store.entity("card", "card_rules_test")
        assert len(entity.rules.use_methods) == 2
        assert entity.deck_info == "♠7、♥8"
        sha = app.state.store.entity_sha("card", entity.id)
        saved = client.post(f"/edit/card/{entity.id}", data={
            "_csrf": app.state.csrf, "_base_sha": sha, "name": "新名称",
            "deck_info": entity.deck_info,
        }, follow_redirects=False)
        assert saved.status_code == 303
        doc = yaml.safe_load((synth / "cards" / "card_rules_test.yaml").read_text(encoding="utf-8"))
        assert doc["deck_info"] == "♠7、♥8"
        assert doc["effect"] == "卡面原文 <保留>"
        assert not isinstance(doc["effect"], dict)
        assert len(doc["rules"]["use_methods"]) == 2
        assert "skills" not in doc
        body = client.get(f"/view/card/{entity.id}").text
        assert "♠7、♥8" in body and "效果乙" in body and "资料备注" in body
        assert client.get(f"/edit/card/{entity.id}").status_code == 200


def test_old_card_skill_and_distance_fields_are_rejected(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "legacy.db")
    with TestClient(app) as client:
        result = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "name": "旧结构",
            "category_1": "游戏牌", "category_2": "装备牌", "category_3": "坐骑",
            "skill_count": "0", "distance_modifier": "-1", "deck_info": "",
        })
        assert result.status_code == 400
        assert "卡牌不关联技能" in result.text
        assert "距离修正字段已移除" in result.text


def test_marker_editor_omits_game_fields(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "markers.db")
    with TestClient(app) as client:
        response = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "id": "card_marker_test",
            "name": "自由指示物", "category_1": "指示物", "category_2": "千奇百怪",
            "effect_text": "自由效果", "use_count": "0", "note": "来源备注",
        }, follow_redirects=False)
        assert response.status_code == 303, response.text
        doc = yaml.safe_load((synth / "cards" / "card_marker_test.yaml").read_text(encoding="utf-8"))
        assert "rules" not in doc and "deck_info" not in doc and "card_set" not in doc
        assert doc["effect"] == "自由效果"


def _create_weapon(client: TestClient, app, card_id: str) -> None:
    response = client.post("/new/card", data={
        "_csrf": app.state.csrf, "_base_sha": "", "id": card_id,
        "name": "测试武器", "category_1": "游戏牌", "category_2": "装备牌",
        "category_3": "武器", "card_set": "测试牌堆", "attack_range": "3",
        "deck_info": "♠7", "effect_text": "武器效果", "use_count": "0",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text


def test_marker_conversion_requires_confirmation_for_existing_fields(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "marker-confirm.db")
    with TestClient(app) as client:
        card_id = "card_marker_confirm"
        _create_weapon(client, app, card_id)
        sha = app.state.store.entity_sha("card", card_id)
        payload = {
            "_csrf": app.state.csrf, "_base_sha": sha, "name": "测试武器",
            "category_1": "指示物", "category_2": "自由标签", "category_3": "",
            "card_set": "", "attack_range": "", "deck_info": "", "use_count": "0",
        }
        rejected = client.post(f"/edit/card/{card_id}", data=payload, follow_redirects=False)
        assert rejected.status_code == 400
        assert "确认舍弃" in rejected.text
        assert app.state.store.entity("card", card_id).card_set == "测试牌堆"

        payload["confirm_inactive_rules"] = "1"
        saved = client.post(f"/edit/card/{card_id}", data=payload, follow_redirects=False)
        assert saved.status_code == 303, saved.text
        marker = app.state.store.entity("card", card_id)
        assert marker.card_set is None and marker.attack_range == "" and marker.deck_info is None


def test_weapon_to_nonweapon_equipment_requires_confirmation(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "range-confirm.db")
    with TestClient(app) as client:
        card_id = "card_range_confirm"
        _create_weapon(client, app, card_id)
        payload = {
            "_csrf": app.state.csrf, "_base_sha": app.state.store.entity_sha("card", card_id),
            "name": "测试武器", "category_1": "游戏牌", "category_2": "装备牌",
            "category_3": "防具", "attack_range": "", "use_count": "0",
        }
        rejected = client.post(f"/edit/card/{card_id}", data=payload, follow_redirects=False)
        assert rejected.status_code == 400
        assert "攻击范围" in rejected.text and "确认舍弃" in rejected.text
        assert app.state.store.entity("card", card_id).attack_range == "3"

        payload["confirm_inactive_rules"] = "1"
        saved = client.post(f"/edit/card/{card_id}", data=payload, follow_redirects=False)
        assert saved.status_code == 303, saved.text
        assert app.state.store.entity("card", card_id).attack_range == ""


def test_weapon_can_clear_its_range_without_type_change(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "range-clear.db")
    with TestClient(app) as client:
        card_id = "card_range_clear"
        _create_weapon(client, app, card_id)
        saved = client.post(f"/edit/card/{card_id}", data={
            "_csrf": app.state.csrf, "_base_sha": app.state.store.entity_sha("card", card_id),
            "name": "测试武器", "category_1": "游戏牌", "category_2": "装备牌",
            "category_3": "武器", "attack_range": "", "use_count": "0",
        }, follow_redirects=False)
        assert saved.status_code == 303, saved.text
        assert app.state.store.entity("card", card_id).attack_range == ""


@pytest.mark.parametrize("subtype,field,value", [
    ("武器", "attack_range", "X+1"),
    ("坐骑", "note", "坐骑备注"),
    ("防具", "note", "防具补充说明"),
    ("宝物", "note", "宝物补充说明"),
])
def test_equipment_form_round_trip(synth: Path, tmp_path: Path, subtype, field, value):
    app = create_app(synth, tmp_path / "equipment.db")
    with TestClient(app) as client:
        result = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "id": "card_equipment_test",
            "name": "装备测试", "category_1": "游戏牌", "category_2": "装备牌", "category_3": subtype,
            "effect_text": "装备能力", "use_count": "0", field: value, "deck_info": "1张：♣A",
        }, follow_redirects=False)
        assert result.status_code == 303, result.text
        saved = app.state.store.entity("card", "card_equipment_test")
        assert getattr(saved, field) == value
        assert saved.deck_info == "1张：♣A"
        assert value in client.get("/wiki/card/card_equipment_test").text


def test_failure_echo_keeps_multiple_methods(synth: Path, tmp_path: Path):
    app = create_app(synth, tmp_path / "failure.db")
    with TestClient(app) as client:
        result = client.post("/new/card", data={
            "_csrf": app.state.csrf, "_base_sha": "", "name": "",
            "category_1": "游戏牌", "category_2": "锦囊牌",
            "use_count": "2", "use_0_effect": "失败保留甲", "use_1_effect": "失败保留乙",
        })
        assert result.status_code == 400
        assert "失败保留甲" in result.text and "失败保留乙" in result.text
