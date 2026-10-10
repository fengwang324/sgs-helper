"""本地资料管理网页（``sgs_helper.admin``）测试。

覆盖需求里点名的六项：
1. 页面能启动；
2. 搜索可用；
3. 编辑 YAML 后能够重新构建数据库；
4. 非法修改会被 validate 拦截，且**不落盘**；
5. SkillVersion 新增不会覆盖旧版本；
6. 被引用实体不能直接删除。

另外补了一批“真的会毁数据”的回归测试：YAML 往返稳定性、并发冲突、CSRF、
ID 路径穿越、反向引用声明完整性，以及本次「卡牌单层化」的字段级回归
（被删字段必须从表单 / 模板 / 路由里彻底消失，GeneralSet / CardPrinting 实体不存在）。

所有测试都在 ``tmp_path`` 里的**副本**上操作，绝不碰仓库里真实的 ``data/``。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sgs_helper.admin import forms, yaml_writer
from sgs_helper.admin.app import create_app
from sgs_helper.db import build_database
from sgs_helper.loader import manifest_digest
from sgs_helper.repo import faq_report, general_report, skill_report
from sgs_helper.schema import MODEL_BY_KIND
from sgs_helper.validate import REFERENCE_FIELDS, validate_data_dir

# ---------------------------------------------------------------- 真实数据里的固定 id

def _general_by_number(number: str) -> str:
    """按卡面编号找 General.id（编号是身份键，id 由它派生，所以不要在测试里写死 id）。"""
    from sgs_helper.loader import load_data_dir

    raws, _ = load_data_dir(Path(__file__).resolve().parents[1] / "data")
    variant = next(
        r for r in raws
        if r.kind == "general_variant" and r.data.get("general_number") == number
    )
    return str(variant.data["general_id"])


#: 真实数据现在只有一个产品（群狼环鼎），武将 / 技能等都挂在它下面
PRODUCT_ID = "qlhd"
#: 诸葛亮（G.SHU 004）——id 由编号派生：general_g_shu_004
GENERAL_ID = _general_by_number("G.SHU 004")
#: 变体 id 同样由编号派生
VARIANT_ID = "variant_g_shu_004"
#: 卡牌：单层 Card（category_* + name + image + card_set + effect）
CARD_ID = "card_sha"
#: 卡牌代表数据：用于验证原始卡面文本和游戏牌规则字段的渲染
CARD_WITH_SKILL_ID = "card_ding_lan_ye_ming_zhu"
#: 旧 CardPrinting 层的 id：整个层已删除，这个值只用于断言各入口 404
CARD_PRINTING_ID = "card_ba_gua_zhen_qlhd_spade_2"
JI_ZHI_V1 = "skill_ji_zhi_guozhan_qlhd_v1"
GUAN_XING_V1 = "skill_guan_xing_guozhan_qlhd_v1"
KONG_CHENG_V1 = "skill_kong_cheng_guozhan_qlhd_v1"

#: 本次精简删掉的字段：表单里不允许再有对应的输入框
REMOVED_FORM_FIELDS = (
    "notes",
    "related_skill_ids",
    "related_general_ids",
    "related_card_ids",
    "related_faq_ids",
    "related_keywords",
    "related_timing_ids",
    "related_keyword_ids",
    "source.title",
    "source.notes",
)


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def workdir(tmp_path: Path, data_dir: Path):
    """真实 data/ 的副本 + 独立的 build/sgs.db。"""
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


def form_payload(app, kind: str, entity_id: str, **overrides) -> dict:
    """按“编辑页会提交什么”构造一份完整表单数据。"""
    store = app.state.store
    entity = store.entity(kind, entity_id)
    assert entity is not None, f"{kind} {entity_id} 不存在"
    payload = dict(forms.values_from_entity(kind, entity))
    payload["_csrf"] = app.state.csrf
    payload["_base_sha"] = store.entity_sha(kind, entity_id)
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
    return payload


def general_payload(app, **overrides) -> dict:
    """构造一份“武将专用录入页会提交什么”的表单数据。

    注意：实体卡类（General / GeneralVariant）**没有** source 字段，
    也没有 ``notes`` / ``review_note``（这两个字段都已删除）。
    """
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "_action": "save",
        "skill_count": "0",
        "name": "",
        "title": "",
        "general_number": "",
        "mode": "",
        "faction": "",
        "hp": "",
        "gender": "unknown",
        "product_id": "",
        "general_set": "",
        "_variant_id": "",
        "_variant_sha": "",
        "variant_id": "",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def yaml_of(workdir: Path, kind: str, entity_id: str) -> Path:
    """定位某条记录所在的 YAML 文件。

    文件名不一定等于 id（真实数据里是
    ``data/general_variants/variant_zhu_ge_liang_guo_zhan_ban.yaml`` 这样的名字），
    所以先在磁盘上按 id 查，查不到才按新增记录的命名约定推断。
    """
    from sgs_helper.constants import KIND_BY_NAME
    from sgs_helper.loader import load_data_dir

    if (workdir / "data").is_dir():
        raws, _issues = load_data_dir(workdir / "data")
        for raw in raws:
            if raw.kind == kind and raw.data.get("id") == entity_id:
                return workdir / "data" / raw.origin.path
    return workdir / "data" / KIND_BY_NAME[kind].dirname / f"{entity_id}.yaml"


def find_variant_yaml(workdir: Path, general_id: str) -> Path:
    """找到某个武将的（第一个）GeneralVariant 文件。"""
    from sgs_helper.loader import load_data_dir

    raws, _issues = load_data_dir(workdir / "data")
    found = [raw for raw in raws if raw.kind == "general_variant"
             and raw.data.get("general_id") == general_id]
    assert found, f"武将 {general_id} 没有任何版本文件"
    return workdir / "data" / found[0].origin.path


def post(client: TestClient, url: str, **kwargs):
    """提交表单：默认**不**跟随 303 重定向，便于断言“确实发生了写盘 + 重建”。"""
    kwargs.setdefault("follow_redirects", False)
    return client.post(url, **kwargs)


# ---------------------------------------------------------------- 1. 页面能启动


def test_pages_start(client: TestClient, workdir: Path) -> None:
    """首页 / 列表 / 详情 / 新增 / 编辑 / 待核验 / 说明 / 健康检查全部能打开。"""
    seed = {
        "product": PRODUCT_ID,
        "general": GENERAL_ID,
        "general_variant": VARIANT_ID,
        "skill": "skill_ji_zhi",
        "skill_version": JI_ZHI_V1,
        "card": CARD_ID,
        "faq": "faq_056",
        "keyword": "keyword_001",
    }
    urls = ["/", "/search?q=集智", "/help", "/healthz"]
    for kind, entity_id in seed.items():
        urls += [f"/list/{kind}", f"/view/{kind}/{entity_id}"]
        if kind != "general_variant":
            # 武将版本走「武将录入页」，没有独立的新增页
            urls += [f"/new/{kind}"]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
    # 编辑页与删除确认页
    assert client.get("/edit/faq/faq_056").status_code == 200
    assert client.get("/delete/faq/faq_056").status_code == 200
    assert client.get("/raw/faq/faq_056").status_code == 200
    # 卡牌走专用录入页（分类 / 卡图 / 效果 / 关联技能）
    assert client.get(f"/edit/card/{CARD_ID}").status_code == 200
    assert client.get(f"/delete/card/{CARD_ID}").status_code == 200
    assert client.get(f"/raw/card/{CARD_ID}").status_code == 200
    assert client.get(
        "/new/card",
        params={"category_1": "游戏牌", "category_2": "基本牌", "category_3": "杀"},
    ).status_code == 200
    # 未知类型 / 未知记录
    assert client.get("/list/nope").status_code == 404
    assert client.get("/view/skill/nope").status_code == 404
    # GeneralSet / CardPrinting 实体已删除：KINDS 里没有它们，所有入口都 404
    from sgs_helper.constants import KIND_BY_NAME

    assert "general_set" not in KIND_BY_NAME
    assert "card_printing" not in KIND_BY_NAME
    assert not (workdir / "data" / "general_sets").exists()
    assert not (workdir / "data" / "card_printings").exists()
    assert client.get("/list/general_set").status_code == 404
    for url in (
        "/list/card_printing",
        "/new/card_printing",
        f"/view/card_printing/{CARD_PRINTING_ID}",
        f"/edit/card_printing/{CARD_PRINTING_ID}",
        f"/delete/card_printing/{CARD_PRINTING_ID}",
        f"/raw/card_printing/{CARD_PRINTING_ID}",
    ):
        assert client.get(url).status_code == 404, url

    health = client.get("/healthz").json()
    assert health["ok"] is True
    assert health["counts"]["skill"] >= 1


def test_healthz_reports_db_state(client: TestClient, workdir: Path) -> None:
    assert client.get("/healthz").json()["db_exists"] is False
    build_database(workdir / "data", workdir / "build" / "sgs.db")
    payload = client.get("/healthz").json()
    assert payload["db_exists"] is True
    assert payload["db_stale"] is False


# ---------------------------------------------------------------- 2. 搜索


def test_search_finds_entities_and_shows_metadata(client: TestClient) -> None:
    response = client.get("/search?q=集智")
    assert response.status_code == 200
    body = response.text
    assert "技能本体" in body
    assert "技能版本" in body
    assert "skill_ji_zhi" in body
    # 结果里有模式；技能版本不再记录来源。
    assert "国战" in body
    assert JI_ZHI_V1 in body
    assert "集智" in body


def test_search_supports_kind_and_mode_filters(client: TestClient) -> None:
    only_skill = client.get("/search?q=集智&kind=skill")
    assert only_skill.status_code == 200
    assert "skill_ji_zhi" in only_skill.text
    assert JI_ZHI_V1 not in only_skill.text

    only_version = client.get("/search?q=集智&kind=skill_version")
    assert JI_ZHI_V1 in only_version.text
    assert '<code class="id">skill_ji_zhi</code>' not in only_version.text

    # 模式筛选：国战能搜到，身份局搜不到
    assert "集智" in client.get("/search?q=集智&mode=guozhan").text
    assert "没有匹配" in client.get("/search?q=集智&mode=identity").text


def test_search_by_id_works(client: TestClient) -> None:
    response = client.get("/search?q=keyword_001")
    assert "keyword_001" in response.text


# ---------------------------------------------------------------- 3. 编辑后重建数据库


def test_edit_writes_yaml_and_rebuilds_sqlite(client: TestClient, app, workdir: Path) -> None:
    payload = form_payload(app, "faq", "faq_056", answer="【测试】改写后的回答")
    response = post(client, "/edit/faq/faq_056", data=payload)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/view/faq/faq_056")

    # YAML 落盘
    text = yaml_of(workdir, "faq", "faq_056").read_text(encoding="utf-8")
    assert "【测试】改写后的回答" in text

    # SQLite 已由 YAML 重建，且与 YAML 完全同步
    db_path = workdir / "build" / "sgs.db"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
    conn.close()
    assert meta["yaml_manifest_sha256"] == manifest_digest(workdir / "data")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = faq_report(conn, "faq_056")
    conn.close()
    assert report["faq"]["answer"] == "【测试】改写后的回答"

    # 页面本身也立即读到新值（读的是 YAML）
    page = client.get("/view/faq/faq_056")
    assert "【测试】改写后的回答" in page.text


def test_edit_keeps_yaml_readable(client: TestClient, app, workdir: Path) -> None:
    payload = form_payload(
        app, "faq", "faq_056", answer="第一行\n第二行\n第三行"
    )
    post(client, "/edit/faq/faq_056", data=payload)
    text = yaml_of(workdir, "faq", "faq_056").read_text(encoding="utf-8")
    assert "answer: |-" in text  # 多行文本用块状标量，不会被折行
    reloaded = yaml.safe_load(text)
    assert reloaded["answer"] == "第一行\n第二行\n第三行"


def test_manual_rebuild_endpoint(client: TestClient, app, workdir: Path) -> None:
    response = post(client, "/rebuild", data={"_csrf": app.state.csrf})
    assert response.status_code == 303
    assert (workdir / "build" / "sgs.db").exists()


# ---------------------------------------------------------------- 4. 非法修改被拦截且不落盘


def test_invalid_edit_is_blocked_and_nothing_written(
    client: TestClient, app, workdir: Path
) -> None:
    path = yaml_of(workdir, "general", GENERAL_ID)
    before = path.read_text(encoding="utf-8")
    db_path = workdir / "build" / "sgs.db"
    build_database(workdir / "data", db_path)
    db_before = db_path.read_bytes()

    payload = general_payload(
        app,
        name="诸葛亮",
        mode="guozhan",
        skill_count="1",
        skill_0_skill_id="skill_does_not_exist",
        _base_sha=app.state.store.entity_sha("general", GENERAL_ID),
    )
    response = post(client, f"/edit/general/{GENERAL_ID}", data=payload)

    assert response.status_code == 400
    assert "skill_does_not_exist" in response.text
    # 具体文件 / 字段 / 原因
    assert "data/general_variants/" in response.text
    assert "skills[0].skill_id" in response.text
    assert "不存在" in response.text
    # 一个字都没写，数据库也没变
    assert path.read_text(encoding="utf-8") == before
    assert db_path.read_bytes() == db_before


def test_invalid_model_field_is_blocked(client: TestClient, app, workdir: Path) -> None:
    payload = general_payload(
        app,
        name="诸葛亮",
        hp="abc",
        _base_sha=app.state.store.entity_sha("general", GENERAL_ID),
    )
    response = post(client, f"/edit/general/{GENERAL_ID}", data=payload)
    assert response.status_code == 400
    assert "勾玉要写数字" in response.text


def test_unknown_field_cannot_be_injected(client: TestClient, app, workdir: Path) -> None:
    """表单之外的字段进不来（专用录入页只读自己认识的字段）。"""
    payload = general_payload(
        app,
        name="诸葛亮",
        _base_sha=app.state.store.entity_sha("general", GENERAL_ID),
    )
    payload["totally_bogus"] = "1"
    response = post(client, f"/edit/general/{GENERAL_ID}", data=payload)
    assert response.status_code == 303
    text = yaml_of(workdir, "general", GENERAL_ID).read_text(encoding="utf-8")
    assert "totally_bogus" not in text


def keyword_payload(app, **overrides) -> dict:
    """关键词表单：只有 id / name / definition / variant 四个字段（不录来源）。"""
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "",
        "name": "",
        "definition": "",
        "variant": "群狼环鼎",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def test_duplicate_id_is_blocked(client: TestClient, app) -> None:
    response = post(
        client,
        "/new/keyword",
        data=keyword_payload(app, id="keyword_001", name="重复 id 测试", definition="测试"),
    )
    assert response.status_code == 400
    assert "id 重复" in response.text


def test_bad_id_is_rejected_without_touching_disk(
    client: TestClient, app, workdir: Path
) -> None:
    payload = form_payload(app, "faq", "faq_056")
    payload["id"] = "../../etc/passwd"
    response = post(client, "/edit/faq/faq_056", data=payload)
    assert response.status_code == 400
    assert "ID 只能" in response.text
    assert not (workdir / "etc").exists()
    assert not Path("/etc/passwd.yaml").exists()


# ---------------------------------------------------------------- 被删字段的字段级回归


def test_removed_fields_are_gone_from_forms(client: TestClient) -> None:
    """被删字段在表单里**彻底消失**（不是隐藏）。

    FAQ 的 source 是普通多行文本，所以拆成 source.type / source.reference 的旧结构
    必须消失；关键词则完全没有来源字段。
    """
    faq = client.get("/edit/faq/faq_056").text
    for needle in REMOVED_FORM_FIELDS:
        assert f'name="{needle}"' not in faq, needle
    assert 'name="source"' in faq
    assert 'name="variant"' in faq
    assert 'name="source.type"' not in faq
    assert 'name="source.reference"' not in faq
    assert 'name="mode"' not in faq

    keyword = client.get("/edit/keyword/keyword_001").text
    assert 'name="variant"' in keyword
    assert 'name="source"' not in keyword
    assert 'name="mode"' not in keyword

    skill = client.get("/edit/skill/skill_ji_zhi").text
    assert 'name="notes"' not in skill
    assert 'name="source.type"' not in skill  # 技能本体没有 source

    version = client.get(f"/edit/skill_version/{JI_ZHI_V1}").text
    for needle in (
        "package", "effective_from", "effective_to", "change_note",
        "related_keywords", "related_timing_ids", "notes", "review_note",
        "source", "previous_version_id", "version_label",
    ):
        assert f'name="{needle}"' not in version, needle

    # 卡牌专用录入页暴露新模型字段；卡牌不再关联技能。
    card = client.get(f"/edit/card/{CARD_ID}").text
    for needle in (
        "category_1", "category_2", "category_3", "card_set", "image",
        "effect_text", "attack_range", "deck_info", "note",
    ):
        assert f'name="{needle}"' in card, needle
    for needle in (
        "card_type", "subtype", "name_printed", "card_id", "product_id", "suit",
        "rank", "quantity", "printed_text", "normalized_text", "illustrator",
        "bottom_right_mark", "card_number", "related_keywords", "mode",
        "source.type", "notes", "skill_count", "skill_0_skill_id",
    ):
        assert f'name="{needle}"' not in card, needle

    product = client.get("/new/product").text
    for needle in (
        "series", "publisher", "edition", "release_date",
        "source.type", "notes",
    ):
        assert f'name="{needle}"' not in product, needle


def test_faq_detail_auto_links_mentions(client: TestClient) -> None:
    """FAQ 的手工 related_* 没了：关联实体按名称自动识别并渲染成可点击链接。"""
    body = client.get("/view/faq/faq_056").text
    assert "自动识别到的关联实体" in body
    assert 'href="/view/skill/skill_yi_ji"' in body
    assert 'href="/view/skill/skill_fan_kui"' in body
    assert 'href="/view/keyword/keyword_092"' in body


def test_skill_version_text_auto_links_entities(client: TestClient) -> None:
    """技能文本里的关键词名 / 卡牌名自动变成链接。"""
    body = client.get(f"/view/skill_version/{KONG_CHENG_V1}").text
    assert 'href="/view/keyword/keyword_102"' in body   # 锁定技
    assert 'href="/view/card/card_jue_dou"' in body      # 决斗


def test_card_is_single_layer_without_mode_or_product(store, client: TestClient) -> None:
    """单层 Card 没有 mode / product_id：拓展包归属只由 ``card_set`` 表达。"""
    card = store.entity("card", CARD_ID)
    assert card is not None
    assert not hasattr(card, "mode")
    assert not hasattr(card, "product_id")
    assert store.mode_of("card", card) is None
    assert store.describe("card", card)["mode_label"] == ""

    # card_set 的候选全集由产品声明：九变篇 是产品声明的拓展包，指示物 是分类不是拓展包
    declared = {
        str(value)
        for product in store.bundle.all("product").values()
        for value in (product.card_sets or [])
    }
    assert store.entity("card", CARD_WITH_SKILL_ID).card_set == "九变篇"
    assert "九变篇" in declared
    assert "指示物" not in declared
    assert client.get("/list/card").status_code == 200


def test_skill_binding_display(client: TestClient, store) -> None:
    """显式绑定优先；为空时按 mode 回退到唯一一条版本。"""
    body = client.get(f"/view/general/{GENERAL_ID}").text
    assert "显式绑定" in body
    assert "按卡面指定" in body
    assert GUAN_XING_V1 in body

    # 没有 version_id 的版本：按 mode 回退
    created = store.save("general", {"id": "general_fallback_demo", "name": "回退演示"})
    assert created.ok, created.error
    created = store.save(
        "general_variant",
        {
            "id": "variant_fallback_demo",
            "general_id": "general_fallback_demo",
            "title": "演示版",
            "mode": "guozhan",
            "skills": [{"skill_id": "skill_ji_zhi"}],
        },
    )
    assert created.ok, created.error
    body = client.get("/view/general/general_fallback_demo").text
    assert "按模式回退" in body
    assert JI_ZHI_V1 in body


# ---------------------------------------------------------------- 5. SkillVersion 不覆盖旧版本


def test_new_skill_version_preserves_existing_versions(client: TestClient, app, workdir: Path) -> None:
    old_id = JI_ZHI_V1
    new_id = "skill_ji_zhi_guozhan_v3"
    old_path = yaml_of(workdir, "skill_version", old_id)
    old_before = old_path.read_text(encoding="utf-8")
    db_path = workdir / "build" / "sgs.db"
    post(client, "/rebuild", data={"_csrf": app.state.csrf})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    before_ids = {v["id"] for v in skill_report(conn, "skill_ji_zhi")["versions"]}
    conn.close()

    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": new_id,
        "skill_id": "skill_ji_zhi",
        "mode": "guozhan",
        "text": "【测试】第三次调整后的技能文本",
        "product_id": PRODUCT_ID,
        # 已删除字段：旧表单请求不能把历史链字段带回 YAML。
        "version_label": "测试新版",
        "previous_version_id": old_id,
        "source.type": "official_rule",
        "source.reference": "https://example.com/notice",
        "package": "旧包",
        "effective_from": "2024-01-01",
        "effective_to": "2024-12-31",
        "change_note": "【测试】",
        "related_keywords": "keyword_001",
        "notes": "旧备注",
    }
    rejected = post(client, "/new/skill_version", data=payload)
    assert rejected.status_code == 400
    assert not yaml_of(workdir, "skill_version", new_id).exists()
    for removed in ("version_label", "previous_version_id", "source.type", "source.reference"):
        payload.pop(removed, None)
    response = post(client, "/new/skill_version", data=payload)
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(f"/view/skill_version/{new_id}")

    # 旧版本原样保留
    assert old_path.read_text(encoding="utf-8") == old_before
    new_path = yaml_of(workdir, "skill_version", new_id)
    new_doc = yaml.safe_load(new_path.read_text(encoding="utf-8"))
    for removed in (
        "package", "effective_from", "effective_to", "change_note",
        "related_keywords", "related_timing_ids", "notes",
        "version_label", "previous_version_id", "source",
    ):
        assert removed not in new_doc, removed
    assert new_doc["id"] == new_id
    assert new_doc["skill_id"] == "skill_ji_zhi"
    assert new_doc["mode"] == "guozhan"
    assert new_doc["product_id"] == PRODUCT_ID
    assert new_doc["text"] == "【测试】第三次调整后的技能文本"

    # 数据库里新增了一条版本，旧版本还在
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = skill_report(conn, "skill_ji_zhi")
    conn.close()
    ids = {v["id"] for v in report["versions"]}
    assert ids == before_ids | {new_id}
    modes = {group["mode"]: group["version_ids"] for group in report["modes"]}
    assert new_id in modes["guozhan"]
    # 技能详情页仍能检索到新旧版本，不依赖显式历史链。
    page = client.get("/view/skill/skill_ji_zhi")
    assert old_id in page.text
    assert new_id in page.text


def test_new_skill_version_prefilled_from_current(client: TestClient) -> None:
    response = client.get(
        f"/new/skill_version?skill_id=skill_ji_zhi&mode=guozhan"
    )
    assert response.status_code == 200
    assert 'name="skill_id" value="skill_ji_zhi"' in response.text
    assert 'name="mode"' in response.text


# ---------------------------------------------------------------- 6. 被引用实体不能删除


def test_delete_blocked_when_referenced(client: TestClient, app, workdir: Path) -> None:
    path = yaml_of(workdir, "product", PRODUCT_ID)
    before = path.read_text(encoding="utf-8")

    response = post(
        client,
        f"/delete/product/{PRODUCT_ID}",
        data={"_csrf": app.state.csrf, "_base_sha": app.state.store.entity_sha("product", PRODUCT_ID)},
    )
    assert response.status_code == 200
    assert "不能直接删除" in response.text
    # 列出引用它的对象（武将版本 / 技能版本 reference product_id）
    assert VARIANT_ID in response.text
    assert "product_id" in response.text
    assert path.exists()
    assert path.read_text(encoding="utf-8") == before


def test_delete_blocked_for_skill_version_used_by_general_variant(
    client: TestClient, app, workdir: Path
) -> None:
    """技能版本被武将技能槽引用时仍受删除保护。"""
    path = yaml_of(workdir, "skill_version", JI_ZHI_V1)
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": app.state.store.entity_sha("skill_version", JI_ZHI_V1),
    }
    response = post(client, f"/delete/skill_version/{JI_ZHI_V1}", data=payload)
    assert response.status_code == 200
    assert "version_id" in response.text
    assert path.exists()


def test_unreferenced_record_can_be_deleted(client: TestClient, app, workdir: Path) -> None:
    created = post(
        client,
        "/new/keyword",
        data=keyword_payload(
            app, id="keyword_temp_test", name="【测试】临时关键词",
            definition="测试用，随后删除",
        ),
    )
    assert created.status_code == 303
    path = yaml_of(workdir, "keyword", "keyword_temp_test")
    assert path.exists()

    deleted = post(
        client,
        "/delete/keyword/keyword_temp_test",
        data={"_csrf": app.state.csrf, "_base_sha": app.state.store.entity_sha("keyword", "keyword_temp_test")},
    )
    assert deleted.status_code == 303
    assert not path.exists()
    assert app.state.store.entity("keyword", "keyword_temp_test") is None


def test_delete_confirm_page_lists_references(client: TestClient) -> None:
    response = client.get(f"/delete/product/{PRODUCT_ID}")
    assert response.status_code == 200
    assert "不能直接删除" in response.text
    assert VARIANT_ID in response.text


# ---------------------------------------------------------------- 7. 安全与并发回归


def test_post_without_csrf_token_is_rejected(client: TestClient, app, workdir: Path) -> None:
    path = yaml_of(workdir, "faq", "faq_056")
    before = path.read_text(encoding="utf-8")
    payload = form_payload(app, "faq", "faq_056", answer="【测试】不该写入")
    payload.pop("_csrf")
    response = post(client, "/edit/faq/faq_056", data=payload)
    assert response.status_code == 403
    assert path.read_text(encoding="utf-8") == before


def test_edit_form_contains_csrf_and_base_sha(client: TestClient) -> None:
    body = client.get("/edit/faq/faq_056").text
    assert 'name="_csrf"' in body
    assert 'name="_base_sha"' in body


def test_post_without_base_sha_is_rejected(client: TestClient, app, workdir: Path) -> None:
    """并发校验字段不能“省略即放过”——否则任何脚本都能覆盖页面打开后的修改。"""
    path = yaml_of(workdir, "faq", "faq_056")
    before = path.read_text(encoding="utf-8")
    payload = form_payload(app, "faq", "faq_056", answer="【测试】不该写入")
    payload.pop("_base_sha")
    response = post(client, "/edit/faq/faq_056", data=payload)
    assert response.status_code == 400
    assert "_base_sha" in response.text
    assert path.read_text(encoding="utf-8") == before


def test_rollback_restores_file_when_untouched(
    client: TestClient, app, workdir: Path, monkeypatch
) -> None:
    """重建失败且文件没被别人动过：回滚到原内容。"""
    path = yaml_of(workdir, "faq", "faq_056")
    before = path.read_text(encoding="utf-8")
    payload = form_payload(app, "faq", "faq_056", answer="【测试】会被回滚")

    def boom():
        raise RuntimeError("模拟重建失败")

    monkeypatch.setattr(app.state.store, "_build_atomic", boom)
    response = post(client, "/edit/faq/faq_056", data=payload)
    assert response.status_code == 400
    assert "已回滚" in response.text
    assert path.read_text(encoding="utf-8") == before


def test_rollback_does_not_clobber_external_edit(
    client: TestClient, app, workdir: Path, monkeypatch
) -> None:
    """重建失败 + 期间有人改了同一个文件：绝不回滚覆盖别人的修改。"""
    path = yaml_of(workdir, "faq", "faq_056")
    payload = form_payload(app, "faq", "faq_056", answer="【测试】失败保存")

    def failing_build_with_external_edit():
        path.write_text(
            path.read_text(encoding="utf-8") + "\n# 外部追加的注释\n", encoding="utf-8"
        )
        raise RuntimeError("模拟重建失败")

    monkeypatch.setattr(
        app.state.store, "_build_atomic", failing_build_with_external_edit
    )
    response = post(client, "/edit/faq/faq_056", data=payload)
    assert response.status_code == 400
    text = path.read_text(encoding="utf-8")
    assert "外部追加的注释" in text   # 别人的改动保住了
    assert "【测试】失败保存" in text  # 我们写的内容也没被回滚（因为文件已被改动）
    assert "没有回滚" in response.text


def test_concurrent_modification_is_detected(client: TestClient, app, workdir: Path) -> None:
    """页面打开后文件被别人改过：必须拒绝写入，而不是覆盖。"""
    payload = form_payload(app, "faq", "faq_056", answer="【测试】来自旧页面")
    path = yaml_of(workdir, "faq", "faq_056")
    external = yaml.safe_load(path.read_text(encoding="utf-8"))
    external["answer"] = "别人改过的内容"
    path.write_text(
        yaml.safe_dump(external, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    response = post(client, "/edit/faq/faq_056", data=payload)
    assert response.status_code == 400
    assert "没有写入任何文件" in response.text
    after = path.read_text(encoding="utf-8")
    assert "别人改过的内容" in after
    assert "【测试】来自旧页面" not in after


# ---------------------------------------------------------------- 8. YAML 与模型往返


def test_yaml_writer_roundtrip_is_lossless() -> None:
    nasty = [
        "123", "2024-08-01", "true", "null", "~", "- 破折号开头", "#井号开头",
        ": 冒号开头", "  前后有空格  ", "a: b", "多行\n第二行", "多行\n带尾换行\n",
        "两个尾换行\n\n", "【示例】「中文标点」；、", "tab\t制表符", "", "2024",
    ]
    for value in nasty:
        text = yaml_writer.dump_yaml({"k": value})
        assert yaml.safe_load(text)["k"] == value, repr(value)
        # 强上块状标量也必须无损
        text = yaml_writer.dump_yaml({"k": yaml_writer.BlockStr(value)})
        assert yaml.safe_load(text)["k"] == value, repr(value)


def test_entity_dump_reload_is_stable(data_dir: Path, tmp_path: Path) -> None:
    """任意真实实体：模型 -> YAML -> 模型 必须完全一致（写盘不会篡改数据）。"""
    result = validate_data_dir(data_dir)
    assert result.ok
    for kind in MODEL_BY_KIND:
        for entity_id, entity in result.bundle.all(kind).items():
            payload = yaml_writer.entity_to_yaml_dict(kind, entity)
            text = yaml_writer.dump_yaml(payload)
            reloaded = MODEL_BY_KIND[kind].model_validate(yaml.safe_load(text))
            assert reloaded.model_dump(mode="json") == entity.model_dump(mode="json"), (
                kind,
                entity_id,
            )


def test_multi_entity_file_keeps_other_records(tmp_path: Path) -> None:
    """列表文件里改一条，其他记录与顶层结构不能被破坏。"""
    data_dir = tmp_path / "data"
    (data_dir / "keywords").mkdir(parents=True)
    (data_dir / "keywords" / "group.yaml").write_text(
        "# 顶部注释\n\n"
        "- id: keyword_a\n  name: 甲\n  definition: 甲的说明\n  variant: 群狼环鼎\n"
        "- id: keyword_b\n  name: 乙\n  definition: 乙的说明\n  variant: 群狼环鼎\n",
        encoding="utf-8",
    )
    path = data_dir / "keywords" / "group.yaml"
    doc = yaml_writer.read_doc(path)
    assert doc.wrapper == "list"
    assert [node["id"] for node in doc.nodes] == ["keyword_a", "keyword_b"]

    entity = MODEL_BY_KIND["keyword"].model_validate(
        {
            "id": "keyword_a",
            "name": "甲（改）",
            "definition": "甲的说明",
            "variant": "群狼环鼎",
        }
    )
    yaml_writer.upsert_entity(path, "keyword", entity)

    reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(reloaded, list)
    assert [node["id"] for node in reloaded] == ["keyword_a", "keyword_b"]
    assert reloaded[0]["name"] == "甲（改）"
    assert reloaded[1]["name"] == "乙"
    assert path.read_text(encoding="utf-8").startswith("# 顶部注释")


def test_remove_only_entity_deletes_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data" / "keywords"
    data_dir.mkdir(parents=True)
    path = data_dir / "solo.yaml"
    path.write_text(
        "id: keyword_solo\nname: 单条\ndefinition: 说明\nvariant: 群狼环鼎\n",
        encoding="utf-8",
    )
    assert yaml_writer.remove_entity(path, "keyword_solo") is True
    assert not path.exists()


# ---------------------------------------------------------------- 9. 声明完整性


def test_reference_fields_cover_every_id_field() -> None:
    """任何 ``*_id`` / ``*_ids`` 模型字段都必须在 REFERENCE_FIELDS 里声明。

    武将版本的 skills 是嵌套关系；卡牌模型不再有技能关联。
    新增引用字段却忘了更新声明表时，删除保护与引用校验会静默失效 —— 这条测试就是为了
    让那种情况在 CI 里立刻炸出来。
    """
    import typing

    from pydantic import BaseModel

    declared = {(kind, field) for kind, field, _target, _is_list in REFERENCE_FIELDS}

    def id_fields_of(model: type[BaseModel], prefix: str):
        """递归收集 ``*_id`` / ``*_ids`` 字段的声明路径。"""
        for name, info in model.model_fields.items():
            path = f"{prefix}{name}"
            if name.endswith("_id") or name.endswith("_ids"):
                yield path
            annotation = info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                yield from id_fields_of(annotation, f"{path}.")
            for arg in typing.get_args(annotation):
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    yield from id_fields_of(arg, f"{path}[].")

    missing: list[tuple[str, str]] = []
    for kind, model in MODEL_BY_KIND.items():
        for field_path in id_fields_of(model, ""):
            if (kind, field_path) not in declared:
                missing.append((kind, field_path))
    assert missing == [], f"这些引用字段没有在 REFERENCE_FIELDS 里声明：{missing}"
    # 武将技能槽仍显式声明技能与版本引用；卡牌不再有关联技能字段。
    assert ("general_variant", "skills[].skill_id") in declared
    assert ("general_variant", "skills[].version_id") in declared
    assert not any(kind == "card" for kind, _field in declared)


def test_reverse_refs_and_delete_guard_agree(app) -> None:
    store = app.state.store
    refs = store.reverse_refs("skill", "skill_ji_zhi")
    kinds = {ref["kind"] for ref in refs}
    assert "general_variant" in kinds
    assert "skill_version" in kinds
    outcome = store.delete("skill", "skill_ji_zhi")
    assert outcome.ok is False
    assert outcome.blocked is True


# ---------------------------------------------------------------- 11. 新增（表单 -> YAML -> DB）


def test_create_general_from_form(client: TestClient, app, workdir: Path) -> None:
    payload = general_payload(
        app,
        id="general_qq_001",
        name="【测试】新武将",
        title="测试称号",
        general_number="qq-001",
        mode="guozhan",
        faction="群",
        hp="2.5",
        gender="male",
        product_id=PRODUCT_ID,
        general_set="国战标准",
        skill_count="1",
        skill_0_skill_id="skill_ji_zhi",
        skill_0_version_id=JI_ZHI_V1,
    )
    response = post(client, "/new/general", data=payload)
    assert response.status_code == 303, response.text

    general = yaml.safe_load(
        yaml_of(workdir, "general", "general_qq_001").read_text(encoding="utf-8")
    )
    assert set(general) == {"id", "name"}   # 武将个体只存名字
    # 模式/体力/称号/编号/系列/技能这些“版本属性”写在 GeneralVariant 里
    variant = yaml.safe_load(
        find_variant_yaml(workdir, "general_qq_001").read_text(encoding="utf-8")
    )
    assert "source" not in variant           # 实体卡类没有 source
    assert "notes" not in variant
    assert variant["hp"] == 2.5
    assert variant["general_number"] == "qq-001"
    assert variant["general_set"] == "国战标准"
    assert variant["title"] == "测试称号"
    assert "variant" not in variant
    assert variant["skills"] == [
        {"skill_id": "skill_ji_zhi", "version_id": JI_ZHI_V1}
    ]

    db_path = workdir / "build" / "sgs.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = general_report(conn, "general_qq_001")
    conn.close()
    assert report["general"]["name"] == "【测试】新武将"
    assert report["variants"][0]["hp"] == 2.5
    assert "variant" not in report["variants"][0]
    assert "aliases" not in report["general"]
    assert report["product"]["id"] == PRODUCT_ID
    entry = report["variants"][0]["skills"][0]
    assert entry["skill_id"] == "skill_ji_zhi"
    assert entry["version_id"] == JI_ZHI_V1
    assert entry["pinned"] is True
    assert entry["version"]["id"] == JI_ZHI_V1


def test_create_general_with_defaults(client: TestClient, app, workdir: Path) -> None:
    """可空字段全部留空时写 null / 空列表，而不是空结构。"""
    payload = general_payload(app, id="general_test_minimal", name="【测试】最简武将")
    response = post(client, "/new/general", data=payload)
    assert response.status_code == 303, response.text
    general = yaml.safe_load(
        yaml_of(workdir, "general", "general_test_minimal").read_text(encoding="utf-8")
    )
    assert general["name"] == "【测试】最简武将"
    variant = yaml.safe_load(
        find_variant_yaml(workdir, "general_test_minimal").read_text(encoding="utf-8")
    )
    assert "source" not in variant
    assert variant.get("skills", []) == []
    assert variant["mode"] is None
    assert variant["general_number"] is None
    assert variant["product_id"] is None
    assert variant["general_set"] is None
    assert variant["hp"] is None


def test_new_skill_version_needs_only_current_fields(client: TestClient, app, workdir: Path) -> None:
    """新技能版本只需要现存字段，旧历史/来源字段不会写回。"""
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "skill_test_nosource_guozhan_v1",
        "skill_id": "skill_ji_zhi",
        "mode": "guozhan",
        "text": "【测试】缺来源的技能版本",
        "product_id": PRODUCT_ID,
        "version_label": "旧标签",
        "previous_version_id": JI_ZHI_V1,
        "source.type": "official_rule",
    }
    rejected = post(client, "/new/skill_version", data=payload)
    assert rejected.status_code == 400
    assert not yaml_of(
        workdir, "skill_version", "skill_test_nosource_guozhan_v1"
    ).exists()
    for removed in ("version_label", "previous_version_id", "source.type", "source.reference"):
        payload.pop(removed, None)
    response = post(client, "/new/skill_version", data=payload)
    assert response.status_code == 303, response.text
    doc = yaml.safe_load(yaml_of(
        workdir, "skill_version", "skill_test_nosource_guozhan_v1"
    ).read_text(encoding="utf-8"))
    assert doc["product_id"] == PRODUCT_ID
    assert doc["text"] == "【测试】缺来源的技能版本"
    for removed in ("source", "previous_version_id", "version_label"):
        assert removed not in doc


# ---------------------------------------------------------------- 12. 列表筛选


def test_list_filters(client: TestClient, store) -> None:
    assert client.get("/list/product?status=unverified").status_code == 200
    assert client.get("/list/skill_version?mode=guozhan").status_code == 200

    # 系列 / 拓展包的筛选参数现在叫 set_name（旧的 package= 已不再是有效参数）
    jiubian = sum(1 for card in store.bundle.all("card").values() if card.card_set == "九变篇")
    cards = client.get("/list/card", params={"set_name": "九变篇"})
    assert cards.status_code == 200
    assert CARD_WITH_SKILL_ID in cards.text
    assert f"{jiubian} 条" in cards.text    # card_set == 九变篇 的卡牌总数
    assert "全部拓展包" in cards.text        # 卡牌列表把这个筛选叫「拓展包」

    # 卡牌的一 / 二级分类筛选
    basic_total = sum(
        1 for card in store.bundle.all("card").values() if card.category_2 == "基本牌"
    )
    assert basic_total == 7                 # 杀3（杀/火杀/雷杀）/ 闪2（闪/掠闪）/ 桃1 / 酒1
    basic = client.get("/list/card", params={"category_2": "基本牌"})
    assert basic.status_code == 200
    assert f"{basic_total} 条" in basic.text

    # 在武将版本上，同一个 set_name 表示「武将牌系列」
    variants = client.get("/list/general_variant", params={"set_name": "国战标准"})
    assert variants.status_code == 200
    assert "诸葛亮（群狼环鼎）" in variants.text

    empty = client.get("/list/card", params={"set_name": "不存在的拓展包"})
    assert "没有符合条件" in empty.text


# ---------------------------------------------------------------- 13. 卡牌专用录入页（单层 Card）


def card_payload(app, **overrides) -> dict:
    """卡牌专用录入页提交分类、卡图、字符串效果与外层规则字段。"""
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "_action": "save",
        "id": "",
        "name": "",
        "category_1": "游戏牌",
        "category_2": "锦囊牌",
        "category_3": "非延时锦囊",
        "card_set": "",
        "image": "",
        "effect_text": "",
        "attack_range": "",
        "deck_info": "",
        "note": "",
        "use_count": "0",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def card_count(workdir: Path) -> int:
    return len(list((workdir / "data" / "cards").glob("*.yaml")))


def test_card_edit_page_is_single_layer(client: TestClient) -> None:
    """卡牌录入页暴露新模型字段，旧层级和卡牌技能关联彻底消失。"""
    body = client.get(f"/edit/card/{CARD_ID}").text
    for needle in (
        "category_1", "category_2", "category_3", "card_set", "image",
        "effect_text", "attack_range", "deck_info", "note", "use_count",
    ):
        assert f'name="{needle}"' in body, needle
    for combo in ("category_1", "category_2", "category_3", "card_set", "image"):
        assert f'data-combo="{combo}"' in body, combo
    assert 'id="card-category-data"' in body          # 联动候选（分类树 ∪ 现有值）
    assert 'data-editor="card"' in body
    assert 'name="skill_count"' not in body
    assert 'name="skill_0_skill_id"' not in body
    assert 'name="variant"' not in client.get(f"/edit/general_variant/{VARIANT_ID}").text
    for needle in (
        "card_type", "subtype", "name_printed", "card_id", "product_id", "suit",
        "rank", "quantity", "printed_text", "normalized_text", "illustrator",
        "bottom_right_mark", "card_number", "related_keywords", "mode",
    ):
        assert f'name="{needle}"' not in body, needle


def test_card_editor_renders_effect_and_game_rule_fields(client: TestClient) -> None:
    """卡牌页面回显新规则字段，不生成卡牌技能关联行。"""
    body = client.get(f"/edit/card/{CARD_WITH_SKILL_ID}").text
    for name in ("effect_text", "attack_range", "deck_info", "note", "use_count"):
        assert f'name="{name}"' in body
    assert 'name="skill_count"' not in body
    assert 'name="skill_0_skill_id"' not in body
    assert "九变篇" in body


def test_card_detail_shows_single_layer_and_top_level_rules(client: TestClient) -> None:
    body = client.get("/view/card/card_ba_gua_zhen").text
    assert "单层 Card" in body
    assert "每当你需要使用或打出一张【闪】时" in body

    pending = client.get("/view/card/card_huo_sha").text
    assert "卡图待补" in pending      # 火杀：卡图待补
    assert "基本牌的具体牌名写在 name" in pending


def test_card_set_candidates_come_from_products(client: TestClient) -> None:
    """拓展包候选来自 Product.card_sets ∪ 现有卡牌；「指示物」是分类，不是拓展包。"""
    values = {
        item["value"]
        for item in client.get("/api/suggest", params={"field": "card_set"}).json()
    }
    # 具体有哪些拓展包随数据变（产品里删掉一个，这里就该消失），所以按数据算期望值
    declared = {
        str(name)
        for product in client.app.state.store.bundle.all("product").values()
        for name in (product.card_sets or [])
    }
    assert declared <= values
    assert "九变篇" in values
    assert "指示物" not in values

    images = client.get("/api/card-images").json()
    assert images and all(item["value"] for item in images)


def test_create_card_with_effect_and_rules(client: TestClient, app, workdir: Path) -> None:
    response = post(
        client,
        "/new/card",
        data=card_payload(
            app,
            id="card_test_new",
            name="【测试】新卡",
            card_set="九变篇",
            effect_text="【测试】效果文本",
            attack_range="3",
            deck_info="红桃 7",
            note="【测试】卡牌备注",
            use_count="1",
            use_0_label="弃牌",
            use_0_timing="出牌阶段",
            use_0_target="一名角色",
            use_0_effect="摸一张牌",
        ),
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/view/card/card_test_new")

    doc = yaml.safe_load(yaml_of(workdir, "card", "card_test_new").read_text(encoding="utf-8"))
    assert set(doc) == {
        "id", "category_1", "category_2", "category_3", "name",
        "card_set", "effect", "attack_range", "deck_info", "note", "rules",
    }
    assert doc["category_1"] == "游戏牌"
    assert doc["category_3"] == "非延时锦囊"
    assert doc["card_set"] == "九变篇"
    assert doc["effect"] == "【测试】效果文本"
    assert doc["attack_range"] == "3"
    assert doc["deck_info"] == "红桃 7"
    assert doc["note"] == "【测试】卡牌备注"
    assert doc["rules"] == {"use_methods": [{
        "label": "弃牌", "timing": "出牌阶段", "target": "一名角色", "effect": "摸一张牌",
    }]}
    # 两层时代的字段没有回来
    for removed in ("card_type", "subtype", "suit", "rank", "product_id", "card_id"):
        assert removed not in doc, removed

    conn = sqlite3.connect(workdir / "build" / "sgs.db")
    raw = conn.execute(
        "SELECT effect, attack_range, deck_info, note, rules FROM cards WHERE id = ?",
        ("card_test_new",),
    ).fetchone()
    conn.close()
    import json as _json

    assert raw[:4] == ("【测试】效果文本", "3", "红桃 7", "【测试】卡牌备注")
    assert _json.loads(raw[4]) == doc["rules"]
    page = client.get("/view/card/card_test_new").text
    assert "单层 Card" in page


def test_create_card_with_empty_effect_is_legal(client: TestClient, app, workdir: Path) -> None:
    """游戏牌效果未知时允许省略空效果，并显式保留空牌堆信息。"""
    response = post(
        client,
        "/new/card",
        data=card_payload(
            app,
            id="card_test_empty",
            name="【测试】空白卡",
            category_2="基本牌",
            category_3="杀",
            card_set="",
            image="",
            effect_text="",
        ),
    )
    assert response.status_code == 303, response.text
    doc = yaml.safe_load(yaml_of(workdir, "card", "card_test_empty").read_text(encoding="utf-8"))
    assert doc.get("card_set") is None
    assert doc.get("image", "") == ""
    assert doc.get("effect", "") == ""
    assert doc["deck_info"] == ""


def test_card_form_requires_name_and_category_1(client: TestClient, app, workdir: Path) -> None:
    """只有卡名与一级分类必填；二 / 三级留空是合法数据（分类不受白名单约束）。"""
    before = card_count(workdir)
    response = post(client, "/new/card", data=card_payload(app, name="", category_2="", category_3=""))
    assert response.status_code == 400
    assert "卡名必填" in response.text
    assert "三级分类必填" not in response.text
    assert "二级分类必填" not in response.text
    assert card_count(workdir) == before      # 一个文件都没写

    blank = post(
        client,
        "/new/card",
        data=card_payload(
            app, id="card_test_only_level_1", name="【测试】只填一级",
            category_2="", category_3="",
        ),
        follow_redirects=False,
    )
    assert blank.status_code == 303, blank.text
    doc = yaml.safe_load(yaml_of(workdir, "card", "card_test_only_level_1").read_text(encoding="utf-8"))
    assert doc["category_1"] == "游戏牌"
    assert doc.get("category_2", "") == "" and doc.get("category_3", "") == ""


def test_card_edit_exposes_legacy_effect_text(client: TestClient) -> None:
    """已有字符串效果的卡：编辑页必须回填文本，空效果仍保留编辑框。

    录入页会按类型折叠「效果」块（基本牌 / 锦囊牌 归到「原始卡面文字」），
    但**有内容时不能折叠**，否则用户看到的就是“这个框没了”，改不了也删不掉。
    页面用 ``data-has-content`` 把这个信号交给前端脚本。
    """
    legacy = client.get("/edit/card/card_ba_gua_zhen").text
    assert 'data-has-content="1"' in legacy
    assert "每当你需要使用或打出一张【闪】时" in legacy

    empty = client.get("/edit/card/card_sha").text
    assert 'data-has-content="0"' in empty
    assert 'name="effect_text"' in empty                          # 空的时候框也还在（只是折叠）


def test_card_form_rejects_removed_skill_association_fields(
    client: TestClient, app, workdir: Path
) -> None:
    before = card_count(workdir)
    rejected = post(
        client,
        "/new/card",
        data=card_payload(
            app, id="card_test_bad1", name="【测试】旧关联字段",
            skill_count="1", skill_0_skill_id="skill_zhi_heng",
        ),
    )
    assert rejected.status_code == 400
    assert "不关联技能" in rejected.text
    assert card_count(workdir) == before


def test_card_form_rejects_removed_use_method_note(client: TestClient, app, workdir: Path) -> None:
    before = card_count(workdir)
    response = post(
        client,
        "/new/card",
        data=card_payload(
            app, id="card_test_old_method_note", name="【测试】旧方式备注",
            use_count="1", use_0_timing="出牌阶段", use_0_note="旧字段",
        ),
    )
    assert response.status_code == 400
    assert "已移除 note 字段" in response.text
    assert card_count(workdir) == before


def test_marker_rejects_game_only_fields(client: TestClient, app, workdir: Path) -> None:
    before = card_count(workdir)
    response = post(
        client,
        "/new/card",
        data=card_payload(
            app, id="card_test_marker_fields", name="【测试】指示物",
            category_1="指示物", category_2="", category_3="",
            card_set="九变篇", attack_range="2", deck_info="黑桃 9",
            use_count="1", use_0_timing="出牌阶段",
        ),
    )
    assert response.status_code == 400
    assert "不能保存拓展包、攻击范围或牌堆信息" in response.text
    assert card_count(workdir) == before


def test_edit_card_updates_effect_and_keeps_untouched_fields(
    client: TestClient, app, workdir: Path
) -> None:
    """编辑卡牌：字符串效果可改，表单没提交的图像、牌堆信息和规则保留。"""
    path = yaml_of(workdir, "card", CARD_WITH_SKILL_ID)
    before = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload = card_payload(
        app,
        id=CARD_WITH_SKILL_ID,
        name=before["name"],
        category_1=before["category_1"],
        category_2=before["category_2"],
        category_3=before["category_3"],
        card_set=before["card_set"],
        image=before["image"],
        effect_text="【测试】改写后的效果",
        deck_info=before["deck_info"],
        use_count="0",
        _base_sha=app.state.store.entity_sha("card", CARD_WITH_SKILL_ID),
    )
    payload.pop("image")                  # 页面里没提交 -> 必须保留
    response = post(client, f"/edit/card/{CARD_WITH_SKILL_ID}", data=payload)
    assert response.status_code == 303, response.text

    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["name"] == before["name"]
    assert after["card_set"] == before["card_set"]
    assert after["image"] == before["image"]
    assert after["effect"] == "【测试】改写后的效果"
    assert after["deck_info"] == before["deck_info"]
