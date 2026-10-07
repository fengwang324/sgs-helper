"""本地 Wiki（``/wiki/*``）测试。

覆盖任务书里的验收项：

1. 武将 / 技能 / 游戏牌 / 指示物 / FAQ 五个入口都能打开；
2. 没有指示物数据时，指示物页显示**空状态**（而不是报错或空白）；
3. 通过网页新增第一条指示物（``category_1: 指示物`` / ``军令`` / ``军令一``）并落盘，
   指示物页立刻出现、刷新后仍在；
4. 通过网页新增游戏牌；
5. 分类树包含指示物子树，且「新增分类」不需要改代码；
6. 详情页之间可以互相跳转（武将 → 技能、技能 → 武将、各实体 → 相关 FAQ、FAQ → 关联实体）；
7. 编辑模式开关（默认浏览；开启后列表页有「+新增」、详情页有 编辑/复制/删除）；
8. 图片上传写进 ``data/card_images/``，实体里只存**文件名**。

武将的身份键是**卡面编号**：``General.id`` / ``GeneralVariant.id`` / 卡图名都由编号派生
（``G.SHU 004`` → ``general_g_shu_004`` / ``variant_g_shu_004`` / ``g_shu_004.jpg``），
所以测试里按编号反查 id，不写死。

所有测试都跑在 ``tmp_path`` 的真实 ``data/`` 副本上，不会碰仓库里的数据。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgs_helper.admin.app import create_app

#: 真实数据里的固定 id。武将 id 由卡面编号派生，所以按编号反查，不写死。
def _general_by_number(number: str) -> str:
    from sgs_helper.loader import load_data_dir

    raws, _ = load_data_dir(Path(__file__).resolve().parents[1] / "data")
    variant = next(
        r for r in raws
        if r.kind == "general_variant" and r.data.get("general_number") == number
    )
    return str(variant.data["general_id"])


GENERAL_ID = _general_by_number("G.SHU 004")   # 诸葛亮
SKILL_ID = "skill_ji_zhi"
CARD_ID = "card_sha"
FAQ_ID = "faq_rule_flow_damage"


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def workdir(tmp_path: Path, data_dir: Path) -> Path:
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


def card_payload(app, **overrides) -> dict:
    """卡牌专用录入页会提交的完整表单。"""
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
        "skill_count": "0",
        "review_note": "",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def cards_yaml_text(workdir: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (workdir / "data" / "cards").glob("*.yaml")
    )


# ---------------------------------------------------------------- Phase 2：只读


@pytest.mark.parametrize(
    "url",
    [
        "/wiki",
        "/wiki/generals",
        "/wiki/skills",
        "/wiki/cards",
        "/wiki/markers",
        "/wiki/faq",
        "/wiki/rules",
        "/wiki/search",
    ],
)
def test_wiki_pages_start(client: TestClient, url: str) -> None:
    """五个一级入口（+ 预留的规则、全库搜索）都能打开。"""
    response = client.get(url)
    assert response.status_code == 200, url
    assert "SGS Helper" in response.text


def test_wiki_home_lists_five_entries(client: TestClient) -> None:
    body = client.get("/wiki").text
    for label in ("武将", "技能", "游戏牌", "指示物", "FAQ"):
        assert label in body
    # 规则只预留入口，不做内容
    assert "预留" in body


def test_wiki_lists_show_data(client: TestClient) -> None:
    """列表页有数据、有分页；用搜索定位不在第一页的记录。

    列表每页 48 条（武将 155 条 = 4 页），所以断言要落在第一页真实存在的数据上，
    跨页的记录用搜索验证。
    """
    generals = client.get("/wiki/generals").text
    assert 'class="wiki-grid"' in generals
    assert "第 1/4 页" in generals
    # 诸葛亮排在第 73 位（第 2 页），用搜索验证它确实可检索
    assert "诸葛亮" in client.get("/wiki/generals", params={"q": "诸葛亮"}).text

    assert "集智" in client.get("/wiki/skills", params={"q": "集智"}).text
    assert "杀" in client.get("/wiki/cards").text
    assert "伤害" in client.get("/wiki/faq").text


def test_wiki_list_pagination(client: TestClient) -> None:
    """分页：第一页 48 条，能翻到下一页且内容不同。"""
    first = client.get("/wiki/generals").text
    # 精确匹配卡片容器，避免把 wiki-card__name / wiki-card__body 也算进来
    assert first.count('class="wiki-card ') == 48
    assert "下一页" in first

    second = client.get("/wiki/generals", params={"page": 2}).text
    assert "第 2/4 页" in second
    assert first != second


def test_markers_empty_state(client: TestClient) -> None:
    """当前没有指示物数据是正常状态：页面显示空状态与新增入口，而不是报错。"""
    body = client.get("/wiki/markers").text
    assert "暂无指示物资料" in body
    # 空状态要能直接引导去创建第一条
    assert "新增" in body
    # 五个二级分类即使没有数据也要能看到
    for sub in ("军令", "鏖战事件", "技能指示物", "衍生技能牌", "通用标记"):
        assert sub in body


def test_markers_does_not_leak_play_cards(client: TestClient) -> None:
    """游戏牌与指示物是两套筛选：指示物页不能出现游戏牌的数据。"""
    body = client.get("/wiki/markers").text
    assert "无懈可击" not in body
    assert "定澜夜明珠" not in body


def test_play_cards_excludes_markers(client: TestClient, app) -> None:
    """category_1 决定归属：新增一条指示物后，它不该出现在游戏牌页。"""
    client.post("/new/card", data=card_payload(
        app, name="归属测试令", category_1="指示物", category_2="军令", category_3="军令二",
    ))
    assert "归属测试令" not in client.get("/wiki/cards").text
    assert "归属测试令" in client.get("/wiki/markers").text


def test_wiki_search_cross_kinds(client: TestClient) -> None:
    """全库搜索跨类型返回，并按类型分组。"""
    body = client.get("/wiki/search", params={"q": "杀"}).text
    assert "游戏牌" in body
    assert "条结果" in body


def test_wiki_search_empty_query(client: TestClient) -> None:
    body = client.get("/wiki/search").text
    assert "输入关键词" in body


# ---------------------------------------------------------------- Phase 3：FAQ


def test_general_detail_cross_links_and_faqs(client: TestClient) -> None:
    body = client.get(f"/wiki/generals/{GENERAL_ID}").text
    # 卡面 / 称号 / 势力 / 体力 / 编号 / 系列 / 产品
    for field in ("称号", "势力", "体力", "编号", "武将牌系列", "产品"):
        assert field in body
    # 技能可以点进技能详情
    assert "/wiki/skills/skill_" in body
    # 详情底部有相关 FAQ
    assert "相关 FAQ" in body


def test_general_detail_links_its_own_skills(client: TestClient, app) -> None:
    """武将详情里列的技能，链接必须指向它自己的技能。"""
    view = app.state.store.general_view(GENERAL_ID)
    body = client.get(f"/wiki/generals/{GENERAL_ID}").text
    for variant in view["variants"]:
        for row in variant["skills"]:
            assert f'/wiki/skills/{row["skill_id"]}' in body


def test_skill_detail_links_back_to_general(client: TestClient) -> None:
    body = client.get(f"/wiki/skills/{SKILL_ID}").text
    assert "技能版本" in body
    assert "所属武将" in body
    # 能回跳武将详情
    assert "/wiki/generals/" in body
    assert "相关 FAQ" in body


def test_card_detail_shows_categories_and_skills(client: TestClient) -> None:
    body = client.get("/wiki/card/card_ding_lan_ye_ming_zhu").text
    for field in ("一级分类", "二级分类", "三级分类", "拓展包"):
        assert field in body
    # 带关联技能的卡要有「关联技能」区块
    assert "关联技能" in body


def test_faq_detail_links_related_entities(client: TestClient) -> None:
    body = client.get(f"/wiki/faq/{FAQ_ID}").text
    assert "问题" in body and "答案" in body
    # 正文里的资料名自动变成可点击链接
    assert "wiki-mention" in body
    # 分组出来的关联实体
    assert "相关" in body


# ---------------------------------------------------------------- 验收 3/4


def test_index_writes_back_and_visible_in_wiki(client: TestClient, app, workdir: Path) -> None:
    """验收第 3 项：完全通过网页新增第一条指示物。"""
    response = client.post(
        "/new/card",
        data=card_payload(
            app,
            name="军令",
            category_1="指示物",
            category_2="军令",
            category_3="军令一",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303

    # 真实写回 data/ 下的 YAML
    text = cards_yaml_text(workdir)
    assert "category_1: 指示物" in text
    assert "category_2: 军令" in text
    assert "category_3: 军令一" in text
    assert "name: 军令" in text

    # 指示物页立刻出现，且空状态消失
    body = client.get("/wiki/markers").text
    assert "军令" in body
    assert "暂无指示物资料" not in body

    # 三级分类筛选可用
    assert "军令一" in client.get("/wiki/markers", params={"category_2": "军令"}).text


def test_new_indicator_survives_fresh_app(client: TestClient, app, workdir: Path) -> None:
    """验收第 3 项后半：刷新（换一个 app 实例重新读盘）后数据依然在。"""
    client.post("/new/card", data=card_payload(
        app, name="刷新仍在令", category_1="指示物", category_2="军令", category_3="军令三",
    ))

    fresh = create_app(workdir / "data", workdir / "build" / "sgs.db")
    with TestClient(fresh) as fresh_client:
        assert "刷新仍在令" in fresh_client.get("/wiki/markers").text


def test_new_play_card_appears(client: TestClient, app) -> None:
    """验收第 4 项：新增游戏牌「测试锦囊」。

    新卡按分类排序会落在列表末尾（不止一页），所以用搜索验证出现。
    """
    client.post("/new/card", data=card_payload(
        app, name="测试锦囊", category_1="游戏牌",
        category_2="锦囊牌", category_3="非延时锦囊",
    ))
    assert "测试锦囊" in client.get("/wiki/cards", params={"q": "测试锦囊"}).text
    # 三级分类筛选里也应该能筛到它
    body = client.get("/wiki/cards", params={"category_2": "非延时锦囊"}).text
    assert "非延时锦囊" in body


def test_category_tree_contains_indicators(client: TestClient) -> None:
    """验收第 5 项（前半）：录入页的分类候选里必须有指示物子树。"""
    body = client.get("/new/card", params={"category_1": "指示物"}).text
    match = re.search(
        r'<script type="application/json" id="card-category-data">(.*?)</script>',
        body,
        re.S,
    )
    assert match, "卡牌录入页应该带分类树 JSON"
    payload = json.loads(match.group(1))
    tree = payload["tree"]
    assert "指示物" in tree
    assert set(tree["指示物"]) == {"军令", "鏖战事件", "技能指示物", "衍生技能牌", "通用标记"}
    # 指示物是分类，不是拓展包
    assert tree.get("游戏牌")


def test_new_category_needs_no_code_change(client: TestClient, app) -> None:
    """验收第 5 项（后半）：直接输入一个新分类名 = 新建了分类，导航树立刻出现。"""
    client.post("/new/card", data=card_payload(
        app, name="自定义分类令", category_1="指示物",
        category_2="自定义二级", category_3="自定义三级",
    ))
    body = client.get("/wiki/markers").text
    assert "自定义二级" in body
    assert "自定义三级" in body


# ---------------------------------------------------------------- 验收 8：图片


def test_general_edit_page_shows_number_and_derived_image(client: TestClient) -> None:
    """武将录入页：编号是身份键（必填），卡图是它的只读派生值。"""
    body = client.get("/new/general").text
    # 编号可填
    assert 'name="general_number"' in body
    # 卡图不再手工输入（由编号派生），但要有上传按钮与预览
    assert "js-upload-image" in body
    assert "wiki-upload.js" in body
    assert "由编号自动决定" in body


def test_card_edit_page_has_upload_button(client: TestClient) -> None:
    body = client.get("/new/card").text
    assert "js-upload-image" in body
    assert "wiki-upload.js" in body


def test_upload_writes_file_and_returns_relative_name(
    client: TestClient, workdir: Path
) -> None:
    """验收第 8 项：图片上传成功，且实体里记录的是**文件名**（相对路径）。"""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48
    response = client.post(
        "/api/upload-image",
        files={"file": ("wiki_test_art.png", png, "image/png")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "/" not in payload["file"] and "\\" not in payload["file"]
    assert (workdir / "data" / "card_images" / payload["file"]).exists()
    # 能通过静态挂载访问
    assert client.get(f"/card-images/{payload['file']}").status_code == 200


def test_upload_rejects_unknown_extension(client: TestClient) -> None:
    response = client.post(
        "/api/upload-image",
        files={"file": ("evil.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400


def test_upload_deduplicates_same_name(client: TestClient) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    first = client.post("/api/upload-image", files={"file": ("dup.png", png, "image/png")}).json()
    second = client.post("/api/upload-image", files={"file": ("dup.png", png, "image/png")}).json()
    assert first["file"] != second["file"]


# ---------------------------------------------------------------- Phase 4：编辑模式


def test_edit_mode_off_by_default(client: TestClient) -> None:
    """默认浏览态：列表页不渲染「新增」，详情页不渲染「删除」。"""
    list_body = client.get("/wiki/cards").text
    assert "+新增卡牌" not in list_body
    detail_body = client.get(f"/wiki/generals/{GENERAL_ID}").text
    assert "/delete/general/" not in detail_body
    assert "编辑模式：开" not in list_body


def test_edit_mode_toggle_sets_cookie(client: TestClient) -> None:
    response = client.get(
        "/wiki/edit-mode", params={"on": "1", "next": "/wiki/cards"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/wiki/cards"
    assert "sgs_edit" in response.headers.get("set-cookie", "")


def test_edit_mode_toggle_off_clears_cookie(client: TestClient) -> None:
    client.cookies.set("sgs_edit", "1")
    response = client.get(
        "/wiki/edit-mode", params={"on": "0", "next": "/wiki"}, follow_redirects=False
    )
    assert response.status_code == 303
    cookie = response.headers.get("set-cookie", "")
    assert "sgs_edit" in cookie
    assert "Max-Age=0" in cookie or "max-age=0" in cookie


def test_edit_mode_shows_actions(client: TestClient) -> None:
    """开启后：列表页有「+新增」，详情页有 编辑 / 复制 / 删除。"""
    client.cookies.set("sgs_edit", "1")

    list_body = client.get("/wiki/cards").text
    assert "+新增卡牌" in list_body

    markers_body = client.get("/wiki/markers").text
    assert "+新增第一条" in markers_body or "+新增卡牌" in markers_body

    detail_body = client.get(f"/wiki/generals/{GENERAL_ID}").text
    assert f"/edit/general/{GENERAL_ID}" in detail_body
    assert f"/delete/general/{GENERAL_ID}" in detail_body
    assert f"/new/general?from={GENERAL_ID}" in detail_body


def test_new_card_link_prefills_category_1(client: TestClient) -> None:
    """指示物页的「+新增」要把一级分类预填成「指示物」。"""
    client.cookies.set("sgs_edit", "1")
    body = client.get("/wiki/markers").text
    assert "category_1=%E6%8C%87%E7%A4%BA%E7%89%A9" in body or "category_1=指示物" in body


def test_edit_mode_redirect_rejects_absolute_url(client: TestClient) -> None:
    """开放重定向防护：只允许跳回站内相对路径。"""
    response = client.get(
        "/wiki/edit-mode",
        params={"on": "1", "next": "https://evil.example.com/x"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/wiki"


def test_edit_mode_post_requires_csrf(client: TestClient) -> None:
    response = client.post("/wiki/edit-mode", data={"_csrf": "wrong", "on": "1"})
    assert response.status_code == 403


def test_marker_filters_are_scoped(client: TestClient, app) -> None:
    """指示物页的筛选不应该带出游戏牌的拓展包。"""
    client.post("/new/card", data=card_payload(
        app, name="scoped-令", category_1="指示物", category_2="军令", category_3="军令四",
    ))
    body = client.get("/wiki/markers").text
    # 游戏牌的拓展包名不该出现在指示物页的下拉里
    assert "九变篇" not in body
