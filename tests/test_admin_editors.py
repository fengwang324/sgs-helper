"""专用录入页（产品 / 武将）与批量事务保存的测试。

对应这次「录入流程重构」的要求，逐条覆盖：

1. 只填武将名字也能创建
2. 内部 ID 为空时自动生成合法唯一 ID
3. 武将编号允许大小写字母/数字/-/_
4. Product 简洁表单正确保存 general_sets / card_sets
5. 编辑旧 Product 只写模型里还存在的字段（被删字段不会回来）
6. 编辑旧 General 时表单没展示的字段（image / review_note）保留
7. 已有 Skill 可以搜索并绑定
8. General 能显式绑定 SkillVersion
9. version_id 必须属于对应 skill_id
10. 新建武将时可以同时创建 Skill
11. 可以同时创建 Skill + SkillVersion
12. 已有 Skill 可以新增 SkillVersion
13. 新版本正确设置 previous_version_id
14. 多实体保存只 rebuild 一次
15. 批量里任何一条校验失败，一个 YAML 都不落盘
16. rebuild 失败时全部回滚
17. `_base_sha` 并发保护继续有效
18. 删除 Skill / SkillVersion 时新的 SkillRef（武将版本 + 卡牌 effect.skills）引用保护正确工作

全部在 tmp_path 的副本上跑，绝不修改仓库里的 data/。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sgs_helper.admin.app import create_app
from sgs_helper.repo import general_report
from sgs_helper.schema import General, Product

#: 真实数据里的固定 id（当前只有一个产品：群狼环鼎）
PRODUCT_ID = "qlhd"
GENERAL_ID = "general_zhu_ge_liang"
VARIANT_ID = "variant_zhu_ge_liang_guo_zhan_ban"
HUANG_YUE_GENERAL_ID = "general_huang_yue_ying"
HUANG_YUE_VARIANT_ID = "variant_huang_yue_ying_guo_zhan_ban"
JI_ZHI_V1 = "skill_ji_zhi_guozhan_qlhd_v1"
QI_CAI_V1 = "skill_qi_cai_guozhan_qlhd_v1"
GUAN_XING_V1 = "skill_guan_xing_guozhan_qlhd_v1"
KONG_CHENG_V1 = "skill_kong_cheng_guozhan_qlhd_v1"

#: 本次精简删掉的字段（YAML 里不允许再出现）
REMOVED_FIELDS = (
    "notes", "series", "publisher", "edition", "release_date", "package",
    "effective_from", "effective_to", "change_note", "normalized_text",
    "related_keywords", "related_timing_ids", "related_keyword_ids",
    "aliases", "source",
)

# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def workdir(tmp_path: Path, data_dir: Path):
    root = tmp_path / "repo"
    (root / "build").mkdir(parents=True, exist_ok=True)
    shutil.copytree(data_dir, root / "data")
    return root


@pytest.fixture()
def app(workdir: Path):
    return create_app(workdir / "data", workdir / "build" / "sgs.db")


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def store(app):
    return app.state.store


def post(client: TestClient, url: str, **kwargs):
    kwargs.setdefault("follow_redirects", False)
    return client.post(url, **kwargs)


def general_payload(app, **overrides) -> dict:
    """武将专用录入页会提交的表单数据（新模型：没有 source / notes）。"""
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "_action": "save",
        "skill_count": "0",
        "id": "",
        "name": "",
        "title": "",
        "general_number": "",
        "illustrator": "",
        "mode": "",
        "faction": "",
        "hp": "",
        "gender": "unknown",
        "product_id": "",
        "general_set": "",
        "variant": "标准版",
        "_variant_id": "",
        "_variant_sha": "",
        "variant_id": "",
        "review_note": "",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def product_payload(app, **overrides) -> dict:
    """产品精简表单：只有 名称 / 年份 / 模式 / 武将牌系列 / 游戏牌拓展包。"""
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "",
        "name": "",
        "release_year": "",
        "mode": "",
        "general_sets": "",
        "card_sets": "",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def variant_of(app, general_id: str) -> str:
    """取某个武将的第一个版本 id（编辑页默认编辑它）。"""
    variants = app.state.store.variants_of(general_id)
    assert variants, f"武将 {general_id} 没有任何版本"
    return variants[0].id


def general_edit(app, general_id: str, **overrides) -> dict:
    """编辑已有武将/版本时应该提交的表单数据。"""
    variant_id = str(overrides.pop("_variant", "") or variant_of(app, general_id))
    payload = general_payload(app, **overrides)
    store = app.state.store
    payload["_base_sha"] = store.entity_sha("general", general_id)
    payload["_variant_id"] = variant_id
    payload["variant_id"] = variant_id
    payload["_variant_sha"] = (
        store.entity_sha("general_variant", variant_id) if variant_id else ""
    )
    if not payload.get("variant"):
        entity = store.entity("general_variant", variant_id) if variant_id else None
        payload["variant"] = entity.variant if entity else "标准版"
    return payload


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def entity_file(workdir: Path, kind: str, entity_id: str) -> Path:
    """定位记录所在的 YAML 文件（文件名不一定等于 id）。"""
    from sgs_helper.constants import KIND_BY_NAME
    from sgs_helper.loader import load_data_dir

    raws, _issues = load_data_dir(workdir / "data")
    for raw in raws:
        if raw.kind == kind and raw.data.get("id") == entity_id:
            return workdir / "data" / raw.origin.path
    return workdir / "data" / KIND_BY_NAME[kind].dirname / f"{entity_id}.yaml"


def variant_file(workdir: Path, general_id: str) -> Path:
    """找到某个武将的（第一个）GeneralVariant 文件。"""
    from sgs_helper.loader import load_data_dir

    raws, _issues = load_data_dir(workdir / "data")
    found = [raw for raw in raws if raw.kind == "general_variant"
             and raw.data.get("general_id") == general_id]
    assert found, f"武将 {general_id} 没有任何版本文件"
    return workdir / "data" / found[0].origin.path


def new_general_id(response) -> str:
    location = response.headers["location"]
    return location.split("/view/general/")[1].split("?")[0]


# ---------------------------------------------------------------- 1 & 2


def test_only_name_is_required(client: TestClient, workdir: Path) -> None:
    """1) 只填名字也能创建武将。"""
    response = post(client, "/new/general", data=general_payload(client.app, name="测试武将"))
    assert response.status_code == 303, response.text
    general_id = new_general_id(response)
    general = load_yaml(entity_file(workdir, "general", general_id))
    assert general["name"] == "测试武将"
    variant = load_yaml(variant_file(workdir, general_id))
    assert variant["general_id"] == general_id
    assert variant["variant"] == "标准版"
    assert variant["mode"] is None
    assert variant["hp"] is None
    assert variant["general_number"] is None
    assert variant["general_set"] is None
    assert variant["product_id"] is None
    assert variant["skills"] == []
    assert "source" not in variant      # 实体卡类没有 source


def test_missing_id_is_generated_and_unique(client: TestClient, workdir: Path) -> None:
    """2) 内部 ID 留空 -> 后端生成合法且唯一的 ID。"""
    from sgs_helper.constants import ID_PATTERN

    first = post(client, "/new/general", data=general_payload(client.app, name="重复名"))
    second = post(client, "/new/general", data=general_payload(client.app, name="重复名"))
    assert first.status_code == 303 and second.status_code == 303
    first_id, second_id = new_general_id(first), new_general_id(second)
    assert first_id != second_id
    for entity_id in (first_id, second_id):
        assert re.fullmatch(ID_PATTERN, entity_id), entity_id
        assert entity_file(workdir, "general", entity_id).exists()
    # 同名的两个武将都保留（同名在不同产品/版本下合法）
    names = {load_yaml(entity_file(workdir, "general", eid))["name"] for eid in (first_id, second_id)}
    assert names == {"重复名"}


def test_generated_id_is_stable_for_ascii_names(client: TestClient, workdir: Path) -> None:
    """ASCII 名字直接转写下划线 id。"""
    response = post(client, "/new/general", data=general_payload(client.app, name="Test Hero"))
    general_id = new_general_id(response)
    assert general_id == "general_test_hero"
    assert entity_file(workdir, "general", general_id).exists()


def test_generated_id_avoids_existing_taken_id(client: TestClient) -> None:
    """重名（真实数据里已有 general_li_dian）时追加序号，绝不覆盖已有记录。"""
    response = post(client, "/new/general", data=general_payload(client.app, name="Li Dian"))
    general_id = new_general_id(response)
    assert general_id != "general_li_dian"
    assert general_id.startswith("general_li_dian")


# ---------------------------------------------------------------- 3


@pytest.mark.parametrize("number", ["wei001", "WEI001", "shu-01", "wu_07", "qun9"])
def test_general_number_accepts_letters_digits_and_separators(
    client: TestClient, workdir: Path, number: str
) -> None:
    """3) 编号：大小写字母 / 数字 / - / _ 都允许（不受内部 ID 正则限制）。"""
    response = post(
        client, "/new/general", data=general_payload(client.app, name=f"武将{number}", general_number=number)
    )
    assert response.status_code == 303, response.text
    general_id = new_general_id(response)
    assert load_yaml(variant_file(workdir, general_id))["general_number"] == number


def test_general_number_rejects_odd_characters(client: TestClient, workdir: Path) -> None:
    response = post(
        client, "/new/general", data=general_payload(client.app, name="怪编号", general_number="编号 001!")
    )
    assert response.status_code == 400
    assert "编号只能包含字母、数字" in response.text
    assert not list((workdir / "data" / "generals").glob("*怪编号*"))


# ---------------------------------------------------------------- 4 & 5


def test_product_minimal_form_saves_sets(client: TestClient, workdir: Path) -> None:
    """4) 产品简洁表单：只填名称/年份/模式/武将牌/游戏牌。"""
    response = post(
        client,
        "/new/product",
        data=product_payload(
            client.app,
            name="测试产品",
            release_year="2026",
            mode="guozhan",
            general_sets="国战标准\n势备篇",
            card_sets="君主游戏牌",
        ),
    )
    assert response.status_code == 303, response.text
    product_id = response.headers["location"].split("/view/product/")[1].split("?")[0]
    data = load_yaml(workdir / "data" / "products" / f"{product_id}.yaml")
    assert data["general_sets"] == ["国战标准", "势备篇"]
    assert data["card_sets"] == ["君主游戏牌"]
    assert data["release_year"] == 2026
    assert data["mode"] == "guozhan"
    Product.model_validate(data)  # 能被模型接受
    # 已删除字段不会凭空出现
    for removed in ("series", "publisher", "edition", "release_date", "source", "notes"):
        assert removed not in data


def test_product_edit_writes_only_model_fields(client: TestClient, workdir: Path, store) -> None:
    """5) 编辑旧产品：只写 名称/年份/模式/武将牌系列/游戏牌拓展包，被删字段不会回来。"""
    path = workdir / "data" / "products" / "qlhd.yaml"
    before = load_yaml(path)
    general_sets = list(before["general_sets"])
    card_sets = list(before["card_sets"])

    payload = product_payload(
        client.app,
        name=before["name"],
        release_year=str(before["release_year"]),
        mode=before["mode"],
        general_sets="\n".join(general_sets + ["测试系列"]),
        card_sets="\n".join(card_sets + ["测试牌堆"]),
        _base_sha=store.entity_sha("product", PRODUCT_ID),
    )
    response = post(client, f"/edit/product/{PRODUCT_ID}", data=payload)
    assert response.status_code == 303, response.text

    after = load_yaml(path)
    assert set(after) == {"id", "name", "release_year", "mode", "general_sets", "card_sets"}
    assert after["general_sets"] == general_sets + ["测试系列"]
    assert after["card_sets"] == card_sets + ["测试牌堆"]
    assert after["release_year"] == before["release_year"]
    assert after["mode"] == before["mode"]
    assert after["name"] == before["name"]
    for removed in ("series", "publisher", "edition", "release_date", "source", "verification_status", "notes"):
        assert removed not in after


# ---------------------------------------------------------------- 6


def test_general_edit_keeps_hidden_fields(client: TestClient, app, workdir: Path) -> None:
    """6) 编辑旧武将：表单没展示的字段（image）与本次没提交的字段保留原值。"""
    general_path = entity_file(workdir, "general", GENERAL_ID)
    variant_path = variant_file(workdir, GENERAL_ID)
    general_before = load_yaml(general_path)
    variant_before = load_yaml(variant_path)
    assert variant_before["image"]          # 卡图属于“页面没展示的字段”
    assert variant_before["review_note"]

    payload = general_edit(
        app,
        GENERAL_ID,
        name=general_before["name"],
        variant=variant_before["variant"],
        faction=variant_before["faction"],
        hp="2.5",
        gender="male",
        product_id=PRODUCT_ID,
        general_set="国战标准",
        skill_count="2",
        skill_0_skill_id="skill_guan_xing",
        skill_0_version_id=GUAN_XING_V1,
        skill_1_skill_id="skill_kong_cheng",
        skill_1_version_id=KONG_CHENG_V1,
    )
    # 页面里没有 image，也不该因为“没提交”而被清空；review_note 同理
    for key in ("review_note", "illustrator", "zhulianbihe"):
        payload.pop(key, None)
    response = post(client, f"/edit/general/{GENERAL_ID}", data=payload)
    assert response.status_code == 303, response.text

    after_variant = load_yaml(variant_path)
    assert after_variant["image"] == variant_before["image"]
    assert after_variant["illustrator"] == variant_before["illustrator"]
    assert after_variant["review_note"] == variant_before["review_note"]
    assert after_variant["hp"] == 2.5
    assert [ref["skill_id"] for ref in after_variant["skills"]] == [
        "skill_guan_xing",
        "skill_kong_cheng",
    ]
    assert "source" not in after_variant      # 实体卡类没有 source
    assert "notes" not in after_variant


# ---------------------------------------------------------------- 7 & 8 & 9


def test_existing_skill_can_be_searched_and_bound(client: TestClient) -> None:
    """7) 技能搜索 API + 绑定已有技能。"""
    hits = client.get("/api/skills", params={"q": "集智"}).json()
    assert hits, "搜索不到已有技能"
    assert hits[0]["id"] == "skill_ji_zhi"
    assert hits[0]["version_count"] >= 1

    by_id = client.get("/api/skills", params={"q": "skill_ji_zhi"}).json()
    assert by_id[0]["id"] == "skill_ji_zhi"


def test_general_can_pin_skill_version(client: TestClient, workdir: Path) -> None:
    """8) 显式绑定 SkillVersion，存入 YAML 并进入 variant_skills 表。"""
    versions = client.get(
        "/api/skills/skill_ji_zhi/versions",
        params={"mode": "guozhan", "product_id": PRODUCT_ID},
    ).json()["versions"]
    assert versions, "该技能应该有版本可绑定"
    picked = versions[0]
    assert picked["id"] == JI_ZHI_V1

    variant_id = variant_of(client.app, HUANG_YUE_GENERAL_ID)
    response = post(
        client,
        f"/edit/general/{HUANG_YUE_GENERAL_ID}",
        data=general_edit(
            client.app,
            HUANG_YUE_GENERAL_ID,
            name="黄月英",
            variant="群狼环鼎",
            mode="guozhan",
            hp="1.5",
            gender="female",
            product_id=PRODUCT_ID,
            general_set="国战标准",
            skill_count="1",
            skill_0_skill_id="skill_ji_zhi",
            skill_0_version_id=picked["id"],
        ),
    )
    assert response.status_code == 303, response.text

    data = load_yaml(variant_file(workdir, HUANG_YUE_GENERAL_ID))
    assert data["skills"] == [{"skill_id": "skill_ji_zhi", "version_id": picked["id"]}]

    conn = sqlite3.connect(workdir / "build" / "sgs.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT version_id FROM variant_skills WHERE variant_id = ? AND skill_id = ?",
        (variant_id, "skill_ji_zhi"),
    ).fetchone()
    report = general_report(conn, HUANG_YUE_GENERAL_ID)
    conn.close()
    assert row["version_id"] == picked["id"]
    entry = report["variants"][0]["skills"][0]
    assert entry["version_id"] == picked["id"]
    assert entry["pinned"] is True
    assert entry["version"]["id"] == picked["id"]


def test_version_must_belong_to_same_skill(client: TestClient, workdir: Path) -> None:
    """9) version_id 必须属于对应 skill_id。"""
    response = post(
        client,
        f"/edit/general/{HUANG_YUE_GENERAL_ID}",
        data=general_edit(
            client.app,
            HUANG_YUE_GENERAL_ID,
            name="黄月英",
            variant="群狼环鼎",
            mode="guozhan",
            skill_count="1",
            skill_0_skill_id="skill_qi_cai",
            skill_0_version_id=JI_ZHI_V1,  # 属于别的技能
        ),
    )
    assert response.status_code == 400
    assert "和选中的技能不一致" in response.text
    data = load_yaml(variant_file(workdir, HUANG_YUE_GENERAL_ID))
    assert data["skills"] == [
        {"skill_id": "skill_ji_zhi", "version_id": JI_ZHI_V1},
        {"skill_id": "skill_qi_cai", "version_id": QI_CAI_V1},
    ]  # 原文件没被改动


def test_validator_reports_foreign_version_reference() -> None:
    """9) 即使直接写 YAML，校验器也会拦住“版本不属于该技能”。"""
    from sgs_helper.loader import Origin, RawEntity, load_data_dir
    from sgs_helper.validate import validate_entities

    data_dir = Path(__file__).resolve().parents[1] / "data"
    raws, issues = load_data_dir(data_dir)
    kept = [
        raw
        for raw in raws
        if not (raw.kind == "general_variant" and raw.data.get("id") == VARIANT_ID)
    ]
    kept.append(
        RawEntity(
            kind="general_variant",
            data={
                "id": VARIANT_ID,
                "general_id": GENERAL_ID,
                "variant": "群狼环鼎",
                "skills": [
                    {"skill_id": "skill_guan_xing", "version_id": KONG_CHENG_V1}
                ],
            },
            origin=Origin(f"general_variants/{VARIANT_ID}.yaml", 1, ""),
        )
    )
    result = validate_entities(data_dir, kept, issues)
    assert not result.ok
    assert "skills[0].version_id" in result.format_report()
    assert "不一致" in result.format_report()


# ---------------------------------------------------------------- 10 & 11 & 12 & 13


def test_create_general_with_new_skill_only(client: TestClient, workdir: Path) -> None:
    """10) 只填技能名称、不填描述：只创建 Skill，不创建 SkillVersion。"""
    response = post(
        client,
        "/new/general",
        data=general_payload(
            client.app, name="甲", mode="guozhan", skill_count="1", skill_0_new_name="咆哮"
        ),
    )
    assert response.status_code == 303, response.text
    general_id = new_general_id(response)
    data = load_yaml(variant_file(workdir, general_id))
    skill_id = data["skills"][0]["skill_id"]
    assert data["skills"][0]["version_id"] is None
    assert (workdir / "data" / "skills" / f"{skill_id}.yaml").exists()
    versions = [p for p in (workdir / "data" / "skill_versions").glob("*.yaml")]
    assert not any(skill_id in p.name for p in versions)


def test_create_general_with_skill_and_version(client: TestClient, workdir: Path) -> None:
    """11) 一起创建 Skill + SkillVersion，并自动带上武将的模式/产品。"""
    response = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="乙",
            mode="guozhan",
            product_id=PRODUCT_ID,
            general_set="国战标准",
            skill_count="1",
            skill_0_new_name="铁骑",
            skill_0_new_text="【测试】当你使用杀指定目标后，你可以进行判定。",
            skill_0_new_version_label="初版",
        ),
    )
    assert response.status_code == 303, response.text
    general_id = new_general_id(response)
    data = load_yaml(variant_file(workdir, general_id))
    skill_id = data["skills"][0]["skill_id"]
    version_id = data["skills"][0]["version_id"]
    assert version_id
    version = load_yaml(workdir / "data" / "skill_versions" / f"{version_id}.yaml")
    assert version["skill_id"] == skill_id
    assert version["mode"] == "guozhan"
    assert version["product_id"] == PRODUCT_ID
    assert version["version_label"] == "初版"
    assert "判定" in version["text"]
    assert skill_id in version_id
    assert set(version["source"]) == {"type", "reference"}   # source 只剩 type + reference


def test_add_version_to_existing_skill_sets_previous(
    client: TestClient, workdir: Path, store
) -> None:
    """12 + 13) 已有技能新增版本，并正确串上 previous_version_id。"""
    old_path = workdir / "data" / "skill_versions" / f"{KONG_CHENG_V1}.yaml"
    old_before = old_path.read_text(encoding="utf-8")

    response = post(
        client,
        f"/edit/general/{GENERAL_ID}",
        data=general_edit(
            client.app,
            GENERAL_ID,
            name="诸葛亮",
            variant="群狼环鼎",
            mode="guozhan",
            product_id=PRODUCT_ID,
            general_set="国战标准",
            skill_count="1",
            skill_0_skill_id="skill_kong_cheng",
            skill_0_version_id=KONG_CHENG_V1,
            skill_0_add_version_text="【测试】2026 调整后的空城文本。",
            skill_0_add_version_label="2026 调整",
            skill_0_add_version_from=KONG_CHENG_V1,
        ),
    )
    assert response.status_code == 303, response.text

    # 旧版本原样保留
    assert old_path.read_text(encoding="utf-8") == old_before
    data = load_yaml(variant_file(workdir, GENERAL_ID))
    version_id = data["skills"][0]["version_id"]
    assert version_id != KONG_CHENG_V1
    new_version = load_yaml(workdir / "data" / "skill_versions" / f"{version_id}.yaml")
    assert new_version["previous_version_id"] == KONG_CHENG_V1
    assert new_version["version_label"] == "2026 调整"
    assert new_version["skill_id"] == "skill_kong_cheng"


def test_identical_version_is_reused(client: TestClient, workdir: Path) -> None:
    """内容完全相同的版本不重复创建：直接复用已有版本。"""
    before_count = len(list((workdir / "data" / "skill_versions").glob("*.yaml")))
    existing = load_yaml(workdir / "data" / "skill_versions" / f"{KONG_CHENG_V1}.yaml")

    response = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="复用测试",
            mode="guozhan",
            product_id=PRODUCT_ID,
            general_set="国战标准",
            skill_count="1",
            skill_0_skill_id="skill_kong_cheng",
            skill_0_add_version_text=existing["text"],
        ),
    )
    assert response.status_code == 303, response.text
    after_count = len(list((workdir / "data" / "skill_versions").glob("*.yaml")))
    assert after_count == before_count
    data = load_yaml(variant_file(workdir, new_general_id(response)))
    assert data["skills"][0]["version_id"] == KONG_CHENG_V1


# ---------------------------------------------------------------- 14 & 15 & 16


def test_batch_saves_rebuild_only_once(client: TestClient, app, workdir: Path, monkeypatch) -> None:
    """14) 一次提交产生 General + Skill + SkillVersion，只 rebuild 一次。"""
    calls = {"n": 0}
    original = app.state.store._build_atomic

    def counting_build():
        calls["n"] += 1
        return original()

    monkeypatch.setattr(app.state.store, "_build_atomic", counting_build)
    response = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="批量测试",
            mode="guozhan",
            skill_count="1",
            skill_0_new_name="批量技能",
            skill_0_new_text="【测试】批量保存的技能文本",
        ),
    )
    assert response.status_code == 303, response.text
    assert calls["n"] == 1


def test_batch_failure_writes_nothing(client: TestClient, app, workdir: Path) -> None:
    """15) 批量里任何一条不合法 -> 一个 YAML 都不落盘，数据库也不变。"""
    db_path = workdir / "build" / "sgs.db"
    post(client, "/rebuild", data={"_csrf": app.state.csrf})
    db_before = db_path.read_bytes()
    skills_before = sorted(p.name for p in (workdir / "data" / "skills").glob("*.yaml"))
    generals_before = sorted(p.name for p in (workdir / "data" / "generals").glob("*.yaml"))

    response = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="不该被保存",
            skill_count="2",
            skill_0_new_name="好技能",
            skill_0_new_text="【测试】正常文本",
            skill_1_skill_id="skill_does_not_exist",
        ),
    )
    assert response.status_code == 400
    assert "skill_does_not_exist" in response.text
    assert sorted(p.name for p in (workdir / "data" / "skills").glob("*.yaml")) == skills_before
    assert sorted(p.name for p in (workdir / "data" / "generals").glob("*.yaml")) == generals_before
    assert db_path.read_bytes() == db_before


def test_batch_rebuild_failure_rolls_back_everything(
    client: TestClient, app, workdir: Path, monkeypatch
) -> None:
    """16) rebuild 失败 -> 本次所有修改全部回滚。"""
    generals_before = {
        p.name: p.read_text(encoding="utf-8")
        for p in (workdir / "data" / "generals").glob("*.yaml")
    }
    skills_before = sorted(p.name for p in (workdir / "data" / "skills").glob("*.yaml"))

    def boom():
        raise RuntimeError("模拟 rebuild 失败")

    monkeypatch.setattr(app.state.store, "_build_atomic", boom)
    response = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="回滚测试",
            mode="guozhan",
            skill_count="1",
            skill_0_new_name="回滚技能",
            skill_0_new_text="【测试】回滚技能文本",
        ),
    )
    assert response.status_code == 400
    assert "已回滚" in response.text

    generals_after = {
        p.name: p.read_text(encoding="utf-8")
        for p in (workdir / "data" / "generals").glob("*.yaml")
    }
    assert generals_after == generals_before
    assert sorted(p.name for p in (workdir / "data" / "skills").glob("*.yaml")) == skills_before
    assert not (workdir / "build" / "sgs.db").exists()


# ---------------------------------------------------------------- 17


def test_base_sha_conflict_still_protects_general(
    client: TestClient, app, workdir: Path
) -> None:
    """17) 专用录入页同样受 `_base_sha` 并发保护约束。"""
    path = entity_file(workdir, "general", GENERAL_ID)
    payload = general_edit(
        app,
        GENERAL_ID,
        name="诸葛亮",
        variant="群狼环鼎",
        mode="guozhan",
        title="不该写入的称号",
    )
    # 页面打开之后，别人改了这条记录（改的是武将个体文件）
    path.write_text(
        path.read_text(encoding="utf-8").replace("name: 诸葛亮", "name: 诸葛亮（别人改过）"),
        encoding="utf-8",
    )

    response = post(client, f"/edit/general/{GENERAL_ID}", data=payload)
    assert response.status_code == 400
    assert "没有写入任何文件" in response.text
    text = path.read_text(encoding="utf-8")
    assert "诸葛亮（别人改过）" in text       # 别人的改动保留
    assert "不该写入的称号" not in text       # 我们这次没有写入


def test_missing_base_sha_is_rejected(client: TestClient, app, workdir: Path) -> None:
    payload = general_payload(app, name="缺字段")
    payload.pop("_base_sha")
    response = post(client, "/new/general", data=payload)
    assert response.status_code == 400
    assert "_base_sha" in response.text


# ---------------------------------------------------------------- 18


def test_delete_skill_blocked_by_general_ref(client: TestClient, store) -> None:
    """18) 武将还在引用技能 -> 不能删除 Skill，并列出引用方。"""
    references = store.reverse_refs("skill", "skill_ji_zhi")
    assert any(ref["kind"] == "general_variant" for ref in references)
    assert any(
        ref["field"].startswith("skills[") for ref in references
        if ref["kind"] == "general_variant"
    )

    outcome = store.delete("skill", "skill_ji_zhi")
    assert outcome.ok is False
    assert outcome.blocked is True
    assert any(ref["kind"] == "general_variant" for ref in outcome.references)


def test_delete_pinned_skill_version_blocked(client: TestClient, store) -> None:
    """18) 版本被武将显式绑定 -> 不能删除 SkillVersion。"""
    references = store.reverse_refs("skill_version", GUAN_XING_V1)
    assert any(
        ref["kind"] == "general_variant" and ref["field"] == "skills[0].version_id"
        for ref in references
    )
    outcome = store.delete("skill_version", GUAN_XING_V1)
    assert outcome.ok is False
    assert outcome.blocked is True


def test_delete_unpinned_skill_version_allowed(client: TestClient, store) -> None:
    """没有被任何武将绑定的版本可以删除（这里用一条新建的版本验证）。"""
    candidate = {
        "id": "skill_ji_zhi_guozhan_v9",
        "skill_id": "skill_ji_zhi",
        "mode": "guozhan",
        "text": "【测试】临时版本",
        "source": {"type": "discussion", "reference": ""},
    }
    created = store.save("skill_version", candidate)
    assert created.ok, created.error
    outcome = store.delete("skill_version", "skill_ji_zhi_guozhan_v9")
    assert outcome.ok is True
    assert store.entity("skill_version", "skill_ji_zhi_guozhan_v9") is None


def test_delete_skill_and_version_blocked_by_card_effect_ref(store) -> None:
    """18) 卡牌的 ``effect.skills`` 也是引用：定澜夜明珠引用 skill_zhi_heng 的那一版。

    卡牌与武将共用同一个 ``SkillRef`` 形状，所以 REFERENCE_FIELDS 里的
    ``card.effect.skills[].skill_id`` / ``...version_id`` 必须同时驱动删除保护。
    """
    card_id = "card_ding_lan_ye_ming_zhu"
    skill_id = "skill_zhi_heng"
    version_id = "skill_zhi_heng_guozhan_qlhd_v1"
    card = store.entity("card", card_id)
    assert card is not None
    assert [(ref.skill_id, ref.version_id) for ref in card.effect.skills] == [
        (skill_id, version_id)
    ]

    references = store.reverse_refs("skill", skill_id)
    assert any(
        ref["kind"] == "card"
        and ref["id"] == card_id
        and ref["field"] == "effect.skills[0].skill_id"
        for ref in references
    )
    outcome = store.delete("skill", skill_id)
    assert outcome.ok is False
    assert outcome.blocked is True
    assert any(ref["kind"] == "card" for ref in outcome.references)

    version_refs = store.reverse_refs("skill_version", version_id)
    assert any(
        ref["kind"] == "card"
        and ref["id"] == card_id
        and ref["field"] == "effect.skills[0].version_id"
        for ref in version_refs
    )
    outcome = store.delete("skill_version", version_id)
    assert outcome.ok is False
    assert outcome.blocked is True
    assert any(ref["kind"] == "card" for ref in outcome.references)


# ---------------------------------------------------------------- 录入辅助 API


def test_product_api_supports_name_id_and_year(client: TestClient) -> None:
    assert client.get("/api/products", params={"q": "群狼"}).json()[0]["id"] == PRODUCT_ID
    assert client.get("/api/products", params={"q": PRODUCT_ID}).json()[0]["id"] == PRODUCT_ID
    years = client.get("/api/products", params={"q": "2026"}).json()
    assert any(item["id"] == PRODUCT_ID for item in years)


def test_product_api_exposes_general_sets(client: TestClient) -> None:
    """GeneralSet 实体已删除：general_set 的候选来自 Product.general_sets。"""
    data = client.get(f"/api/products/{PRODUCT_ID}").json()
    assert data["general_sets"] == [
        "紫凤衔佩", "金印紫绶", "紫气东来", "君临天下·阵", "君临天下·势",
        "君临天下·变", "君临天下·权", "国战限定", "国战标准", "不臣篇·上",
        "不臣篇·下", "纵横捭阖", "不臣篇",
    ]
    assert "国战标准" in data["general_sets"]
    assert client.get("/api/products/no_such_product").status_code == 404


def test_suggest_api_returns_existing_values(client: TestClient) -> None:
    factions = client.get("/api/suggest", params={"field": "faction"}).json()
    assert any(item["value"] == "群" for item in factions)
    sets = client.get("/api/suggest", params={"field": "general_set"}).json()
    assert sets  # 至少有已录入的系列
    assert any(item["value"] == "国战标准" and item["count"] >= 1 for item in sets)
    assert client.get("/api/suggest", params={"field": "nope"}).status_code == 400


def test_duplicate_name_api(client: TestClient) -> None:
    hits = client.get("/api/generals", params={"name": "诸葛亮"}).json()
    assert hits and hits[0]["id"] == GENERAL_ID
    assert hits[0]["general_set"] == "国战标准"  # 系列取自它的第一个版本
    assert hits[0]["variant_labels"] == ["群狼环鼎"]
    assert hits[0]["variant_count"] == 1


def test_version_options_are_ranked_and_descriptive(client: TestClient) -> None:
    """版本候选：按 产品匹配 > 模式匹配 > common > 其它 排序，且信息足够人判断。"""
    data = client.get(
        "/api/skills/skill_ji_zhi/versions",
        params={"mode": "guozhan", "product_id": PRODUCT_ID},
    ).json()
    versions = data["versions"]
    assert versions
    ranks = [v["rank"] for v in versions]
    assert ranks == sorted(ranks)                     # 排序单调
    assert versions[0]["same_mode"] is True           # 同模式的排最前
    for item in versions:
        assert item["text_excerpt"] and item["text"]
        assert item["mode_label"] and item["id"]


def test_general_editor_page_renders_rows_and_combos(client: TestClient) -> None:
    body = client.get(f"/edit/general/{GENERAL_ID}").text
    assert 'data-combo="skill"' in body
    assert 'data-combo="product"' in body
    assert "skill-rows" in body
    assert "skill-row-template" in body
    assert 'name="skill_0_skill_id"' in body
    assert "skill_guan_xing" in body
    assert "保存并新增下一位武将" in body


def test_new_general_page_can_copy_from_template(client: TestClient) -> None:
    body = client.get("/new/general", params={"from": GENERAL_ID, "copy": "attrs"}).text
    assert "为模板" in body
    assert 'data-field="faction"' in body
    assert "copy-toggle" in body
    assert "称号" in body and 'data-field="title"' not in body  # 称号不参与复制
    # 模板只带入模式/系列这类公共信息（产品不强制关联，所以这里不再要求带上产品）


def test_general_detail_shows_variants_and_skills(client: TestClient) -> None:
    body = client.get(f"/view/general/{GENERAL_ID}").text
    assert "General（武将个体）→ GeneralVariant（版本）" in body
    assert "群狼环鼎" in body
    assert "观星" in body and "空城" in body
    # 这一版的技能显式绑定了 skill_guan_xing_guozhan_qlhd_v1
    assert GUAN_XING_V1 in body
    assert "显式绑定" in body


def test_product_detail_shows_sets(client: TestClient) -> None:
    body = client.get(f"/view/product/{PRODUCT_ID}").text
    assert "武将牌系列" in body
    assert "九变篇" in body
    assert "游戏牌拓展包" in body
    assert "君主游戏牌" in body


def test_save_next_inherits_only_product_info(client: TestClient, workdir: Path) -> None:
    """保存并新增下一位：只继承 产品/模式/系列，不继承 势力/勾玉/性别/称号。"""
    source = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="继承源",
            mode="guozhan",
            faction="魏",
            hp="2.5",
            gender="male",
            product_id=PRODUCT_ID,
            general_set="国战标准",
        ),
    )
    general_id = new_general_id(source)
    page = client.get("/new/general", params={"from": general_id}).text
    assert "已继承上一位武将" in page
    assert 'data-field="faction"' not in page       # 没有复制势力/勾玉/性别的开关
    assert f'name="product_id" value="{PRODUCT_ID}"' in page
    assert 'name="general_set" value="国战标准"' in page
    assert 'name="faction" value="魏"' not in page
    assert 'name="hp"' in page
    assert 'value="2.5" selected' not in page       # 体力没有被继承


def test_copy_general_template_does_not_copy_name_number_title(
    client: TestClient, workdir: Path
) -> None:
    """以此武将为模板新增：不复制 名字/编号/称号/内部 ID/技能。"""
    source = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="模板源",
            title="不该被复制",
            general_number="TMP-1",
            mode="guozhan",
            faction="群",
            hp="3",
            product_id=PRODUCT_ID,
            general_set="国战标准",
            skill_count="1",
            skill_0_skill_id="skill_ji_zhi",
        ),
    )
    template_id = new_general_id(source)
    page = client.get(
        "/new/general", params={"from": template_id, "copy": "attrs"}
    ).text
    assert 'name="title" value=""' in page
    assert "不该被复制" not in page
    assert 'value="TMP-1"' not in page
    assert 'value="模板源"' not in page
    assert f'name="id" value="{template_id}"' not in page
    assert 'name="skill_0_skill_id"' not in page  # 技能行不复制
    assert 'name="faction" value="群"' in page    # 势力按勾选复制


# ---------------------------------------------------------------- schema 迁移


def test_legacy_general_shape_is_rejected() -> None:
    """19) 旧字段不再兼容：skill_ids / package / included_sets 会直接报错。"""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        General.model_validate({"id": "general_x", "name": "X", "skill_ids": ["skill_ji_zhi"]})
    with pytest.raises(pydantic.ValidationError):
        General.model_validate({"id": "general_x", "name": "X", "package": "p"})
    with pytest.raises(pydantic.ValidationError):
        Product.model_validate({"id": "product_x", "name": "X", "included_sets": ["a"]})


def test_real_data_matches_new_schema(app, store) -> None:
    """真实数据能通过完整校验，并且武将是 General + GeneralVariant 两层。"""
    snapshot = store.snapshot()
    assert snapshot.ok, snapshot.format_report()
    assert snapshot.bundle.count("general") >= 25
    assert snapshot.bundle.count("general_variant") >= 25
    # General 只有稳定信息：名字；模式/体力/技能都在版本上
    for general in snapshot.bundle.all("general").values():
        assert not hasattr(general, "skills")
        assert not hasattr(general, "hp")
        assert not hasattr(general, "mode")
    for variant in snapshot.bundle.all("general_variant").values():
        assert variant.general_id in snapshot.bundle.ids("general")
    for product in snapshot.bundle.all("product").values():
        assert not hasattr(product, "included_sets")
