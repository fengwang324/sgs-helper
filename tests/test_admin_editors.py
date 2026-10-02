"""专用录入页（产品 / 武将）与批量事务保存的测试。

对应这次「录入流程重构」的要求，逐条覆盖：

1. 只填武将名字也能创建
2. 内部 ID 为空时自动生成合法唯一 ID
3. 武将编号允许大小写字母/数字/-/_
4. Product 简洁表单正确保存 general_sets / card_sets
5. 编辑旧 Product 时没展示的字段不会丢
6. 编辑旧 General 时 aliases/source/notes 不会丢
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
18. 删除 Skill / SkillVersion 时新的 GeneralSkillRef 引用保护正确工作

全部在 tmp_path 的副本上跑，绝不修改仓库里的 data/。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sgs_helper.admin.app import create_app
from sgs_helper.loader import manifest_digest
from sgs_helper.repo import general_report
from sgs_helper.schema import General, Product

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
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "_action": "save",
        "skill_count": "0",
        "id": "",
        "name": "",
        "title": "",
        "general_number": "",
        "mode": "",
        "faction": "",
        "hp": "",
        "gender": "unknown",
        "product_id": "",
        "general_set": "",
        "aliases": "",
        "source.type": "",
        "source.title": "",
        "source.reference": "",
        "source.notes": "",
        "verification_status": "unverified",
        "notes": "",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def product_payload(app, **overrides) -> dict:
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "",
        "name": "",
        "release_year": "",
        "mode": "",
        "general_sets": "",
        "card_sets": "",
        "series": "",
        "publisher": "",
        "edition": "",
        "release_date": "",
        "source.type": "",
        "source.title": "",
        "source.reference": "",
        "source.notes": "",
        "verification_status": "unverified",
        "notes": "",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def entity_file(workdir: Path, kind: str, entity_id: str) -> Path:
    """定位记录所在的 YAML 文件（文件名不一定等于 id，例如 kongrong_sample_guozhan.yaml）。"""
    from sgs_helper.constants import KIND_BY_NAME
    from sgs_helper.loader import load_data_dir

    raws, _issues = load_data_dir(workdir / "data")
    for raw in raws:
        if raw.kind == kind and raw.data.get("id") == entity_id:
            return workdir / "data" / raw.origin.path
    return workdir / "data" / KIND_BY_NAME[kind].dirname / f"{entity_id}.yaml"


def new_general_id(response) -> str:
    location = response.headers["location"]
    return location.split("/view/general/")[1].split("?")[0]


# ---------------------------------------------------------------- 1 & 2


def test_only_name_is_required(client: TestClient, workdir: Path) -> None:
    """1) 只填名字也能创建武将。"""
    response = post(client, "/new/general", data=general_payload(client.app, name="测试武将"))
    assert response.status_code == 303, response.text
    general_id = new_general_id(response)
    data = load_yaml(entity_file(workdir, "general", general_id))
    assert data["name"] == "测试武将"
    assert data["mode"] is None
    assert data["hp"] is None
    assert data["general_number"] is None
    assert data["general_set"] is None
    assert data["product_id"] is None
    assert data["skills"] == []
    assert data["source"] is None


def test_missing_id_is_generated_and_unique(client: TestClient, workdir: Path) -> None:
    """2) 内部 ID 留空 -> 后端生成合法且唯一的 ID。"""
    import re

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
    response = post(client, "/new/general", data=general_payload(client.app, name="Zhang Liao"))
    general_id = new_general_id(response)
    assert general_id == "general_zhang_liao"


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
    assert load_yaml(entity_file(workdir, "general", general_id))["general_number"] == number


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


def test_product_edit_keeps_hidden_fields(client: TestClient, workdir: Path, store) -> None:
    """5) 编辑旧产品：页面没展示的 publisher/edition/source 等不能被清空。"""
    path = workdir / "data" / "products" / "guozhan_sample_2024.yaml"
    before = load_yaml(path)
    assert before["publisher"] and before["edition"] and before["source"]

    payload = product_payload(
        client.app,
        name=before["name"],
        release_year="2024",
        mode="guozhan",
        general_sets="国战标准\n九变篇",
        card_sets="标准牌堆",
        _base_sha=store.entity_sha("product", "product_guozhan_sample_2024"),
    )
    # 模拟“页面完全没有展示这些字段”：提交内容里根本没有它们
    for key in (
        "publisher", "edition", "series", "notes",
        "source.type", "source.title", "source.reference", "source.notes",
        "verification_status",
    ):
        payload.pop(key)
    response = post(client, "/edit/product/product_guozhan_sample_2024", data=payload)
    assert response.status_code == 303, response.text
    after = load_yaml(path)
    assert after["publisher"] == before["publisher"]
    assert after["edition"] == before["edition"]
    assert after["source"] == before["source"]
    assert after["notes"] == before["notes"]
    assert after["series"] == before["series"]
    assert after["general_sets"] == ["国战标准", "九变篇"]
    assert after["card_sets"] == ["标准牌堆"]


# ---------------------------------------------------------------- 6


def test_general_edit_keeps_hidden_fields(client: TestClient, workdir: Path, store) -> None:
    """6) 编辑旧武将：aliases / source / notes 必须保留。"""
    path = entity_file(workdir, "general", "general_kongrong_sample_guozhan")
    before = load_yaml(path)
    assert before["aliases"] and before["source"] and before["notes"]

    payload = general_payload(
        client.app,
        name=before["name"],
        mode="guozhan",
        faction=before["faction"],
        hp="2",
        gender="male",
        product_id=before["product_id"],
        general_set=before["general_set"],
        skill_count="2",
        skill_0_skill_id="skill_mingshi",
        skill_0_version_id="skill_mingshi_guozhan_v1",
        skill_1_skill_id="skill_lirang",
        skill_1_version_id="skill_lirang_guozhan_v1",
        _base_sha=store.entity_sha("general", "general_kongrong_sample_guozhan"),
    )
    for key in (
        "aliases", "notes", "verification_status",
        "source.type", "source.title", "source.reference", "source.notes",
    ):
        payload.pop(key)
    response = post(client, "/edit/general/general_kongrong_sample_guozhan", data=payload)
    assert response.status_code == 303, response.text
    after = load_yaml(path)
    assert after["aliases"] == before["aliases"]
    assert after["source"] == before["source"]
    assert after["notes"] == before["notes"]
    assert after["hp"] == 2.0
    assert [ref["skill_id"] for ref in after["skills"]] == ["skill_mingshi", "skill_lirang"]


# ---------------------------------------------------------------- 7 & 8 & 9


def test_existing_skill_can_be_searched_and_bound(client: TestClient, store) -> None:
    """7) 技能搜索 API + 绑定已有技能。"""
    hits = client.get("/api/skills", params={"q": "集智"}).json()
    assert hits, "搜索不到已有技能"
    assert hits[0]["id"] == "skill_jizhi"
    assert hits[0]["version_count"] >= 1

    by_alias = client.get("/api/skills", params={"q": "集智"}).json()
    assert any(item["id"] == "skill_jizhi" for item in by_alias)

    by_id = client.get("/api/skills", params={"q": "skill_jizhi"}).json()
    assert by_id[0]["id"] == "skill_jizhi"


def test_general_can_pin_skill_version(client: TestClient, workdir: Path, store) -> None:
    """8) 显式绑定 SkillVersion，存入 YAML 并进入 general_skills 表。"""
    versions = client.get(
        "/api/skills/skill_jizhi/versions",
        params={"mode": "identity", "product_id": "product_identity_sample_2025"},
    ).json()["versions"]
    assert versions, "该技能应该有版本可绑定"
    picked = versions[0]

    response = post(
        client,
        "/edit/general/general_huangyueying_sample_identity",
        data=general_payload(
            client.app,
            name="【SAMPLE】黄月英",
            mode="identity",
            hp="3",
            gender="female",
            product_id="product_identity_sample_2025",
            skill_count="1",
            skill_0_skill_id="skill_jizhi",
            skill_0_version_id=picked["id"],
            _base_sha=store.entity_sha("general", "general_huangyueying_sample_identity"),
        ),
    )
    assert response.status_code == 303, response.text

    data = load_yaml(entity_file(workdir, "general", "general_huangyueying_sample_identity"))
    assert data["skills"] == [{"skill_id": "skill_jizhi", "version_id": picked["id"]}]

    conn = sqlite3.connect(workdir / "build" / "sgs.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT version_id FROM general_skills WHERE general_id = ? AND skill_id = ?",
        ("general_huangyueying_sample_identity", "skill_jizhi"),
    ).fetchone()
    report = general_report(conn, "general_huangyueying_sample_identity")
    conn.close()
    assert row["version_id"] == picked["id"]
    assert report["skills"][0]["pinned"]["id"] == picked["id"]
    # 没绑定版本的技能仍然走自动推导
    assert report["skills"][0]["version_id"] == picked["id"]


def test_version_must_belong_to_same_skill(client: TestClient, workdir: Path, store) -> None:
    """9) version_id 必须属于对应 skill_id。"""
    response = post(
        client,
        "/edit/general/general_huangyueying_sample_identity",
        data=general_payload(
            client.app,
            name="【SAMPLE】黄月英",
            mode="identity",
            skill_count="1",
            skill_0_skill_id="skill_qicai",
            skill_0_version_id="skill_jizhi_identity_v1",  # 属于别的技能
            _base_sha=store.entity_sha("general", "general_huangyueying_sample_identity"),
        ),
    )
    assert response.status_code == 400
    assert "和选中的技能不一致" in response.text
    data = load_yaml(entity_file(workdir, "general", "general_huangyueying_sample_identity"))
    assert data["skills"] == [
        {"skill_id": "skill_jizhi", "version_id": None},
        {"skill_id": "skill_qicai", "version_id": None},
    ]  # 原文件没被改动


def test_validator_reports_foreign_version_reference() -> None:
    """9) 即使直接写 YAML，校验器也会拦住“版本不属于该技能”。"""
    from sgs_helper.validate import validate_entities
    from sgs_helper.loader import RawEntity, Origin
    from sgs_helper.loader import load_data_dir

    data_dir = Path(__file__).resolve().parents[1] / "data"
    raws, issues = load_data_dir(data_dir)
    kept = [
        raw
        for raw in raws
        if not (raw.kind == "general" and raw.data.get("id") == "general_kongrong_sample_guozhan")
    ]
    kept.append(
        RawEntity(
            kind="general",
            data={
                "id": "general_kongrong_sample_guozhan",
                "name": "孔融",
                "skills": [{"skill_id": "skill_mingshi", "version_id": "skill_lirang_guozhan_v1"}],
            },
            origin=Origin("generals/kongrong_sample_guozhan.yaml", 1, ""),
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
    data = load_yaml(entity_file(workdir, "general", general_id))
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
            product_id="product_guozhan_sample_2024",
            skill_count="1",
            skill_0_new_name="铁骑",
            skill_0_new_text="【测试】当你使用杀指定目标后，你可以进行判定。",
            skill_0_new_version_label="初版",
        ),
    )
    assert response.status_code == 303, response.text
    general_id = new_general_id(response)
    data = load_yaml(entity_file(workdir, "general", general_id))
    skill_id = data["skills"][0]["skill_id"]
    version_id = data["skills"][0]["version_id"]
    assert version_id
    version = load_yaml(workdir / "data" / "skill_versions" / f"{version_id}.yaml")
    assert version["skill_id"] == skill_id
    assert version["mode"] == "guozhan"
    assert version["product_id"] == "product_guozhan_sample_2024"
    assert version["version_label"] == "初版"
    assert "判定" in version["text"]
    assert skill_id in version_id


def test_add_version_to_existing_skill_sets_previous(
    client: TestClient, workdir: Path, store
) -> None:
    """12 + 13) 已有技能新增版本，并正确串上 previous_version_id。"""
    old_path = workdir / "data" / "skill_versions" / "lirang_guozhan_v1.yaml"
    old_before = old_path.read_text(encoding="utf-8")

    response = post(
        client,
        "/edit/general/general_kongrong_sample_guozhan",
        data=general_payload(
            client.app,
            name="【SAMPLE】孔融",
            mode="guozhan",
            skill_count="1",
            skill_0_skill_id="skill_lirang",
            skill_0_version_id="skill_lirang_guozhan_v1",
            skill_0_add_version_text="【测试】2026 调整后的礼让文本。",
            skill_0_add_version_label="2026 调整",
            skill_0_add_version_from="skill_lirang_guozhan_v1",
            _base_sha=store.entity_sha("general", "general_kongrong_sample_guozhan"),
        ),
    )
    assert response.status_code == 303, response.text

    # 旧版本原样保留
    assert old_path.read_text(encoding="utf-8") == old_before
    data = load_yaml(entity_file(workdir, "general", "general_kongrong_sample_guozhan"))
    version_id = data["skills"][0]["version_id"]
    assert version_id != "skill_lirang_guozhan_v1"
    new_version = load_yaml(workdir / "data" / "skill_versions" / f"{version_id}.yaml")
    assert new_version["previous_version_id"] == "skill_lirang_guozhan_v1"
    assert new_version["version_label"] == "2026 调整"
    assert new_version["skill_id"] == "skill_lirang"


def test_identical_version_is_reused(client: TestClient, workdir: Path) -> None:
    """18) 内容完全相同的版本不重复创建：直接复用已有版本。"""
    before_count = len(list((workdir / "data" / "skill_versions").glob("*.yaml")))
    existing = load_yaml(workdir / "data" / "skill_versions" / "lirang_guozhan_v1.yaml")

    response = post(
        client,
        "/new/general",
        data=general_payload(
            client.app,
            name="复用测试",
            mode="guozhan",
            product_id="product_guozhan_sample_2024",
            skill_count="1",
            skill_0_skill_id="skill_lirang",
            skill_0_add_version_text=existing["text"],
        ),
    )
    assert response.status_code == 303, response.text
    after_count = len(list((workdir / "data" / "skill_versions").glob("*.yaml")))
    assert after_count == before_count
    data = load_yaml(entity_file(workdir, "general", new_general_id(response)))
    assert data["skills"][0]["version_id"] == "skill_lirang_guozhan_v1"


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
    path = entity_file(workdir, "general", "general_kongrong_sample_guozhan")
    payload = general_payload(
        client.app,
        name="【SAMPLE】孔融",
        mode="guozhan",
        title="不该写入的称号",
        _base_sha=app.state.store.entity_sha("general", "general_kongrong_sample_guozhan"),
    )
    # 页面打开之后，别人改了这条记录
    path.write_text(
        path.read_text(encoding="utf-8").replace("faction: 群", "faction: 魏"), encoding="utf-8"
    )

    response = post(client, "/edit/general/general_kongrong_sample_guozhan", data=payload)
    assert response.status_code == 400
    assert "没有写入任何文件" in response.text
    text = path.read_text(encoding="utf-8")
    assert "faction: 魏" in text            # 别人的改动保留
    assert "不该写入的称号" not in text      # 我们这次没有写入


def test_missing_base_sha_is_rejected(client: TestClient, app, workdir: Path) -> None:
    payload = general_payload(client.app, name="缺字段")
    payload.pop("_base_sha")
    response = post(client, "/new/general", data=payload)
    assert response.status_code == 400
    assert "_base_sha" in response.text


# ---------------------------------------------------------------- 18


def test_delete_skill_blocked_by_general_ref(client: TestClient, store) -> None:
    """18) 武将还在引用技能 -> 不能删除 Skill，并列出引用方。"""
    references = store.reverse_refs("skill", "skill_jizhi")
    assert any(ref["kind"] == "general" for ref in references)
    assert any(ref["field"] == "skills[0].skill_id" for ref in references if ref["kind"] == "general")

    outcome = store.delete("skill", "skill_jizhi")
    assert outcome.ok is False
    assert outcome.blocked is True
    assert any(ref["kind"] == "general" for ref in outcome.references)


def test_delete_pinned_skill_version_blocked(client: TestClient, store) -> None:
    """18) 版本被武将显式绑定 -> 不能删除 SkillVersion。"""
    references = store.reverse_refs("skill_version", "skill_mingshi_guozhan_v1")
    assert any(
        ref["kind"] == "general" and ref["field"] == "skills[0].version_id"
        for ref in references
    )
    outcome = store.delete("skill_version", "skill_mingshi_guozhan_v1")
    assert outcome.ok is False
    assert outcome.blocked is True


def test_delete_unpinned_skill_version_allowed(client: TestClient, store) -> None:
    """没有被任何武将绑定的版本可以删除（这里用一条新建的版本验证）。"""
    candidate = {
        "id": "skill_jizhi_guozhan_v9",
        "skill_id": "skill_jizhi",
        "mode": "guozhan",
        "text": "【测试】临时版本",
        "source": {"type": "discussion", "title": "测试"},
    }
    created = store.save("skill_version", candidate)
    assert created.ok, created.error
    outcome = store.delete("skill_version", "skill_jizhi_guozhan_v9")
    assert outcome.ok is True
    assert store.entity("skill_version", "skill_jizhi_guozhan_v9") is None


# ---------------------------------------------------------------- 录入辅助 API


def test_product_api_supports_name_id_and_year(client: TestClient) -> None:
    assert client.get("/api/products", params={"q": "群狼"}).json()[0]["id"] == "qlhd"
    assert client.get("/api/products", params={"q": "qlhd"}).json()[0]["id"] == "qlhd"
    years = client.get("/api/products", params={"q": "2026"}).json()
    assert any(item["id"] == "qlhd" for item in years)


def test_suggest_api_returns_existing_values(client: TestClient) -> None:
    factions = client.get("/api/suggest", params={"field": "faction"}).json()
    assert any(item["value"] == "群" for item in factions)
    sets = client.get("/api/suggest", params={"field": "general_set"}).json()
    assert sets  # 至少有已录入的系列
    assert client.get("/api/suggest", params={"field": "nope"}).status_code == 400


def test_duplicate_name_api(client: TestClient) -> None:
    hits = client.get("/api/generals", params={"name": "孔融"}).json()
    assert hits and hits[0]["id"] == "general_kongrong_sample_guozhan"
    assert hits[0]["general_set"] and hits[0]["product_name"]


def test_version_options_rank_matching_first(client: TestClient) -> None:
    data = client.get(
        "/api/skills/skill_jizhi/versions",
        params={"mode": "identity", "product_id": "product_identity_sample_2025"},
    ).json()
    versions = data["versions"]
    assert versions
    assert versions[0]["same_product"] is True
    assert versions[0]["rank"] == 0
    assert versions[0]["text_excerpt"]
    assert versions[0]["text"]


def test_general_editor_page_renders_rows_and_combos(client: TestClient) -> None:
    body = client.get("/edit/general/general_kongrong_sample_guozhan").text
    assert 'data-combo="skill"' in body
    assert 'data-combo="product"' in body
    assert "skill-rows" in body
    assert "skill-row-template" in body
    assert 'name="skill_0_skill_id"' in body
    assert "skill_mingshi" in body
    assert "保存并新增下一位武将" in body


def test_new_general_page_can_copy_from_template(client: TestClient) -> None:
    body = client.get("/new/general", params={"from": "general_kongrong_sample_guozhan", "copy": "attrs"}).text
    assert "为模板" in body
    assert 'data-field="faction"' in body
    assert "copy-toggle" in body
    assert "称号" in body and 'data-field="title"' not in body  # 称号不参与复制
    # 产品信息已带入
    assert 'value="product_guozhan_sample_2024"' in body


def test_general_detail_shows_pinned_version(client: TestClient) -> None:
    body = client.get("/view/general/general_kongrong_sample_guozhan").text
    assert "显式绑定版本" in body
    assert "skill_mingshi_guozhan_v1" in body
    assert "按卡面指定" in body


def test_product_detail_shows_sets(client: TestClient) -> None:
    body = client.get("/view/product/qlhd").text
    assert "武将牌系列" in body
    assert "九变篇" in body
    assert "游戏牌牌堆" in body
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
            product_id="product_guozhan_sample_2024",
            general_set="sample_jiubian",
        ),
    )
    general_id = new_general_id(source)
    page = client.get("/new/general", params={"from": general_id}).text
    assert "已继承上一位武将" in page
    assert 'data-field="faction"' not in page       # 没有复制势力/勾玉/性别的开关
    assert 'name="product_id" value="product_guozhan_sample_2024"' in page
    assert 'name="general_set" value="sample_jiubian"' in page
    assert 'name="faction" value="魏"' not in page
    assert 'name="hp"' in page and 'value="2.5"' not in page.split('name="hp"')[1][:80]


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
            product_id="product_guozhan_sample_2024",
            general_set="sample_jiubian",
            skill_count="1",
            skill_0_skill_id="skill_jizhi",
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
        General.model_validate({"id": "general_x", "name": "X", "skill_ids": ["skill_jizhi"]})
    with pytest.raises(pydantic.ValidationError):
        General.model_validate({"id": "general_x", "name": "X", "package": "p"})
    with pytest.raises(pydantic.ValidationError):
        Product.model_validate({"id": "product_x", "name": "X", "included_sets": ["a"]})


def test_real_data_matches_new_schema(app, store) -> None:
    """迁移后的示例数据能通过完整校验，并且 manifest 与数据库一致。"""
    snapshot = store.snapshot()
    assert snapshot.ok, snapshot.format_report()
    assert snapshot.bundle.count("general") >= 3
    for general in snapshot.bundle.all("general").values():
        assert hasattr(general, "skills")
    for product in snapshot.bundle.all("product").values():
        assert not hasattr(product, "included_sets")
